"""Operations the desktop UI calls, shared by the stdio server and the CLI."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any

from .auth import Authenticator
from .fabric_api import FabricClient
from . import lakehouse
from .jobs import Job, run_local
from .notebook import Kernel, NotebookStore, to_ipynb
from .onelake import FileStore, TableCatalog
from .publish import run_job


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "FabricDesktop"


class Service:
    def __init__(self, client_id: str | None = None, root: Path | None = None):
        self._client_id = client_id or os.environ.get("FABRIC_DESKTOP_CLIENT_ID", "")
        self.root = root or data_dir()
        self._auth: Authenticator | None = None
        self.kernel = Kernel()
        self.notebooks = NotebookStore(self.root / "notebooks")

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
        user = None
        try:
            user = self.auth.signed_in_user()
        except Exception:  # no client ID yet, or offline: the UI offers sign-in
            pass
        return {"python": platform.python_version(), "user": user}

    def sign_in(self) -> dict[str, Any]:
        self.auth.fabric_token()
        self.auth.storage_token()
        return {"user": self.auth.signed_in_user()}

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self.fabric.list_workspaces()

    def list_lakehouses(self, workspace_id: str) -> list[dict[str, Any]]:
        return self.fabric.list_lakehouses(workspace_id)

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
