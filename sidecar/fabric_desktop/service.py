"""Operations the desktop UI calls, shared by the stdio server and the CLI."""

from __future__ import annotations

import json
import os
import math
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pyarrow.csv as pa_csv

from .auth import ADMIN_SCOPES, Authenticator
from .fabric_api import FabricClient
from . import lakehouse, sql
from .jobs import Job, run_local
from .meta_service import MetaMixin
from .notebook import Kernel, NotebookStore, to_ipynb
from .onelake import FileStore, TableCatalog
from .publish import run_job


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "FabricDesktop"


class Service(MetaMixin):
    def __init__(self, client_id: str | None = None, root: Path | None = None):
        self._client_id = client_id or os.environ.get("FABRIC_DESKTOP_CLIENT_ID", "")
        self.root = root or data_dir()
        self._auth: Authenticator | None = None
        self.kernel = Kernel()
        self.notebooks = NotebookStore(self.root / "notebooks")
        self._schema_cache: dict[str, bool] = {}
        self._tables: dict[str, Any] | None = None
        self._tables_lock = threading.Lock()
        self._endorse: dict[str, Any] | None = None
        self._endorse_lock = threading.Lock()

    @property
    def auth(self) -> Authenticator:
        if self._auth is None:
            self._auth = Authenticator(self._client_id)
        return self._auth

    @property
    def fabric(self) -> FabricClient:
        return FabricClient(self.auth.fabric_token)

    @property
    def history_file(self) -> Path:
        return self.root / "history.jsonl"

    # Each public method is one UI action.

    def environment(self) -> dict[str, Any]:
        user = tenant = None
        try:
            user = self.auth.signed_in_user()
            tenant = self.auth.signed_in_tenant()
        except Exception:  # no client ID yet, or offline: the UI offers sign-in
            pass
        return {"python": platform.python_version(), "user": user, "tenant": tenant}

    def sign_in(self) -> dict[str, Any]:
        self.auth.fabric_token()
        self.auth.storage_token()
        return {"user": self.auth.signed_in_user(), "tenant": self.auth.signed_in_tenant()}

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self.fabric.list_workspaces()

    def list_lakehouses(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.fabric.list_lakehouses(workspace_id)

    def catalog(self, all_types: bool = False) -> dict[str, Any]:
        """Every lakehouse (or, with all_types, every item) in every workspace the user can open.

        Rows carry ``item_id``; lakehouse rows also carry ``lakehouse_id``, so a row can be
        attached to a notebook as it is."""
        token = self.auth.fabric_token()  # once: the MSAL cache is not shared across threads
        workspaces = FabricClient(lambda: token).list_workspaces()
        with self._tables_lock:  # a reloaded catalog reads table names afresh
            if self._tables and not self._tables["pending"]:
                self._tables = None

        def listing(ws: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
            client = FabricClient(lambda: token)
            try:
                if all_types:
                    return ws, client.list_items(ws["id"])
                return ws, [lh | {"type": "Lakehouse"} for lh in client.list_lakehouses(ws["id"])]
            except Exception:  # e.g. a paused capacity; list the rest
                return ws, None

        items, skipped = [], []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for ws, found in pool.map(listing, workspaces):
                if found is None:
                    skipped.append(ws["displayName"])
                    continue
                for it in found:
                    row = {"workspace_id": ws["id"], "workspace_name": ws["displayName"], "item_id": it["id"],
                           "name": it["displayName"], "type": it.get("type", ""), "description": it.get("description", "")}
                    if row["type"] == "Lakehouse":
                        row["lakehouse_id"] = it["id"]
                    items.append(row)
        return {"workspaces": [{"id": w["id"], "displayName": w["displayName"]} for w in workspaces],
                "items": items, "skipped": skipped, "favorites": sorted(self._favorites())}

    def catalog_tables(self, lakehouses: list[dict[str, str]]) -> dict[str, Any]:
        """Table names per lakehouse id ("schema.table"), so the catalog can search inside lakehouses.

        Runs in the background like endorsements: {"pending": True} until every lakehouse is read."""
        with self._tables_lock:
            if self._tables is not None:
                return dict(self._tables)
            token = self.auth.storage_token()
            self._tables = {"pending": True, "items": {}}

        def read(ref: dict[str, str]) -> tuple[str, list[str]]:
            catalog = TableCatalog(lambda: token)
            try:
                return ref["lakehouse_id"], [f"{schema}.{t}" for schema in catalog.list_schemas(ref["workspace_id"], ref["lakehouse_id"])
                                             for t in catalog.list_tables(ref["workspace_id"], ref["lakehouse_id"], schema)]
            except Exception:
                return ref["lakehouse_id"], []

        def run() -> None:
            with ThreadPoolExecutor(max_workers=8) as pool:
                found = dict(pool.map(read, lakehouses))
            with self._tables_lock:
                self._tables = {"pending": False, "items": found}

        threading.Thread(target=run, daemon=True).start()
        return {"pending": True, "items": {}}

    def endorsements(self, workspace_ids: list[str]) -> dict[str, Any]:
        """Endorsement per lakehouse id, when the account can read Fabric's admin scan.

        The scan takes a while, so it runs in the background and this returns {"pending": True}
        until it is done; the RPC loop stays free for other calls meanwhile."""
        with self._endorse_lock:
            if self._endorse is not None:
                return dict(self._endorse)
        token = None
        try:
            token = self.auth.silent_token(ADMIN_SCOPES)  # here, not in the thread: MSAL stays single-threaded
        except Exception:
            pass
        if not token:
            self._endorse = {"pending": False, "available": False, "items": {},
                             "reason": "Endorsement needs Fabric admin rights and the Tenant.Read.All permission "
                                       "on the app registration."}
            return dict(self._endorse)
        self._endorse = {"pending": True, "available": False, "items": {}, "reason": ""}
        threading.Thread(target=self._scan_endorsements, args=(token, list(workspace_ids)), daemon=True).start()
        return dict(self._endorse)

    def _scan_endorsements(self, token: str, workspace_ids: list[str]) -> None:
        try:
            result = {"pending": False, "available": True, "reason": "",
                      "items": FabricClient(lambda: token).scan_endorsements(workspace_ids)}
        except Exception as e:
            result = {"pending": False, "available": False, "items": {},
                      "reason": f"Fabric's admin scan is not available to this account ({e})."}
        with self._endorse_lock:
            self._endorse = result

    @property
    def favorites_file(self) -> Path:
        return self.root / "favorites.json"

    def _favorites(self) -> set[str]:
        try:
            return set(json.loads(self.favorites_file.read_text()))
        except (OSError, ValueError):
            return set()

    def set_favorite(self, lakehouse_id: str, favorite: bool) -> list[str]:
        """Stars are kept on this PC; Fabric has no public API for its own favorites."""
        favorites = self._favorites()
        (favorites.add if favorite else favorites.discard)(lakehouse_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.favorites_file.write_text(json.dumps(sorted(favorites)))
        return sorted(favorites)

    def list_items(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.fabric.list_items(workspace_id)

    @property
    def workspace_marks_file(self) -> Path:
        return self.root / "workspaces.json"

    def workspace_marks(self) -> dict[str, list[str]]:
        """Workspace ids starred as favorites and pinned to the top, kept on this PC.

        Pins keep the order they were pinned in."""
        try:
            marks = json.loads(self.workspace_marks_file.read_text())
        except (OSError, ValueError):
            marks = {}
        return {"favorites": list(marks.get("favorites", [])), "pins": list(marks.get("pins", []))}

    def set_workspace_mark(self, workspace_id: str, mark: str, on: bool) -> dict[str, list[str]]:
        if mark not in ("favorites", "pins"):
            raise ValueError(f"unknown workspace mark {mark!r}")
        marks = self.workspace_marks()
        ids = [i for i in marks[mark] if i != workspace_id]
        marks[mark] = ids + [workspace_id] if on else ids
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace_marks_file.write_text(json.dumps(marks))
        return marks

    def create_lakehouse(self, workspace_id: str, name: str) -> dict[str, Any]:
        return self.fabric.create_lakehouse(workspace_id, name)

    def list_tables(self, workspace_id: str, lakehouse_id: str) -> dict[str, list[str]]:
        catalog = TableCatalog(self.auth.storage_token)
        return {s: catalog.list_tables(workspace_id, lakehouse_id, s)
                for s in catalog.list_schemas(workspace_id, lakehouse_id)}

    def preview_job(self, path: str, limit: int = 100) -> dict[str, Any]:
        data = run_local(Job.load(path))
        return {"rows": data.num_rows, "columns": data.schema.names,
                "sample": data.slice(0, limit).to_pylist()}

    def run_job(self, path: str) -> dict[str, Any]:
        record = run_job(Job.load(path), self.fabric, self.auth.storage_token,
                         work_dir=self.root, history_file=self.history_file)
        return record.__dict__

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.history_file.exists():
            return []
        lines = self.history_file.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:]][::-1]

    def list_files(self, workspace_id: str, lakehouse_id: str, directory: str = "Files") -> list[dict[str, Any]]:
        return FileStore(self.auth.storage_token).list(workspace_id, lakehouse_id, directory)

    # Notebook kernel. ``lakehouse`` is the notebook's attached lakehouse, sent with
    # every cell so a restarted sidecar picks it up again.

    def run_cell(self, code: str, lakehouse_ref: dict[str, str] | None = None) -> dict[str, Any]:
        self._attach(lakehouse_ref)
        return self.kernel.run(code)

    def _attach(self, lakehouse_ref: dict[str, str] | None) -> None:
        lakehouse.attach(None if not lakehouse_ref else lakehouse.Attached(
            workspace_id=lakehouse_ref["workspace_id"], lakehouse_id=lakehouse_ref["lakehouse_id"],
            name=lakehouse_ref["name"], storage_token=lambda: self.auth.storage_token(),
            work_dir=self.root, history_file=self.history_file,
            schemas_enabled=lambda: self._schemas_enabled(lakehouse_ref["workspace_id"], lakehouse_ref["lakehouse_id"])))

    # SQL query page: Spark SQL over one lakehouse's tables, run on this PC.

    def run_sql(self, query: str, lakehouse_ref: dict[str, str], limit: int = 1000) -> dict[str, Any]:
        self._attach(lakehouse_ref)
        started = time.monotonic()
        result = sql.run(query)
        columns = [c.to_pylist() for c in result.slice(0, limit).columns]  # by position: names can repeat
        return {"columns": [{"name": f.name, "type": str(f.type)} for f in result.schema],
                "rows": [[_json_value(v) for v in row] for row in zip(*columns)],
                "row_count": result.num_rows, "seconds": round(time.monotonic() - started, 2)}

    def save_sql(self, query: str, lakehouse_ref: dict[str, str], table: str, mode: str = "overwrite") -> dict[str, Any]:
        self._attach(lakehouse_ref)
        written = lakehouse.write_table(sql.run(query), table, mode)
        return {"table": written.table, "rows": written.rows, "message": repr(written)}

    def export_sql_csv(self, query: str, lakehouse_ref: dict[str, str]) -> dict[str, Any]:
        """Write the full result to the Downloads folder."""
        self._attach(lakehouse_ref)
        result = sql.run(query)
        folder = Path.home() / "Downloads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{lakehouse_ref['name']} query {time.strftime('%Y-%m-%d %H%M%S')}.csv"
        pa_csv.write_csv(result, str(path))
        return {"path": str(path), "rows": result.num_rows}

    def table_columns(self, lakehouse_ref: dict[str, str], table: str) -> list[dict[str, str]]:
        self._attach(lakehouse_ref)
        # Delta's own type names (string, long, timestamp), as Fabric shows them.
        return [{"name": f.name, "type": f.type.type if isinstance(f.type.type, str) else str(f.type)}
                for f in lakehouse.open_table(table).schema().fields]

    def _schemas_enabled(self, workspace_id: str, lakehouse_id: str) -> bool:
        """Fabric reports a defaultSchema only for lakehouses created with schemas. Asked once per lakehouse."""
        if lakehouse_id not in self._schema_cache:
            try:
                props = self.fabric.get_lakehouse(workspace_id, lakehouse_id).get("properties") or {}
                self._schema_cache[lakehouse_id] = bool(props.get("defaultSchema"))
            except Exception:  # offline or no access: keep the schema layout this app creates
                return True
        return self._schema_cache[lakehouse_id]

    def restart_kernel(self) -> dict[str, Any]:
        self.kernel.restart()
        return {"restarted": True}

    # Notebook files, stored as .ipynb under the app data folder.

    def list_notebooks(self) -> list[dict[str, Any]]:
        return self.notebooks.list()

    def new_notebook(self) -> dict[str, Any]:
        return self.notebooks.new()

    def open_notebook(self, name: str) -> dict[str, Any]:
        return self.notebooks.load(name)

    def save_notebook(self, name: str, cells: list[dict[str, Any]],
                      lakehouse_ref: dict[str, str] | None = None) -> dict[str, Any]:
        return self.notebooks.save(name, cells, lakehouse_ref)

    def rename_notebook(self, name: str, new_name: str) -> dict[str, Any]:
        return self.notebooks.rename(name, new_name)

    def delete_notebook(self, name: str) -> dict[str, Any]:
        self.notebooks.delete(name)
        return {"deleted": name}

    def publish_notebook(self, name: str, cells: list[dict[str, Any]],
                         lakehouse_ref: dict[str, str]) -> dict[str, Any]:
        """Save the notebook, then create or update it as a notebook item in the lakehouse's workspace."""
        self.notebooks.save(name, cells, lakehouse_ref)
        ipynb = json.dumps(to_ipynb(cells, lakehouse_ref)).encode()
        item = self.fabric.save_notebook(lakehouse_ref["workspace_id"], name, ipynb)
        return {"name": name, "workspace_id": lakehouse_ref["workspace_id"], "id": item.get("id")}


def _json_value(value: Any) -> Any:
    """A cell value the UI can parse: JSON has no NaN, dates or decimals."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)
