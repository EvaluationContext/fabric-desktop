"""JSON-lines RPC over stdin/stdout, used by the Tauri shell.

Request:  {"id": 1, "method": "list_workspaces", "params": {}}
Response: {"id": 1, "result": [...]}  or  {"id": 1, "error": "message"}
"""

from __future__ import annotations

import json
import os
import sys
from typing import IO

from .service import Service

METHODS = {"environment", "sign_in", "list_workspaces", "list_lakehouses", "create_lakehouse",
           "catalog", "catalog_tables", "endorsements", "set_favorite",
           "list_tables", "list_files", "preview_job", "run_job", "history",
           "run_cell", "restart_kernel", "run_sql", "save_sql", "export_sql_csv", "table_columns", "list_notebooks", "new_notebook", "open_notebook",
           "save_notebook", "rename_notebook", "delete_notebook", "publish_notebook",
           "meta_project", "meta_init", "meta_open", "meta_set_environment", "meta_entity", "meta_save_entity",
           "meta_new_entity", "meta_delete_entity", "meta_set_layers", "meta_run", "meta_runs", "meta_run_detail",
           "meta_preview", "meta_promote_check", "meta_promote"}


def handle(service: Service, line: str) -> dict:
    try:
        req = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"id": None, "error": f"invalid JSON: {exc}"}
    req_id, method = req.get("id"), req.get("method")
    if method not in METHODS:
        return {"id": req_id, "error": f"unknown method {method!r}"}
    try:
        return {"id": req_id, "result": getattr(service, method)(**req.get("params", {}))}
    except Exception as exc:  # report every failure to the UI rather than dying
        if hasattr(exc, "errors"):  # a gate: its message already lists every reason
            return {"id": req_id, "error": str(exc)}
        return {"id": req_id, "error": f"{type(exc).__name__}: {exc}"}


def serve(service: Service | None = None, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
    service = service or Service()
    for line in stdin:
        if line.strip():
            stdout.write(json.dumps(handle(service, line), default=str) + "\n")
            stdout.flush()


def _log_file() -> IO[str]:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    os.makedirs(os.path.join(base, "FabricDesktop"), exist_ok=True)
    return open(os.path.join(base, "FabricDesktop", "sidecar.log"), "a", encoding="utf-8", buffering=1)


def main() -> None:
    # Started from a windowed app, Python can have no stderr at all (sys.stderr is
    # None). Log to a file then, rather than failing on the first write.
    if sys.stderr is None:
        sys.stderr = _log_file()
    # Keep a private handle on stdout for responses, and send fd 1 to stderr so
    # anything a notebook cell or a native library prints cannot corrupt the protocol.
    rpc_out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    sys.stdin.reconfigure(encoding="utf-8")  # the shell sends UTF-8 whatever the Windows code page
    serve(stdin=sys.stdin, stdout=rpc_out)


if __name__ == "__main__":
    main()
