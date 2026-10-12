"""Developer stories run against the metadata framework, on real Delta tables in a temp folder."""

import datetime as dt
import textwrap
from pathlib import Path

import polars as pl
import pytest

from fabric_desktop.meta import Engine, GateError, Layer, LocalStore, Project, impact, promote, run_order, validate
from fabric_desktop.meta.engine import scd2_invariants
from fabric_desktop.meta.templates import entity_text, init_project

D = dt.date
DAY1, DAY2, DAY3 = D(2026, 10, 1), D(2026, 10, 2), D(2026, 10, 3)


def entity_file(root: Path, name: str) -> Path:
    return root / "metadata" / "entities" / f"{name}.toml"


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")


def edit(root: Path, rel: str, old: str, new: str) -> None:
    p = root / rel
    text = p.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {rel}"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


def edit_entity(root, name, old, new):
    edit(root, f"metadata/entities/{name}.toml", old, new)


def append_entity(root, name, text):
    p = entity_file(root, name)
    p.write_text(p.read_text(encoding="utf-8") + "\n" + textwrap.dedent(text), encoding="utf-8")


def landing(root: Path, rel: str, csv: str, lakehouse: str = "LH_Bronze") -> None:
    write(root, f".local/dev/{lakehouse}/Files/{rel}", csv)


class Repo:
    def __init__(self, root: Path, preset="3", sample=True):
        self.root = root
        self.p = init_project(root, preset, "Chris", sample)
        self.dev = LocalStore(root / ".local" / "dev", "dev")

    def reload(self):
        self.p.reload()
        return self

    def engine(self, day=DAY1, store=None):
        return Engine(self.p, store or self.dev, day)

    def table(self, name, store=None):
        return self.engine(store=store).read_entity(name)

    def errors(self, today=DAY2):
        return validate(self.p, today)


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path / "project")
    results = r.engine().run_many()
    assert [x.status for x in results] == ["ok"] * 6, [(x.entity, x.message) for x in results]
    return r


def value(df, key_col, key, col):
    return df.filter(pl.col(key_col) == key)[col].item()


# ----------------------------------------------------------------------------- 0. baseline


def test_00_baseline_runs_bronze_to_gold_with_lineage(repo):
    assert run_order(repo.p) == ["bronze.customers", "bronze.orders", "silver.customers", "silver.orders",
                                 "gold.dim_customer", "gold.fact_sales"]
    fact = repo.table("gold.fact_sales")
    hist = repo.dev.history("LH_Gold", "dbo", "fact_sales")[0]
    assert hist["fabric_desktop_metadata"] == repo.p.version
    assert set(fact["_run_id"]) == {hist["fabric_desktop_run_id"]}
    # order 103's customer doesn't exist: Silver warns, Gold points it at the Unknown member
    assert value(fact, "order_id", 103, "customer_sk") == -1
    # order 105 is a return with a positive amount: quarantined in Silver, with the reason
    q = repo.dev.read("LH_Silver", "quarantine", "dbo_orders")
    assert q["order_id"].to_list() == ["105"] and q["_check"].to_list() == ["returns_are_negative"]
    dq = repo.dev.meta("dq_results").filter((pl.col("check") == "customer_id") & (pl.col("entity") == "silver.orders"))
    assert dq["failed"].to_list() == [1] and dq["action"].to_list() == ["warn"]
    # cleaning ran: trimmed names, lower-cased status
    assert value(repo.table("silver.customers"), "customer_id", 1, "name") == "Ada Lovelace"
    assert set(repo.table("silver.orders")["status"]) == {"shipped", "returned"}


def test_00b_tables_follow_the_fabric_delta_profile(repo):
    from deltalake import DeltaTable
    t = DeltaTable(repo.dev.uri("LH_Gold", "dbo", "dim_customer"))
    assert t.protocol().min_writer_version <= 2 and not t.protocol().writer_features


# ----------------------------------------------------------------------------- 1. adding something


PRODUCTS = {
    "bronze.products": """
        name = "bronze.products"
        [source]
        files = "landing/products/*.csv"
        [contract]
        keys = ["product_id"]
        [contract.columns]
        product_id = "int64"
        product_name = "string"
        category = "string"
    """,
    "silver.products": """
        name = "silver.products"
        owner = "Chris"
        [source]
        entity = "bronze.products"
        [contract]
        keys = ["product_id"]
        [contract.columns]
        product_id = "int64"
        product_name = "string"
        category = "string"
    """,
}


def test_01_add_a_new_source_bronze_to_gold(repo):
    root = repo.root
    landing(root, "landing/products/p.csv", "product_id,product_name,category\n7,Desk,office\n8,Lamp,office\n")
    for name, text in PRODUCTS.items():
        write(root, f"metadata/entities/{name}.toml", text)
    errs = repo.reload().errors()
    assert "bronze.products: needs an owner" in errs
    assert "silver.products: layer silver requires a key not null check on the key" in errs
    assert "silver.products: layer silver requires a key unique check on the key" in errs

    edit_entity(root, "bronze.products", 'name = "bronze.products"', 'name = "bronze.products"\nowner = "Chris"')
    append_entity(root, "silver.products", """
        [[check]]
        column = "product_id"
        not_null = true
        unique = true
    """)
    text, _ = entity_text(repo.reload().p, "dim1", "gold.dim_product", "Chris", "silver.products", ["product_id"])
    write(root, "metadata/entities/gold.dim_product.toml", text)
    assert repo.reload().errors() == []
    res = repo.engine(DAY2).run_many(["bronze.products", "silver.products", "gold.dim_product"])
    assert [r.status for r in res] == ["ok", "ok", "ok"]
    dim = repo.table("gold.dim_product")
    assert dim.sort("product_sk")["product_sk"].to_list() == [-1, 1, 2]
    assert value(dim, "product_sk", -1, "product_name") == "Unknown"
    assert impact(repo.p, "bronze.products") == ["silver.products", "gold.dim_product"]


def test_01b_bronze_from_an_api_uses_a_custom_step(repo):
    root = repo.root
    text, files = entity_text(repo.p, "custom", "bronze.crm_accounts", "Chris", keys=["account_id"])
    text = text.replace('id = "int64"', 'account_id = "int64"\ntier = "string"')
    write(root, "metadata/entities/bronze.crm_accounts.toml", text)
    write(root, "transforms/crm_accounts.py", """
        import polars as pl

        def build(df, ctx):
            # real code would call the API with ctx.params["base_url"]
            return pl.DataFrame({"account_id": [1, 2], "tier": ["gold", "silver"]})
    """)
    assert "bronze.crm_accounts: custom step transforms.crm_accounts:build needs a test" in repo.reload().errors()
    write(root, "tests/test_crm_accounts.py", files["tests/test_crm_accounts.py"])
    assert repo.reload().errors() == []
    assert repo.engine(DAY2).run("bronze.crm_accounts").status == "ok"
    assert repo.table("bronze.crm_accounts").height == 2


# ----------------------------------------------------------------------------- 2. modifying something


ORDER_COLS = "order_id,customer_id,order_date,amount,discount,status,modified_at"


def test_02_source_adds_a_column(repo):
    root = repo.root
    landing(root, "landing/orders/orders_2026-10-02.csv",
            f"{ORDER_COLS},channel\n106,2,2026-10-02,30.00,,shipped,2026-10-02,web\n")
    bronze = repo.engine(DAY2).run("bronze.orders")
    silver = repo.engine(DAY2).run("silver.orders")
    assert "channel" in repo.table("bronze.orders").columns
    assert "add them to the contract" in bronze.steps[1].detail
    assert "channel" not in repo.table("silver.orders").columns
    assert repo.dev.meta("schema_history")["columns"].to_list() == ["channel"]
    assert silver.status == "ok"

    edit_entity(root, "silver.orders", 'net_amount = "float64"', 'net_amount = "float64"\nchannel = "string"')
    assert "silver.orders: contract column channel is not in the source and nothing derives it" in repo.reload().errors()
    edit_entity(root, "bronze.orders", 'modified_at = "date"', 'modified_at = "date"\nchannel = "string"')
    assert repo.reload().errors() == []
    assert repo.engine(DAY2).run("bronze.orders").status == "ok"
    ask = repo.engine(DAY2).run("silver.orders")
    assert ask.status == "needs_decision" and ask.options == ["none", "full"]
    assert repo.engine(DAY2).run("silver.orders", backfill="full").status == "ok"
    so = repo.table("silver.orders")
    assert value(so, "order_id", 106, "channel") == "web"
    assert value(so, "order_id", 100, "channel") is None


def test_02b_source_drops_a_column(repo):
    landing(repo.root, "landing/orders/orders_2026-10-01.csv",
            "order_id,customer_id,order_date,amount,status,modified_at\n100,1,2026-09-30,50,shipped,2026-10-01\n")
    res = repo.engine(DAY2).run("bronze.orders")
    assert res.status == "failed" and "no longer has discount" in res.message
    assert len(repo.dev.history("LH_Bronze", "dbo", "orders")) == 1


def test_02c_rename_shows_what_breaks(repo):
    edit_entity(repo.root, "silver.orders", 'net_amount = "float64"', 'net_sales = "float64"')
    edit_entity(repo.root, "silver.orders", 'column = "net_amount"', 'column = "net_sales"')
    errs = repo.reload().errors()
    assert "gold.fact_sales: contract column net_amount is not in the source and nothing derives it" in errs
    assert impact(repo.p, "silver.orders") == ["gold.fact_sales"]


def test_02d_delete_a_table_others_read(repo):
    entity_file(repo.root, "silver.customers").unlink()
    errs = repo.reload().errors()
    assert "gold.dim_customer: reads silver.customers, which is not a managed entity" in errs
    assert "silver.orders: reads silver.customers, which is not a managed entity" in errs


def test_02e_values_that_dont_fit_the_type_are_quarantined(repo):
    landing(repo.root, "landing/orders/orders_2026-10-02.csv", f"{ORDER_COLS}\n106,2,not a date,30,,shipped,2026-10-02\n")
    res = repo.engine(DAY2).run("bronze.orders")
    assert res.status == "ok" and res.quarantined == 1
    q = repo.dev.read("LH_Bronze", "quarantine", "dbo_orders")
    assert q["_check"].to_list() == ["contract types"] and q["order_date"].to_list() == ["not a date"]


# ----------------------------------------------------------------------------- 3. changing logic


def test_03_change_a_derived_column(repo):
    root = repo.root
    old_run = repo.table("silver.orders")["_run_id"][0]
    edit_entity(root, "silver.orders", 'expr = "amount - coalesce(discount, 0)"',
                'expr = "amount - least(coalesce(discunt, 0), amount * 0.5)"')
    assert "silver.orders: derive net_amount: uses unknown column discunt" in repo.reload().errors()
    edit_entity(root, "silver.orders", "discunt", "discount")
    assert repo.reload().errors() == []

    landing(root, "landing/orders/orders_2026-10-02.csv", f"{ORDER_COLS}\n106,3,2026-10-02,100,80,shipped,2026-10-02\n")
    repo.engine(DAY2).run("bronze.orders")
    assert repo.engine(DAY2).run("silver.orders").status == "needs_decision"

    # new rows only: old rows keep the old logic, and _run_id shows which is which
    assert repo.engine(DAY2).run("silver.orders", backfill="none").status == "ok"
    so = repo.table("silver.orders")
    assert value(so, "order_id", 106, "net_amount") == 50.0
    assert value(so, "order_id", 102, "net_amount") == 20.0
    log = repo.dev.meta("run_log")
    logic = dict(zip(log["run_id"], log["logic"]))
    assert logic[value(so, "order_id", 102, "_run_id")] == logic[old_run]
    assert len({logic[r] for r in so["_run_id"]}) == 2

    # rebuilding Silver alone leaves Gold stale: the rebuilt rows are behind Gold's watermark
    repo.engine(DAY2).run("silver.orders", backfill="full")
    repo.engine(DAY2).run("gold.fact_sales")
    assert value(repo.table("gold.fact_sales"), "order_id", 102, "net_amount") == 20.0

    res = repo.engine(DAY2).backfill("silver.orders")
    assert [r.entity for r in res] == ["silver.orders", "gold.fact_sales"]
    assert value(repo.table("gold.fact_sales"), "order_id", 102, "net_amount") == 40.0


def test_03b_a_stop_check_writes_nothing(repo):
    append_entity(repo.root, "silver.orders", """
        [[check]]
        name = "amount_under_75"
        expr = "amount < 75"
        on_fail = "stop"
    """)
    repo.reload()
    before = len(repo.dev.history("LH_Silver", "dbo", "orders"))
    res = repo.engine(DAY2).run("silver.orders", backfill="full")
    assert res.status == "failed" and "amount_under_75 failed on 1 row" in res.message
    assert len(repo.dev.history("LH_Silver", "dbo", "orders")) == before
    downstream = repo.engine(DAY2).run_many(["silver.orders", "gold.fact_sales"], backfill="full")
    assert [r.status for r in downstream] == ["failed", "skipped"]


# ----------------------------------------------------------------------------- 4. enhancements


def test_04_type1_dimension_becomes_type2(tmp_path):
    r = Repo(tmp_path / "project")
    edit_entity(r.root, "gold.dim_customer", 'mode = "scd2"', 'mode = "scd1"')
    r.reload().engine().run_many()
    keys = dict(r.table("gold.dim_customer").filter(pl.col("customer_sk") > 0)
                .select("customer_id", "customer_sk").iter_rows())

    edit_entity(r.root, "gold.dim_customer", 'mode = "scd1"', 'mode = "scd2"')
    ask = r.reload().engine(DAY2).run("gold.dim_customer")
    assert ask.status == "needs_decision" and ask.options == ["migrate"]
    assert r.engine(DAY2).run("gold.dim_customer", backfill="migrate").status == "ok"
    dim = r.table("gold.dim_customer")
    assert dict(dim.filter(pl.col("customer_sk") > 0).select("customer_id", "customer_sk").iter_rows()) == keys
    assert scd2_invariants(dim, "customer_sk", ["customer_id"]) == []

    landing(r.root, "landing/customers/customers_2026-10-03.csv",
            "customer_id,name,segment,city,email,updated_at\n1,Ada Lovelace,retail,Denver,ada@new.com,2026-10-03\n")
    landing(r.root, "landing/orders/orders_2026-10-03.csv", f"{ORDER_COLS}\n107,1,2026-10-03,9,,shipped,2026-10-03\n")
    res = r.engine(DAY3).run_many()
    assert all(x.status == "ok" for x in res), [(x.entity, x.message) for x in res]
    dim = r.table("gold.dim_customer")
    ada = dim.filter(pl.col("customer_id") == 1).sort("customer_sk")
    assert ada["city"].to_list() == ["Austin", "Denver"]
    assert ada["email"].to_list() == ["ada@new.com", "ada@new.com"]
    assert ada["valid_to"].to_list() == [DAY3, D(9999, 12, 31)]
    assert scd2_invariants(dim, "customer_sk", ["customer_id"]) == []
    fact = r.table("gold.fact_sales")
    assert value(fact, "order_id", 100, "customer_sk") == keys[1]
    assert value(fact, "order_id", 107, "customer_sk") == ada["customer_sk"][1]


def test_04b_rebuilding_a_dimension_keeps_its_keys(repo):
    before = repo.table("gold.dim_customer").select("customer_id", "customer_sk").sort("customer_sk")
    assert all(r.status == "ok" for r in repo.engine(DAY2).backfill("gold.dim_customer"))
    after = repo.table("gold.dim_customer").select("customer_id", "customer_sk").sort("customer_sk")
    assert before.equals(after)


def test_04c_history_starts_in_1900_so_older_facts_find_their_customer(repo):
    assert value(repo.table("gold.fact_sales"), "order_id", 100, "customer_sk") == 1


def test_04d_history_from_the_load_date_sends_older_facts_to_unknown(tmp_path):
    r = Repo(tmp_path / "project")
    edit_entity(r.root, "gold.dim_customer", 'surrogate_key = "customer_sk"',
                'surrogate_key = "customer_sk"\nfirst_version_from = "load_date"')
    r.reload().engine().run_many()
    assert value(r.table("gold.fact_sales"), "order_id", 100, "customer_sk") == -1


# ----------------------------------------------------------------------------- 5. custom Python


FREIGHT_HOOK = """
    import polars as pl
    from transforms.common import round_money

    def allocate_freight(df, ctx):
        freight = ctx.read_table("silver.shipments")
        out = df.join(freight.select("order_id", "freight"), on="order_id", how="left")
        ctx.log(f"{out['freight'].is_not_null().sum()} orders had freight")
        return out.with_columns(round_money(pl.col("freight").fill_null(0)).alias("freight_alloc")).drop("freight")
"""


def add_shipments(r: Repo):
    landing(r.root, "landing/shipments/s.csv", "order_id,freight\n100,3.333\n101,4\n")
    write(r.root, "metadata/entities/bronze.shipments.toml", """
        name = "bronze.shipments"
        owner = "Chris"
        [source]
        files = "landing/shipments/*.csv"
        [contract]
        keys = ["order_id"]
        [contract.columns]
        order_id = "int64"
        freight = "float64"
    """)
    write(r.root, "metadata/entities/silver.shipments.toml", """
        name = "silver.shipments"
        owner = "Chris"
        [source]
        entity = "bronze.shipments"
        [contract]
        keys = ["order_id"]
        [contract.columns]
        order_id = "int64"
        freight = "float64"
        [[check]]
        column = "order_id"
        not_null = true
        unique = true
    """)
    write(r.root, "tests/test_orders.py", "def test_allocate_freight():\n    pass\n")


def add_hook(r: Repo, hooks: str):
    edit_entity(r.root, "silver.orders", 'net_amount = "float64"', 'net_amount = "float64"\nfreight_alloc = "float64"')
    append_entity(r.root, "silver.orders", hooks)


HOOKS = """
    [hooks]
    after_clean = "transforms.orders:allocate_freight"
    reads = ["silver.shipments"]
    adds = ["freight_alloc"]
    owner = "Chris"
    test = "tests/test_orders.py::test_allocate_freight"
"""


def test_05_hook_adds_a_column_using_a_shared_helper(repo):
    add_shipments(repo)
    (repo.root / "tests" / "test_orders.py").unlink()
    write(repo.root, "transforms/orders.py", FREIGHT_HOOK)
    add_hook(repo, HOOKS)
    assert repo.reload().errors() == ["silver.orders: hook after_clean transforms.orders:allocate_freight needs a test"]
    write(repo.root, "tests/test_orders.py", "def test_allocate_freight():\n    pass\n")
    assert repo.reload().errors() == []
    order = run_order(repo.p)
    assert order.index("silver.shipments") < order.index("silver.orders")
    res = repo.engine(DAY2).run_many(["bronze.shipments", "silver.shipments"])
    assert [r.status for r in res] == ["ok", "ok"]
    res = repo.engine(DAY2).run("silver.orders", backfill="full")
    hook = next(s for s in res.steps if s.name == "hook after_clean")
    assert "2 orders had freight" in hook.detail
    assert value(repo.table("silver.orders"), "order_id", 100, "freight_alloc") == 3.33


def test_05b_hook_guardrails(repo):
    add_shipments(repo)
    repo.reload().engine(DAY2).run_many(["bronze.shipments", "silver.shipments"])

    write(repo.root, "transforms/orders.py", FREIGHT_HOOK)
    add_hook(repo, HOOKS.replace('reads = ["silver.shipments"]\n', ""))
    res = repo.reload().engine(DAY2).run("silver.orders", backfill="full")
    assert res.status == "failed" and "without declaring it in reads" in res.message

    cases = {
        "def allocate_freight(df, ctx):\n    ctx.write_table(df, 'gold.fact_sales')\n    return df\n": "write_table",
        "def allocate_freight(df, ctx):\n    return df.drop('order_id')\n": "dropped key columns order_id",
        "import polars as pl\ndef allocate_freight(df, ctx):\n    return df.with_columns(pl.lit(1.0).alias('freight_alloc'),"
        " pl.lit('x').alias('debug'))\n": "added columns debug that are not in the contract",
    }
    for code, message in cases.items():
        write(repo.root, "transforms/orders.py", code)
        res = repo.reload().engine(DAY2).run("silver.orders", backfill="full")
        assert res.status == "failed" and message in res.message, res.message

    write(repo.root, "transforms/orders.py", "def allocate_freight(frame):\n    return frame\n")
    assert ("silver.orders: hook after_clean transforms.orders:allocate_freight: must take (df, ctx)"
            in repo.reload().errors())


def test_05c_custom_check_in_python(repo):
    write(repo.root, "transforms/checks.py", """
        def valid_email(df, ctx):
            return df["email"].str.contains(r"^[^@ ]+@[^@ ]+\\.[a-z]+$")
    """)
    write(repo.root, "tests/test_checks.py", "def test_valid_email():\n    pass\n")
    append_entity(repo.root, "silver.customers", """
        [[check]]
        name = "valid_email"
        python = "transforms.checks:valid_email"
        on_fail = "quarantine"
        owner = "Chris"
        test = "tests/test_checks.py::test_valid_email"
    """)
    landing(repo.root, "landing/customers/customers_2026-10-02.csv",
            "customer_id,name,segment,city,email,updated_at\n4,Di,retail,Dallas,not-an-email,2026-10-02\n")
    assert repo.reload().errors() == []
    repo.engine(DAY2).run("bronze.customers")
    res = repo.engine(DAY2).run("silver.customers", backfill="none")
    assert res.quarantined == 1 and 4 not in repo.table("silver.customers")["customer_id"].to_list()


def test_05d_custom_step_output_is_still_checked(repo):
    write(repo.root, "transforms/sales.py", """
        import polars as pl

        def build(df, ctx):
            return df.group_by("order_id", "customer_id", "order_date", "modified_at").agg(pl.col("net_amount").sum())
    """)
    write(repo.root, "tests/test_sales.py", "def test_build():\n    pass\n")
    edit_entity(repo.root, "gold.fact_sales", "[[lookup]]",
                '[step]\ncustom = "transforms.sales:build"\nowner = "Dana"\ntest = "tests/test_sales.py::test_build"\n\n[[lookup]]')
    assert repo.reload().errors() == []
    res = repo.engine(DAY2).run("gold.fact_sales", backfill="full")
    assert res.status == "ok", res.message
    assert [s.name for s in res.steps] == ["read", "contract", "custom step", "lookup gold.dim_customer", "checks", "write"]


def test_05e_exceptions_expire(repo):
    write(repo.root, "transforms/sales.py", "def build(df, ctx):\n    return df\n")
    edit_entity(repo.root, "gold.fact_sales", "[[lookup]]", '[step]\ncustom = "transforms.sales:build"\nowner = "Dana"\n\n[[lookup]]')
    write(repo.root, "exceptions.toml", """
        [[exception]]
        entity = "gold.fact_sales"
        rule = "custom_code_test"
        reason = "Allocation logic signed off by finance; test due next sprint"
        approver = "Chris"
        expires = "2026-10-20"
    """)
    repo.reload()
    assert validate(repo.p, D(2026, 10, 15)) == []
    errs = validate(repo.p, D(2026, 10, 21))
    assert "gold.fact_sales: exception for custom_code_test expired on 2026-10-20" in errs
    assert "gold.fact_sales: custom step transforms.sales:build needs a test" in errs


def test_05f_notebooks_cannot_write_managed_tables(repo, tmp_path):
    from fabric_desktop import lakehouse
    from fabric_desktop.service import Service
    svc = Service(root=tmp_path / "app")
    svc.meta_open(str(repo.root))
    guard = lakehouse.write_guard
    assert "managed by the metadata project" in guard("LH_Silver", "dbo", "orders")
    assert guard("LH_Silver", "dbo", "scratch_orders") is None
    assert guard("SalesLakehouse", "dbo", "orders") is None


# ----------------------------------------------------------------------------- 6. layers stay apart


def test_06_no_reads_from_a_later_layer(repo):
    edit_entity(repo.root, "silver.orders", "silver.customers.customer_id", "gold.dim_customer.customer_id")
    assert "silver.orders: reads gold.dim_customer from a later layer (gold)" in repo.reload().errors()


def test_06b_same_layer_reads_can_be_turned_off(repo):
    edit(repo.root, "layers.toml", 'same_layer_reads = "dag"', 'same_layer_reads = "never"')
    errs = repo.reload().errors()
    assert "gold.fact_sales: reads gold.dim_customer from its own layer" in errs


def test_06c_same_layer_cycles_are_rejected(repo):
    write(repo.root, "transforms/cust.py", "def h(df, ctx):\n    return df\n")
    write(repo.root, "tests/test_cust.py", "")
    append_entity(repo.root, "silver.customers", """
        [hooks]
        after_clean = "transforms.cust:h"
        reads = ["silver.orders"]
        owner = "Chris"
        test = "tests/test_cust.py"
    """)
    assert any(e.startswith("dependency cycle: silver.") for e in repo.reload().errors())


def test_06d_a_layer_can_limit_where_it_reads_from(tmp_path):
    r = Repo(tmp_path / "project", preset="5", sample=False)
    layers = r.p.layers
    layers[3].reads_from = ["silver"]
    r.p.set_layers(layers)
    write(r.root, "metadata/entities/bronze.a.toml", """
        name = "bronze.a"
        owner = "Chris"
        [source]
        files = "a/*.csv"
        [contract]
        keys = ["id"]
        [contract.columns]
        id = "int64"
    """)
    write(r.root, "metadata/entities/gold.a.toml", """
        name = "gold.a"
        owner = "Chris"
        [source]
        entity = "bronze.a"
        [contract]
        keys = ["id"]
        [contract.columns]
        id = "int64"
        [[check]]
        column = "id"
        unique = true
    """)
    assert "gold.a: layer gold may only read from silver, not bronze" in r.reload().errors()


# ----------------------------------------------------------------------------- 7. adding and removing layers


def test_07_layer_changes(repo):
    p = repo.p
    names = lambda: [l.name for l in p.layers]  # noqa: E731
    p.set_layers(p.layers + [Layer("semantic", "strict", ["key_unique"])])
    p.set_layers(p.layers + [Layer("landing", "evolve")])
    assert names() == ["bronze", "silver", "gold", "semantic", "landing"]
    with pytest.raises(GateError, match="still has entities"):
        p.set_layers([l for l in p.layers if l.name != "silver"])
    gold_first = [p.layer("gold")] + [l for l in p.layers if l.name != "gold"]
    with pytest.raises(GateError, match="from a later layer"):
        p.set_layers(gold_first)
    assert names() == ["bronze", "silver", "gold", "semantic", "landing"]
    p.set_layers([p.layer("landing")] + [l for l in p.layers if l.name not in ("landing", "semantic")])
    assert names() == ["landing", "bronze", "silver", "gold"]
    with pytest.raises(GateError, match="at most 10"):
        p.set_layers(p.layers + [Layer(f"extra{i}") for i in range(7)])
    with pytest.raises(GateError, match="lowercase"):
        p.set_layers(p.layers + [Layer("Platinum")])
    assert p.layer("landing").lakehouse == "LH_Landing"


def test_07b_two_layer_project(tmp_path):
    r = Repo(tmp_path / "project", preset="2", sample=False)
    landing(r.root, "orders/a.csv", "order_id,amount\n1,1.5\n2,2.5\n", lakehouse="LH_Raw")
    text, _ = entity_text(r.p, "files", "raw.orders", "Chris", "orders/*.csv", ["order_id"],
                          sample=pl.DataFrame({"order_id": [1], "amount": [1.5]}))
    write(r.root, "metadata/entities/raw.orders.toml", text)
    text, _ = entity_text(r.reload().p, "clean", "curated.orders", "Chris", "raw.orders", ["order_id"])
    write(r.root, "metadata/entities/curated.orders.toml", text)
    assert r.reload().errors() == []
    assert [x.status for x in r.engine().run_many()] == ["ok", "ok"]
    assert r.table("curated.orders").height == 2


# ----------------------------------------------------------------------------- 8. promoting


def test_08_promote_dev_to_test_to_prod(repo):
    test = LocalStore(repo.root / ".local" / "test", "test")
    prod = LocalStore(repo.root / ".local" / "prod", "prod")
    for name in ("Customers", "Orders"):
        src = repo.root / ".local" / "dev" / "LH_Bronze" / "Files" / "landing" / name.lower()
        for env in ("test", "prod"):
            dst = repo.root / ".local" / env / "LH_Bronze" / "Files" / "landing" / name.lower()
            dst.mkdir(parents=True)
            for f in src.iterdir():
                (dst / f.name).write_bytes(f.read_bytes())
    with pytest.raises(GateError, match="Promote it first"):
        repo.engine(DAY2, test).run("bronze.orders")
    v = promote(repo.p, repo.dev, test, DAY2)["version"]
    with pytest.raises(GateError, match="bronze.orders: has never run in test"):
        promote(repo.p, test, prod, DAY2)
    assert all(r.status == "ok" for r in repo.engine(DAY2, test).run_many())
    promote(repo.p, test, prod, DAY2)
    assert prod.meta("deployments")["version"].to_list() == [v]
    assert prod.meta("entities").height == 6

    # a comment changes the version, so Test refuses to run it until it's promoted...
    edit_entity(repo.root, "silver.orders", 'case = "lower"', 'case = "lower"  # tidy')
    repo.reload()
    with pytest.raises(GateError, match="Promote it first"):
        repo.engine(DAY3, test).run("silver.orders")
    # ...but it isn't a logic change, so nothing needs to run again in Dev before promoting
    assert promote(repo.p, repo.dev, test, DAY3)["version"] != v
    edit_entity(repo.root, "silver.orders", 'case = "lower"', 'case = "upper"')
    repo.reload()
    with pytest.raises(GateError, match="Promote it first"):
        repo.engine(DAY3, test).run("silver.orders")
    with pytest.raises(GateError, match="silver.orders: changed since its last dev run"):
        promote(repo.p, repo.dev, test, DAY3)


def test_08b_a_failed_run_blocks_promote(repo):
    landing(repo.root, "landing/orders/orders_2026-10-01.csv", "order_id,amount\n1,2\n")
    repo.engine(DAY2).run("bronze.orders")
    with pytest.raises(GateError, match="bronze.orders: its last dev run failed"):
        promote(repo.p, repo.dev, LocalStore(repo.root / ".local" / "test", "test"), DAY2)


def test_08c_paths_on_this_pc_block_promote(repo):
    edit_entity(repo.root, "bronze.orders", 'files = "landing/orders/*.csv"', 'files = "C:/exports/orders/*.csv"')
    assert any("only exists on this PC" in e for e in repo.reload().errors())


# ----------------------------------------------------------------------------- templates


@pytest.mark.parametrize("template,source", [("clean", "bronze.orders"), ("dim1", "silver.customers"),
                                             ("dim2", "silver.customers"), ("fact", "silver.orders")])
def test_templates_meet_the_standards(repo, template, source):
    layer = {"clean": "silver", "dim1": "gold", "dim2": "gold", "fact": "gold"}[template]
    name = f"{layer}.new_{template}"
    keys = repo.p.entities[source].keys
    text, _ = entity_text(repo.p, template, name, "Chris", source, keys)
    write(repo.root, f"metadata/entities/{name}.toml", text)
    assert repo.reload().errors() == []
    res = repo.engine(DAY2).run(name)
    assert res.status == "ok", res.message


# ----------------------------------------------------------------------------- the app's calls


def test_service_round_trip(tmp_path):
    import json as _json

    from fabric_desktop.server import handle
    from fabric_desktop.service import Service
    svc = Service(root=tmp_path / "app")
    call = lambda method, **params: _json.loads(_json.dumps(  # noqa: E731
        handle(svc, _json.dumps({"id": 1, "method": method, "params": params})), default=str))

    assert call("meta_project")["result"]["path"] is None
    proj = call("meta_init", path=str(tmp_path / "proj"), preset="3")["result"]
    assert [l["lakehouse"] for l in proj["layers"]] == ["LH_Bronze", "LH_Silver", "LH_Gold"]
    assert len(proj["entities"]) == 6 and proj["errors"] == []
    runs = call("meta_run")["result"]
    assert [r["status"] for r in runs] == ["ok"] * 6
    log = call("meta_runs")["result"]
    assert len(log) == 6
    detail = call("meta_run_detail", run_id=log[0]["run_id"])["result"]
    assert detail["steps"][0]["name"] == "read"
    ent = call("meta_entity", name="silver.orders")["result"]
    assert ent["impact"] == ["gold.fact_sales"] and ent["preview"]["total"] == 5
    bad = call("meta_save_entity", name="silver.orders", text="name = ")
    assert "isn't valid TOML" in bad["error"]
    blocked = call("meta_delete_entity", name="silver.orders")
    assert blocked["error"].startswith("Blocked by the save gate:\n- can't delete silver.orders")
    new = call("meta_new_entity", template="dim1", name="gold.dim_product", source="silver.orders",
               keys=["order_id"])["result"]
    assert new["mode"] == "scd1" and new["errors"] == []
    layers = proj["layers"] + [{"name": "semantic", "drift": "strict", "required_checks": ["key_unique"]}]
    assert len(call("meta_set_layers", layers=layers)["result"]["layers"]) == 4
    check = call("meta_promote_check", source="dev", target="test")["result"]
    assert check["errors"] == ["gold.dim_product: has never run in dev"]
    assert all(r["status"] == "ok" for r in call("meta_run")["result"])
    assert call("meta_promote_check", source="dev", target="test")["result"]["errors"] == []
    assert "Pick a Fabric workspace for Test" in call("meta_promote", source="dev", target="test")["error"]
