"""Running notebooks on Fabric capacity: the published notebook, Fabric's job scheduler, schedules.

A notebook written here imports ``fabric_desktop.lakehouse``, which only exists on this PC.
When such a notebook is published, a first cell is added that defines the same module with
the libraries a Fabric Python notebook already has (Polars, deltalake, DuckDB), so the
notebook runs unchanged in Fabric. ``%%sql`` cells become ``lakehouse.sql`` calls there.
Notebooks that don't use the module (such as the surrogate key templates, which MERGE with
Spark SQL) are published as they are, for a Spark notebook.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from .notebook import to_ipynb

SHIM_MARKER = "# Fabric Desktop setup"
TERMINAL = {"Completed", "Failed", "Cancelled", "Deduped"}

# A Python (not Spark) notebook in Fabric.
PYTHON_NOTEBOOK_METADATA = {
    "kernel_info": {"name": "jupyter", "jupyter_kernel_name": "python3.11"},
    "kernelspec": {"name": "jupyter", "display_name": "Jupyter"},
    "language_info": {"name": "python"},
    "microsoft": {"language": "python", "language_group": "jupyter_python"},
}

_SHIM = '''# Fabric Desktop setup: lets `from fabric_desktop import lakehouse` run in Fabric.
# Added when the notebook is published from Fabric Desktop; it isn't in the local copy.
import os as _os, sys as _sys, types as _types
import polars as _pl
from deltalake import DeltaTable as _DeltaTable, write_deltalake as _write_deltalake
import notebookutils as _nbu

def _fabric_desktop_module(workspace_id, lakehouse_id, name, schemas):
    root = f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}"
    mount = "/lakehouse/default"

    def opts():
        return {"bearer_token": _nbu.credentials.getToken("storage"), "use_fabric_endpoint": "true"}

    def path(p):
        p = str(p).replace("\\\\", "/")
        return f"{mount}/{p}" if p == "Files" or p.startswith("Files/") else p

    def uri(table):
        schema, _, tbl = table.rpartition(".")
        return f"{root}/Tables/{schema or 'dbo'}/{tbl}" if schemas else f"{root}/Tables/{tbl}"

    lh = _types.ModuleType("fabric_desktop.lakehouse")
    lh.name = lambda: name
    lh.read_csv = lambda p, **kw: _pl.read_csv(path(p), **kw)
    lh.read_parquet = lambda p, **kw: _pl.read_parquet(path(p), **kw)
    lh.read_json = lambda p, **kw: _pl.read_json(path(p), **kw)
    lh.open_table = lambda table: _DeltaTable(uri(table), storage_options=opts())
    lh.read_table = lambda table: _pl.from_arrow(lh.open_table(table).to_pyarrow_table())
    lh.files = lambda directory="Files": sorted(f"{directory.rstrip('/')}/{n}" for n in _os.listdir(path(directory)))

    def write_table(data, table, mode="overwrite"):
        if isinstance(data, _pl.LazyFrame):
            data = data.collect()
        arrow = data.to_arrow() if isinstance(data, _pl.DataFrame) else data
        _write_deltalake(uri(table), arrow, mode=mode, storage_options=opts(),
                         schema_mode="overwrite" if mode == "overwrite" else None)
        version = lh.open_table(table).version()
        print(f"Wrote {arrow.num_rows} rows to {table} (Delta version {version})")
        return version
    lh.write_table = write_table

    def sql(query):
        import duckdb
        try:
            import sqlglot
            from sqlglot import exp
        except ImportError:
            import subprocess
            subprocess.check_call([_sys.executable, "-m", "pip", "install", "-q", "sqlglot"])
            import sqlglot
            from sqlglot import exp
        con, views, result = duckdb.connect(), {}, None
        for st in [s for s in sqlglot.parse(query, read="spark") if s is not None]:
            ctes = {c.alias_or_name.lower() for c in st.find_all(exp.CTE)}
            for t in list(st.find_all(exp.Table)):
                if not isinstance(t.this, exp.Identifier) or (not t.db and t.name.lower() in ctes):
                    continue
                key = (f"{t.db}.{t.name}" if t.db else t.name).lower()
                if key not in views:
                    views[key] = f"__t{len(views)}"
                    con.register(views[key], lh.open_table(key).to_pyarrow_dataset())
                alias = t.args.get("alias") or exp.TableAlias(this=exp.to_identifier(t.name))
                t.replace(exp.Table(this=exp.to_identifier(views[key]), alias=alias))
            result = con.execute(st.sql("duckdb"))
        if result is None or not result.description:
            return _pl.DataFrame()
        return _pl.from_arrow((getattr(result, "to_arrow_table", None) or result.fetch_arrow_table)())
    lh.sql = sql

    pkg = _types.ModuleType("fabric_desktop")
    pkg.lakehouse = lh
    _sys.modules["fabric_desktop"], _sys.modules["fabric_desktop.lakehouse"] = pkg, lh

_fabric_desktop_module(%(workspace_id)r, %(lakehouse_id)r, %(name)r, %(schemas)r)
'''

_USES_MODULE = re.compile(r"\b(from\s+fabric_desktop\b|import\s+fabric_desktop\b)")


def uses_local_module(cells: list[dict[str, Any]]) -> bool:
    return any(c.get("type") != "markdown" and _USES_MODULE.search(c.get("source", "")) for c in cells)


def shim_cell(lakehouse: dict[str, Any], schemas_enabled: bool = True) -> dict[str, Any]:
    return {"type": "code", "source": SHIM_MARKER + "\n" + _SHIM % {
        "workspace_id": lakehouse["workspace_id"], "lakehouse_id": lakehouse["lakehouse_id"],
        "name": lakehouse.get("name", ""), "schemas": bool(schemas_enabled)}}


def _sql_cell(source: str) -> dict[str, Any]:
    query = source.lstrip()[len("%%sql"):].strip()
    return {"type": "code", "source": f"lakehouse.sql({json.dumps(query)})"}


_USES_SPARK = re.compile(r"(\bspark\s*\.|\b(from|import)\s+pyspark\b|^\s*%%pyspark\b|\bSparkSession\b)", re.M)


def uses_spark(cells: list[dict[str, Any]]) -> bool:
    return any(c.get("type") != "markdown" and _USES_SPARK.search(c.get("source", "")) for c in cells)


def fabric_ipynb(cells: list[dict[str, Any]], lakehouse: dict[str, Any] | None,
                 schemas_enabled: bool = True) -> dict[str, Any]:
    """The notebook as it is published to Fabric.

    A notebook that uses Spark is published as a Spark notebook, where ``spark`` and ``%%sql``
    are Fabric's own; one that uses only the lakehouse module becomes a Python notebook."""
    if not lakehouse or not uses_local_module(cells):
        return to_ipynb(cells, lakehouse)
    if uses_spark(cells):
        return to_ipynb([shim_cell(lakehouse, schemas_enabled)] + cells, lakehouse)
    body = [_sql_cell(c["source"]) if c.get("type") != "markdown" and c.get("source", "").lstrip().startswith("%%sql")
            else c for c in cells]
    nb = to_ipynb([shim_cell(lakehouse, schemas_enabled)] + body, lakehouse)
    nb["metadata"] = PYTHON_NOTEBOOK_METADATA | {k: v for k, v in nb["metadata"].items()
                                                 if k not in ("kernelspec", "language_info")}
    return nb


# ---- where a notebook can run

# Code that needs Fabric's Spark runtime or its notebook services. On this PC, ``spark`` is
# DuckDB's PySpark-compatible API and ``notebookutils`` covers fs, notebook, credentials and
# runtime, so most PySpark notebooks run here; these parts don't.
_FABRIC_ONLY = [
    (re.compile(r"^\s*%%(spark|sparkr|csharp)\b", re.M), "uses a Scala, R or .NET cell"),
    (re.compile(r"\b(sparkContext|\.rdd\b|pandas_udf|spark\.udf|\.mapInPandas|\.applyInPandas)"),
     "uses a Spark feature this PC's Spark doesn't have"),
    (re.compile(r"\b(spark\.readStream|\.writeStream)\b"), "uses Spark streaming"),
    (re.compile(r"\b(notebookutils|mssparkutils)\.(fs\.(mount|unmount)|session|lakehouse|warehouse|"
                r"variableLibrary|udf|connections)\b"), "uses Fabric notebook utilities that need Fabric"),
]
_SQL_WRITE = re.compile(r"\b(MERGE\s+INTO|UPDATE\s+\S+\s+SET|DELETE\s+FROM)\b", re.I)
# A path on this PC's disk, like C:\data or D:/exports. Fabric can't reach it.
_LOCAL_PATH = re.compile(r"""["'][A-Za-z]:[\\/]""")


def _snippet(source: str, match: re.Match[str] | None = None) -> str:
    """The line that matched (or the first line), short enough to quote."""
    start = source.rfind("\n", 0, match.start()) + 1 if match else 0
    line = next((ln.strip() for ln in source[start:].splitlines() if ln.strip()), "")
    return line if len(line) <= 60 else line[:57] + "..."


def compute_check(cells: list[dict[str, Any]], lakehouse: dict[str, Any] | None) -> dict[str, Any]:
    """Whether a notebook can run on this PC and in Fabric, and if not, which cells stop it.

    Each side is {"ok": bool, "reasons": [{"cell": n, "text": ..., "code": first line}]}, cells numbered from 1
    among all cells, as the notebook shows them."""
    local: list[dict[str, Any]] = []
    fabric: list[dict[str, Any]] = []
    for n, c in enumerate(cells, 1):
        source = c.get("source", "")
        if c.get("type") == "markdown" or not source.strip():
            continue
        if source.lstrip().startswith("%%sql"):
            if _SQL_WRITE.search(source):
                local.append({"cell": n, "text": "changes table rows in place (MERGE, UPDATE or DELETE), which runs in Fabric only",
                              "code": _snippet(source)})
        else:
            hit = next(((m, text) for rx, text in _FABRIC_ONLY if (m := rx.search(source))), None)
            if hit:
                local.append({"cell": n, "text": hit[1], "code": _snippet(source, hit[0])})
        if m := _LOCAL_PATH.search(source):
            fabric.append({"cell": n, "text": "reads or writes a path on this PC, which Fabric can't reach",
                           "code": _snippet(source, m)})
    if not lakehouse:
        fabric.insert(0, {"cell": None, "text": "has no lakehouse attached. A Fabric run reads and writes "
                                                "through the attached lakehouse.", "code": ""})
    return {"local": {"ok": not local, "reasons": local}, "fabric": {"ok": not fabric, "reasons": fabric}}


# ---- schedules

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def schedule_configuration(kind: str, times: list[str] | None = None, every_minutes: int | None = None,
                           weekdays: list[str] | None = None, time_zone: str = "UTC",
                           start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Fabric's schedule configuration for "every N minutes", "daily at" or "weekly on ... at"."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    start = start or now.strftime("%Y-%m-%dT%H:%M:%S")
    end = end or (now + dt.timedelta(days=3650)).strftime("%Y-%m-%dT%H:%M:%S")
    config: dict[str, Any] = {"startDateTime": start, "endDateTime": end, "localTimeZoneId": time_zone}
    times = [t.strip() for t in (times or []) if t.strip()]
    for t in times:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
            raise ValueError(f"Times are HH:MM in 24-hour form, like 06:00 (got {t!r})")
    if kind == "cron":
        if not every_minutes or not 1 <= int(every_minutes) <= 5270400:
            raise ValueError("Pick how many minutes between runs")
        return config | {"type": "Cron", "interval": int(every_minutes)}
    if kind == "daily":
        if not times:
            raise ValueError("Pick at least one time of day")
        return config | {"type": "Daily", "times": times}
    if kind == "weekly":
        days = [d for d in (weekdays or []) if d in WEEKDAYS]
        if not days or not times:
            raise ValueError("Pick at least one day and one time")
        return config | {"type": "Weekly", "weekdays": days, "times": times}
    raise ValueError(f"unknown schedule kind {kind!r}")


def describe_schedule(s: dict[str, Any]) -> str:
    c = s.get("configuration", {})
    tz = c.get("localTimeZoneId", "")
    if c.get("type") == "Cron":
        return f"Every {c.get('interval')} minutes"
    if c.get("type") == "Daily":
        return f"Daily at {', '.join(c.get('times', []))} {tz}".strip()
    if c.get("type") == "Weekly":
        return f"{', '.join(d[:3] for d in c.get('weekdays', []))} at {', '.join(c.get('times', []))} {tz}".strip()
    return c.get("type", "Schedule")
