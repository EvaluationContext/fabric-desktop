"""Operations the desktop UI calls, shared by the stdio server and the CLI."""

from __future__ import annotations

import json
import os
import platform
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .auth import ADMIN_SCOPES, Authenticator
from .fabric_api import FabricClient
from . import lakehouse
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

    def catalog(self) -> dict[str, Any]:
        """Every lakehouse in every workspace the user can open, for the Add data items picker."""
        token = self.auth.fabric_token()  # once: the MSAL cache is not shared across threads
        workspaces = FabricClient(lambda: token).list_workspaces()

        def lakehouses(ws: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
            try:
                return ws, FabricClient(lambda: token).list_lakehouses(ws["id"])
            except Exception:  # e.g. a paused capacity; list the rest
                return ws, None

        items, skipped = [], []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for ws, found in pool.map(lakehouses, workspaces):
                if found is None:
                    skipped.append(ws["displayName"])
                    continue
                items += [{"workspace_id": ws["id"], "workspace_name": ws["displayName"],
                           "lakehouse_id": lh["id"], "name": lh["displayName"]} for lh in found]
        return {"workspaces": [{"id": w["id"], "displayName": w["displayName"]} for w in workspaces],
                "items": items, "skipped": skipped, "favorites": sorted(self._favorites())}

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
        lakehouse.attach(None if not lakehouse_ref else lakehouse.Attached(
            workspace_id=lakehouse_ref["workspace_id"], lakehouse_id=lakehouse_ref["lakehouse_id"],
            name=lakehouse_ref["name"], storage_token=lambda: self.auth.storage_token(),
            work_dir=self.root, history_file=self.history_file))
        return self.kernel.run(code)

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
