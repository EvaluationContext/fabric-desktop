"""SQL over the attached lakehouse's tables, run on this PC with DuckDB.

Queries are written the way Fabric's SQL query tab takes them, in Spark SQL.
sqlglot translates each statement to DuckDB SQL, and every table it names is
read from OneLake as a Delta dataset, so DuckDB fetches only the columns and
files the query needs. Queries never change the lakehouse; writing a result
is ``lakehouse.write_table``.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from . import lakehouse


def _parse(query: str) -> list[exp.Expression]:
    """Spark SQL first; DuckDB SQL is accepted too when it isn't valid Spark."""
    try:
        statements = sqlglot.parse(query, read="spark")
    except ParseError as spark_error:
        try:
            statements = sqlglot.parse(query, read="duckdb")
        except ParseError:
            raise ValueError(f"Could not read the query: {spark_error}") from None
    statements = [s for s in statements if s is not None]
    if not statements:
        raise ValueError("The query is empty.")
    return statements


def run(query: str) -> pa.Table:
    """Run every statement in ``query`` and return the last one's result."""
    con = duckdb.connect()
    views: dict[str, str] = {}
    result = None
    for statement in _parse(query):
        ctes = {c.alias_or_name.lower() for c in statement.find_all(exp.CTE)}
        for table in list(statement.find_all(exp.Table)):
            if not isinstance(table.this, exp.Identifier):
                continue  # a table function such as range(10)
            if not table.db and table.name.lower() in ctes:
                continue
            if table.catalog:
                raise ValueError(f"{table.sql('spark')}: name tables as table or schema.table "
                                 f"in the attached lakehouse.")
            name = f"{table.db}.{table.name}" if table.db else table.name
            key = name.lower()
            if key not in views:
                views[key] = f"__lakehouse_table_{len(views)}"
                con.register(views[key], lakehouse.open_table(name).to_pyarrow_dataset())
            # Keep the name the query used as the alias, so sales.Country still resolves.
            alias = table.args.get("alias") or exp.TableAlias(this=exp.to_identifier(table.name))
            table.replace(exp.Table(this=exp.to_identifier(views[key]), alias=alias))
        result = con.execute(statement.sql("duckdb"))
    if not result.description:
        return pa.table({})
    return (getattr(result, "to_arrow_table", None) or result.fetch_arrow_table)()
