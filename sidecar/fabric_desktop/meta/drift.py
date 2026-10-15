"""Schema drift review: how an entity's source differs from what its contract expects, and the
config changes that settle each difference.

Runs already apply the layer's drift policy (stop, ignore or keep new columns) and stop when an
expected column is gone. This module shows those differences before or after a run and turns a
decision into config: add a new column to the contract, use a column's new type, map a renamed
column (``[source] rename``, applied as the source is read), drop a column from the contract, or
set this table's own policy (``[contract] drift``).
"""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any

import polars as pl

from .project import SYSTEM_COLUMNS, Entity, Project

_TYPE_NAMES = {pl.Int64: "int64", pl.Int32: "int32", pl.Int16: "int32", pl.Int8: "int32", pl.UInt32: "int64",
               pl.UInt64: "int64", pl.Float64: "float64", pl.Float32: "float32", pl.String: "string",
               pl.Boolean: "bool", pl.Date: "date"}


def type_name(dtype: pl.DataType) -> str:
    if isinstance(dtype, pl.Datetime):
        return "timestamp"
    return _TYPE_NAMES.get(dtype.base_type() if hasattr(dtype, "base_type") else dtype, "string")


def _kind(dtype: pl.DataType) -> str:
    if dtype.is_numeric():
        return "number"
    if dtype == pl.Boolean:
        return "bool"
    if dtype == pl.Date or isinstance(dtype, pl.Datetime):
        return "date"
    return "text"


def expected(project: Project, e: Entity) -> dict[str, str]:
    """Columns the entity expects from its source, with their contract types (same rule as a run)."""
    src = e.spec.get("source", {})
    if "entity" in src:
        up = project.entities[src["entity"]]
        cols = dict(up.columns)
        for c in list(up.derived) + list(up.hook_adds) + sorted(up.mode_columns()):
            cols.setdefault(c, "")
        return cols
    return {c: t for c, t in e.columns.items()
            if c not in e.derived and c not in e.hook_adds and c not in e.mode_columns()}


def detect(project: Project, e: Entity, source: pl.DataFrame | None) -> dict[str, Any]:
    from .engine import polars_type  # the engine owns the contract type names
    layer_policy = project.layer(e.layer).drift
    out: dict[str, Any] = {"entity": e.name, "source": e.spec.get("source", {}).get("entity") or
                           e.spec.get("source", {}).get("files") or e.spec.get("source", {}).get("from") or "", "added": [], "dropped": [], "retyped": [],
                           "policy": e.spec.get("contract", {}).get("drift") or layer_policy,
                           "own_policy": e.spec.get("contract", {}).get("drift"), "layer_policy": layer_policy,
                           "checked": source is not None}
    if source is None or e.spec.get("step") or not e.spec.get("source"):
        return out
    want = expected(project, e)
    renames = e.spec.get("source", {}).get("rename", {})
    have = {renames.get(c, c): source.schema[c] for c in source.columns if c not in SYSTEM_COLUMNS}
    from_entity = "entity" in e.spec.get("source", {})
    used = set(e.columns) | {c for d in e.spec.get("derive", []) for c in pl.sql_expr(d["expr"]).meta.root_names()}
    for c, dtype in have.items():
        if c not in want and c not in used:
            out["added"].append({"column": c, "type": type_name(dtype)})
    for c in want:
        if c not in have and (not from_entity or c in used):
            out["dropped"].append({"column": c, "type": want[c] or "", "key": c in e.keys})
    for c, ctype in e.columns.items():
        if c in have and c in want and ctype and c not in e.derived:
            if _kind(have[c]) != _kind(polars_type(ctype)):
                out["retyped"].append({"column": c, "contract": ctype, "source": type_name(have[c])})
    return out


# ---- turning decisions into config

def _section(lines: list[str], header: str) -> tuple[int, int] | None:
    """(header line, end line) of a [table] section; end is where the next section starts."""
    start = next((i for i, l in enumerate(lines) if l.strip() == header), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def apply(text: str, add: dict[str, str] | None = None, retype: dict[str, str] | None = None,
          rename: dict[str, str] | None = None, remove: list[str] | None = None, policy: str | None = None) -> str:
    """The entity file with the drift decisions written in. ``rename`` maps an expected column to the
    source column that replaced it."""
    lines = text.splitlines()
    cols = _section(lines, "[contract.columns]")
    if cols is None and (add or retype or remove):
        con = _section(lines, "[contract]")
        at = con[1] if con else len(lines)
        lines[at:at] = ["", "[contract.columns]"]
        cols = (at + 1, at + 2)
    if cols:
        start, end = cols
        body = lines[start + 1:end]
        name_of = lambda l: re.match(r'\s*"?([A-Za-z0-9_]+)"?\s*=', l)
        out = []
        for l in body:
            m = name_of(l)
            name = m.group(1) if m else None
            if name and name in (remove or []):
                continue
            if name and name in (retype or {}):
                l = f"{name} = {json.dumps(retype[name])}"
            out.append(l)
        present = {name_of(l).group(1) for l in out if name_of(l)}
        out += [f"{c} = {json.dumps(t)}" for c, t in (add or {}).items() if c not in present]
        lines[start + 1:end] = out
    if policy is not None:
        con = _section(lines, "[contract]")
        if con is None:
            lines += ["", "[contract]"]
            con = (len(lines) - 1, len(lines))
        start, end = con
        body = [l for l in lines[start + 1:end] if not re.match(r"\s*drift\s*=", l)]
        if policy:
            body.insert(0, f"drift = {json.dumps(policy)}")
        lines[start + 1:end] = body
    if rename:
        current = tomllib.loads(text).get("source", {}).get("rename", {})
        merged = current | {new: old for old, new in rename.items()}
        src = _section(lines, "[source]")
        if src is None:
            lines[0:0] = ["[source]", ""]
            src = (0, 1)
        start, end = src
        body = [l for l in lines[start + 1:end] if not re.match(r"\s*rename\s*=", l)]
        body.append("rename = { " + ", ".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in merged.items()) + " }")
        lines[start + 1:end] = body
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"
