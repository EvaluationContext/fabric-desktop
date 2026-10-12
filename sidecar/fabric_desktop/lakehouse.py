"""The attached lakehouse, as notebooks see it.

    import polars as pl
    from fabric_desktop import lakehouse

    sales = lakehouse.read_csv("Files/raw/sales.csv")
    lakehouse.write_table(sales, "dbo.sales")

Paths that start with ``Files/`` are read from the attached lakehouse through
OneLake; any other path is a file on this machine. Tables are named
``schema.table`` (``dbo`` when the schema is left out). All compute runs
locally; only the input file download and the Delta write touch OneLake.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import polars as pl
import pyarrow as pa
from deltalake import DeltaTable

from .onelake import FileStore, TableRef, storage_options
from .publish import publish, stage_locally

MODES = ("overwrite", "append")


@dataclass(frozen=True)
class Attached:
    workspace_id: str
    lakehouse_id: str
    name: str
    storage_token: Callable[[], str]
    work_dir: Path
    history_file: Path | None = None


@dataclass(frozen=True)
class WriteResult:
    table: str
    rows: int
    delta_version: int
    uri: str

    def __repr__(self) -> str:
        return f"Wrote {self.rows} rows to {self.table} (Delta version {self.delta_version})"


_attached: Attached | None = None


def attach(context: Attached | None) -> None:
    """Set the lakehouse that notebook code reads from and writes to."""
    global _attached
    _attached = context


def _current() -> Attached:
    if _attached is None:
        raise RuntimeError("No lakehouse is attached. Use Add data items to attach one.")
    return _attached


def name() -> str | None:
    return _attached.name if _attached else None


def table_ref(table: str) -> TableRef:
    schema, _, name_ = table.rpartition(".")
    lh = _current()
    return TableRef(lh.workspace_id, lh.lakehouse_id, schema or "dbo", name_)


def local_path(path: str) -> Path:
    """A local path for ``path``, downloading it first when it is under ``Files/``."""
    if not path.replace("\\", "/").startswith("Files/"):
        return Path(path)
    lh = _current()
    rel = path.replace("\\", "/")
    dest = lh.work_dir / "files" / lh.lakehouse_id / rel
    return FileStore(lh.storage_token).download(lh.workspace_id, lh.lakehouse_id, rel, dest)


def read_csv(path: str, **kwargs: Any) -> pl.DataFrame:
    return pl.read_csv(local_path(path), **kwargs)


def read_parquet(path: str, **kwargs: Any) -> pl.DataFrame:
    return pl.read_parquet(local_path(path), **kwargs)


def read_json(path: str, **kwargs: Any) -> pl.DataFrame:
    return pl.read_json(local_path(path), **kwargs)


def read_table(table: str) -> pl.DataFrame:
    ref = table_ref(table)
    opts = storage_options(_current().storage_token())
    return pl.from_arrow(DeltaTable(ref.uri, storage_options=opts).to_pyarrow_table())


def files(directory: str = "Files") -> list[str]:
    lh = _current()
    return [e["path"] for e in FileStore(lh.storage_token).list(lh.workspace_id, lh.lakehouse_id, directory)]


def _to_arrow(data: Any) -> pa.Table:
    if isinstance(data, pa.Table):
        return data
    if isinstance(data, pl.LazyFrame):
        data = data.collect()
    if isinstance(data, pl.DataFrame):
        return data.to_arrow()
    if type(data).__module__.startswith("duckdb"):
        return (getattr(data, "to_arrow_table", None) or data.arrow)()
    if type(data).__module__.startswith("pandas"):
        return pa.Table.from_pandas(data, preserve_index=False)
    return pa.table(data)


def write_table(data: Any, table: str, mode: str = "overwrite") -> WriteResult:
    """Write ``data`` to the attached lakehouse as the Delta table ``schema.table``."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    started = time.monotonic()
    lh = _current()
    ref = table_ref(table)
    arrow = _to_arrow(data)
    stage_locally(arrow, lh.work_dir / "staging" / lh.lakehouse_id / ref.schema / ref.table)
    delta = publish(arrow, ref, mode, lh.storage_token())
    result = WriteResult(f"{ref.schema}.{ref.table}", arrow.num_rows, delta.version(), ref.uri)
    if lh.history_file:
        record = {"job": f"Notebook write to {lh.name}", "table_uri": ref.uri, "mode": mode,
                  "rows": result.rows, "delta_version": result.delta_version,
                  "seconds": round(time.monotonic() - started, 2),
                  "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        lh.history_file.parent.mkdir(parents=True, exist_ok=True)
        with lh.history_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    return result
