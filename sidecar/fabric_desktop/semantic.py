"""A Direct Lake semantic model over one layer's tables, written as TMDL.

Each entity in the layer becomes a table that reads its Delta table through the
lakehouse's SQL analytics endpoint (Direct Lake, no import or refresh schedule).
Relationships come from the metadata: a fact's ``[[lookup]]`` names the dimension and
the key column, and any other column named like a dimension's surrogate key joins
to it. Surrogate keys and the framework's own columns (``_run_id``, ``_loaded_at``) are
hidden. Lineage tags are derived from names, so updating the model keeps them stable.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

_NS = uuid.UUID("2b0e1b9c-6a51-4f0e-9b4d-6b8f3f1d7c11")
_PLAIN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMERIC = {"int64", "double", "decimal"}

# Delta type names (as delta-rs reports them) to TMDL data types.
_TYPES = {"string": "string", "long": "int64", "integer": "int64", "short": "int64", "byte": "int64",
          "double": "double", "float": "double", "boolean": "boolean", "date": "dateTime",
          "timestamp": "dateTime", "timestamp_ntz": "dateTime", "binary": "binary"}


def tmdl_type(delta_type: str) -> str | None:
    t = delta_type.lower()
    if t.startswith("decimal"):
        return "decimal"
    return _TYPES.get(t)


def quote(name: str) -> str:
    return name if _PLAIN.match(name) else "'" + name.replace("'", "''") + "'"


def tag(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "/".join(parts)))


@dataclass
class ModelTable:
    name: str             # the table's name in the model
    entity: str           # metadata entity, e.g. gold.dim_customer
    schema: str           # schema in the lakehouse
    table: str            # Delta table name in that schema
    columns: list[tuple[str, str]] = field(default_factory=list)  # (name, Delta type)
    keys: list[str] = field(default_factory=list)
    surrogate_key: str | None = None
    is_dimension: bool = False


@dataclass
class Relationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    active: bool = True
    why: str = ""


def plan(project: Any, layer: str) -> tuple[list[ModelTable], list[Relationship]]:
    """The tables and relationships of a model over ``layer``; columns are filled in later."""
    entities = [e for e in project.entities.values() if e.layer == layer]
    tables: list[ModelTable] = []
    for e in sorted(entities, key=lambda x: x.name):
        clash = sum(1 for x in entities if x.table == e.table) > 1
        name = f"{e.schema}_{e.table}" if clash or not e.table else e.table
        tables.append(ModelTable(name=name, entity=e.name, schema=e.schema, table=e.table, keys=list(e.keys),
                                 surrogate_key=e.surrogate_key,
                                 is_dimension=e.mode in ("scd1", "scd2") and bool(e.surrogate_key)))
    by_entity = {t.entity: t for t in tables}
    rels: list[Relationship] = []
    for t in tables:
        e = project.entities[t.entity]
        for lk in e.spec.get("lookup", []):
            dim = by_entity.get(lk.get("dim"))
            if dim and dim.surrogate_key and lk.get("as"):
                rels.append(Relationship(t.name, lk["as"], dim.name, dim.surrogate_key,
                                         why=f"[[lookup]] of {dim.entity}"))
    for t in tables:
        cols = set(project.entities[t.entity].columns)
        for dim in tables:
            if dim is t or not dim.is_dimension or dim.surrogate_key not in cols:
                continue
            if any(r.from_table == t.name and r.from_column == dim.surrogate_key for r in rels):
                continue
            rels.append(Relationship(t.name, dim.surrogate_key, dim.name, dim.surrogate_key,
                                     why=f"same name as {dim.entity}'s surrogate key"))
    # Power BI allows one active relationship between two tables.
    seen: set[tuple[str, str]] = set()
    for r in rels:
        pair = (r.from_table, r.to_table)
        r.active = pair not in seen
        seen.add(pair)
    return tables, rels


def _hidden(t: ModelTable, col: str) -> bool:
    return col.startswith("_") or col.endswith("_sk") or col == t.surrogate_key


def _summarize(t: ModelTable, col: str, dtype: str) -> str:
    keyish = col in t.keys or col.endswith(("_sk", "_id", "_key")) or col == t.surrogate_key
    return "sum" if dtype in _NUMERIC and not keyish else "none"


def _table_tmdl(t: ModelTable, expression: str) -> str:
    lines = [f"table {quote(t.name)}", f"\tlineageTag: {tag('table', t.entity)}", ""]
    if not t.is_dimension:
        lines += [f"\tmeasure {quote(t.name + ' rows')} = COUNTROWS({quote(t.name)})",
                  "\t\tformatString: #,0", f"\t\tlineageTag: {tag('measure', t.entity)}", ""]
    for col, delta_type in t.columns:
        dtype = tmdl_type(delta_type)
        if dtype is None:
            continue  # e.g. arrays and structs, which Direct Lake can't read
        lines.append(f"\tcolumn {quote(col)}")
        lines.append(f"\t\tdataType: {dtype}")
        if _hidden(t, col):
            lines.append("\t\tisHidden")
        if dtype == "dateTime" and delta_type.lower() == "date":
            lines.append("\t\tformatString: Short Date")
        lines += [f"\t\tlineageTag: {tag('column', t.entity, col)}", f"\t\tsummarizeBy: {_summarize(t, col, dtype)}",
                  f"\t\tsourceColumn: {col}", ""]
    lines += [f"\tpartition {quote(t.name)} = entity", "\t\tmode: directLake", "\t\tsource",
              f"\t\t\tentityName: {t.table}", f"\t\t\tschemaName: {t.schema}", f"\t\t\texpressionSource: {expression}", ""]
    return "\n".join(lines)


def build(tables: list[ModelTable], rels: list[Relationship], sql_server: str, sql_endpoint_id: str) -> dict[str, str]:
    """TMDL definition parts, keyed by path, for Fabric's semantic model API."""
    expression = "DatabaseQuery"
    parts = {
        "definition.pbism": json.dumps({"version": "4.0", "settings": {}}, indent=2),
        "definition/database.tmdl": "database\n\tcompatibilityLevel: 1604\n",
        "definition/model.tmdl": "\n".join(
            ["model Model", "\tculture: en-US", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
             "\tsourceQueryCulture: en-US", ""]
            + [f"ref table {quote(t.name)}" for t in tables]) + "\n",
        "definition/expressions.tmdl": "\n".join([
            f"expression {expression} =", "\t\tlet",
            f'\t\t    database = Sql.Database("{sql_server}", "{sql_endpoint_id}")', "\t\tin", "\t\t    database",
            f"\tlineageTag: {tag('expression', sql_endpoint_id)}", ""]),
    }
    for t in tables:
        parts[f"definition/tables/{t.name}.tmdl"] = _table_tmdl(t, expression)
    if rels:
        lines = []
        for r in rels:
            lines += [f"relationship {tag('rel', r.from_table, r.from_column, r.to_table, r.to_column)}"]
            if not r.active:
                lines.append("\tisActive: false")
            lines += [f"\tfromColumn: {quote(r.from_table)}.{quote(r.from_column)}",
                      f"\ttoColumn: {quote(r.to_table)}.{quote(r.to_column)}", ""]
        parts["definition/relationships.tmdl"] = "\n".join(lines)
    return parts
