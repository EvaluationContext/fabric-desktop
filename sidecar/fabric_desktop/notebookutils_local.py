"""``notebookutils`` (and ``mssparkutils``) for notebooks on this PC.

The calls Fabric notebooks use most work the same here:

- ``fs``: ls, head, cp, mv, put, rm, mkdirs, exists, over the attached lakehouse
  (``Files/...``, ``/lakehouse/default/Files/...`` or abfss paths) and this PC (``file:/...``).
- ``notebook``: run another notebook from this PC's notebooks folder, exit with a value.
- ``credentials``: getToken for storage and Fabric, getSecret from Azure Key Vault as you.
- ``runtime.context``: the notebook, lakehouse and workspace, with ``isLocal`` set.

What only Fabric has (mounting other storage, session settings) raises an error that says so.
"""

from __future__ import annotations

import os
import shutil
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from . import lakehouse
from .onelake import FileStore


class NotebookExit(Exception):
    """Raised by notebookutils.notebook.exit; the kernel ends the run quietly with the value."""

    def __init__(self, value: Any):
        super().__init__(str(value))
        self.value = value


@dataclass
class FileInfo:
    name: str
    path: str
    size: int
    isDir: bool  # noqa: N815 - Fabric's name

    @property
    def isFile(self) -> bool:  # noqa: N802
        return not self.isDir

    def __repr__(self) -> str:
        return f"FileInfo(path={self.path}, name={self.name}, size={self.size})"


class Host:
    """What the utilities need from the app: tokens, the notebook store and the kernel."""

    def __init__(self, token: Callable[[str], str] | None = None,
                 run_notebook: Callable[[str, int, dict[str, Any]], Any] | None = None,
                 notebook_name: Callable[[], str] | None = None):
        self.token = token
        self.run_notebook = run_notebook
        self.notebook_name = notebook_name or (lambda: "")


def _store() -> FileStore:
    return FileStore(lakehouse._current().storage_token)


def _split(path: str) -> tuple[str, str]:
    """("onelake", "Files/...") for the lakehouse, or ("local", path) for this PC."""
    p = str(path)
    if p.startswith("file:"):
        return "local", p[len("file:"):] if not p.startswith("file://") else p[len("file://"):]
    rel = lakehouse.relative_path(p)
    if rel is not None:
        return "onelake", rel
    if p.startswith(("abfss://", "wasbs://", "https://")):
        raise ValueError(f"{path}: on this PC only the attached lakehouse and local files are reachable.")
    return "local", p


def _make_fs() -> types.SimpleNamespace:
    def ls(path: str) -> list[FileInfo]:
        where, p = _split(path)
        if where == "local":
            base = Path(p)
            return [FileInfo(e.name, "file:" + str(e), 0 if e.is_dir() else e.stat().st_size, e.is_dir())
                    for e in sorted(base.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))]
        lh = lakehouse._current()
        return [FileInfo(e["name"], e["path"], e["size"], e["is_dir"])
                for e in _store().list(lh.workspace_id, lh.lakehouse_id, p)]

    def _read(path: str) -> bytes:
        where, p = _split(path)
        if where == "local":
            return Path(p).read_bytes()
        lh = lakehouse._current()
        data = _store().read_bytes(lh.workspace_id, lh.lakehouse_id, p)
        if data is None:
            raise FileNotFoundError(path)
        return data

    def _write(path: str, data: bytes) -> None:
        where, p = _split(path)
        if where == "local":
            Path(p).parent.mkdir(parents=True, exist_ok=True)
            Path(p).write_bytes(data)
            return
        if not p.startswith("Files/"):
            raise ValueError(f"{path}: write files under Files/. Tables are written with write_table or saveAsTable.")
        lh = lakehouse._current()
        _store().upload(lh.workspace_id, lh.lakehouse_id, p, data)

    def exists(path: str) -> bool:
        where, p = _split(path)
        if where == "local":
            return Path(p).exists()
        lh = lakehouse._current()
        parent, _, name = p.rstrip("/").rpartition("/")
        try:
            return any(e["name"] == name for e in _store().list(lh.workspace_id, lh.lakehouse_id, parent or "Files"))
        except requests.HTTPError:
            return False

    def head(path: str, maxBytes: int = 65536) -> str:  # noqa: N803
        return _read(path)[:maxBytes].decode("utf-8", errors="replace")

    def put(path: str, content: str, overwrite: bool = False) -> bool:
        if not overwrite and exists(path):
            raise FileExistsError(f"{path} exists. Pass overwrite=True to replace it.")
        _write(path, content.encode("utf-8"))
        return True

    def cp(src: str, dest: str, recurse: bool = False) -> bool:
        where, p = _split(src)
        if where == "local" and Path(p).is_dir():
            if not recurse:
                raise IsADirectoryError(f"{src} is a folder. Pass recurse=True to copy it.")
            for f in Path(p).rglob("*"):
                if f.is_file():
                    _write(f"{dest.rstrip('/')}/{f.relative_to(p).as_posix()}", f.read_bytes())
            return True
        if where == "onelake" and recurse:
            for e in ls(src):
                cp(e.path, f"{dest.rstrip('/')}/{e.name}", recurse=e.isDir)
            return True
        _write(dest, _read(src))
        return True

    def rm(path: str, recurse: bool = False) -> bool:
        where, p = _split(path)
        if where == "local":
            target = Path(p)
            if target.is_dir():
                if not recurse:
                    raise IsADirectoryError(f"{path} is a folder. Pass recurse=True to remove it.")
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            return True
        lh = lakehouse._current()
        _store().delete(lh.workspace_id, lh.lakehouse_id, p)
        return True

    def mv(src: str, dest: str, create_path: bool = False, overwrite: bool = False) -> bool:  # noqa: ARG001
        if not overwrite and exists(dest):
            raise FileExistsError(f"{dest} exists. Pass overwrite=True to replace it.")
        cp(src, dest, recurse=True)
        rm(src, recurse=True)
        return True

    def mkdirs(path: str) -> bool:
        where, p = _split(path)
        if where == "local":
            Path(p).mkdir(parents=True, exist_ok=True)
        # OneLake folders appear when a file is written into them.
        return True

    def append(path: str, content: str, createFileIfNotExists: bool = False) -> bool:  # noqa: N803
        old = b""
        if exists(path):
            old = _read(path)
        elif not createFileIfNotExists:
            raise FileNotFoundError(path)
        _write(path, old + content.encode("utf-8"))
        return True

    def getMountPath(mountPoint: str = "/default") -> str:  # noqa: N802, N803
        if mountPoint.strip("/") != "default":
            raise ValueError("On this PC only the attached lakehouse is mounted, as /lakehouse/default.")
        return "/lakehouse/default"

    def mount(*_a: Any, **_kw: Any) -> None:
        raise NotImplementedError("Mounting other storage runs in Fabric. On this PC, read the attached lakehouse.")

    def help(*_a: Any) -> None:  # noqa: A001
        print("notebookutils.fs on this PC: ls, head, put, append, cp, mv, rm, mkdirs, exists, getMountPath")

    return types.SimpleNamespace(ls=ls, head=head, put=put, append=append, cp=cp, fastcp=cp, mv=mv, rm=rm,
                                 mkdirs=mkdirs, exists=exists, getMountPath=getMountPath, mount=mount,
                                 unmount=mount, mounts=lambda: [], help=help)


_AUDIENCES = {
    "storage": ["https://storage.azure.com/user_impersonation"],
    "pbi": ["https://analysis.windows.net/powerbi/api/.default"],
    "keyvault": ["https://vault.azure.net/user_impersonation"],
    "kusto": ["https://kusto.kusto.windows.net/.default"],
}


def _make_credentials(host: Host) -> types.SimpleNamespace:
    def getToken(audience: str) -> str:  # noqa: N802
        if host.token is None:
            raise RuntimeError("Sign in first: tokens come from your Fabric sign-in.")
        key = audience.lower()
        if key not in _AUDIENCES and not key.startswith("https://"):
            raise ValueError(f"Unknown audience {audience!r}. Use storage, pbi, keyvault, kusto or a resource URL.")
        return host.token(key)

    def getSecret(akvName: str, secret: str, linkedService: str | None = None) -> str:  # noqa: N802, N803, ARG001
        vault = akvName if akvName.startswith("https://") else f"https://{akvName}.vault.azure.net"
        resp = requests.get(f"{vault.rstrip('/')}/secrets/{secret}", params={"api-version": "7.4"},
                            headers={"Authorization": f"Bearer {getToken('keyvault')}"}, timeout=30)
        if resp.status_code in (401, 403):
            raise PermissionError(f"Key Vault {vault} refused access to {secret!r} for your account. "
                                  f"You need the Key Vault Secrets User role on it.")
        resp.raise_for_status()
        return resp.json()["value"]

    return types.SimpleNamespace(getToken=getToken, getSecret=getSecret,
                                 help=lambda *_a: print("notebookutils.credentials on this PC: getToken, getSecret"))


def _make_notebook(host: Host) -> types.SimpleNamespace:
    def run(path: str, timeout_seconds: int = 90, arguments: dict[str, Any] | None = None, **_kw: Any) -> Any:
        if host.run_notebook is None:
            raise RuntimeError("Running other notebooks isn't available here.")
        return host.run_notebook(path, timeout_seconds, arguments or {})

    def exit(value: Any = "") -> None:  # noqa: A001
        raise NotebookExit(value)

    def runMultiple(dag: Any, *_a: Any, **_kw: Any) -> dict[str, Any]:  # noqa: N802
        names = dag if isinstance(dag, list) else [a.get("path") for a in dag.get("activities", [])]
        return {n: {"exitVal": run(n), "exception": None} for n in names}

    return types.SimpleNamespace(run=run, exit=exit, runMultiple=runMultiple,
                                 help=lambda *_a: print("notebookutils.notebook on this PC: run, exit, runMultiple"))


def _make_runtime(host: Host) -> types.SimpleNamespace:
    class _Context(dict):
        def __getitem__(self, key: str) -> Any:
            self.refresh()
            return super().__getitem__(key)

        def __repr__(self) -> str:
            self.refresh()
            return super().__repr__()

        def get(self, key: str, default: Any = None) -> Any:
            self.refresh()
            return super().get(key, default)

        def refresh(self) -> None:
            lh = lakehouse._attached
            self.update({
                "currentNotebookName": host.notebook_name(),
                "defaultLakehouseId": lh.lakehouse_id if lh else None,
                "defaultLakehouseName": lh.name if lh else None,
                "defaultLakehouseWorkspaceId": lh.workspace_id if lh else None,
                "currentWorkspaceId": lh.workspace_id if lh else None,
                "currentWorkspaceName": lh.workspace_name if lh else None,
                "isForPipeline": False,
                "isReferenceRun": False,
                "isLocal": True,
                "runtimeVersion": "Fabric Desktop (this PC)",
                "userName": os.environ.get("USERNAME") or os.environ.get("USER") or "",
            })

    return types.SimpleNamespace(context=_Context())


def build(host: Host) -> types.ModuleType:
    mod = types.ModuleType("notebookutils", __doc__)
    mod.fs = _make_fs()
    mod.credentials = _make_credentials(host)
    mod.notebook = _make_notebook(host)
    mod.runtime = _make_runtime(host)
    mod.mssparkutils = mod  # ``from notebookutils import mssparkutils`` still works
    mod.help = lambda *_a: print("notebookutils on this PC: fs, notebook, credentials, runtime")
    return mod
