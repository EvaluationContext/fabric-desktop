"""Where an environment's tables live: Delta tables on this PC, or in OneLake.

Each layer is a lakehouse named LH_<Layer>. Run metadata (run log, quality results,
watermarks, schema history, deployments) goes to the ``meta`` schema of LH_Meta in the
same environment, so the same rows exist locally in Dev and in OneLake for Test and Prod.
Quarantined rows go to the ``quarantine`` schema of the entity's own lakehouse.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
from pathlib import Path
from typing import Any, Callable

import polars as pl
from deltalake import CommitProperties, DeltaTable, write_deltalake
from deltalake.exceptions import TableNotFoundError

from .. import delta_profile
from ..onelake import FileStore, TableRef, storage_options

META_LAKEHOUSE = "LH_Meta"
META_SCHEMAS: dict[str, dict[str, pl.DataType]] = {
    "run_log": {"run_id": pl.String, "batch_id": pl.String, "entity": pl.String, "env": pl.String,
                "status": pl.String, "metadata": pl.String, "logic": pl.String, "load_date": pl.String,
                "started_at": pl.String, "seconds": pl.Float64, "rows_read": pl.Int64, "rows_written": pl.Int64,
                "quarantined": pl.Int64, "delta_version": pl.Int64, "backfill": pl.String, "message": pl.String,
                "steps": pl.String},
    "dq_results": {"run_id": pl.String, "entity": pl.String, "check": pl.String, "failed": pl.Int64,
                   "action": pl.String},
    "watermarks": {"entity": pl.String, "value": pl.String, "run_id": pl.String, "at": pl.String},
    "schema_history": {"entity": pl.String, "run_id": pl.String, "change": pl.String, "columns": pl.String,
                       "at": pl.String},
    "deployments": {"env": pl.String, "version": pl.String, "source_env": pl.String, "at": pl.String},
    "entities": {"version": pl.String, "name": pl.String, "config": pl.String, "at": pl.String},
}
# Delta returns rows in no particular order, so every meta table is sorted by when it was written.
_ORDER = {"run_log": "started_at", "dq_results": None, "watermarks": "at", "schema_history": "at",
          "deployments": "at", "entities": "at"}


def _read(uri: str, options: dict[str, str] | None, version: int | None = None) -> pl.DataFrame | None:
    try:
        table = DeltaTable(uri, storage_options=options, version=version)
    except TableNotFoundError:
        return None
    return pl.from_arrow(table.to_pyarrow_table())


class Store:
    env: str

    def uri(self, lakehouse: str, schema: str, table: str) -> str:
        raise NotImplementedError

    def options(self) -> dict[str, str] | None:
        return None

    def file_paths(self, lakehouse: str, pattern: str) -> list[Path]:
        raise NotImplementedError

    # ---- tables

    def read(self, lakehouse: str, schema: str, table: str, version: int | None = None) -> pl.DataFrame | None:
        """The table now, or as it was at a Delta ``version`` (time travel)."""
        return _read(self.uri(lakehouse, schema, table), self.options(), version)

    def write(self, lakehouse: str, schema: str, table: str, df: pl.DataFrame, mode: str = "overwrite",
              metadata: dict[str, str] | None = None) -> int:
        uri = self.uri(lakehouse, schema, table)
        if isinstance(self, LocalStore):
            Path(uri).mkdir(parents=True, exist_ok=True)
        write_deltalake(uri, df.to_arrow(), mode=mode, storage_options=self.options(),
                        schema_mode="overwrite" if mode == "overwrite" else "merge",
                        commit_properties=CommitProperties(custom_metadata=metadata) if metadata else None)
        dt_ = DeltaTable(uri, storage_options=self.options())
        delta_profile.check_table(dt_)
        return dt_.version()

    def merge_insert(self, lakehouse: str, schema: str, table: str, df: pl.DataFrame, on: list[str]) -> int:
        """Insert the rows whose ``on`` columns aren't in the table yet, as one Delta MERGE commit."""
        uri = self.uri(lakehouse, schema, table)
        try:
            target = DeltaTable(uri, storage_options=self.options())
        except TableNotFoundError:
            return self.write(lakehouse, schema, table, df, mode="overwrite")
        predicate = " AND ".join(f"t.{c} = s.{c}" for c in on)
        target.merge(df.to_arrow(), predicate, source_alias="s", target_alias="t") \
            .when_not_matched_insert_all().execute()
        dt_ = DeltaTable(uri, storage_options=self.options())
        delta_profile.check_table(dt_)
        return dt_.version()

    def history(self, lakehouse: str, schema: str, table: str, limit: int = 20) -> list[dict[str, Any]]:
        try:
            return DeltaTable(self.uri(lakehouse, schema, table), storage_options=self.options()).history(limit)
        except TableNotFoundError:
            return []

    # ---- files

    def read_files(self, lakehouse: str, pattern: str) -> tuple[pl.DataFrame | None, list[str]]:
        paths = self.file_paths(lakehouse, pattern)
        frames = []
        for p in paths:
            suffix = p.suffix.lower()
            if suffix == ".csv":
                frames.append(pl.read_csv(p, try_parse_dates=True, infer_schema_length=10000))
            elif suffix == ".parquet":
                frames.append(pl.read_parquet(p))
            elif suffix in (".json", ".jsonl", ".ndjson"):
                frames.append(pl.read_ndjson(p) if suffix != ".json" else pl.read_json(p))
            else:
                raise ValueError(f"Can't read {p.name}: use CSV, Parquet or JSON")
        if not frames:
            return None, []
        return pl.concat(frames, how="diagonal_relaxed"), [p.name for p in paths]

    # ---- run metadata

    def meta_append(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        schema = META_SCHEMAS[table]
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")
        df = pl.DataFrame([{k: r.get(k) for k in schema} | ({"at": r.get("at") or now} if "at" in schema else {})
                           for r in rows], schema=schema)
        self.write(META_LAKEHOUSE, "meta", table, df, mode="append")

    def meta(self, table: str) -> pl.DataFrame:
        df = self.read(META_LAKEHOUSE, "meta", table)
        if df is None:
            return pl.DataFrame(schema=META_SCHEMAS[table])
        order = _ORDER.get(table)
        return df.sort(order, maintain_order=True) if order else df


class LocalStore(Store):
    """Delta tables on this PC, laid out like OneLake: <root>/<lakehouse>/Tables/<schema>/<table>."""

    def __init__(self, root: Path, env: str = "dev"):
        self.root = Path(root)
        self.env = env

    def uri(self, lakehouse: str, schema: str, table: str) -> str:
        return str(self.root / lakehouse / "Tables" / schema / table)

    def files_dir(self, lakehouse: str) -> Path:
        return self.root / lakehouse / "Files"

    def file_paths(self, lakehouse: str, pattern: str) -> list[Path]:
        return sorted(p for p in self.files_dir(lakehouse).glob(pattern) if p.is_file())


class OneLakeStore(Store):
    """Delta tables in an environment's workspace: one lakehouse per layer, addressed by GUID."""

    def __init__(self, env: str, workspace_id: str, lakehouse_ids: dict[str, str],
                 storage_token: Callable[[], str], cache_dir: Path, files: FileStore | None = None):
        self.env = env
        self.workspace_id = workspace_id
        self.lakehouse_ids = lakehouse_ids
        self._token = storage_token
        self.cache_dir = Path(cache_dir)
        self._files = files or FileStore(storage_token)

    def _lakehouse_id(self, lakehouse: str) -> str:
        if lakehouse not in self.lakehouse_ids:
            how = "run Dev to create it" if self.env == "dev" else "promote to create it"
            raise LookupError(f"{lakehouse} doesn't exist in the {self.env} workspace yet; {how}")
        return self.lakehouse_ids[lakehouse]

    def uri(self, lakehouse: str, schema: str, table: str) -> str:
        return TableRef(self.workspace_id, self._lakehouse_id(lakehouse), schema, table).uri

    def options(self) -> dict[str, str]:
        return storage_options(self._token())

    def file_paths(self, lakehouse: str, pattern: str) -> list[Path]:
        lh = self._lakehouse_id(lakehouse)
        directory, _, name_pattern = f"Files/{pattern.strip('/')}".rpartition("/")
        out = []
        for entry in self._files.list(self.workspace_id, lh, directory):
            if not entry["is_dir"] and fnmatch.fnmatch(entry["name"], name_pattern):
                dest = self.cache_dir / self.env / lakehouse / entry["path"]
                out.append(self._files.download(self.workspace_id, lh, entry["path"], dest))
        return sorted(out)
