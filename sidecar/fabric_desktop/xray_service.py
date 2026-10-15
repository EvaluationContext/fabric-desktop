"""Estate X-ray on Home: scan every lakehouse, fix what it finds, adopt a lakehouse into metadata."""

from __future__ import annotations

import json
import re
import threading
from typing import Any

import polars as pl
import pyarrow as pa
from deltalake import DeltaTable

from . import maintenance, xray
from .fabric_api import FabricClient
from .meta.templates import _columns_toml, _dtype_name
from .onelake import FileStore, TableRef, storage_options


def _entity_name(text: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")
    return name if name and not name[0].isdigit() else f"t_{name}"


def guess_keys(columns: list[str]) -> list[str]:
    """The column that most looks like the table's own id."""
    lower = [c.lower() for c in columns]
    for want in ("id",):
        if want in lower:
            return [columns[lower.index(want)]]
    ids = [c for c in columns if c.lower().endswith(("_id", "id", "_key", "key", "_code"))]
    return ids[:1] or columns[:1]


def adopted_entity_text(name: str, owner: str, uri: str, label: str, schema: dict[str, pl.DataType],
                        keys: list[str]) -> str:
    """An entity that loads an existing lakehouse table as it is (a full copy each run, or new rows by a timestamp)."""
    columns = {c: _dtype_name(t) for c, t in schema.items()}
    wm = next((c for c in columns if c.lower() in ("modified_at", "updated_at", "last_modified", "modifieddate",
                                                    "updated", "last_updated")), "")
    load = f'mode = "incremental"\nwatermark = {json.dumps(wm)}' if wm else 'mode = "full"'
    checks = "".join(f'\n[[check]]\ncolumn = {json.dumps(k)}\nnot_null = true\nunique = true\non_fail = "stop"\n' for k in keys[:1])
    return f"""name = {json.dumps(name)}
owner = {json.dumps(owner or "me")}
description = {json.dumps(f"Adopted from {label}")}

[source]
table = {json.dumps(uri)}   # read as it is; the original table is never changed
from = {json.dumps(label)}

[load]
{load}

[contract]
keys = {json.dumps(keys)}

[contract.columns]
{_columns_toml(columns)}
{checks}"""


class XRayMixin:
    _xray_state: dict[str, Any] | None = None
    _xray_lock = threading.Lock()

    @property
    def _xray_file(self):
        return self.root / "xray.json"  # type: ignore[attr-defined]

    def _xray_saved(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._xray_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def xray(self, refresh: bool = False) -> dict[str, Any]:
        """The last scan, and whether one is running ({"running": True, "progress": {...}})."""
        with self._xray_lock:
            state = self._xray_state
            running = bool(state and state.get("running"))
            if refresh and not running:
                fabric_token = self.auth.fabric_token()  # type: ignore[attr-defined]  # MSAL stays on this thread
                storage_token = self.auth.storage_token()  # type: ignore[attr-defined]
                self._xray_state = state = {"running": True, "progress": {"phase": "Starting", "done": 0, "total": 0}}
                threading.Thread(target=self._xray_run, args=(fabric_token, storage_token), daemon=True).start()
                running = True
        saved = self._xray_saved()
        out: dict[str, Any] = {"running": running, "progress": (state or {}).get("progress"), "error": (state or {}).get("error"),
                               "result": saved}
        if saved:
            out["result"] = self._xray_annotate(saved)
        return out

    def _xray_progress(self, p: dict[str, Any]) -> None:
        with self._xray_lock:
            if self._xray_state:
                self._xray_state["progress"] = p

    def _xray_run(self, fabric_token: str, storage_token: str) -> None:
        options = storage_options(storage_token)
        files = FileStore(lambda: storage_token)

        def health(ws: str, lh: str, schema: str | None, table: str) -> dict[str, Any]:
            ref = TableRef(ws, lh, schema or "dbo", table, flat=schema is None)
            return maintenance.health(ref.uri, options)

        scanner = xray.Scanner(FabricClient(lambda: fabric_token), files.list, health, self._xray_progress)
        try:
            result = scanner.scan()
            try:
                report = self.cost_report(30)  # type: ignore[attr-defined]
                local = {n["name"] for n in self.notebooks.list()}  # type: ignore[attr-defined]
                result["findings"] = xray.rank(result["findings"] + xray.cost_findings(report | {"days": 30}, local))
            except Exception:  # cost needs the run logs; the rest stands without it
                pass
            result["counts"] = xray.counts(result["findings"])
            self._xray_file.parent.mkdir(parents=True, exist_ok=True)
            self._xray_file.write_text(json.dumps(result, default=str), encoding="utf-8")
            state: dict[str, Any] = {"running": False}
        except Exception as exc:
            state = {"running": False, "error": f"{type(exc).__name__}: {exc}"}
        with self._xray_lock:
            self._xray_state = state

    def _xray_annotate(self, result: dict[str, Any]) -> dict[str, Any]:
        """Which lakehouses the metadata project already manages, and which findings were fixed."""
        managed: set[tuple[str, str]] = set()
        adopted: set[str] = set()
        try:
            project = self._project()  # type: ignore[attr-defined]
            layers = {project.layer(n).lakehouse for n in project.layer_names()}
            for cfg in project.environments.values():
                if cfg.get("workspace_id"):
                    managed |= {(cfg["workspace_id"], lh) for lh in layers}
            adopted = {str(e.spec.get("source", {}).get("table", "")) for e in project.entities.values()}
        except Exception:
            pass
        fixed = set(self._settings().get("xray_fixed", []))  # type: ignore[attr-defined]
        rows = []
        for r in result.get("lakehouse_rows", []):
            prefix = f"abfss://{r['workspace_id']}@"
            n_adopted = sum(1 for a in adopted if a.startswith(prefix) and f"/{r['lakehouse_id']}/" in a)
            rows.append(r | {"managed": (r["workspace_id"], r["lakehouse_name"]) in managed, "adopted": n_adopted})
        findings = [f | {"fixed": _finding_id(f) in fixed, "id": _finding_id(f)} for f in result.get("findings", [])]
        return result | {"lakehouse_rows": rows, "findings": findings}

    def xray_optimize(self, workspace_id: str, lakehouse_id: str, table: str, schema: str | None = None,
                      lakehouse_name: str = "", workspace_name: str = "", finding_id: str = "") -> dict[str, Any]:
        """Fabric's table maintenance (OPTIMIZE with V-Order, then VACUUM) on one table."""
        job_id = self.fabric_jobs.run_table_maintenance(workspace_id, lakehouse_id, table, schema)  # type: ignore[attr-defined]
        label = f"{schema}.{table}" if schema else table
        if finding_id:
            self._save_settings(xray_fixed=sorted(set(self._settings().get("xray_fixed", [])) | {finding_id}))  # type: ignore[attr-defined]
        return self._log_fabric_run({"kind": "Table maintenance", "name": f"{lakehouse_name} {label}".strip(),  # type: ignore[attr-defined]
                                     "table": label, "workspace_id": workspace_id, "workspace_name": workspace_name,
                                     "item_id": lakehouse_id, "job_id": job_id})

    # ---- adopt a lakehouse into the metadata project

    def _source_table(self, workspace_id: str, lakehouse_id: str, schema: str | None, table: str) -> tuple[str, DeltaTable]:
        ref = TableRef(workspace_id, lakehouse_id, schema or "dbo", table, flat=schema is None)
        return ref.uri, DeltaTable(ref.uri, storage_options=self._source_options())  # type: ignore[attr-defined]

    def xray_adopt_plan(self, workspace_id: str, lakehouse_id: str, lakehouse_name: str,
                        workspace_name: str = "") -> dict[str, Any]:
        """The lakehouse's tables, each with the entity it would become and the columns it has."""
        project = self._project()  # type: ignore[attr-defined]
        layer = project.layer_names()[0]
        files = FileStore(self.auth.storage_token)  # type: ignore[attr-defined]
        scanner = xray.Scanner(None, files.list, lambda *_a: {})
        tables, _ = scanner._tables({"workspace_id": workspace_id, "workspace_name": workspace_name,
                                     "lakehouse_id": lakehouse_id, "lakehouse_name": lakehouse_name})
        out = []
        for t in tables:
            try:
                uri, dt_ = self._source_table(workspace_id, lakehouse_id, t["schema"], t["table"])
                cols = list(_table_schema(dt_))
            except Exception as exc:
                out.append({"schema": t["schema"], "table": t["table"], "ok": False, "why": "Not a Delta table"})
                continue
            name = f"{layer}.{_entity_name(t['table'])}"
            already = next((e.name for e in project.entities.values() if e.spec.get("source", {}).get("table") == uri), None)
            out.append({"schema": t["schema"], "table": t["table"], "ok": not already, "columns": cols,
                        "keys": guess_keys(cols), "name": name, "exists": name in project.entities,
                        "why": f"Already adopted as {already}" if already else ""})
        return {"layer": layer, "layers": project.layer_names(), "tables": out}

    def xray_adopt(self, workspace_id: str, lakehouse_id: str, lakehouse_name: str, workspace_name: str,
                   tables: list[dict[str, Any]], layer: str | None = None) -> dict[str, Any]:
        """Write one entity per picked table; each loads from the original, which is never changed."""
        project = self._project()  # type: ignore[attr-defined]
        layer = layer or project.layer_names()[0]
        if layer not in project.layer_names():
            raise ValueError(f"{layer} is not a layer")
        try:
            owner = (self.auth.signed_in_user() or "").split("@")[0]  # type: ignore[attr-defined]
        except Exception:
            owner = ""
        written, problems = [], []
        for t in tables:
            name = f"{layer}.{_entity_name(t.get('name', '').split('.')[-1] or t['table'])}"
            if name in project.entities:
                problems.append(f"{name} already exists")
                continue
            try:
                uri, table = self._source_table(workspace_id, lakehouse_id, t.get("schema"), t["table"])
                schema = _table_schema(table)
            except Exception as exc:
                problems.append(f"{t['table']}: {type(exc).__name__}: {exc}")
                continue
            keys = [k for k in t.get("keys") or [] if k in schema] or guess_keys(list(schema))
            where = f"{t['schema']}.{t['table']}" if t.get("schema") else t["table"]
            label = f"{workspace_name} / {lakehouse_name} / {where}".strip(" /")
            text = adopted_entity_text(name, owner, uri, label, dict(schema), keys)
            (project.entities_dir / f"{name}.toml").write_text(text, encoding="utf-8")
            written.append(name)
        return {"adopted": written, "problems": problems, "layer": layer}


def _table_schema(table: DeltaTable) -> pl.Schema:
    return pl.from_arrow(pa.schema(table.schema().to_arrow()).empty_table()).schema  # type: ignore[union-attr]


def _finding_id(f: dict[str, Any]) -> str:
    return "|".join(str(f.get(k) or "") for k in ("kind", "workspace_id", "lakehouse_id", "schema", "table", "person", "notebook"))
