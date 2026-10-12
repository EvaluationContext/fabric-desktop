"""Notebooks: a local Python kernel and .ipynb storage.

The kernel keeps one namespace across cells, like Jupyter. A cell's printed
output is captured, and when its last statement is an expression the value
is shown: data frames as a table, anything else as its repr.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import traceback
from pathlib import Path
from typing import Any

import pyarrow as pa

PREVIEW_ROWS = 100
_CELL_FILE = "<cell>"


def _no_input(*_args, **_kwargs):
    raise RuntimeError("input() is not supported in notebook cells.")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def as_table(value: Any) -> pa.Table | None:
    """Arrow view of a data frame-like value, or None if it is not one."""
    module = type(value).__module__
    if isinstance(value, pa.Table):
        return value
    if module.startswith("polars"):
        if hasattr(value, "collect"):
            value = value.head(PREVIEW_ROWS + 1).collect()
        return value.to_arrow() if hasattr(value, "to_arrow") else None
    if module.startswith("duckdb") and hasattr(value, "limit"):
        rel = value.limit(PREVIEW_ROWS + 1)
        return (getattr(rel, "to_arrow_table", None) or rel.arrow)()
    if module.startswith("pandas") and hasattr(value, "columns"):
        return pa.Table.from_pandas(value.head(PREVIEW_ROWS + 1), preserve_index=False)
    return None


def display(value: Any) -> dict[str, Any]:
    table = as_table(value)
    if table is None:
        return {"type": "text", "text": repr(value)}
    total = getattr(value, "height", None) or (len(value) if hasattr(value, "__len__") else None)
    rows = [[_json_value(v) for v in row.values()] for row in table.slice(0, PREVIEW_ROWS).to_pylist()]
    return {"type": "table", "columns": table.schema.names, "rows": rows,
            "total_rows": total if total is not None else table.num_rows}


class Kernel:
    def __init__(self):
        self.restart()

    def restart(self) -> None:
        self.namespace: dict[str, Any] = {"__name__": "__main__", "input": _no_input}
        self.count = 0

    def run(self, code: str) -> dict[str, Any]:
        self.count += 1
        out = io.StringIO()
        outputs: list[dict[str, Any]] = []
        status = "ok"
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                tree = ast.parse(code, _CELL_FILE)
                last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
                exec(compile(tree, _CELL_FILE, "exec"), self.namespace)
                value = None
                if last is not None:
                    value = eval(compile(ast.Expression(last.value), _CELL_FILE, "eval"), self.namespace)
                if value is not None:
                    self.namespace["_"] = value
                    outputs.append(display(value))
            except (Exception, SystemExit) as exc:  # a cell must never take the kernel down
                status = "error"
                outputs.append(self._error(exc))
        if out.getvalue():
            outputs.insert(0, {"type": "stream", "text": out.getvalue()})
        return {"status": status, "execution_count": self.count, "outputs": outputs}

    @staticmethod
    def _error(exc: BaseException) -> dict[str, Any]:
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename != __file__]
        lines = traceback.format_list(frames) + traceback.format_exception_only(type(exc), exc)
        return {"type": "error", "ename": type(exc).__name__, "evalue": str(exc),
                "traceback": "Traceback (most recent call last):\n" + "".join(lines)}


# Storage: notebooks are standard .ipynb files with the attached lakehouse in metadata.

_SAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def check_notebook_name(name: str) -> str:
    name = name.strip()
    if not name or _SAFE_NAME.search(name) or name.endswith("."):
        raise ValueError(f"Invalid notebook name {name!r}: avoid < > : \" / \\ | ? * and a trailing dot.")
    return name


def to_ipynb(cells: list[dict[str, Any]], lakehouse: dict[str, Any] | None) -> dict[str, Any]:
    nb_cells = []
    for c in cells:
        cell = {"cell_type": "markdown" if c.get("type") == "markdown" else "code",
                "metadata": {}, "source": c.get("source", "").splitlines(keepends=True)}
        if cell["cell_type"] == "code":
            cell.update(execution_count=None, outputs=[])
        nb_cells.append(cell)
    metadata: dict[str, Any] = {
        "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
        "language_info": {"name": "python"},
    }
    if lakehouse:
        metadata["fabric_desktop"] = {"lakehouse": lakehouse}
        # Fabric reads this to show the default lakehouse when the notebook is published.
        metadata["dependencies"] = {"lakehouse": {
            "default_lakehouse": lakehouse["lakehouse_id"],
            "default_lakehouse_name": lakehouse["name"],
            "default_lakehouse_workspace_id": lakehouse["workspace_id"],
        }}
    return {"nbformat": 4, "nbformat_minor": 5, "metadata": metadata, "cells": nb_cells}


def from_ipynb(nb: dict[str, Any]) -> dict[str, Any]:
    cells = []
    for c in nb.get("cells", []):
        source = c.get("source", "")
        cells.append({"type": "markdown" if c.get("cell_type") == "markdown" else "code",
                      "source": "".join(source) if isinstance(source, list) else source})
    lakehouse = nb.get("metadata", {}).get("fabric_desktop", {}).get("lakehouse")
    return {"cells": cells, "lakehouse": lakehouse}


class NotebookStore:
    def __init__(self, folder: Path):
        self.folder = folder

    def path(self, name: str) -> Path:
        return self.folder / f"{check_notebook_name(name)}.ipynb"

    def list(self) -> list[dict[str, Any]]:
        if not self.folder.exists():
            return []
        files = sorted(self.folder.glob("*.ipynb"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.stem, "modified": p.stat().st_mtime} for p in files]

    def load(self, name: str) -> dict[str, Any]:
        nb = json.loads(self.path(name).read_text(encoding="utf-8"))
        return {"name": name, **from_ipynb(nb)}

    def save(self, name: str, cells: list[dict[str, Any]], lakehouse: dict[str, Any] | None = None) -> dict[str, Any]:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(to_ipynb(cells, lakehouse), indent=1), encoding="utf-8")
        return {"name": name}

    def new(self, prefix: str = "Notebook") -> dict[str, Any]:
        taken = {n["name"] for n in self.list()}
        i = 1
        while f"{prefix} {i}" in taken:
            i += 1
        name = f"{prefix} {i}"
        self.save(name, [{"type": "code", "source": ""}])
        return self.load(name)

    def rename(self, name: str, new_name: str) -> dict[str, Any]:
        target = self.path(new_name)
        if target.exists():
            raise ValueError(f"A notebook named {new_name!r} already exists.")
        self.path(name).rename(target)
        return {"name": new_name}

    def delete(self, name: str) -> None:
        self.path(name).unlink(missing_ok=True)
