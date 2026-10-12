"""New projects and new entity files, written from templates that already meet the standards."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import polars as pl

from .project import Layer, Project, write_layers_file

PRESETS: dict[str, list[Layer]] = {
    "2": [Layer("raw", "evolve", [], description="Data as it arrived"),
          Layer("curated", "ignore_new", ["key_not_null", "key_unique"], description="Clean, modeled, ready to report")],
    "3": [Layer("bronze", "evolve", [], description="Land raw data as is"),
          Layer("silver", "ignore_new", ["key_not_null", "key_unique"], description="Clean, conform, deduplicate"),
          Layer("gold", "strict", ["key_unique"], description="Model for reporting")],
    "5": [Layer("landing", "evolve", [], description="Files as delivered"),
          Layer("bronze", "evolve", [], description="Raw data as Delta tables"),
          Layer("silver", "ignore_new", ["key_not_null", "key_unique"], description="Clean, conform, deduplicate"),
          Layer("gold", "strict", ["key_unique"], description="Model for reporting"),
          Layer("semantic", "strict", ["key_unique"], description="Shaped for one report or model")],
}

TEMPLATES = {
    "files": "Ingest files (CSV, Parquet or JSON) as they arrived",
    "clean": "Clean, deduplicate and merge changed rows from another table",
    "dim1": "Dimension, Type 1: overwrite changes, stable surrogate keys",
    "dim2": "Dimension, Type 2: keep history with valid from and to dates",
    "fact": "Fact table: look up dimension keys, unknowns go to -1",
    "custom": "Custom Python step: you write the transform, the framework checks and writes it",
}

README = """\
# Fabric Desktop metadata project

Every managed table is one file in `metadata/entities/`. The framework runs each one
through the same steps, checks it, and writes it to its layer's lakehouse.

| Folder or file | What goes there |
| --- | --- |
| `layers.toml` | Your layers, in order. Each one is its own lakehouse, `LH_<Layer>`. |
| `metadata/entities/` | One TOML file per table: source, contract, cleaning, checks, load mode. |
| `transforms/` | Your Python: hooks, custom checks and custom steps. Shared helpers go in `common.py`. |
| `tests/` | Tests for your Python. Every hook and custom step must point at one. |
| `exceptions.toml` | Approved exceptions to a rule, each with a reason, an approver and an expiry. |
| `environments.toml` | Where Dev, Test and Prod write. Dev writes to this PC by default. |
| `.local/` | Dev's local lakehouses. Not committed. |

Where custom logic goes, simplest first:

1. **Expression**: `[[derive]] expr = "amount - coalesce(discount, 0)"` or `[[check]] expr = "..."`.
2. **Hook**: `[hooks] after_clean = "transforms.orders:allocate_freight"`, a function `(df, ctx)` that
   returns rows. Declare the tables it reads in `reads` and the columns it adds in `adds`.
3. **Custom check**: `[[check]] python = "transforms.checks:valid_email"`, returns true per good row.
4. **Custom step**: `[step] custom = "transforms.sales:build"` replaces the transform for one table.

Commit this folder to Git and review changes with pull requests.
"""

SAMPLE_ENTITIES = {
    "bronze.customers": """\
        name = "bronze.customers"
        owner = "{owner}"
        description = "Customers as delivered by the CRM export"

        [source]
        files = "landing/customers/*.csv"

        [load]
        mode = "full"

        [contract]
        keys = ["customer_id"]

        [contract.columns]
        customer_id = "int64"
        name = "string"
        segment = "string"
        city = "string"
        email = "string"
        updated_at = "date"
        """,
    "bronze.orders": """\
        name = "bronze.orders"
        owner = "{owner}"
        description = "Orders as delivered by the shop export"

        [source]
        files = "landing/orders/*.csv"

        [load]
        mode = "full"

        [contract]
        keys = ["order_id"]

        [contract.columns]
        order_id = "int64"
        customer_id = "int64"
        order_date = "date"
        amount = "float64"
        discount = "float64"
        status = "string"
        modified_at = "date"
        """,
    "silver.customers": """\
        name = "silver.customers"
        owner = "{owner}"

        [source]
        entity = "bronze.customers"

        [load]
        mode = "incremental"
        watermark = "updated_at"

        [contract]
        keys = ["customer_id"]

        [contract.columns]
        customer_id = "int64"
        name = "string"
        segment = "string"
        city = "string"
        email = "string"
        updated_at = "date"

        [[clean]]
        column = "name"
        trim = true

        [[clean]]
        column = "email"
        trim = true
        case = "lower"

        [[check]]
        column = "customer_id"
        not_null = true
        unique = true
        on_fail = "stop"
        """,
    "silver.orders": """\
        name = "silver.orders"
        owner = "{owner}"

        [source]
        entity = "bronze.orders"

        [load]
        mode = "incremental"
        watermark = "modified_at"

        [contract]
        keys = ["order_id"]

        [contract.columns]
        order_id = "int64"
        customer_id = "int64"
        order_date = "date"
        amount = "float64"
        discount = "float64"
        status = "string"
        modified_at = "date"
        net_amount = "float64"

        [[clean]]
        column = "status"
        trim = true
        case = "lower"

        [[clean]]
        dedupe_keep = "newest:modified_at"

        [[derive]]
        column = "net_amount"
        expr = "amount - coalesce(discount, 0)"

        [[check]]
        column = "order_id"
        not_null = true
        unique = true
        on_fail = "stop"

        # Unknown customers are kept and flagged; Gold sends them to the Unknown member (-1).
        [[check]]
        column = "customer_id"
        exists_in = "silver.customers.customer_id"
        on_fail = "warn"

        [[check]]
        name = "returns_are_negative"
        expr = "status <> 'returned' or amount < 0"
        on_fail = "quarantine"
        """,
    "gold.dim_customer": """\
        name = "gold.dim_customer"
        owner = "{owner}"

        [source]
        entity = "silver.customers"

        [load]
        mode = "scd2"

        [scd]
        surrogate_key = "customer_sk"
        tracked = ["segment", "city"]   # a change here makes a new version
        type1 = ["email", "name"]       # overwritten on every version

        [contract]
        keys = ["customer_id"]

        [contract.columns]
        customer_sk = "int64"
        customer_id = "int64"
        name = "string"
        segment = "string"
        city = "string"
        email = "string"
        """,
    "gold.fact_sales": """\
        name = "gold.fact_sales"
        owner = "{owner}"

        [source]
        entity = "silver.orders"

        [load]
        mode = "incremental"
        watermark = "modified_at"

        [[lookup]]
        dim = "gold.dim_customer"
        on = "customer_id"
        as = "customer_sk"
        at = "order_date"

        [contract]
        keys = ["order_id"]

        [contract.columns]
        order_id = "int64"
        customer_sk = "int64"
        order_date = "date"
        net_amount = "float64"
        modified_at = "date"

        [[check]]
        column = "order_id"
        not_null = true
        unique = true
        on_fail = "stop"
        """,
}

SAMPLE_FILES = {
    "landing/customers/customers_2026-10-01.csv": (
        "customer_id,name,segment,city,email,updated_at\n"
        "1, Ada Lovelace ,retail,Austin,ADA@example.com,2026-10-01\n"
        "2,Bo Diddley,retail,Boston,bo@example.com,2026-10-01\n"
        "3,Cy Young,corporate,Chicago,cy@example.com,2026-10-01\n"),
    "landing/orders/orders_2026-10-01.csv": (
        "order_id,customer_id,order_date,amount,discount,status,modified_at\n"
        "100,1,2026-09-30,50.00,5.00, Shipped,2026-10-01\n"
        "101,2,2026-10-01,20.00,,shipped,2026-10-01\n"
        "102,3,2026-10-01,80.00,60.00,SHIPPED ,2026-10-01\n"
        "103,99,2026-10-01,10.00,,shipped,2026-10-01\n"
        "104,1,2026-10-01,-15.00,,returned,2026-10-01\n"
        "105,2,2026-10-01,12.00,,returned,2026-10-01\n"),
}


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def init_project(root: Path, preset: str = "3", owner: str = "", sample: bool = True) -> Project:
    root = Path(root)
    if (root / "layers.toml").exists():
        raise FileExistsError(f"{root} already has a metadata project")
    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {', '.join(PRESETS)}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata" / "entities").mkdir(parents=True, exist_ok=True)
    _write(root, "README.md", README)
    _write(root, ".gitignore", ".local/\n__pycache__/\n.pytest_cache/\n")
    _write(root, "environments.toml", textwrap.dedent("""\
        # Dev runs on this PC. Test and Prod write to a Fabric workspace each, with one
        # lakehouse per layer (LH_<Layer>) created the first time you promote.
        [dev]
        target = "local"

        [test]
        workspace_id = ""

        [prod]
        workspace_id = ""
        """))
    _write(root, "exceptions.toml", textwrap.dedent("""\
        # Approved exceptions. Each needs a reason, an approver and an expiry date (or "permanent").
        #
        # [[exception]]
        # entity = "gold.fact_sales"
        # rule = "custom_code_test"
        # reason = "Signed off by finance; test due next sprint"
        # approver = "Chris"
        # expires = "2026-11-01"
        """))
    _write(root, "transforms/__init__.py", "")
    _write(root, "transforms/common.py", textwrap.dedent('''\
        """Helpers shared by hooks, custom checks and custom steps."""
        import polars as pl


        def round_money(expr: pl.Expr) -> pl.Expr:
            return expr.round(2)
        '''))
    _write(root, "tests/test_common.py", textwrap.dedent("""\
        import polars as pl

        from transforms.common import round_money


        def test_round_money():
            df = pl.DataFrame({"x": [1.005, 2.0]})
            assert df.select(round_money(pl.col("x")))["x"].to_list() == [1.0, 2.0]
        """))
    write_layers_file(root, PRESETS[preset], {"same_layer_reads": "dag"})
    if sample and preset == "3":
        for name, text in SAMPLE_ENTITIES.items():
            _write(root, f"metadata/entities/{name}.toml", textwrap.dedent(text).format(owner=owner or "me"))
        for rel, text in SAMPLE_FILES.items():
            _write(root, f".local/dev/LH_Bronze/Files/{rel}", text)
    return Project(root)


def _columns_toml(columns: dict[str, str]) -> str:
    return "\n".join(f"{c} = {json.dumps(t)}" for c, t in columns.items())


def _dtype_name(dtype: pl.DataType) -> str:
    if dtype == pl.Date:
        return "date"
    if isinstance(dtype, pl.Datetime):
        return "datetime"
    if dtype == pl.Boolean:
        return "bool"
    if dtype.is_integer():
        return "int64"
    if dtype.is_float():
        return "float64"
    if isinstance(dtype, pl.Decimal):
        return f"decimal({dtype.precision},{dtype.scale})"
    return "string"


def entity_text(project: Project, template: str, name: str, owner: str, source: str = "",
                keys: list[str] | None = None, sample: pl.DataFrame | None = None) -> tuple[str, dict[str, str]]:
    """The TOML for a new entity, plus any code files it needs, as (toml, {path: text})."""
    if template not in TEMPLATES:
        raise ValueError(f"template must be one of {', '.join(TEMPLATES)}")
    keys = keys or []
    table = name.split(".")[-1]
    extra: dict[str, str] = {}
    upstream = project.entities.get(source)
    if template == "files":
        columns = {c: _dtype_name(t) for c, t in sample.schema.items()} if sample is not None else {"id": "int64"}
    elif upstream is not None:
        columns = dict(upstream.columns)
        for d in upstream.derived + upstream.hook_adds:
            columns.setdefault(d, "string")
    else:
        columns = {"id": "int64"}
    keys = keys or ([next(iter(columns))] if columns else [])
    head = [f"name = {json.dumps(name)}", f"owner = {json.dumps(owner or 'me')}", ""]
    checks = "\n".join(textwrap.dedent(f"""\

        [[check]]
        column = {json.dumps(k)}
        not_null = true
        unique = true
        on_fail = "stop\"""") for k in keys[:1])

    if template == "files":
        body = f"""\
[source]
files = {json.dumps(source or f"landing/{table}/*.csv")}   # under this layer's lakehouse Files area

[load]
mode = "full"

[contract]
keys = {json.dumps(keys)}

[contract.columns]
{_columns_toml(columns)}
"""
    elif template == "clean":
        wm = next((c for c in columns if c in ("modified_at", "updated_at", "last_modified")), "")
        load = f'mode = "incremental"\nwatermark = {json.dumps(wm)}' if wm else 'mode = "full"'
        body = f"""\
[source]
entity = {json.dumps(source)}

[load]
{load}

[contract]
keys = {json.dumps(keys)}

[contract.columns]
{_columns_toml(columns)}

# Cleaning rules run in order. Profile the table to find more.
# [[clean]]
# column = "status"
# trim = true
# case = "lower"
{checks}
"""
    elif template in ("dim1", "dim2"):
        base = table[4:] if table.startswith("dim_") else table
        sk = f"{base}_sk"
        cols = {sk: "int64", **columns}
        tracked = [c for c in columns if c not in keys][:3]
        scd = f'surrogate_key = "{sk}"'
        if template == "dim2":
            scd += f"\ntracked = {json.dumps(tracked)}   # a change here makes a new version\ntype1 = []   # overwritten on every version"
        body = f"""\
[source]
entity = {json.dumps(source)}

[load]
mode = "{'scd2' if template == 'dim2' else 'scd1'}"

[scd]
{scd}

[contract]
keys = {json.dumps(keys)}

[contract.columns]
{_columns_toml(cols)}
"""
    elif template == "fact":
        wm = next((c for c in columns if c in ("modified_at", "updated_at", "last_modified")), "")
        load = f'mode = "incremental"\nwatermark = {json.dumps(wm)}' if wm else 'mode = "full"'
        body = f"""\
[source]
entity = {json.dumps(source)}

[load]
{load}

# Look up each dimension's surrogate key as of the fact's date. Unknown members get -1.
# [[lookup]]
# dim = "gold.dim_customer"
# on = "customer_id"
# as = "customer_sk"
# at = "order_date"

[contract]
keys = {json.dumps(keys)}

[contract.columns]
{_columns_toml(columns)}
{checks}
"""
    else:  # custom
        module = table.replace("-", "_")
        extra[f"transforms/{module}.py"] = textwrap.dedent(f'''\
            """Custom step for {name}. Return the rows to write; the framework checks and writes them."""
            import polars as pl


            def build(df: pl.DataFrame, ctx) -> pl.DataFrame:
                # df is the source table's rows (empty when there is no source).
                # ctx.read_table("<entity>") reads a table listed in reads; ctx.params holds [params].
                return df
            ''')
        extra[f"tests/test_{module}.py"] = textwrap.dedent(f"""\
            import polars as pl

            from transforms.{module} import build


            def test_build():
                df = pl.DataFrame({{"id": [1]}})
                assert build(df, ctx=None).height == 1
            """)
        src = f"[source]\nentity = {json.dumps(source)}\n\n" if source else ""
        body = f"""\
{src}[step]
custom = "transforms.{module}:build"
owner = {json.dumps(owner or 'me')}
test = "tests/test_{module}.py::test_build"
reads = []

[load]
mode = "full"

[contract]
keys = {json.dumps(keys)}

[contract.columns]
{_columns_toml(columns)}
"""
    return "\n".join(head) + body, extra


def describe() -> dict[str, Any]:
    return {"templates": TEMPLATES, "presets": {k: [l.to_dict() for l in v] for k, v in PRESETS.items()}}
