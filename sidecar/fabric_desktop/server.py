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
           "list_tables", "list_files", "preview_job", "run_job", "history",
           "run_cell", "restart_kernel", "list_notebooks", "new_notebook", "open_notebook",
           "save_notebook", "rename_notebook", "delete_notebook", "publish_notebook"}


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
        return {"id": req_id, "error": f"{type(exc).__name__}: {exc}"}


def serve(service: Service | None = None, stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout) -> None:
    service = service or Service()
    for line in stdin:
        if line.strip():
            stdout.write(json.dumps(handle(service, line), default=str) + "\n")
            stdout.flush()


def main() -> None:
    # Keep a private handle on stdout for responses, and send fd 1 to stderr so
    # anything a notebook cell or a native library prints cannot corrupt the protocol.
    rpc_out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    sys.stdin.reconfigure(encoding="utf-8")  # the shell sends UTF-8 whatever the Windows code page
    serve(stdin=sys.stdin, stdout=rpc_out)


if __name__ == "__main__":
    main()
