"""A ``spark`` session for notebooks on this PC, so PySpark code written for Fabric runs here too.

It is DuckDB's PySpark-compatible API (``duckdb.experimental.spark``) wired to the attached
lakehouse: ``spark.read.table("dbo.sales")`` and ``spark.sql("SELECT * FROM sales")`` read the
lakehouse's Delta tables from OneLake, ``Files/`` paths are downloaded first, and
``df.write.saveAsTable("dbo.out")`` writes a Delta table back with ``lakehouse.write_table``.
Everything runs on this PC; there is no Spark cluster. When the notebook runs in Fabric, the
same code runs on Fabric's real Spark.

``install()`` also makes ``import pyspark`` and ``from pyspark.sql import functions as F``
resolve to the DuckDB versions, unless real PySpark is installed.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from typing import Any

import duckdb
import pyarrow as pa
from duckdb.experimental.spark.context import SparkContext
from duckdb.experimental.spark.sql import dataframe as _dataframe
from duckdb.experimental.spark.sql.dataframe import DataFrame
from duckdb.experimental.spark.sql.session import SparkSession

from . import lakehouse, sql as _sql

_UNSUPPORTED_SQL = re.compile(r"^\s*(MERGE\s+INTO|UPDATE|DELETE\s+FROM)\b", re.I)


def _arrow(df: DataFrame) -> pa.Table:
    rel = df.relation
    return (getattr(rel, "to_arrow_table", None) or rel.arrow)()


class LakehouseWriter:
    """``df.write``: Delta tables in the attached lakehouse, or files on this PC."""

    def __init__(self, df: DataFrame):
        self._df = df
        self._mode = "errorifexists"
        self._format = "delta"
        self._options: dict[str, Any] = {}

    def mode(self, mode: str) -> LakehouseWriter:
        self._mode = mode.lower()
        return self

    def format(self, fmt: str) -> LakehouseWriter:
        self._format = fmt.lower()
        return self

    def option(self, key: str, value: Any) -> LakehouseWriter:
        self._options[key] = value
        return self

    def options(self, **opts: Any) -> LakehouseWriter:
        self._options.update(opts)
        return self

    def partitionBy(self, *_cols: Any) -> LakehouseWriter:
        print("Note: partitionBy is ignored on this PC; the table is written without partitions.")
        return self

    def _write_mode(self, table: str) -> str:
        if self._mode in ("overwrite", "append"):
            return self._mode
        if self._mode == "ignore" or self._mode in ("error", "errorifexists"):
            try:
                lakehouse.open_table(table)
                exists = True
            except Exception:  # noqa: BLE001 - missing table (or no access): treat as new
                exists = False
            if exists and self._mode == "ignore":
                return "skip"
            if exists:
                raise ValueError(f"Table {table} already exists. Use .mode(\"overwrite\") or .mode(\"append\").")
            return "overwrite"
        raise ValueError(f"Unknown save mode {self._mode!r}. Use overwrite, append, ignore or error.")

    def saveAsTable(self, name: str, format: str | None = None, mode: str | None = None, **_kw: Any) -> None:  # noqa: A002
        if mode:
            self.mode(mode)
        if (format or self._format) not in ("delta", None):
            raise ValueError("Lakehouse tables are Delta tables. Leave out format, or use format(\"delta\").")
        how = self._write_mode(name)
        if how != "skip":
            print(lakehouse.write_table(_arrow(self._df), name, mode=how))

    insertInto = saveAsTable

    def save(self, path: str | None = None, format: str | None = None, mode: str | None = None, **_kw: Any) -> None:  # noqa: A002
        if mode:
            self.mode(mode)
        fmt = format or self._format
        rel = lakehouse.relative_path(path) if path else None
        if fmt == "delta" and rel and rel.startswith("Tables/"):
            parts = rel.split("/")[1:]
            return self.saveAsTable(".".join(p for p in parts if p))
        if rel is not None:
            raise ValueError(f"On this PC, write lakehouse tables with saveAsTable or a Tables/ path. "
                             f"Writing files to {rel} runs in Fabric.")
        if fmt == "parquet":
            return self._df.relation.write_parquet(path)
        if fmt == "csv":
            return self._df.relation.write_csv(path, header=bool(self._options.get("header", True)))
        raise ValueError(f"Can't write format {fmt!r} to {path} on this PC.")

    def parquet(self, path: str, mode: str | None = None, **_kw: Any) -> None:
        self.save(path, format="parquet", mode=mode)

    def csv(self, path: str, mode: str | None = None, header: Any = True, **_kw: Any) -> None:
        self._options["header"] = header in (True, "true", "True")
        self.save(path, format="csv", mode=mode)


class LakehouseReader:
    """``spark.read``: tables and files of the attached lakehouse, or files on this PC."""

    def __init__(self, session: LocalSparkSession):
        self._session = session
        self._format: str | None = None
        self._options: dict[str, Any] = {}

    def format(self, fmt: str) -> LakehouseReader:
        self._format = fmt.lower()
        return self

    def option(self, key: str, value: Any) -> LakehouseReader:
        self._options[str(key).lower()] = value
        return self

    def options(self, **opts: Any) -> LakehouseReader:
        for k, v in opts.items():
            self.option(k, v)
        return self

    def schema(self, _schema: Any) -> LakehouseReader:
        return self  # DuckDB infers types; an explicit schema is not applied on this PC

    def table(self, name: str) -> DataFrame:
        return self._session.table(name)

    def _local(self, path: str) -> str:
        return str(lakehouse.local_path(path))

    def load(self, path: str | None = None, format: str | None = None, **opts: Any) -> DataFrame:  # noqa: A002
        self.options(**opts)
        fmt = (format or self._format or "parquet").lower()
        rel = lakehouse.relative_path(path) if path else None
        if fmt == "delta":
            if rel and rel.startswith("Tables/"):
                return self._session.table(".".join(p for p in rel.split("/")[1:] if p))
            from deltalake import DeltaTable
            return self._session._from_arrow_dataset(DeltaTable(path).to_pyarrow_dataset())
        if fmt == "csv":
            return self.csv(path)
        if fmt == "json":
            return self.json(path)
        if fmt == "parquet":
            return self.parquet(path)
        if fmt == "text":
            return self.text(path)
        raise ValueError(f"Format {fmt!r} isn't supported on this PC. Use csv, json, parquet, text or delta.")

    def csv(self, path: str, header: Any = None, sep: str | None = None, inferSchema: Any = None, **opts: Any) -> DataFrame:
        self.options(**opts)
        header = self._options.get("header", header)
        sep = self._options.get("sep", self._options.get("delimiter", sep))
        conn = self._session.conn
        rel = conn.read_csv(self._local(path), header=header in (True, "true", "True"),
                            **({"sep": sep} if sep else {}))
        return DataFrame(rel, self._session)

    def parquet(self, *paths: str, **_opts: Any) -> DataFrame:
        files = [self._local(p) for p in paths]
        return DataFrame(self._session.conn.read_parquet(files if len(files) > 1 else files[0]), self._session)

    def json(self, path: str, **_opts: Any) -> DataFrame:
        return DataFrame(self._session.conn.read_json(self._local(path)), self._session)

    def text(self, path: str, **_opts: Any) -> DataFrame:
        conn = self._session.conn
        rel = conn.sql("SELECT line AS value FROM read_csv(?, columns={'line': 'VARCHAR'}, header=false, "
                       "delim=chr(1), quote='', escape='')", params=[self._local(path)])
        return DataFrame(rel, self._session)


class LocalSparkSession(SparkSession):
    """DuckDB's SparkSession, reading and writing the attached lakehouse."""

    def __init__(self) -> None:
        super().__init__(SparkContext("local"))
        self._views: dict[str, str] = {}
        self._snapshot = None

    # Lakehouse tables are registered once per attached lakehouse; a new attach starts over.
    def _bound(self) -> dict[str, str]:
        current = lakehouse._attached
        if current is not self._snapshot:
            for view in self._views.values():
                try:
                    self.conn.unregister(view)
                except Exception:  # noqa: BLE001
                    pass
            self._views = {}
            self._snapshot = current
        return self._views

    def _is_local(self, name: str) -> bool:
        rows = self.conn.execute("SELECT 1 FROM duckdb_views() WHERE lower(view_name) = lower(?) "
                                 "UNION ALL SELECT 1 FROM duckdb_tables() WHERE lower(table_name) = lower(?)",
                                 [name, name]).fetchall()
        return bool(rows)

    def _from_arrow_dataset(self, data: Any) -> DataFrame:
        return DataFrame(self.conn.from_arrow(data), self)

    @property
    def read(self) -> LakehouseReader:
        return LakehouseReader(self)

    def table(self, tableName: str) -> DataFrame:  # noqa: N803
        if "." not in tableName and self._is_local(tableName):
            return DataFrame(self.conn.table(tableName), self)
        return self.sql(f"SELECT * FROM {tableName}")

    def sql(self, sqlQuery: str, **kwargs: Any) -> DataFrame:  # noqa: N803
        if kwargs:
            sqlQuery = sqlQuery.format(**kwargs)
        if _UNSUPPORTED_SQL.match(sqlQuery):
            raise ValueError("MERGE, UPDATE and DELETE change tables in place, which runs in Fabric. "
                             "On this PC, read with spark.sql and write with df.write.saveAsTable.")
        statements = _sql.parse(sqlQuery)
        result: DataFrame | None = None
        for statement in statements:
            result = self._statement(statement)
        return result if result is not None else DataFrame(self.conn.sql("SELECT 1 WHERE false"), self)

    def _statement(self, statement: Any) -> DataFrame | None:
        from sqlglot import exp
        # CREATE TABLE ... AS SELECT and INSERT INTO ... SELECT write a lakehouse table.
        if isinstance(statement, (exp.Create, exp.Insert)) and isinstance(statement.expression, (exp.Query, exp.Select)):
            target = statement.this
            while not isinstance(target, exp.Table) and target is not None:
                target = target.this
            is_view = isinstance(statement, exp.Create) and str(statement.args.get("kind", "")).upper() == "VIEW"
            temporary = statement.args.get("properties") and "TEMPORARY" in statement.args["properties"].sql().upper()
            if isinstance(target, exp.Table) and not is_view and not temporary:
                name = f"{target.db}.{target.name}" if target.db else target.name
                query = statement.expression.copy()
                _sql.bind_tables(query, self.conn, self._bound(), self._is_local)
                data = self.conn.sql(query.sql("duckdb"))
                mode = "append" if isinstance(statement, exp.Insert) and not statement.args.get("overwrite") else "overwrite"
                print(lakehouse.write_table((getattr(data, "to_arrow_table", None) or data.arrow)(), name, mode=mode))
                return None
        _sql.bind_tables(statement, self.conn, self._bound(), self._is_local)
        text = statement.sql("duckdb")
        if isinstance(statement, (exp.Query, exp.Select, exp.Describe, exp.Show)) or text.upper().startswith(("SELECT", "WITH", "FROM", "VALUES", "DESCRIBE", "SHOW")):
            return DataFrame(self.conn.sql(text), self)
        self.conn.execute(text)
        return None

    def createDataFrame(self, data: Any, schema: Any = None, **kw: Any) -> DataFrame:  # noqa: N802
        module = type(data).__module__
        if module.startswith(("polars", "pyarrow")):
            arrow = data.to_arrow() if module.startswith("polars") else data
            return DataFrame(self.conn.from_arrow(arrow), self)
        return super().createDataFrame(data, schema, **kw)


# ``df.write`` goes to the lakehouse instead of a DuckDB table in memory.
_dataframe.DataFrame.write = property(lambda self: LakehouseWriter(self))  # type: ignore[assignment]


def _alias_pyspark() -> None:
    """``import pyspark...`` resolves to DuckDB's API when real PySpark isn't installed."""
    if "pyspark" in sys.modules or importlib.util.find_spec("pyspark") is not None:
        return
    import duckdb.experimental.spark as spark_pkg
    import duckdb.experimental.spark.sql as spark_sql
    import duckdb.experimental.spark.sql.functions as functions
    import duckdb.experimental.spark.sql.types as types
    sys.modules.update({"pyspark": spark_pkg, "pyspark.sql": spark_sql,
                        "pyspark.sql.functions": functions, "pyspark.sql.types": types})
    # ``SparkSession.builder.getOrCreate()`` in a cell returns the notebook's session.
    spark_sql.SparkSession = LocalSparkSession  # type: ignore[attr-defined]
    LocalSparkSession.builder = _Builder()  # type: ignore[attr-defined]


class _Builder:
    def appName(self, *_a: Any) -> _Builder:  # noqa: N802
        return self

    def config(self, *_a: Any, **_kw: Any) -> _Builder:
        return self

    master = enableHiveSupport = appName

    def getOrCreate(self) -> LocalSparkSession:  # noqa: N802
        return session()


_session: LocalSparkSession | None = None


def session() -> LocalSparkSession:
    global _session
    if _session is None:
        _session = LocalSparkSession()
    return _session


def reset() -> None:
    """A fresh session, as after a kernel restart."""
    global _session
    if _session is not None:
        try:
            _session.conn.close()
        except Exception:  # noqa: BLE001
            pass
    _session = None


def install(namespace: dict[str, Any]) -> None:
    _alias_pyspark()
    namespace["spark"] = session()


def interrupt() -> None:
    """Stop a running DuckDB query, for Cancel."""
    for conn in ([_session.conn] if _session else []) + [duckdb.default_connection()]:
        try:
            conn.interrupt()
        except Exception:  # noqa: BLE001
            pass
