"""Centralized surrogate keys and the source systems they come from.

    metadata/keys/<name>.toml        one file per business key; its key table is sk.<name> in LH_Keys
    metadata/source_systems.toml     every source system, with its connection in each environment

Every key table has the same three columns: ``<name>_sk`` (INT), ``<name>_bk`` (the business key,
unique) and ``create_date``. Any source with business key ABC gets the same surrogate key, so
Customer ABC is 123 wherever it comes from. Entities name the key tables they use in ``[[keys]]``;
before anything else changes a row, a run MERGEs the new business keys into LH_Keys and puts the
surrogate key on every row. Template notebooks start with the same MERGE, written in Spark SQL.

LH_Keys lives in its own workspace per environment (``keys_workspace_id`` in environments.toml);
Dev keeps it on this PC. The ``config`` schema holds the source system tables, ``sk`` the key tables.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl

if TYPE_CHECKING:
    from .project import Entity, Project
    from .store import Store

KEYS_LAKEHOUSE = "LH_Keys"
KEY_SCHEMA = "sk"
CONFIG_SCHEMA = "config"
UNKNOWN_KEY = -1
UNKNOWN_BK = "UNKNOWN"
BK_SEPARATOR = "||"
SOURCE_SYSTEMS_FILE = "source_systems.toml"
ENVIRONMENTS = ("dev", "test", "prod")
SYSTEM_TYPES = ("SQL Server", "Azure SQL", "Oracle", "PostgreSQL", "MySQL", "Snowflake", "Dataverse",
                "SAP", "Salesforce", "REST API", "SFTP", "ADLS / Blob", "SharePoint", "Files", "Other")
SOURCE_FIELDS = ("source_system_id", "source_system_code", "source_system_name", "system_type", "owner",
                 "description", "is_active")
# The connection fields most sources need. Secrets never go here: only where Key Vault keeps them.
CONNECTION_FIELDS = ("connection_type", "server_host", "port", "database_name", "schema_name", "base_url",
                     "file_path", "auth_method", "tenant_id", "username", "key_vault_url", "secret_name",
                     "fabric_connection_id", "gateway_id", "extra_options")
_SECRET_FIELDS = ("password", "secret", "client_secret", "api_key", "token", "connection_string")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
MAX_ATTEMPTS = 5


def key_schema() -> dict[str, pl.DataType]:
    return {"sk": pl.Int32, "bk": pl.String, "create_date": pl.Datetime("us", "UTC")}


@dataclass
class KeyTable:
    name: str
    spec: dict
    path: Path

    @property
    def sk(self) -> str:
        return f"{self.name}_sk"

    @property
    def bk(self) -> str:
        return f"{self.name}_bk"

    @property
    def table(self) -> str:
        return self.name

    def schema(self) -> dict[str, pl.DataType]:
        s = key_schema()
        return {self.sk: s["sk"], self.bk: s["bk"], "create_date": s["create_date"]}

    def seed(self) -> pl.DataFrame:
        """A new key table: just the Unknown member, for rows that have no business key."""
        return pl.DataFrame({self.sk: [UNKNOWN_KEY], self.bk: [UNKNOWN_BK],
                             "create_date": [dt.datetime(1900, 1, 1, tzinfo=dt.timezone.utc)]},
                            schema=self.schema())

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "table": f"{KEY_SCHEMA}.{self.name}", "sk": self.sk, "bk": self.bk,
                "owner": self.spec.get("owner", ""), "description": self.spec.get("description", ""),
                "path": str(self.path),
                "columns": [{"name": self.sk, "type": "INT", "rule": "Primary key, issued once, never reused"},
                            {"name": self.bk, "type": "STRING", "rule": "Unique business key, trimmed"},
                            {"name": "create_date", "type": "TIMESTAMP", "rule": "When the key was issued"}]}


@dataclass
class SourceSystem:
    id: int
    code: str
    name: str = ""
    system_type: str = ""
    owner: str = ""
    description: str = ""
    active: bool = True
    connections: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "code": self.code, "name": self.name, "system_type": self.system_type,
                "owner": self.owner, "description": self.description, "active": self.active,
                "connections": {env: {f: self.connections.get(env, {}).get(f, "") for f in CONNECTION_FIELDS}
                                for env in ENVIRONMENTS}}


# --------------------------------------------------------------------------- loading


def keys_dir(root: Path) -> Path:
    return Path(root) / "metadata" / "keys"


def load_key_tables(root: Path) -> tuple[dict[str, KeyTable], list[str]]:
    tables: dict[str, KeyTable] = {}
    errors: list[str] = []
    for p in sorted(keys_dir(root).glob("*.toml")):
        if p.name == SOURCE_SYSTEMS_FILE:
            continue
        try:
            spec = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            errors.append(f"keys/{p.name}: not valid TOML ({exc})")
            continue
        name = spec.get("name") or p.stem
        if p.stem != name:
            errors.append(f"keys/{p.name}: file name must match its name ({name})")
        tables[name] = KeyTable(name, spec, p)
    return tables, errors


def load_source_systems(root: Path) -> tuple[list[SourceSystem], list[str]]:
    path = keys_dir(root) / SOURCE_SYSTEMS_FILE
    if not path.exists():
        return [], []
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [], [f"keys/{SOURCE_SYSTEMS_FILE}: not valid TOML ({exc})"]
    out, errors = [], []
    for s in cfg.get("source_system", []):
        try:
            out.append(SourceSystem(id=int(s.get("id", 0)), code=str(s.get("code", "")), name=s.get("name", ""),
                                    system_type=s.get("system_type", ""), owner=s.get("owner", ""),
                                    description=s.get("description", ""), active=bool(s.get("active", True)),
                                    connections={env: dict(c) for env, c in s.get("connection", {}).items()}))
        except (TypeError, ValueError):
            errors.append(f"keys/{SOURCE_SYSTEMS_FILE}: source system {s.get('code', '?')} needs a whole-number id")
    return out, errors


def entity_keys(e: Entity) -> list[dict[str, Any]]:
    """The entity's [[keys]] blocks, each with the column it adds and its business key columns."""
    out = []
    for k in e.spec.get("keys", []) if isinstance(e.spec.get("keys"), list) else []:
        table = str(k.get("table", ""))
        bk = k.get("business_key", [])
        out.append({"table": table, "column": f"{table}_sk", "business_key": bk if isinstance(bk, list) else [bk]})
    return out


def validate_keys(project: Project, today: dt.date | None = None) -> list[str]:
    """Key tables, source systems and every entity's [[keys]] (part of the save gate)."""
    from .project import source_columns

    errors = list(project.key_errors)
    for name in project.key_tables:
        if not _NAME.match(name):
            errors.append(f"keys/{name}: name must be lowercase letters, digits or _ and start with a letter")
    ids, codes = set(), set()
    for s in project.source_systems:
        if s.id <= 0:
            errors.append(f"source system {s.code or '?'}: id must be a whole number above 0")
        if s.id in ids:
            errors.append(f"source system {s.code}: id {s.id} is used twice")
        if not _CODE.match(s.code):
            errors.append(f"source system {s.code or '?'}: code must be letters, digits or _ and start with a letter")
        if s.code.upper() in codes:
            errors.append(f"source system {s.code}: code is used twice")
        ids.add(s.id)
        codes.add(s.code.upper())
        for env, conn in s.connections.items():
            if env not in ENVIRONMENTS:
                errors.append(f"source system {s.code}: unknown environment {env}")
            for f in conn:
                if f.lower() in _SECRET_FIELDS:
                    errors.append(f"source system {s.code}: {f} looks like a secret; store it in Key Vault and "
                                  f"put only secret_name here")
    for e in project.entities.values():
        blocks = entity_keys(e)
        if "keys" in e.spec and not isinstance(e.spec["keys"], list):
            errors.append(f"{e.name}: keys must be [[keys]] blocks")
        layer = next((l for l in project.layers if l.name == e.layer), None)
        if layer is not None and layer.require_keys and not blocks:
            if not project.active_exception(e.name, "surrogate_keys", today or dt.date.today()):
                errors.append(f"{e.name}: layer {e.layer} requires surrogate keys; add a [[keys]] block")
        if not blocks:
            continue
        if "entity" not in e.spec.get("source", {}):
            errors.append(f"{e.name}: [[keys]] needs a source entity to read business keys from")
            continue
        available = source_columns(project, e)
        seen = set()
        for b in blocks:
            if b["table"] not in project.key_tables:
                errors.append(f"{e.name}: key table {b['table'] or '?'} is not defined in Metadata > Surrogate keys")
            if b["column"] in seen:
                errors.append(f"{e.name}: key table {b['table']} is listed twice")
            seen.add(b["column"])
            if not b["business_key"]:
                errors.append(f"{e.name}: key table {b['table']} needs a business_key column")
            for c in b["business_key"]:
                if c not in available:
                    errors.append(f"{e.name}: business key {c} is not in the source")
            declared = e.spec.get("contract", {}).get("columns", {}).get(b["column"])
            if declared and declared.strip().lower() not in ("int", "int32"):
                errors.append(f"{e.name}: {b['column']} is a surrogate key, so its type must be int")
    return errors


# --------------------------------------------------------------------------- matching


def business_key(columns: list[str]) -> pl.Expr:
    """The business key as text: each part trimmed, parts joined with ||, null when any part is missing."""
    parts = [pl.col(c).cast(pl.String).str.strip_chars().replace("", None) for c in columns]
    return parts[0] if len(parts) == 1 else pl.concat_str(parts, separator=BK_SEPARATOR)


def read_keys(store: Store, kt: KeyTable) -> pl.DataFrame:
    df = store.read(KEYS_LAKEHOUSE, KEY_SCHEMA, kt.table)
    return kt.seed().clear() if df is None else df


def ensure_key_table(store: Store, kt: KeyTable) -> bool:
    """Create the key table with its Unknown member if it doesn't exist. True if it was created."""
    if store.read(KEYS_LAKEHOUSE, KEY_SCHEMA, kt.table) is not None:
        return False
    store.write(KEYS_LAKEHOUSE, KEY_SCHEMA, kt.table, kt.seed(), mode="overwrite")
    return True


def merge_keys(store: Store, kt: KeyTable, bks: pl.Series) -> dict[str, Any]:
    """Insert keys for business keys the key table doesn't have yet, continuing from the highest key.

    The insert is a Delta MERGE on the business key, so a business key never gets two keys. If
    another run commits first, the MERGE fails and is retried against the new table.
    """
    from deltalake.exceptions import CommitFailedError

    wanted = pl.DataFrame({kt.bk: bks}).filter(pl.col(kt.bk).is_not_null()).unique()
    ensure_key_table(store, kt)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        existing = read_keys(store, kt)
        new = wanted.join(existing.select(kt.bk), on=kt.bk, how="anti").sort(kt.bk)
        if new.height == 0:
            return {"inserted": 0, "existing": wanted.height, "first": None, "last": None, "attempts": attempt}
        start = max(int(existing[kt.sk].max() or 0), 0) + 1
        rows = new.with_columns(pl.int_range(start, start + new.height, dtype=pl.Int32).alias(kt.sk),
                                pl.lit(dt.datetime.now(dt.timezone.utc)).cast(pl.Datetime("us", "UTC"))
                                .alias("create_date")).select(list(kt.schema()))
        try:
            store.merge_insert(KEYS_LAKEHOUSE, KEY_SCHEMA, kt.table, rows, on=[kt.bk])
        except CommitFailedError:
            time.sleep(0.2 * attempt)
            continue
        after = read_keys(store, kt)
        if after[kt.sk].is_duplicated().any():
            raise RuntimeError(f"sk.{kt.table}: two runs issued the same {kt.sk} at once. Run this table again; "
                               f"the duplicate keys need fixing in LH_Keys first.")
        return {"inserted": new.height, "existing": wanted.height - new.height, "first": start,
                "last": start + new.height - 1, "attempts": attempt}
    raise RuntimeError(f"sk.{kt.table}: other runs kept adding keys at the same time; try again")


def assign(store: Store, project: Project, e: Entity, df: pl.DataFrame) -> tuple[pl.DataFrame, list[str]]:
    """Cell 1 of every template: MERGE new business keys, then put each surrogate key on every row."""
    details = []
    for b in entity_keys(e):
        kt = project.key_tables[b["table"]]
        bk = business_key(b["business_key"])
        df = df.with_columns(bk.alias("__bk"))
        stats = merge_keys(store, kt, df["__bk"])
        keys = read_keys(store, kt).filter(pl.col(kt.sk) != UNKNOWN_KEY).select(
            pl.col(kt.bk).alias("__bk"), pl.col(kt.sk).alias("__sk"))
        df = (df.join(keys, on="__bk", how="left", maintain_order="left")
              .with_columns(pl.col("__sk").fill_null(UNKNOWN_KEY).cast(pl.Int32).alias(b["column"]))
              .drop("__bk", "__sk"))
        unknown = int((df[b["column"]] == UNKNOWN_KEY).sum())
        new = (f"{stats['inserted']} new ({stats['first']} to {stats['last']})" if stats["inserted"]
               else "0 new")
        details.append(f"{b['column']}: {new}, {stats['existing']} existing, {unknown} without a business key (-1)")
    return df, details


def lookup(store: Store, kt: KeyTable, value: str) -> dict[str, Any] | None:
    df = read_keys(store, kt)
    hit = df.filter(pl.col(kt.bk) == value.strip())
    if not hit.height:
        return None
    row = hit.row(0, named=True)
    return {"sk": row[kt.sk], "bk": row[kt.bk], "create_date": str(row["create_date"])}


def key_stats(store: Store, kt: KeyTable) -> dict[str, Any]:
    df = store.read(KEYS_LAKEHOUSE, KEY_SCHEMA, kt.table)
    if df is None:
        return {"created": False, "keys": 0, "next": 1}
    real = df.filter(pl.col(kt.sk) != UNKNOWN_KEY)
    return {"created": True, "keys": real.height, "next": max(int(real[kt.sk].max() or 0), 0) + 1}


# --------------------------------------------------------------------------- template notebook


def _spark_name(workspace: str, lakehouse: str, schema: str, table: str) -> str:
    # Backticks: key table names such as order are reserved words in Spark SQL.
    prefix = f"`{workspace}`." if workspace else ""
    return f"{prefix}{lakehouse}.{schema}.`{table}`"


def _spark_bk(columns: list[str], alias: str = "") -> str:
    a = f"{alias}." if alias else ""
    parts = [f"NULLIF(TRIM(CAST({a}{c} AS STRING)), '')" for c in columns]
    return parts[0] if len(parts) == 1 else f"CONCAT_WS('{BK_SEPARATOR}', {', '.join(parts)})"


def merge_sql(project: Project, e: Entity, keys_workspace: str = "") -> str:
    """The Spark SQL that starts every template notebook for this entity."""
    src = project.entities[e.spec["source"]["entity"]]
    source = f"{project.layer(src.layer).lakehouse}.{src.schema}.{src.table}"
    out = [f"-- Surrogate keys for {e.name}. Generated from metadata/entities/{e.name}.toml; change it there.",
           ""]
    joins, cols = [], []
    for i, b in enumerate(entity_keys(e), start=1):
        kt = project.key_tables[b["table"]]
        target = _spark_name(keys_workspace, KEYS_LAKEHOUSE, KEY_SCHEMA, kt.table)
        bk_cols = b["business_key"]
        not_null = " AND ".join(f"{c} IS NOT NULL" for c in bk_cols)
        out += [
            f"-- {i}. New business keys get the next {kt.sk}. Existing ones keep theirs.",
            f"MERGE INTO {target} AS k",
            "USING (",
            f"    SELECT n.{kt.bk},",
            f"           CAST(m.max_sk + ROW_NUMBER() OVER (ORDER BY n.{kt.bk}) AS INT) AS {kt.sk}",
            f"    FROM (SELECT DISTINCT {_spark_bk(bk_cols)} AS {kt.bk}",
            f"          FROM {source}",
            f"          WHERE {not_null}) n",
            f"    LEFT ANTI JOIN {target} e ON e.{kt.bk} = n.{kt.bk}",
            f"    CROSS JOIN (SELECT GREATEST(COALESCE(MAX({kt.sk}), 0), 0) AS max_sk FROM {target}) m",
            f"    WHERE n.{kt.bk} IS NOT NULL",
            ") AS s",
            f"ON k.{kt.bk} = s.{kt.bk}",
            "WHEN NOT MATCHED THEN",
            f"    INSERT ({kt.sk}, {kt.bk}, create_date)",
            f"    VALUES (s.{kt.sk}, s.{kt.bk}, current_timestamp());",
            ""]
        alias = f"k{i}"
        joins.append(f"LEFT JOIN {target} {alias} ON {alias}.{kt.bk} = {_spark_bk(bk_cols, 'b')}")
        cols.append(f"COALESCE({alias}.{kt.sk}, {UNKNOWN_KEY}) AS {b['column']}")
    out += ["-- Put the surrogate keys on every source row. Rows without a business key get -1.",
            "CREATE OR REPLACE TEMP VIEW src_keyed AS",
            f"SELECT {', '.join(cols)}, b.*",
            f"FROM {source} b",
            *joins]
    return "\n".join(out) + ";\n"


def template_cells(project: Project, e: Entity, keys_workspace: str = "") -> list[dict[str, str]]:
    src = e.spec["source"]["entity"]
    return [
        {"type": "code", "source": "%%sql\n" + merge_sql(project, e, keys_workspace)},
        {"type": "code", "source": (
            f"# Every row of {src} now has its surrogate keys, in the view src_keyed.\n"
            f"# Clean, check and write {e.name} from here.\n"
            "df = spark.table(\"src_keyed\")\n"
            "display(df.limit(10))\n")},
    ]


# --------------------------------------------------------------------------- files


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return json.dumps("" if v is None else str(v))


def write_key_table(root: Path, name: str, owner: str = "", description: str = "") -> Path:
    if not _NAME.match(name):
        raise ValueError("A key table name must be lowercase letters, digits or _ and start with a letter, "
                         "like customer")
    path = keys_dir(root) / f"{name}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([
        f"# Key table sk.{name} in LH_Keys: {name}_sk (INT), {name}_bk (unique) and create_date.",
        f"name = {_toml_value(name)}",
        f"owner = {_toml_value(owner)}",
        f"description = {_toml_value(description)}",
        ""]), encoding="utf-8")
    return path


def write_source_systems(root: Path, systems: list[SourceSystem]) -> Path:
    lines = ["# Source systems. The id is used everywhere, so it never changes; connections are per environment.",
             "# Never put passwords or keys here: give the Key Vault URL and the secret's name.", ""]
    for s in sorted(systems, key=lambda s: s.id):
        lines += ["[[source_system]]", f"id = {s.id}", f"code = {_toml_value(s.code)}",
                  f"name = {_toml_value(s.name)}", f"system_type = {_toml_value(s.system_type)}",
                  f"owner = {_toml_value(s.owner)}", f"description = {_toml_value(s.description)}",
                  f"active = {_toml_value(s.active)}", ""]
        for env in ENVIRONMENTS:
            conn = {f: v for f, v in s.connections.get(env, {}).items() if f in CONNECTION_FIELDS and v not in ("", None)}
            if conn:
                lines.append(f"[source_system.connection.{env}]")
                lines += [f"{f} = {_toml_value(v)}" for f, v in conn.items()]
                lines.append("")
    path = keys_dir(root) / SOURCE_SYSTEMS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def source_system_from_dict(d: dict[str, Any]) -> SourceSystem:
    try:
        sid = int(d.get("id") or 0)
    except (TypeError, ValueError):
        raise ValueError("The source system id must be a whole number") from None
    conns = {env: {f: ("" if v is None else str(v)).strip() for f, v in (d.get("connections", {}).get(env) or {}).items()
                   if f in CONNECTION_FIELDS}
             for env in ENVIRONMENTS}
    return SourceSystem(id=sid, code=str(d.get("code", "")).strip(), name=str(d.get("name", "")).strip(),
                        system_type=str(d.get("system_type", "")), owner=str(d.get("owner", "")).strip(),
                        description=str(d.get("description", "")).strip(), active=bool(d.get("active", True)),
                        connections=conns)


# --------------------------------------------------------------------------- LH_Keys


def config_tables(project: Project) -> dict[str, pl.DataFrame]:
    now = dt.datetime.now(dt.timezone.utc)
    systems = pl.DataFrame(
        [{"source_system_id": s.id, "source_system_code": s.code, "source_system_name": s.name,
          "system_type": s.system_type, "owner": s.owner, "description": s.description, "is_active": s.active,
          "_metadata_version": project.version, "_loaded_at": now} for s in project.source_systems],
        schema={"source_system_id": pl.Int32, "source_system_code": pl.String, "source_system_name": pl.String,
                "system_type": pl.String, "owner": pl.String, "description": pl.String, "is_active": pl.Boolean,
                "_metadata_version": pl.String, "_loaded_at": pl.Datetime("us", "UTC")})
    conn_schema: dict[str, pl.DataType] = {"source_system_id": pl.Int32, "environment": pl.String}
    conn_schema |= {f: (pl.Int32 if f == "port" else pl.String) for f in CONNECTION_FIELDS}
    conn_schema["_loaded_at"] = pl.Datetime("us", "UTC")
    rows = []
    for s in project.source_systems:
        for env in ENVIRONMENTS:
            c = s.connections.get(env)
            if not c:
                continue
            row: dict[str, Any] = {"source_system_id": s.id, "environment": env, "_loaded_at": now}
            for f in CONNECTION_FIELDS:
                v = c.get(f)
                if f == "port":
                    try:
                        v = int(v) if v not in ("", None) else None
                    except ValueError:
                        v = None
                row[f] = v if v != "" else None
            rows.append(row)
    return {"source_system": systems, "source_connection": pl.DataFrame(rows, schema=conn_schema)}


def sync(project: Project, store: Store) -> dict[str, Any]:
    """Write the source system tables and create any missing key table (with its -1 row) in LH_Keys."""
    written = {}
    for table, df in config_tables(project).items():
        store.write(KEYS_LAKEHOUSE, CONFIG_SCHEMA, table, df, mode="overwrite",
                    metadata={"fabric_desktop_metadata": project.version})
        written[table] = df.height
    created = [kt.name for kt in project.key_tables.values() if ensure_key_table(store, kt)]
    return {"env": store.env, "config": written, "created": created, "version": project.version}
