"""The metadata project: a folder of config and code that defines every managed table.

    layers.toml                  ordered layers and each layer's policy
    environments.toml            where Dev, Test and Prod write
    exceptions.toml              approved rule exceptions (reason, approver, expiry)
    metadata/entities/*.toml     one file per managed table
    transforms/*.py              custom Python: hooks, custom checks, custom steps
    tests/*.py                   tests that custom code points at

Each layer is its own lakehouse (``LH_<Layer>``) in every environment. An entity is
named ``<layer>.<table>`` or ``<layer>.<schema>.<table>``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import inspect
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

MAX_LAYERS = 10
SYSTEM_COLUMNS = ["_run_id", "_loaded_at"]
SCD2_COLUMNS = ["valid_from", "valid_to", "is_current", "row_hash"]
DRIFT_POLICIES = ("evolve", "ignore_new", "strict")
REQUIRED_CHECKS = ("key_not_null", "key_unique")
LOAD_MODES = ("full", "incremental", "scd1", "scd2")
LOGIC_KEYS = ("source", "load", "contract", "clean", "derive", "check", "hooks", "step", "scd", "lookup")
CUSTOM_RULES = ("owner", "custom_code_owner", "custom_code_test", "key_not_null", "key_unique")
ENVIRONMENTS = ("dev", "test", "prod")
_LAYER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_IGNORED = {".git", ".local", "__pycache__", ".pytest_cache"}


class GateError(Exception):
    """A gate refused an action. ``errors`` lists every reason."""

    def __init__(self, gate: str, errors: list[str]):
        self.gate, self.errors = gate, errors
        super().__init__(f"Blocked by the {gate} gate:\n" + "\n".join(f"- {e}" for e in errors))


def as_list(v: Any) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


@dataclass
class Layer:
    name: str
    drift: str = "ignore_new"
    required_checks: list[str] = field(default_factory=list)
    reads_from: list[str] = field(default_factory=list)  # empty: any earlier layer
    description: str = ""

    @property
    def lakehouse(self) -> str:
        return "LH_" + self.name[:1].upper() + self.name[1:]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "drift": self.drift, "required_checks": self.required_checks,
                "reads_from": self.reads_from, "description": self.description, "lakehouse": self.lakehouse}


@dataclass
class Entity:
    name: str
    spec: dict
    path: Path

    @property
    def layer(self) -> str:
        return self.name.split(".")[0]

    @property
    def schema(self) -> str:
        parts = self.name.split(".")
        return parts[1] if len(parts) == 3 else "dbo"

    @property
    def table(self) -> str:
        return self.name.split(".")[-1]

    @property
    def mode(self) -> str:
        return self.spec.get("load", {}).get("mode", "full")

    @property
    def keys(self) -> list[str]:
        return list(self.spec.get("contract", {}).get("keys", []))

    @property
    def columns(self) -> dict[str, str]:
        return dict(self.spec.get("contract", {}).get("columns", {}))

    @property
    def derived(self) -> list[str]:
        return [d["column"] for d in self.spec.get("derive", [])]

    @property
    def hook_adds(self) -> list[str]:
        return list(self.spec.get("hooks", {}).get("adds", []))

    @property
    def surrogate_key(self) -> str | None:
        return self.spec.get("scd", {}).get("surrogate_key")

    def logic_hash(self) -> str:
        logic = {k: self.spec.get(k) for k in LOGIC_KEYS}
        return hashlib.sha1(json.dumps(logic, sort_keys=True, default=str).encode()).hexdigest()[:8]

    def sources(self) -> list[str]:
        return as_list(self.spec.get("source", {}).get("entity"))

    def reads(self) -> list[str]:
        """Every entity this one reads, from any part of its config."""
        out = list(self.sources())
        out += self.spec.get("hooks", {}).get("reads", [])
        out += self.spec.get("step", {}).get("reads", [])
        for chk in self.spec.get("check", []):
            if "exists_in" in chk:
                out.append(chk["exists_in"].rsplit(".", 1)[0])
        for lk in self.spec.get("lookup", []):
            out.append(lk["dim"])
        return list(dict.fromkeys(out))

    def mode_columns(self) -> set[str]:
        cols = set()
        if self.mode in ("scd1", "scd2") and self.surrogate_key:
            cols.add(self.surrogate_key)
        if self.mode == "scd2":
            cols |= set(SCD2_COLUMNS)
        for lk in self.spec.get("lookup", []):
            cols.add(lk["as"])
        return cols

    def python_refs(self):
        """(kind, "module:function", settings) for every piece of custom code."""
        hooks = self.spec.get("hooks", {})
        for point in ("after_clean", "before_write"):
            if point in hooks:
                yield f"hook {point}", hooks[point], hooks
        for c in self.spec.get("check", []):
            if "python" in c:
                yield "custom check", c["python"], c
        if "custom" in self.spec.get("step", {}):
            yield "custom step", self.spec["step"]["custom"], self.spec["step"]

    def summary(self) -> dict[str, Any]:
        s = self.spec
        src = s.get("source", {})
        return {
            "name": self.name, "layer": self.layer, "schema": self.schema, "table": self.table,
            "owner": s.get("owner", ""), "mode": self.mode, "watermark": s.get("load", {}).get("watermark"),
            "source": src.get("files") or ", ".join(self.sources()) or ("custom step" if s.get("step") else ""),
            "clean": len(s.get("clean", [])), "derive": len(s.get("derive", [])), "checks": len(s.get("check", [])),
            "custom": [kind for kind, _, _ in self.python_refs()],
            "keys": self.keys, "columns": self.columns, "derived": self.derived, "hook_adds": self.hook_adds,
            "reads": self.reads(), "path": str(self.path),
        }


def write_layers_file(root: Path, layers: list[Layer], rules: dict[str, Any]) -> None:
    lines = ["# Layers, in order. Each one is its own lakehouse (LH_<Layer>) in every environment.", ""]
    if rules:
        lines.append("[rules]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in rules.items()]
        lines.append("")
    for l in layers:
        lines += ["[[layer]]", f"name = {json.dumps(l.name)}", f"drift = {json.dumps(l.drift)}",
                  f"required_checks = {json.dumps(l.required_checks)}"]
        if l.reads_from:
            lines.append(f"reads_from = {json.dumps(l.reads_from)}")
        if l.description:
            lines.append(f"description = {json.dumps(l.description)}")
        lines.append("")
    (Path(root) / "layers.toml").write_text("\n".join(lines), encoding="utf-8")


class Project:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.reload()

    @property
    def entities_dir(self) -> Path:
        return self.root / "metadata" / "entities"

    @property
    def local_data(self) -> Path:
        return self.root / ".local"

    def reload(self) -> None:
        cfg = tomllib.loads((self.root / "layers.toml").read_text(encoding="utf-8"))
        self.layers = [Layer(**{k: v for k, v in l.items() if k != "lakehouse"}) for l in cfg.get("layer", [])]
        self.rules = cfg.get("rules", {})
        self.entities: dict[str, Entity] = {}
        self.load_errors: list[str] = []
        for p in sorted(self.entities_dir.glob("*.toml")):
            try:
                spec = tomllib.loads(p.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as exc:
                self.load_errors.append(f"{p.name}: not valid TOML ({exc})")
                continue
            name = spec.get("name") or p.stem
            if p.stem != name:
                self.load_errors.append(f"{p.name}: file name must match its name ({name})")
            self.entities[name] = Entity(name, spec, p)
        exc_file = self.root / "exceptions.toml"
        self.exceptions = tomllib.loads(exc_file.read_text(encoding="utf-8")).get("exception", []) \
            if exc_file.exists() else []
        env_file = self.root / "environments.toml"
        self.environments = tomllib.loads(env_file.read_text(encoding="utf-8")) if env_file.exists() else {}
        for env in ENVIRONMENTS:
            self.environments.setdefault(env, {"target": "local"} if env == "dev" else {"workspace_id": ""})
        self.purge_code()

    def purge_code(self) -> None:
        """Forget imported custom code so edits to transforms/ take effect on the next run."""
        root = str(self.root)
        if root not in sys.path:
            sys.path.insert(0, root)
        for mod in [m for m in sys.modules if m == "transforms" or m.startswith("transforms.")]:
            del sys.modules[mod]
        importlib.invalidate_caches()

    @property
    def version(self) -> str:
        """Content hash of every config and code file, so any edit makes a new version."""
        h = hashlib.sha1()
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if p.is_file() and not (set(rel.parts) & _IGNORED) and p.suffix not in (".pyc",):
                h.update(rel.as_posix().encode() + b"\0" + p.read_bytes().replace(b"\r\n", b"\n"))
        return h.hexdigest()[:7]

    def fingerprint(self, e: Entity) -> str:
        """The entity's logic plus any Python it runs, so code changes count as logic changes."""
        h = hashlib.sha1(e.logic_hash().encode())
        if any(True for _ in e.python_refs()):
            for p in sorted((self.root / "transforms").rglob("*.py")):
                h.update(p.relative_to(self.root).as_posix().encode() + p.read_bytes().replace(b"\r\n", b"\n"))
        return h.hexdigest()[:8]

    def layer_names(self) -> list[str]:
        return [l.name for l in self.layers]

    def layer_index(self, name: str) -> int:
        return self.layer_names().index(name)

    def layer(self, name: str) -> Layer:
        return self.layers[self.layer_index(name)]

    # ---- layer management

    def write_layers(self, layers: list[Layer]) -> None:
        write_layers_file(self.root, layers, self.rules)

    def set_layers(self, layers: list[Layer]) -> None:
        """Replace the layer list, keeping the old one if the result breaks any entity."""
        problems = []
        names = [l.name for l in layers]
        if not layers:
            problems.append("a project needs at least one layer")
        if len(layers) > MAX_LAYERS:
            problems.append(f"a project can have at most {MAX_LAYERS} layers")
        if len(set(names)) != len(names):
            problems.append("layer names must be unique")
        for l in layers:
            if not _LAYER_NAME.match(l.name):
                problems.append(f"layer name {l.name!r} must be lowercase letters, digits or _ and start with a letter")
            if l.drift not in DRIFT_POLICIES:
                problems.append(f"layer {l.name}: drift must be one of {', '.join(DRIFT_POLICIES)}")
        for e in self.entities.values():
            if e.layer not in names:
                problems.append(f"layer {e.layer} still has entities: {e.name}")
        if problems:
            raise GateError("save", sorted(set(problems)))
        old = (self.root / "layers.toml").read_text(encoding="utf-8")
        self.write_layers(layers)
        self.reload()
        errors = [e for e in validate(self) if "layer" in e or "reads" in e]
        if errors:
            (self.root / "layers.toml").write_text(old, encoding="utf-8")
            self.reload()
            raise GateError("save", errors)

    def active_exception(self, entity: str, rule: str, today: dt.date) -> dict | None:
        for e in self.exceptions:
            if e.get("entity") == entity and e.get("rule") == rule:
                exp = str(e.get("expires", "permanent"))
                if exp == "permanent" or dt.date.fromisoformat(exp) >= today:
                    return e
        return None

    def summary(self, today: dt.date | None = None) -> dict[str, Any]:
        errors = validate(self, today)
        return {
            "path": str(self.root), "version": self.version,
            "layers": [l.to_dict() | {"entities": sum(e.layer == l.name for e in self.entities.values())}
                       for l in self.layers],
            "entities": [e.summary() for e in sorted(self.entities.values(),
                                                     key=lambda e: (self._layer_pos(e), e.name))],
            "environments": self.environments,
            "exceptions": self.exceptions,
            "errors": errors,
            "run_order": run_order(self) if not any("cycle" in e for e in errors) else [],
        }

    def _layer_pos(self, e: Entity) -> int:
        return self.layer_index(e.layer) if e.layer in self.layer_names() else 99


# --------------------------------------------------------------------------- gate 1: on save


def check_expr(expr: str, available: set[str]) -> list[str]:
    try:
        roots = pl.sql_expr(expr).meta.root_names()
    except Exception as ex:  # noqa: BLE001
        return [f"expression {expr!r} does not parse ({type(ex).__name__})"]
    return [f"uses unknown column {c}" for c in roots if c not in available]


def load_python(ref: str):
    module, fn = ref.split(":")
    return getattr(importlib.import_module(module), fn)


def check_python(ref: str) -> str | None:
    if ":" not in ref or not ref.startswith("transforms."):
        return "must be transforms.<module>:<function>"
    try:
        fn = load_python(ref)
    except Exception as ex:  # noqa: BLE001
        return f"cannot be imported ({type(ex).__name__}: {ex})"
    params = list(inspect.signature(fn).parameters)
    if params[:2] != ["df", "ctx"]:
        return "must take (df, ctx)"
    return None


def source_columns(project: Project, e: Entity) -> set[str]:
    src = e.spec.get("source", {})
    if "files" in src:
        return set(src.get("columns", e.columns))
    cols: set[str] = set()
    for s in e.sources():
        if s in project.entities:
            up = project.entities[s]
            cols |= set(up.columns) | set(up.derived) | set(up.hook_adds) | up.mode_columns() | set(SYSTEM_COLUMNS)
    return cols


def validate(project: Project, today: dt.date | None = None) -> list[str]:
    """The save gate. Every problem in the project; an empty list means it is clean."""
    today = today or dt.date.today()
    errors: list[str] = list(project.load_errors)
    names = project.layer_names()
    same_layer = project.rules.get("same_layer_reads", "dag")

    def err(entity: str, rule: str, msg: str) -> None:
        if not project.active_exception(entity, rule, today):
            errors.append(f"{entity}: {msg}")

    for e in project.entities.values():
        s = e.spec
        if len(e.name.split(".")) not in (2, 3):
            errors.append(f"{e.name}: name must be <layer>.<table> or <layer>.<schema>.<table>")
            continue
        if e.layer not in names:
            errors.append(f"{e.name}: layer {e.layer} does not exist")
            continue
        if not s.get("owner"):
            err(e.name, "owner", "needs an owner")
        if e.mode not in LOAD_MODES:
            errors.append(f"{e.name}: load mode must be one of {', '.join(LOAD_MODES)}")
        if not e.columns and not s.get("step"):
            errors.append(f"{e.name}: contract needs at least one column")
        for k in e.keys:
            if k not in e.columns:
                errors.append(f"{e.name}: key {k} is not a contract column")
        if e.mode == "incremental" and not s.get("load", {}).get("watermark") and "entity" in s.get("source", {}):
            errors.append(f"{e.name}: incremental loads need a watermark column")
        if e.mode in ("scd1", "scd2"):
            if not e.surrogate_key:
                errors.append(f"{e.name}: dimensions need [scd] surrogate_key")
            if not e.keys:
                errors.append(f"{e.name}: dimensions need a business key in [contract] keys")
            if e.mode == "scd2" and not s.get("scd", {}).get("tracked"):
                errors.append(f"{e.name}: Type 2 dimensions need [scd] tracked columns")
        files = s.get("source", {}).get("files", "")
        if files and (Path(files).is_absolute() or re.match(r"^[A-Za-z]:", files)):
            errors.append(f"{e.name}: source path {files} only exists on this PC; put the files in the "
                          f"lakehouse Files area and use a relative path")

        # No mixing: never read a later layer; the same layer only when the layer allows it.
        me = project.layer_index(e.layer)
        allowed = project.layer(e.layer).reads_from
        for r in e.reads():
            if r not in project.entities:
                errors.append(f"{e.name}: reads {r}, which is not a managed entity")
                continue
            other = project.entities[r]
            if other.layer not in names:
                continue
            theirs = project.layer_index(other.layer)
            if theirs > me:
                errors.append(f"{e.name}: reads {r} from a later layer ({other.layer})")
            elif theirs == me and same_layer == "never":
                errors.append(f"{e.name}: reads {r} from its own layer")
            elif theirs < me and allowed and other.layer not in allowed:
                errors.append(f"{e.name}: layer {e.layer} may only read from {', '.join(allowed)}, not {other.layer}")

        # Required checks for the layer.
        have = set()
        for c in s.get("check", []):
            if c.get("column") in e.keys and c.get("not_null"):
                have.add("key_not_null")
            if c.get("column") in e.keys and c.get("unique"):
                have.add("key_unique")
        if e.mode in ("scd1", "scd2"):
            have |= {"key_not_null", "key_unique"}
        for req in project.layer(e.layer).required_checks:
            if req not in have:
                err(e.name, req, f"layer {e.layer} requires a {req.replace('_', ' ')} check on the key")

        # Columns used must exist upstream: catches renames, deletes and typos.
        available = source_columns(project, e)
        produced: set[str] = set()
        for d in s.get("derive", []):
            errors += [f"{e.name}: derive {d.get('column')}: {m}"
                       for m in check_expr(d.get("expr", ""), available | produced)]
            produced.add(d.get("column"))
        for c in s.get("clean", []):
            col = c.get("column")
            if col and col not in available:
                errors.append(f"{e.name}: clean rule uses unknown column {col}")
        if not s.get("step") and s.get("source"):
            missing = set(e.columns) - available - produced - set(e.hook_adds) - e.mode_columns()
            for c in sorted(missing):
                errors.append(f"{e.name}: contract column {c} is not in the source and nothing derives it")
        output = set(e.columns) | produced | set(e.hook_adds) | e.mode_columns()
        for c in s.get("check", []):
            if "expr" in c:
                errors += [f"{e.name}: check {c.get('name', '')}: {m}" for m in check_expr(c["expr"], output)]
            if c.get("column") and c["column"] not in output:
                errors.append(f"{e.name}: check on unknown column {c['column']}")
            if c.get("on_fail", "stop") not in ("stop", "quarantine", "warn"):
                errors.append(f"{e.name}: on_fail must be stop, quarantine or warn")
            if "exists_in" in c:
                tgt, col = c["exists_in"].rsplit(".", 1)
                if tgt in project.entities and col not in project.entities[tgt].columns:
                    errors.append(f"{e.name}: check exists_in {c['exists_in']}: no such column")
        for lk in s.get("lookup", []):
            dim = project.entities.get(lk.get("dim", ""))
            if dim and dim.mode not in ("scd1", "scd2"):
                errors.append(f"{e.name}: lookup {dim.name} is not a dimension")
            for col in (lk.get("on"), lk.get("at")):
                if col and col not in available | produced:
                    errors.append(f"{e.name}: lookup uses unknown column {col}")

        # Custom Python: importable, right signature, an owner and a test.
        for kind, ref, settings in e.python_refs():
            problem = check_python(ref)
            if problem:
                errors.append(f"{e.name}: {kind} {ref}: {problem}")
            if not settings.get("owner"):
                err(e.name, "custom_code_owner", f"{kind} {ref} needs an owner")
            test = settings.get("test")
            if not test or not (project.root / test.split("::")[0]).exists():
                err(e.name, "custom_code_test", f"{kind} {ref} needs a test")

    cycle = find_cycle(project)
    if cycle:
        errors.append("dependency cycle: " + " -> ".join(cycle))

    for x in project.exceptions:
        exp = str(x.get("expires", "permanent"))
        if exp != "permanent":
            try:
                if dt.date.fromisoformat(exp) < today:
                    errors.append(f"{x.get('entity')}: exception for {x.get('rule')} expired on {exp}")
            except ValueError:
                errors.append(f"{x.get('entity')}: exception expiry {exp} is not a date")
        if not x.get("reason") or not x.get("approver"):
            errors.append(f"{x.get('entity')}: exception for {x.get('rule')} needs a reason and an approver")
    return list(dict.fromkeys(errors))


def _graph(project: Project) -> dict[str, list[str]]:
    return {n: [r for r in e.reads() if r in project.entities and r != n] for n, e in project.entities.items()}


def find_cycle(project: Project) -> list[str] | None:
    graph = _graph(project)
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        state[n] = 1
        stack.append(n)
        for m in graph[n]:
            if state.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if m not in state and (c := visit(m)):
                return c
        stack.pop()
        state[n] = 2
        return None

    for n in sorted(graph):
        if n not in state and (c := visit(n)):
            return c
    return None


def run_order(project: Project) -> list[str]:
    graph = _graph(project)
    out: list[str] = []
    seen: set[str] = set()

    def visit(n: str) -> None:
        if n in seen:
            return
        seen.add(n)
        for m in graph[n]:
            visit(m)
        out.append(n)

    for n in sorted(graph, key=lambda n: (project._layer_pos(project.entities[n]), n)):
        visit(n)
    return out


def impact(project: Project, entity: str) -> list[str]:
    """Everything downstream of an entity, nearest first."""
    out: list[str] = []
    frontier = [entity]
    while frontier:
        nxt = []
        for f in frontier:
            for n, e in project.entities.items():
                if f in e.reads() and n not in out and n != entity:
                    out.append(n)
                    nxt.append(n)
        frontier = nxt
    return out
