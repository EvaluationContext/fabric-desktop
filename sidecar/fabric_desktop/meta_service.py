"""UI actions for the metadata framework, mixed into ``Service``."""

from __future__ import annotations

import datetime as dt
import json
import tomllib
from pathlib import Path
from typing import Any

import polars as pl

from . import lakehouse
from .meta import keys as keys_mod
from .meta import Engine, GateError, Layer, LocalStore, OneLakeStore, Project, impact, promote
from .meta.engine import RunResult
from .meta.project import ENVIRONMENTS, validate
from .meta.promote import promote_checks
from .meta.store import META_LAKEHOUSE, Store
from .meta.templates import TEMPLATES, describe, entity_text, init_project


def _json_rows(df: pl.DataFrame, limit: int) -> list[dict[str, Any]]:
    return json.loads(df.head(limit).write_json())


class MetaMixin:
    root: Path

    # ---- which project is open

    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    def _settings(self) -> dict[str, Any]:
        try:
            return json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _save_settings(self, **changes: Any) -> None:
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(self._settings() | changes, indent=2), encoding="utf-8")

    def _project(self) -> Project:
        path = self._settings().get("meta_project")
        if not path or not (Path(path) / "layers.toml").exists():
            raise LookupError("No metadata project is open. Create one or open its folder.")
        project = getattr(self, "_meta_project", None)
        if project is None or str(project.root) != path:
            project = Project(path)
            self._meta_project = project
        else:
            project.reload()
        self._install_guard(project)
        return project

    def _install_guard(self, project: Project) -> None:
        def guard(lakehouse_name: str, schema: str, table: str) -> str | None:
            for e in project.entities.values():
                if e.layer in project.layer_names() and project.layer(e.layer).lakehouse == lakehouse_name \
                        and e.schema == schema and e.table == table:
                    return (f"{e.name} is managed by the metadata project, so only a framework run can write it. "
                            f"Write to a scratch_ table to explore, or change {e.path.name}.")
            return None
        lakehouse.write_guard = guard

    def meta_project(self) -> dict[str, Any]:
        try:
            project = self._project()
        except LookupError:
            return {"path": None, "default_path": str(Path.home() / "Documents" / "FabricDesktop" / "metadata"),
                    **describe()}
        return project.summary() | describe()

    def meta_init(self, path: str, preset: str = "3", sample: bool = True) -> dict[str, Any]:
        owner = ""
        try:
            owner = (self.auth.signed_in_user() or "").split("@")[0]  # type: ignore[attr-defined]
        except Exception:  # not signed in: the owner can be filled in later
            pass
        init_project(Path(path), preset, owner, sample)
        self._save_settings(meta_project=str(Path(path)))
        return self.meta_project()

    def meta_open(self, path: str) -> dict[str, Any]:
        if not (Path(path) / "layers.toml").exists():
            raise FileNotFoundError(f"{path} isn't a metadata project (it has no layers.toml)")
        self._save_settings(meta_project=str(Path(path)))
        return self.meta_project()

    # ---- environments and stores

    def _store(self, project: Project, env: str, create: bool = False) -> Store:
        if env not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {', '.join(ENVIRONMENTS)}")
        cfg = project.environments.get(env, {})
        if cfg.get("target", "onelake" if env != "dev" else "local") == "local":
            return LocalStore(project.local_data / env, env)
        ws = cfg.get("workspace_id")
        if not ws:
            raise LookupError(f"Pick a Fabric workspace for {env.title()} first (Metadata > Environments).")
        fabric = self.fabric  # type: ignore[attr-defined]
        wanted = [l.lakehouse for l in project.layers] + [META_LAKEHOUSE]
        existing = {lh["displayName"]: lh["id"] for lh in fabric.list_lakehouses(ws)}
        if create:
            for name in wanted:
                if name not in existing:
                    existing[name] = fabric.create_lakehouse(
                        ws, name, "Managed by Fabric Desktop's metadata framework")["id"]
        return OneLakeStore(env, ws, existing, lambda: self.auth.storage_token(),  # type: ignore[attr-defined]
                            self.root / "onelake-cache")

    def _keys_store(self, project: Project, env: str, create: bool = False) -> Store:
        """LH_Keys for an environment: on this PC for a local environment, else in its Keys workspace."""
        if env not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {', '.join(ENVIRONMENTS)}")
        cfg = project.environments.get(env, {})
        if cfg.get("target", "onelake" if env != "dev" else "local") == "local":
            return LocalStore(project.local_data / f"keys-{env}", env)
        ws = cfg.get("keys_workspace_id")
        if not ws:
            raise LookupError(f"Pick a Keys workspace for {env.title()} first (Metadata > Surrogate keys).")
        fabric = self.fabric  # type: ignore[attr-defined]
        existing = {lh["displayName"]: lh["id"] for lh in fabric.list_lakehouses(ws)}
        if create and keys_mod.KEYS_LAKEHOUSE not in existing:
            existing[keys_mod.KEYS_LAKEHOUSE] = fabric.create_lakehouse(
                ws, keys_mod.KEYS_LAKEHOUSE, "Surrogate keys and source systems, managed by Fabric Desktop")["id"]
        return OneLakeStore(env, ws, existing, lambda: self.auth.storage_token(),  # type: ignore[attr-defined]
                            self.root / "onelake-cache")

    def _uses_keys(self, project: Project, names: list[str] | None = None) -> bool:
        return any(keys_mod.entity_keys(e) for n, e in project.entities.items() if names is None or n in names)

    def _write_environments(self, project: Project, envs: dict[str, dict[str, Any]]) -> None:
        lines = ["# Where each environment writes. Lakehouses LH_<Layer> are created on promote.",
                 "# keys_workspace_id is the workspace that holds LH_Keys, the surrogate keys.", ""]
        for name in ENVIRONMENTS:
            lines.append(f"[{name}]")
            lines += [f"{k} = {json.dumps(v)}" for k, v in envs.get(name, {}).items()]
            lines.append("")
        (project.root / "environments.toml").write_text("\n".join(lines), encoding="utf-8")

    def meta_set_environment(self, env: str, workspace_id: str = "", workspace_name: str = "",
                             target: str = "onelake") -> dict[str, Any]:
        project = self._project()
        envs = dict(project.environments)
        keep = {k: v for k, v in envs.get(env, {}).items() if k.startswith("keys_")}
        envs[env] = ({"target": "local"} if target == "local" else {
            "target": "onelake", "workspace_id": workspace_id, "workspace_name": workspace_name}) | keep
        self._write_environments(project, envs)
        return self.meta_project()

    def meta_set_keys_workspace(self, env: str, workspace_id: str, workspace_name: str = "") -> dict[str, Any]:
        """Point an environment at the workspace that holds its LH_Keys."""
        project = self._project()
        if env not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {', '.join(ENVIRONMENTS)}")
        envs = dict(project.environments)
        envs[env] = dict(envs.get(env, {})) | {"keys_workspace_id": workspace_id,
                                               "keys_workspace_name": workspace_name}
        self._write_environments(project, envs)
        return self.meta_keys(env)

    # ---- entities

    def meta_entity(self, name: str, env: str = "dev") -> dict[str, Any]:
        project = self._project()
        e = project.entities[name]
        runs: list[dict[str, Any]] = []
        preview: dict[str, Any] | None = None
        try:
            store = self._store(project, env)
            log = store.meta("run_log").filter(pl.col("entity") == name).reverse()
            runs = _json_rows(log.drop("steps"), 10)
            df = store.read(project.layer(e.layer).lakehouse, e.schema, e.table) \
                if e.layer in project.layer_names() else None
            if df is not None:
                preview = {"columns": df.columns, "rows": json.loads(df.head(50).write_json()), "total": df.height}
        except Exception as exc:  # the config is still useful when the environment can't be read
            runs = [{"status": "unavailable", "message": str(exc)}]
        errors = [x for x in project.summary()["errors"] if x.startswith(name + ":")]
        keys_sql = None
        if keys_mod.entity_keys(e) and not errors and "entity" in e.spec.get("source", {}):
            keys_sql = keys_mod.merge_sql(project, e, project.environments.get(env, {}).get("keys_workspace_name", ""))
        return e.summary() | {"text": e.path.read_text(encoding="utf-8"), "impact": impact(project, name),
                              "runs": runs, "preview": preview, "errors": errors, "keys_sql": keys_sql}

    def meta_save_entity(self, name: str, text: str) -> dict[str, Any]:
        """Save an entity file. TOML that doesn't parse is refused; other problems are listed."""
        project = self._project()
        try:
            spec = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Not saved: the file isn't valid TOML ({exc})") from exc
        new_name = spec.get("name")
        if not new_name:
            raise ValueError("Not saved: the file needs a name, like name = \"silver.orders\"")
        path = project.entities_dir / f"{new_name}.toml"
        if new_name != name:
            if path.exists():
                raise ValueError(f"Not saved: {new_name} already exists")
            downstream = impact(project, name) if name in project.entities else []
            if downstream:
                raise GateError("save", [f"can't rename {name}: {', '.join(downstream)} read it"])
            (project.entities_dir / f"{name}.toml").unlink(missing_ok=True)
        path.write_text(text, encoding="utf-8")
        return self.meta_entity(new_name)

    def meta_new_entity(self, template: str, name: str, source: str = "", keys: list[str] | None = None,
                        owner: str = "") -> dict[str, Any]:
        project = self._project()
        if template not in TEMPLATES:
            raise ValueError(f"template must be one of {', '.join(TEMPLATES)}")
        if name in project.entities:
            raise ValueError(f"{name} already exists")
        layer = name.split(".")[0]
        if layer not in project.layer_names():
            raise ValueError(f"{layer} is not a layer; use one of {', '.join(project.layer_names())}")
        sample = None
        if template == "files" and source:
            try:
                store = self._store(project, "dev")
                sample, _ = store.read_files(project.layer(layer).lakehouse, source)
            except Exception:  # no files yet: the contract starts with a placeholder column
                sample = None
        if not owner:
            try:
                owner = (self.auth.signed_in_user() or "").split("@")[0]  # type: ignore[attr-defined]
            except Exception:
                owner = ""
        text, files = entity_text(project, template, name, owner, source, keys, sample)
        for rel, content in files.items():
            p = project.root / rel
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
        (project.entities_dir / f"{name}.toml").write_text(text, encoding="utf-8")
        return self.meta_entity(name)

    def meta_delete_entity(self, name: str) -> dict[str, Any]:
        project = self._project()
        downstream = impact(project, name)
        if downstream:
            raise GateError("save", [f"can't delete {name}: {', '.join(downstream)} read it"])
        project.entities[name].path.unlink()
        return {"deleted": name}

    def meta_set_layers(self, layers: list[dict[str, Any]]) -> dict[str, Any]:
        project = self._project()
        project.set_layers([Layer(name=l["name"].strip().lower(), drift=l.get("drift", "ignore_new"),
                                  required_checks=list(l.get("required_checks", [])),
                                  reads_from=list(l.get("reads_from", [])), description=l.get("description", ""),
                                  require_keys=bool(l.get("require_keys", False)))
                            for l in layers])
        return self.meta_project()

    # ---- runs

    def meta_run(self, names: list[str] | None = None, env: str = "dev", backfill: str | None = None,
                 cascade: bool = False) -> list[dict[str, Any]]:
        project = self._project()
        keys_store = self._keys_store(project, env, create=env == "dev") if self._uses_keys(project, names) else None
        engine = Engine(project, self._store(project, env, create=env == "dev"), keys_store=keys_store)
        if cascade and names and len(names) == 1:
            results: list[RunResult] = engine.backfill(names[0])
        else:
            results = engine.run_many(names, backfill=backfill)
        return [r.to_dict() for r in results]

    def meta_runs(self, env: str = "dev", limit: int = 100) -> list[dict[str, Any]]:
        project = self._project()
        log = self._store(project, env).meta("run_log").reverse()
        return _json_rows(log, limit)

    def meta_run_detail(self, run_id: str, env: str = "dev") -> dict[str, Any]:
        project = self._project()
        store = self._store(project, env)
        run = store.meta("run_log").filter(pl.col("run_id") == run_id)
        if not run.height:
            raise KeyError(f"no run {run_id} in {env}")
        row = _json_rows(run, 1)[0]
        row["steps"] = json.loads(row.get("steps") or "[]")
        row["dq"] = _json_rows(store.meta("dq_results").filter(pl.col("run_id") == run_id), 100)
        return row

    def meta_preview(self, name: str, env: str = "dev", limit: int = 100) -> dict[str, Any]:
        project = self._project()
        e = project.entities[name]
        df = self._store(project, env).read(project.layer(e.layer).lakehouse, e.schema, e.table)
        if df is None:
            return {"columns": [], "rows": [], "total": 0}
        return {"columns": df.columns, "rows": json.loads(df.head(limit).write_json()), "total": df.height}

    # ---- promote

    def meta_promote_check(self, source: str, target: str) -> dict[str, Any]:
        project = self._project()
        try:
            errors = promote_checks(project, self._store(project, source), target, dt.date.today())
        except LookupError as exc:
            errors = [str(exc)]
        cfg = project.environments.get(target, {})
        if (project.key_tables or project.source_systems) and cfg.get("target", "onelake") != "local" \
                and not cfg.get("keys_workspace_id"):
            errors.append(f"Pick a Keys workspace for {target.title()} first (Metadata > Surrogate keys)")
        return {"source": source, "target": target, "version": project.version, "errors": errors}

    def meta_promote(self, source: str, target: str) -> dict[str, Any]:
        project = self._project()
        src = self._store(project, source)
        errors = promote_checks(project, src, target, dt.date.today())
        if errors:
            raise GateError("promote", errors)
        target_store = self._store(project, target, create=True)
        uses_keys = bool(project.key_tables or project.source_systems)
        keys_store = self._keys_store(project, target, create=True) if uses_keys else None  # fails before promoting
        result = promote(project, src, target_store, dt.date.today())
        if keys_store is not None:
            # Each environment issues its own keys; promote copies the source systems and seeds new key tables.
            result["keys"] = keys_mod.sync(project, keys_store)
        return result

    # ---- surrogate keys and source systems

    def meta_keys(self, env: str = "dev") -> dict[str, Any]:
        project = self._project()
        summary = project.summary()
        cfg = project.environments.get(env, {})
        local = cfg.get("target", "onelake" if env != "dev" else "local") == "local"
        tables, problem = summary["key_tables"], None
        try:
            store = self._keys_store(project, env)
            tables = [t | keys_mod.key_stats(store, project.key_tables[t["name"]]) for t in tables]
        except Exception as exc:  # the definitions are still useful when LH_Keys can't be read
            problem = str(exc)
        return {"env": env, "local": local, "path": str(project.local_data / f"keys-{env}") if local else None,
                "workspace_id": cfg.get("keys_workspace_id", ""), "workspace_name": cfg.get("keys_workspace_name", ""),
                "lakehouse": keys_mod.KEYS_LAKEHOUSE, "key_tables": tables,
                "source_systems": summary["source_systems"], "system_types": list(keys_mod.SYSTEM_TYPES),
                "connection_fields": list(keys_mod.CONNECTION_FIELDS), "problem": problem,
                "errors": [x for x in summary["errors"] if x.startswith(("keys/", "source system"))]}

    def meta_new_key_table(self, name: str, description: str = "", env: str = "dev") -> dict[str, Any]:
        project = self._project()
        name = name.strip().lower()
        if name in project.key_tables:
            raise ValueError(f"key table {name} already exists")
        owner = ""
        try:
            owner = self.auth.signed_in_user() or ""  # type: ignore[attr-defined]
        except Exception:
            pass
        keys_mod.write_key_table(project.root, name, owner, description)
        return self.meta_keys(env)

    def meta_delete_key_table(self, name: str, env: str = "dev") -> dict[str, Any]:
        project = self._project()
        users = [e.name for e in project.entities.values() if name in [k["table"] for k in keys_mod.entity_keys(e)]]
        if users:
            raise GateError("save", [f"can't delete key table {name}: {', '.join(users)} use it"])
        project.key_tables[name].path.unlink()
        return self.meta_keys(env)

    def meta_key_lookup(self, table: str, value: str, env: str = "dev") -> dict[str, Any] | None:
        project = self._project()
        return keys_mod.lookup(self._keys_store(project, env), project.key_tables[table], value)

    def meta_save_source_system(self, system: dict[str, Any], original_id: int | None = None,
                                env: str = "dev") -> dict[str, Any]:
        project = self._project()
        new = keys_mod.source_system_from_dict(system)
        # original_id is the id being edited; without it this is a new source system.
        others = [s for s in project.source_systems if original_id is None or s.id != int(original_id)]
        if original_id is not None and new.id != int(original_id):
            raise ValueError("A source system's id never changes once it is saved")
        if any(s.id == new.id for s in others):
            raise ValueError(f"id {new.id} is already used by another source system")
        if any(s.code.upper() == new.code.upper() for s in others):
            raise ValueError(f"code {new.code} is already used by another source system")
        path = keys_mod.keys_dir(project.root) / keys_mod.SOURCE_SYSTEMS_FILE
        old = path.read_text(encoding="utf-8") if path.exists() else None
        keys_mod.write_source_systems(project.root, others + [new])
        project.reload()
        problems = [x for x in keys_mod.validate_keys(project) if x.startswith(("source system", "keys/source"))]
        if problems:
            if old is None:
                path.unlink()
            else:
                path.write_text(old, encoding="utf-8")
            raise GateError("save", problems)
        return self.meta_keys(env)

    def meta_delete_source_system(self, id: int, env: str = "dev") -> dict[str, Any]:
        project = self._project()
        keys_mod.write_source_systems(project.root, [s for s in project.source_systems if s.id != int(id)])
        return self.meta_keys(env)

    def meta_keys_sync(self, env: str = "dev") -> dict[str, Any]:
        """Write the source system tables and create any missing key tables in the environment's LH_Keys."""
        project = self._project()
        errors = [x for x in validate(project, dt.date.today()) if x.startswith(("keys/", "source system"))]
        if errors:
            raise GateError("save", errors)
        return keys_mod.sync(project, self._keys_store(project, env, create=True))

    def meta_key_notebook(self, name: str, env: str = "dev") -> dict[str, Any]:
        """A template notebook for the entity whose first cell is the surrogate key MERGE."""
        project = self._project()
        e = project.entities[name]
        errors = [x for x in validate(project, dt.date.today()) if x.startswith(name + ":")]
        if errors:
            raise GateError("save", errors)
        if not keys_mod.entity_keys(e):
            raise ValueError(f"{name} has no [[keys]] block, so there is no key MERGE to start its notebook with")
        src = project.entities[e.spec["source"]["entity"]]
        nb_name = f"nb_{src.layer}_to_{e.layer}_{e.table}"
        cells = keys_mod.template_cells(project, e, project.environments.get(env, {}).get("keys_workspace_name", ""))
        self.notebooks.save(nb_name, cells)  # type: ignore[attr-defined]
        return {"name": nb_name}
