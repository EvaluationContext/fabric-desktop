"""Run in Fabric, schedules, table maintenance, the Direct Lake model and profiling."""

import ast
import io
import json
import sys
import types
from pathlib import Path

import polars as pl
import pytest
from deltalake import DeltaTable, write_deltalake

from fabric_desktop import maintenance, semantic
from fabric_desktop.fabric_run import fabric_ipynb, schedule_configuration, SHIM_MARKER
from fabric_desktop.meta.store import OneLakeStore
from fabric_desktop.profiling import profile
from fabric_desktop.service import Service
from test_fabric_api import Resp, client

LH = {"workspace_id": "ws1", "lakehouse_id": "lh1", "name": "Sales", "workspace_name": "Dev"}


# ---- Fabric REST calls

def test_run_item_job_returns_the_job_id_from_location():
    c, s = client([Resp(202, headers={"Location": "https://api.fabric.microsoft.com/v1/workspaces/ws1/items/nb1/jobs/instances/job9"})])
    assert c.run_item_job("ws1", "nb1", "RunNotebook") == "job9"
    assert s.calls[0][:2] == ("POST", "https://api.fabric.microsoft.com/v1/workspaces/ws1/items/nb1/jobs/instances")


def test_table_maintenance_asks_for_v_order_and_vacuum_on_the_schema_table():
    c, s = client([Resp(202, headers={"Location": "https://x/jobs/instances/j1"})])
    assert c.run_table_maintenance("ws1", "lh1", "fact_sales", "gold") == "j1"
    method, url, body = s.calls[0]
    assert url.endswith("/workspaces/ws1/lakehouses/lh1/jobs/instances")
    assert body == {"executionData": {"tableName": "fact_sales", "schemaName": "gold",
                                      "optimizeSettings": {"vOrder": "true"},
                                      "vacuumSettings": {"retentionPeriod": "7.01:00:00"}}}


def test_save_semantic_model_updates_an_existing_one():
    c, s = client([Resp(200, {"value": [{"id": "m1", "displayName": "SM_Gold"}]}), Resp(200, {})])
    out = c.save_semantic_model("ws1", "SM_Gold", {"definition.pbism": "{}"})
    assert out["created"] is False
    assert s.calls[1][1].endswith("/semanticModels/m1/updateDefinition")


# ---- notebooks published for Fabric

def test_notebook_using_the_local_module_gets_a_setup_cell_and_a_python_kernel():
    cells = [{"type": "code", "source": "from fabric_desktop import lakehouse\nx = 1"},
             {"type": "code", "source": "%%sql\nSELECT * FROM sales"},
             {"type": "markdown", "source": "# Notes"}]
    nb = fabric_ipynb(cells, LH)
    assert nb["metadata"]["kernel_info"]["name"] == "jupyter"
    assert nb["metadata"]["dependencies"]["lakehouse"]["default_lakehouse"] == "lh1"
    first = "".join(nb["cells"][0]["source"])
    assert first.startswith(SHIM_MARKER)
    ast.parse(first)  # the setup cell is valid Python
    assert "".join(nb["cells"][2]["source"]) == 'lakehouse.sql("SELECT * FROM sales")'


def test_spark_notebook_is_published_as_it_is():
    cells = [{"type": "code", "source": "%%sql\nMERGE INTO sk.customer t USING x s ON t.a = s.a"}]
    nb = fabric_ipynb(cells, LH)
    assert len(nb["cells"]) == 1 and nb["metadata"]["kernelspec"]["name"] == "python3"


def test_setup_cell_runs_a_notebook_against_the_mounted_lakehouse(tmp_path, monkeypatch):
    """Run the setup cell the way Fabric would, with OneLake swapped for a folder."""
    mount = tmp_path / "lakehouse" / "default"
    (mount / "Files").mkdir(parents=True)
    (mount / "Files" / "sales.csv").write_text("region,amount\nwest,1\neast,2\nwest,3\n")
    fake_nbu = types.SimpleNamespace(credentials=types.SimpleNamespace(getToken=lambda aud: "tok"))
    monkeypatch.setitem(sys.modules, "notebookutils", fake_nbu)
    for name in ("fabric_desktop", "fabric_desktop.lakehouse"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    source = "".join(fabric_ipynb([{"type": "code", "source": "from fabric_desktop import lakehouse"}], LH)["cells"][0]["source"])
    source = source.replace('"/lakehouse/default"', repr(str(mount)))
    ns: dict = {}
    exec(compile(source, "setup", "exec"), ns)
    local = lambda uri: str(tmp_path / uri.split("/Tables/")[1])  # noqa: E731
    real_dt, real_write = ns["_DeltaTable"], ns["_write_deltalake"]
    ns["_DeltaTable"] = lambda uri, storage_options=None: real_dt(local(uri))
    ns["_write_deltalake"] = lambda uri, data, **kw: real_write(local(uri), data, mode=kw["mode"], schema_mode=kw["schema_mode"])
    from fabric_desktop import lakehouse as shim
    sales = shim.read_csv("Files/sales.csv")
    assert shim.write_table(sales.group_by("region").agg(pl.col("amount").sum()), "dbo.by_region") == 0
    assert shim.read_table("dbo.by_region").sort("region")["amount"].to_list() == [2, 4]
    assert shim.sql("SELECT sum(amount) AS total FROM by_region")["total"].to_list() == [6]
    assert shim.files() == ["Files/sales.csv"]


def test_schedule_configuration():
    daily = schedule_configuration("daily", ["06:00", "18:30"], time_zone="Central Standard Time")
    assert daily["type"] == "Daily" and daily["times"] == ["06:00", "18:30"]
    assert daily["localTimeZoneId"] == "Central Standard Time"
    assert schedule_configuration("cron", every_minutes=30)["interval"] == 30
    weekly = schedule_configuration("weekly", ["07:00"], weekdays=["Monday", "Friday"])
    assert weekly["weekdays"] == ["Monday", "Friday"]
    with pytest.raises(ValueError):
        schedule_configuration("daily", ["25:00"])
    with pytest.raises(ValueError):
        schedule_configuration("weekly", ["07:00"])


# ---- table health

def test_health_advises_compaction_and_compact_local_fixes_it(tmp_path):
    uri = str(tmp_path / "t")
    for i in range(25):
        write_deltalake(uri, pl.DataFrame({"a": [i]}).to_arrow(), mode="append")
    h = maintenance.health(uri, local=True)
    assert h["files"] == 25 and h["status"] == "needs_maintenance"
    assert h["advice"][0].startswith("25 small files")
    assert not any("V-Order" in a for a in h["advice"])
    assert maintenance.compact_local(uri)["files_removed"] == 25
    after = maintenance.health(uri, local=True)
    assert after["files"] == 1 and after["status"] == "healthy"
    assert pl.from_arrow(DeltaTable(uri).to_pyarrow_table())["a"].sort().to_list() == list(range(25))
    assert maintenance.health(str(tmp_path / "missing"))["status"] == "missing"


def test_onelake_tables_written_here_need_v_order(tmp_path):
    uri = str(tmp_path / "t")
    write_deltalake(uri, pl.DataFrame({"a": [1]}).to_arrow())
    assert any("V-Order" in a for a in maintenance.health(uri)["advice"])


# ---- profiling

def test_profile_suggests_rules_the_engine_runs():
    df = pl.read_csv(io.BytesIO(b"order_id,order_date,amount,status\n"
                                b"1,2026-09-30,50, Shipped\n2,2026-10-01,20,shipped\n3,2026-10-01,80,SHIPPED \n"
                                b"4,2026-10-01,10,shipped\n5,2026-10-01,15,returned\n6,2026-10-01,12,N/A\n"))
    out = profile(df, keys=["order_id"])
    ids = [s["id"] for s in out["suggestions"]]
    assert {"check:order_id:not_null", "check:order_id:unique", "clean:order_date:parse_date",
            "clean:status:trim", "clean:status:null_if", "clean:status:case", "check:status:accepted_values",
            "check:amount:range"} <= set(ids)
    accepted = next(s for s in out["suggestions"] if s["id"] == "check:status:accepted_values")
    assert accepted["rule"]["accepted_values"] == ["returned", "shipped"]
    status = next(c for c in out["columns"] if c["name"] == "status")
    assert status["nulls"] == 0 and status["issues"]
    # Rules already in the entity aren't suggested again.
    again = profile(df, ["order_id"], [{"kind": "clean", "column": "status", "trim": True}])
    assert "clean:status:trim" not in [s["id"] for s in again["suggestions"]]


# ---- through the service, on the sample project

class FakeFabric:
    def __init__(self):
        self.calls = []
        self.models = []

    def run_table_maintenance(self, ws, lh, table, schema, vacuum_retention=None):
        self.calls.append(("maintenance", lh, f"{schema}.{table}"))
        self.retention = vacuum_retention
        return f"job-{table}"

    def job_instance(self, ws, item, job):
        return {"status": "Completed", "endTimeUtc": "2026-10-15T02:00:00Z"}

    def get_lakehouse(self, ws, lh):
        return {"properties": {"sqlEndpointProperties": {"connectionString": "abc.datawarehouse.fabric.microsoft.com",
                                                         "id": "sql1"}}}

    def refresh_sql_endpoint(self, ws, sql_id):
        self.calls.append(("refresh", sql_id))

    def list_semantic_models(self, ws):
        return self.models

    def save_semantic_model(self, ws, name, parts, description=""):
        self.calls.append(("model", name, parts))
        self.models = [{"id": "m1", "displayName": name}]
        return {"id": "m1", "created": True}


class CloudService(Service):
    fake = None

    @property
    def fabric(self):
        return self.fake

    @property
    def fabric_jobs(self):
        return self.fake

    def _quiet_fabric(self):
        return self.fake


class FolderOneLake(OneLakeStore):
    """A OneLake environment kept in a folder, for tests."""

    def __init__(self, root: Path, env: str, lakehouses: list[str]):
        super().__init__(env, "ws-test", {n: f"id-{n}" for n in lakehouses}, lambda: "tok", root / "cache")
        self.root = root

    def uri(self, lakehouse, schema, table):
        self._lakehouse_id(lakehouse)
        return str(self.root / lakehouse / "Tables" / schema / table)

    def options(self):
        return None


@pytest.fixture
def svc(tmp_path):
    s = CloudService(root=tmp_path / "app")
    s.fake = FakeFabric()
    s.meta_init(str(tmp_path / "proj"), "3")
    assert [r["status"] for r in s.meta_run()] == ["ok"] * 6
    return s


def test_profile_preview_and_add_rules_round_trip(svc):
    prof = svc.meta_profile("silver.orders")
    ids = [s["id"] for s in prof["suggestions"]]
    assert "clean:status:trim" not in ids  # the sample already trims status
    assert prof["rows"] == 6 and prof["source"] is True
    rules = [{"column": "status", "case": "upper"}]
    preview = svc.meta_profile_preview("silver.orders", rules)
    assert preview["columns"] == ["status"] and preview["rows"]
    assert preview["rows"][0]["status (after)"] == preview["rows"][0]["status"].upper()
    check = next(s for s in prof["suggestions"] if s["kind"] == "check")
    saved = svc.meta_add_rules("silver.orders", [check["toml"]])
    assert saved["errors"] == [] and "# Added from Profile in Fabric Desktop" in saved["text"]
    assert svc.meta_profile("silver.orders", source=False)["source"] is False


def test_dev_health_and_local_optimize(svc):
    h = svc.meta_health("dev")
    assert h["local"] is True and h["optimize_after_run"] is False
    rows = {t["entity"]: t for t in h["tables"]}
    assert rows["gold.fact_sales"]["exists"] and rows["meta.run_log"]["exists"]
    done = svc.meta_optimize("dev", ["gold.fact_sales"])
    assert [(d["table"], d["status"]) for d in done] == [("dbo.fact_sales", "Completed")]


def test_onelake_run_queues_maintenance_and_builds_the_model(svc, tmp_path, monkeypatch):
    project = svc._project()
    store = FolderOneLake(tmp_path / "onelake", "test", [l.lakehouse for l in project.layers] + ["LH_Meta"])
    # Copy Dev's tables into the "test" environment, as promote would.
    for e in project.entities.values():
        lh = project.layer(e.layer).lakehouse
        df = svc._store(project, "dev").read(lh, e.schema, e.table)
        store.write(lh, e.schema, e.table, df)
    monkeypatch.setattr(svc, "_store", lambda p, env, create=False: store if env == "test" else type(svc)._store(svc, p, env, create))

    out = [{"entity": "gold.fact_sales", "status": "ok", "rows_written": 3}]
    svc._after_run(project, "test", store, out)
    assert out[0]["maintenance"] == "queued"
    assert ("maintenance", "id-LH_Gold", "dbo.fact_sales") in svc.fake.calls
    runs = svc.fabric_runs()
    assert runs[0]["kind"] == "Table maintenance" and runs[0]["status"] == "Completed"

    plan = svc.meta_model_plan("test")
    assert plan["layer"] == "gold" and plan["name"] == "SM_Gold"
    assert {"from_table": "fact_sales", "from_column": "customer_sk", "to_table": "dim_customer",
            "to_column": "customer_sk", "active": True, "why": "[[lookup]] of gold.dim_customer"} in plan["relationships"]
    built = svc.meta_build_model("test")
    assert built["id"] == "m1" and built["relationships"] == 1 and built["tables"] == ["dim_customer", "fact_sales"]
    parts = next(c for c in svc.fake.calls if c[0] == "model")[2]
    fact = parts["definition/tables/fact_sales.tmdl"]
    assert "\t\tmode: directLake" in fact and "\t\t\tschemaName: dbo" in fact
    assert "column customer_sk\n\t\tdataType: int64\n\t\tisHidden" in fact
    assert "column net_amount\n\t\tdataType: double" in fact and "summarizeBy: sum" in fact
    assert 'Sql.Database("abc.datawarehouse.fabric.microsoft.com", "sql1")' in parts["definition/expressions.tmdl"]
    assert "fromColumn: fact_sales.customer_sk\n\ttoColumn: dim_customer.customer_sk" in parts["definition/relationships.tmdl"]
    # Building again keeps every lineage tag, so reports on the model keep working.
    svc.meta_build_model("test")
    again = [c for c in svc.fake.calls if c[0] == "model"][-1][2]
    assert again == parts

    with pytest.raises(ValueError, match="Direct Lake reads OneLake"):
        svc.meta_build_model("dev")


def test_monitor_never_signs_in_to_refresh(tmp_path):
    s = Service(root=tmp_path)  # no client ID, so no sign-in is possible
    s._log_fabric_run({"kind": "Notebook run", "name": "nb", "workspace_id": "w", "item_id": "i", "job_id": "j"})
    assert s.fabric_runs()[0]["status"] == "NotStarted"


def test_quote_and_types():
    assert semantic.quote("fact sales") == "'fact sales'" and semantic.quote("ok_1") == "ok_1"
    assert semantic.tmdl_type("decimal(10,2)") == "decimal" and semantic.tmdl_type("timestamp_ntz") == "dateTime"
    assert semantic.tmdl_type("array<string>") is None
