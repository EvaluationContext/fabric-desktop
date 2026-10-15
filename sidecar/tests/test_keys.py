"""Centralized surrogate keys: one key table per business key in LH_Keys, MERGEd first on every run."""

import json
import textwrap

import polars as pl
import pytest

from fabric_desktop.meta import keys as K
from fabric_desktop.meta.engine import Engine
from fabric_desktop.meta.project import Project, validate
from fabric_desktop.meta.store import LocalStore
from fabric_desktop.meta.templates import init_project
from fabric_desktop.notebook import Kernel

from test_meta import DAY1, DAY2, append_entity, edit_entity, landing, write


@pytest.fixture
def proj(tmp_path):
    p = init_project(tmp_path / "project", "3", "Chris", True)
    store = LocalStore(p.root / ".local" / "dev", "dev")
    results = Engine(p, store, DAY1).run_many()
    assert [r.status for r in results] == ["ok"] * 6, [(r.entity, r.message) for r in results]
    return p, store


def table(store, lakehouse, schema, name):
    return store.read(lakehouse, schema, name)


def test_each_business_key_has_its_own_three_column_table(proj):
    p, store = proj
    customers = table(store, K.KEYS_LAKEHOUSE, "sk", "customer")
    assert customers.columns == ["customer_sk", "customer_bk", "create_date"]
    assert customers.schema["customer_sk"] == pl.Int32
    assert customers["customer_bk"].is_unique().all() and customers["customer_sk"].is_unique().all()
    assert customers.filter(pl.col("customer_sk") == -1)["customer_bk"].to_list() == ["UNKNOWN"]
    assert table(store, K.KEYS_LAKEHOUSE, "sk", "order").columns == ["order_sk", "order_bk", "create_date"]


def test_every_row_gets_its_key_and_any_source_maps_to_the_same_key(proj):
    p, store = proj
    keys = dict(table(store, K.KEYS_LAKEHOUSE, "sk", "customer").select("customer_bk", "customer_sk").iter_rows())
    silver_c = table(store, "LH_Silver", "dbo", "customers")
    silver_o = table(store, "LH_Silver", "dbo", "orders")
    assert silver_c.schema["customer_sk"] == pl.Int32
    for df in (silver_c, silver_o):
        assert all(keys[str(c)] == sk for c, sk in df.select("customer_id", "customer_sk").iter_rows())
    # customer 99 exists only on an order: it got its key there, so it never lands on -1
    assert 99 in silver_o["customer_id"].to_list() and "99" in keys and keys["99"] > 0
    run = store.meta("run_log").filter(pl.col("entity") == "silver.orders")["steps"][-1]
    assert [s["name"] for s in json.loads(run)][:3] == ["read", "contract", "surrogate keys"]

    # A second source with the same customer number gets the same customer_sk.
    landing(p.root, "landing/erp/c.csv", "cust_no,full_name\n1,Ada L\n7,New Person\n")
    write(p.root, "metadata/entities/bronze.erp_customers.toml", """
        name = "bronze.erp_customers"
        owner = "Chris"
        [source]
        files = "landing/erp/*.csv"
        [contract]
        keys = ["cust_no"]
        [contract.columns]
        cust_no = "int64"
        full_name = "string"
    """)
    write(p.root, "metadata/entities/silver.erp_customers.toml", """
        name = "silver.erp_customers"
        owner = "Chris"
        [source]
        entity = "bronze.erp_customers"
        [contract]
        keys = ["cust_no"]
        [contract.columns]
        cust_no = "int64"
        full_name = "string"
        [[check]]
        column = "cust_no"
        not_null = true
        unique = true
        [[keys]]
        table = "customer"
        business_key = "cust_no"
    """)
    p.reload()
    assert validate(p, DAY2) == []
    assert all(r.status == "ok" for r in Engine(p, store, DAY2).run_many(["bronze.erp_customers",
                                                                         "silver.erp_customers"]))
    erp = table(store, "LH_Silver", "dbo", "erp_customers")
    assert dict(erp.select("cust_no", "customer_sk").iter_rows())[1] == keys["1"]
    assert dict(erp.select("cust_no", "customer_sk").iter_rows())[7] == max(keys.values()) + 1


def test_rerunning_issues_no_new_keys(proj):
    p, store = proj
    before = table(store, K.KEYS_LAKEHOUSE, "sk", "customer").sort("customer_sk")
    Engine(p, store, DAY2).run_many(backfill="full")
    after = table(store, K.KEYS_LAKEHOUSE, "sk", "customer").sort("customer_sk")
    assert after.equals(before)


def test_composite_and_missing_business_keys(tmp_path):
    p = init_project(tmp_path / "project", "3", "Chris", False)
    store = LocalStore(p.root / ".local" / "dev", "dev")
    K.write_key_table(p.root, "store")
    landing(p.root, "s/a.csv", "region,code,name\nwest, 10 ,A\nwest,11,B\n,12,C\neast,10,D\n")
    write(p.root, "metadata/entities/bronze.stores.toml", """
        name = "bronze.stores"
        owner = "Chris"
        [source]
        files = "s/*.csv"
        [contract.columns]
        region = "string"
        code = "string"
        name = "string"
    """)
    write(p.root, "metadata/entities/silver.stores.toml", """
        name = "silver.stores"
        owner = "Chris"
        [source]
        entity = "bronze.stores"
        [contract]
        keys = ["name"]
        [contract.columns]
        region = "string"
        code = "string"
        name = "string"
        [[check]]
        column = "name"
        not_null = true
        unique = true
        [[keys]]
        table = "store"
        business_key = ["region", "code"]
    """)
    p.reload()
    assert validate(p, DAY1) == []
    assert all(r.status == "ok" for r in Engine(p, store, DAY1).run_many())
    out = dict(table(store, "LH_Silver", "dbo", "stores").select("name", "store_sk").iter_rows())
    assert out["C"] == -1  # no region, so no business key
    bks = table(store, K.KEYS_LAKEHOUSE, "sk", "store")["store_bk"].to_list()
    assert sorted(bks) == ["UNKNOWN", "east||10", "west||10", "west||11"]  # trimmed, joined with ||
    assert len({out["A"], out["B"], out["D"]}) == 3


def test_merge_keys_never_gives_a_business_key_two_keys(tmp_path):
    store = LocalStore(tmp_path, "dev")
    kt = K.KeyTable("customer", {}, tmp_path / "customer.toml")
    first = K.merge_keys(store, kt, pl.Series(["B", "A", None, "A"]))
    second = K.merge_keys(store, kt, pl.Series(["C", "A"]))
    assert (first["inserted"], first["first"], first["last"]) == (2, 1, 2)
    assert (second["inserted"], second["existing"], second["first"]) == (1, 1, 3)
    df = K.read_keys(store, kt)
    assert dict(df.select("customer_bk", "customer_sk").iter_rows()) == {"UNKNOWN": -1, "A": 1, "B": 2, "C": 3}


def test_type1_dimension_uses_the_central_key(proj):
    p, store = proj
    write(p.root, "metadata/entities/gold.dim_customer1.toml", """
        name = "gold.dim_customer1"
        owner = "Chris"
        [source]
        entity = "silver.customers"
        [load]
        mode = "scd1"
        [scd]
        surrogate_key = "customer_sk"
        [contract]
        keys = ["customer_id"]
        [contract.columns]
        customer_sk = "int64"
        customer_id = "int64"
        name = "string"
    """)
    p.reload()
    assert validate(p, DAY2) == []
    assert Engine(p, store, DAY2).run("gold.dim_customer1").status == "ok"
    keys = dict(table(store, K.KEYS_LAKEHOUSE, "sk", "customer").select("customer_bk", "customer_sk").iter_rows())
    dim = table(store, "LH_Gold", "dbo", "dim_customer1")
    assert all(keys[str(c)] == sk for c, sk in dim.filter(pl.col("customer_sk") > 0)
               .select("customer_id", "customer_sk").iter_rows())
    assert dim.filter(pl.col("customer_sk") == -1).height == 1


def test_the_save_gate_checks_keys(proj):
    p, store = proj
    append_entity(p.root, "silver.customers", '[[keys]]\ntable = "vendor"\nbusiness_key = "nope"\n')
    p.reload()
    errors = validate(p, DAY2)
    assert "silver.customers: key table vendor is not defined in Metadata > Surrogate keys" in errors
    assert "silver.customers: business key nope is not in the source" in errors
    edit_entity(p.root, "silver.customers", 'table = "vendor"\nbusiness_key = "nope"', 'table = "customer"\nbusiness_key = "name"')
    assert "silver.customers: key table customer is listed twice" in validate(p.reload() or p, DAY2)

    # A layer can require keys on every table.
    from fabric_desktop.meta.project import Layer
    p2 = init_project(p.root.parent / "two", "3", "Chris", True)
    layers = [Layer(**{k: v for k, v in l.to_dict().items() if k != "lakehouse"}) for l in p2.layers]
    layers[2].require_keys = True
    p2.write_layers(layers)
    p2.reload()
    assert p2.layer("gold").require_keys
    errors = validate(p2, DAY2)
    assert "gold.dim_customer: layer gold requires surrogate keys; add a [[keys]] block" in errors

    # Source systems: unique ids and codes, and no secrets in the file.
    K.write_source_systems(p.root, [K.SourceSystem(1, "CRM"), K.SourceSystem(1, "crm")])
    path = K.keys_dir(p.root) / K.SOURCE_SYSTEMS_FILE
    path.write_text(path.read_text() + '[source_system.connection.dev]\npassword = "hunter2"\n')
    errors = validate(p.reload() or p, DAY2)
    assert "source system crm: id 1 is used twice" in errors
    assert "source system crm: code is used twice" in errors
    assert any("password looks like a secret" in e for e in errors)


def test_template_notebook_starts_with_the_merge(proj):
    p, _ = proj
    sql = K.merge_sql(p, p.entities["silver.orders"], "Keys - Test")
    assert sql.index("MERGE INTO `Keys - Test`.LH_Keys.sk.`order`") < sql.index("MERGE INTO `Keys - Test`.LH_Keys.sk.`customer`")
    assert "FROM LH_Bronze.dbo.orders" in sql
    assert "INSERT (customer_sk, customer_bk, create_date)" in sql and "WHEN NOT MATCHED THEN" in sql
    assert "COALESCE(k2.customer_sk, -1) AS customer_sk" in sql
    cells = K.template_cells(p, p.entities["silver.orders"])
    assert cells[0]["source"].startswith("%%sql\n-- Surrogate keys for silver.orders")
    assert "MERGE INTO LH_Keys.sk.`order` AS k" in cells[0]["source"]
    import sqlglot
    assert [type(x).__name__ for x in sqlglot.parse(sql, read="spark")] == ["Merge", "Merge", "Create"]
    out = Kernel().run(cells[0]["source"])
    assert out["status"] == "ok" and "runs in Fabric" in out["outputs"][0]["text"]


def test_service_keys_round_trip(tmp_path):
    from fabric_desktop.server import handle
    from fabric_desktop.service import Service
    svc = Service(root=tmp_path / "app")
    call = lambda method, **params: json.loads(json.dumps(  # noqa: E731
        handle(svc, json.dumps({"id": 1, "method": method, "params": params})), default=str))

    call("meta_init", path=str(tmp_path / "proj"), preset="3")
    keys = call("meta_keys")["result"]
    assert [t["name"] for t in keys["key_tables"]] == ["customer", "order"]
    assert keys["key_tables"][0]["used_by"] == ["silver.customers", "silver.orders"]
    assert keys["key_tables"][0]["created"] is False and keys["local"] is True
    assert [s["code"] for s in keys["source_systems"]] == ["CRM", "SHOP"]

    assert all(r["status"] == "ok" for r in call("meta_run")["result"])
    assert (tmp_path / "proj" / ".local" / "keys-dev" / "LH_Keys" / "Tables" / "sk" / "customer").exists()
    keys = call("meta_keys")["result"]
    assert keys["key_tables"][0]["keys"] == 4 and keys["key_tables"][0]["next"] == 5
    assert call("meta_key_lookup", table="customer", value=" 2 ")["result"]["sk"] == 2
    assert call("meta_key_lookup", table="customer", value="nobody")["result"] is None

    assert "product" in [t["name"] for t in call("meta_new_key_table", name="Product")["result"]["key_tables"]]
    assert "already exists" in call("meta_new_key_table", name="product")["error"]
    assert "customer: silver.customers, silver.orders use it" in call("meta_delete_key_table", name="customer")["error"]
    assert "product" not in [t["name"] for t in call("meta_delete_key_table", name="product")["result"]["key_tables"]]

    erp = {"id": 3, "code": "ERP", "name": "Finance ERP", "system_type": "SQL Server", "active": True,
           "connections": {"dev": {"server_host": "sql-dev.kratos.local", "port": "1433", "database_name": "erp",
                                   "auth_method": "Entra ID", "key_vault_url": "https://kv.vault.azure.net",
                                   "secret_name": "erp-dev"},
                           "prod": {"server_host": "sql.kratos.local", "port": "1433"}}}
    saved = call("meta_save_source_system", system=erp)["result"]["source_systems"]
    assert saved[2]["connections"]["dev"]["server_host"] == "sql-dev.kratos.local"
    assert "already used" in call("meta_save_source_system", system=erp | {"code": "erp2"})["error"]
    assert "never changes" in call("meta_save_source_system", system=erp | {"id": 9}, original_id=3)["error"]
    edited = call("meta_save_source_system", system=erp | {"name": "Finance ERP (D365)"}, original_id=3)["result"]
    assert [s["name"] for s in edited["source_systems"]][2] == "Finance ERP (D365)"
    sync = call("meta_keys_sync")["result"]
    assert sync["config"] == {"source_system": 3, "source_connection": 4}
    conn = LocalStore(tmp_path / "proj" / ".local" / "keys-dev", "dev").read(K.KEYS_LAKEHOUSE, "config",
                                                                              "source_connection")
    assert conn.filter(pl.col("environment") == "prod")["port"].to_list() == [1433]
    assert len(call("meta_delete_source_system", id=3)["result"]["source_systems"]) == 2

    nb = call("meta_key_notebook", name="silver.customers")["result"]
    assert nb["name"] == "nb_bronze_to_silver_customers"
    cells = call("open_notebook", name=nb["name"])["result"]["cells"]
    assert cells[0]["source"].startswith("%%sql") and "MERGE INTO LH_Keys.sk.`customer`" in cells[0]["source"]
    ent = call("meta_entity", name="silver.customers")["result"]
    assert ent["keys_sql"].startswith("-- Surrogate keys for silver.customers")
    assert ent["surrogate_keys"] == [{"table": "customer", "column": "customer_sk", "business_key": ["customer_id"]}]

    assert "Pick a Keys workspace for Test" in call("meta_keys", env="test")["result"]["problem"]
    call("meta_set_environment", env="test", workspace_id="ws-test", workspace_name="Sales Test")
    keys = call("meta_set_keys_workspace", env="test", workspace_id="ws-keys", workspace_name="Keys - Test")["result"]
    assert keys["workspace_name"] == "Keys - Test"
    call("meta_set_environment", env="test", workspace_id="ws-test2", workspace_name="Sales Test 2")
    envs = Project(tmp_path / "proj").environments["test"]
    assert envs["keys_workspace_id"] == "ws-keys" and envs["workspace_id"] == "ws-test2"
