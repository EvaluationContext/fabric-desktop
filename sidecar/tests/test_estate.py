"""Restore and retention, cost, shortcuts, access review, tests that gate promote, VL_Sources."""

import datetime as dt
import json

import polars as pl
import pytest
from deltalake import DeltaTable, write_deltalake

from fabric_desktop import access, costs, maintenance, shortcuts
from fabric_desktop.meta import GateError, restore, varlib
from fabric_desktop.meta.project import Layer
from fabric_desktop.notebookutils_local import Host, build
from fabric_desktop.service import Service
from test_fabric_api import Resp, client


@pytest.fixture
def svc(tmp_path):
    s = Service(root=tmp_path / "app")
    s.meta_init(str(tmp_path / "proj"), "3")
    assert [r["status"] for r in s.meta_run()] == ["ok"] * 6
    return s


# ---- restore and retention

def test_restore_a_bad_load_and_move_the_watermark_back(svc):
    project = svc._project()
    store = svc._store(project, "dev")
    good = store.read("LH_Silver", "dbo", "customers")
    hist = svc.meta_table_history("silver.customers")
    assert hist["retention_days"] == 7 and hist["incremental"] and hist["versions"][0]["current"]
    good_version = hist["versions"][0]["version"]

    # A bad load: one row's watermark jumps far ahead and the rest disappear.
    bad = good.head(1).with_columns(pl.lit("2099-01-01").alias("updated_at")) if good.schema["updated_at"] == pl.String \
        else good.head(1).with_columns(pl.col("updated_at").max() + pl.duration(days=30000))
    store.write("LH_Silver", "dbo", "customers", bad, mode="overwrite")
    store.meta_append("watermarks", [{"entity": "silver.customers", "value": "2099-01-01", "run_id": "bad"}])

    versions = svc.meta_table_history("silver.customers")["versions"]
    target = next(v for v in versions if v["version"] == good_version)
    assert target["restorable"] and not target["current"]

    out = svc.meta_restore("silver.customers", "dev", good_version, reason="bad export")
    assert out["to_version"] == good_version and out["new_version"] == good_version + 2
    assert "gold.dim_customer" in out["downstream"]
    assert store.read("LH_Silver", "dbo", "customers").height == good.height
    wm = store.meta("watermarks").filter(pl.col("entity") == "silver.customers")["value"][-1]
    assert wm == out["watermark"] and not wm.startswith("2099")
    again = svc.meta_table_history("silver.customers")
    assert again["restores"][0]["reason"] == "bad export" and again["restores"][0]["to_version"] == good_version
    assert again["versions"][0]["restored_to"] == str(good_version)
    with pytest.raises(ValueError):
        svc.meta_restore("silver.customers", "dev", again["versions"][0]["version"])


def test_versions_vacuumed_away_cannot_be_restored(tmp_path):
    uri = str(tmp_path / "t")
    for i in range(3):
        write_deltalake(uri, pl.DataFrame({"a": [i]}).to_arrow(), mode="overwrite")
    table = DeltaTable(uri)
    table.vacuum(retention_hours=0, dry_run=False, enforce_retention_duration=False)
    rows = {v["version"]: v for v in restore.versions(uri, None, 7)}
    assert rows[4]["current"] and not rows[4]["restorable"]  # 3 and 4 are VACUUM's own commits
    assert rows[2]["restorable"]  # the files it reads are still there
    assert not rows[0]["restorable"] and "VACUUM" in rows[0]["why_not"]


def test_layer_retention_is_saved_checked_and_used_by_maintenance(svc, tmp_path):
    layers = [l.to_dict() for l in svc._project().layers]
    layers[1]["retention_days"] = 30
    svc.meta_set_layers(layers)
    project = svc._project()
    assert project.layer("silver").retention_days == 30 and project.layer("bronze").retention_days == 7
    assert "retention_days = 30" in (project.root / "layers.toml").read_text()
    layers[1]["retention_days"] = 3
    with pytest.raises(GateError, match="at least 7"):
        svc.meta_set_layers(layers)

    svc.meta_optimize("dev", ["silver.customers"])
    uri = svc._store(project, "dev").uri("LH_Silver", "dbo", "customers")
    config = DeltaTable(uri).metadata().configuration
    assert config["delta.deletedFileRetentionDuration"] == "interval 30 days"
    assert config["delta.logRetentionDuration"] == "interval 30 days"
    assert maintenance.fabric_retention(30) == "30.01:00:00"
    assert not maintenance.set_retention(uri, 30)  # already set: no new commit


# ---- cost

def test_cost_estimates_fabric_runs_and_what_local_runs_saved():
    now = dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc)
    cfg = costs.settings({"price_per_cu_hour": 0.2})
    meta = [{"entity": "silver.orders", "env": "dev", "compute": "local", "seconds": 3600, "started_at": "2026-10-14T10:00:00+00:00"},
            {"entity": "silver.orders", "env": "prod", "compute": "fabric", "seconds": 1800, "started_at": "2026-10-14T11:00:00+00:00"},
            {"entity": "old", "env": "prod", "compute": "fabric", "seconds": 1800, "started_at": "2026-08-01T11:00:00+00:00"}]
    fabric = [{"kind": "Table maintenance", "table": "dbo.orders", "env": "prod", "started_at": "2026-10-14T12:00:00Z",
               "finished_at": "2026-10-14T12:15:00Z"},
              {"kind": "Notebook run", "name": "nb", "engine": "python", "started_at": "2026-10-14T13:00:00Z",
               "finished_at": "2026-10-14T13:30:00Z"},
              {"kind": "Metadata run", "name": "runner", "started_at": "2026-10-14T11:00:00Z", "finished_at": "2026-10-14T11:40:00Z"},
              {"kind": "Notebook run", "name": "still running", "started_at": "2026-10-14T13:00:00Z", "finished_at": None}]
    rows = costs.activities(meta, fabric, [], cfg)
    out = costs.report(rows, cfg, 30, now, {"prod": {"name": "Cap", "sku": "F2"}})
    t = out["total"]
    # Fabric: 0.5 h x 1 CU + 0.25 h x 8 CU + 0.5 h x 1 CU = 3 CU hours; the runner job isn't counted twice.
    assert t["cu_hours"] == 3.0 and t["cost"] == 0.6
    assert t["saved_cu_hours"] == 1.0 and t["saved"] == 0.2 and t["runs"] == 4
    prod = next(e for e in out["by_env"] if e["env"] == "prod")
    assert prod["sku"] == "F2" and prod["capacity_share"] == pytest.approx(2.5 / (2 * 30 * 24) * 100, abs=1e-3)
    assert out["by_item"][0]["name"] == "dbo.orders"
    assert costs.sku_cu("f64") == 64


def test_cost_report_through_the_service(svc):
    svc.set_cost_settings(price_per_cu_hour=0.5, currency="eur")
    out = svc.cost_report(7)
    assert out["settings"]["price_per_cu_hour"] == 0.5 and out["settings"]["currency"] == "EUR"
    assert out["total"]["local_runs"] == 6 and out["total"]["cost"] == 0
    with pytest.raises(ValueError):
        svc.set_cost_settings(spark_cu=0)


# ---- shortcuts

def test_shortcut_targets_and_descriptions():
    one = shortcuts.target({"type": "oneLake", "workspace_id": "w", "item_id": "i", "path": "/Tables/dbo/orders/"})
    assert one == {"oneLake": {"workspaceId": "w", "itemId": "i", "path": "Tables/dbo/orders"}}
    adls = shortcuts.target({"type": "adlsGen2", "location": "https://acct.dfs.core.windows.net/", "subpath": "raw/x",
                             "connection_id": "c1"})
    assert adls["adlsGen2"] == {"location": "https://acct.dfs.core.windows.net", "subpath": "/raw/x", "connectionId": "c1"}
    with pytest.raises(ValueError):
        shortcuts.target({"type": "adlsGen2", "location": "acct", "connection_id": "c1"})
    assert shortcuts.location("Tables") == "Tables/dbo" and shortcuts.location("Files/landing") == "Files/landing"
    with pytest.raises(ValueError):
        shortcuts.location("Tables/dbo/x")
    d = shortcuts.describe({"name": "orders", "path": "Files/landing",
                            "target": {"type": "AdlsGen2", "adlsGen2": {"location": "https://a", "subpath": "/raw", "connectionId": "c"}}})
    assert d["kind"] == "folder" and d["target_type"] == "ADLS Gen2" and d["target"] == "https://a/raw"
    managed = [{"name": "bronze.orders", "files": "landing/orders/*.csv", "schema": "dbo", "table": "orders"}]
    assert shortcuts.used_by(d, managed)[0]["entity"] == "bronze.orders"
    clash = shortcuts.describe({"name": "orders", "path": "Tables/dbo", "target": {"oneLake": {"path": "Tables/dbo/o"}}})
    assert "same name" in shortcuts.used_by(clash, managed)[0]["how"]


def test_shortcut_rest_calls():
    c, s = client([Resp(201, {"name": "x"}), Resp(200, {})])
    c.create_shortcut("ws", "lh", "Tables/dbo", "x", {"oneLake": {"workspaceId": "w", "itemId": "i", "path": "Tables/dbo/x"}})
    assert s.calls[0][0] == "POST" and s.calls[0][1].endswith("/workspaces/ws/items/lh/shortcuts")
    assert s.calls[0][2] == {"path": "Tables/dbo", "name": "x", "target": {"oneLake": {"workspaceId": "w", "itemId": "i", "path": "Tables/dbo/x"}}}
    c.delete_shortcut("ws", "lh", "Files/landing/", "orders")
    assert s.calls[1][:2] == ("DELETE", "https://api.fabric.microsoft.com/v1/workspaces/ws/items/lh/shortcuts/Files/landing/orders")


# ---- access

def _assignment(pid, name, role, kind="User", upn=None):
    p = {"id": pid, "displayName": name, "type": kind}
    if kind == "User":
        p["userDetails"] = {"userPrincipalName": upn or f"{name.lower()}@contoso.com"}
    return {"id": f"a-{pid}", "principal": p, "role": role}


def test_access_review_flags_what_needs_a_second_look():
    out = access.review([
        {"key": "dev", "label": "Dev", "env": "dev", "workspace_id": "d",
         "assignments": [_assignment("1", "Chris", "Admin"), _assignment("3", "Eng", "Member", "Group")]},
        {"key": "prod", "label": "Prod", "env": "prod", "workspace_id": "p",
         "assignments": [_assignment("1", "Chris", "Admin"), _assignment("2", "Pat", "Contributor"),
                         _assignment("4", "Guest", "Viewer", upn="g_x.com#EXT#@contoso.com"),
                         _assignment("3", "Eng", "Member", "Group")]},
        {"key": "test", "label": "Test", "env": "test", "workspace_id": "t", "error": "403"}])
    rows = {r["name"]: r for r in out["people"]}
    assert rows["Chris"]["roles"] == {"dev": {"role": "Admin", "assignment_id": "a-1"}, "prod": {"role": "Admin", "assignment_id": "a-1"}}
    assert any("Can change Prod directly" in f for f in rows["Chris"]["flags"])
    assert "Can change Prod but has no access to Dev" in rows["Pat"]["flags"]
    assert rows["Guest"]["flags"] == ["Guest account from another organization"]
    assert rows["Eng"]["flags"] == [] and out["flagged"] == 3
    assert out["workspaces"][2]["error"] == "403"


def test_data_access_roles_and_who_can_read():
    role = access.make_role("SalesReaders", ["Tables/dbo"], ["oid-9"], "tenant")
    assert role["decisionRules"][0]["permission"][0]["attributeValueIncludedIn"] == ["/Tables/dbo"]
    assert role["members"]["microsoftEntraMembers"] == [{"tenantId": "tenant", "objectId": "oid-9"}]
    default = {"name": "DefaultReader", "decisionRules": [{"effect": "Permit", "permission": [
        {"attributeName": "Path", "attributeValueIncludedIn": ["*"]}, {"attributeName": "Action", "attributeValueIncludedIn": ["Read"]}]}],
        "members": {"fabricItemMembers": [{"itemAccess": ["ReadAll"], "sourcePath": "w/i"}]}}
    kept = access.make_role("DefaultReader", ["*"], [], "tenant", keep=default)
    assert kept["members"]["fabricItemMembers"] == default["members"]["fabricItemMembers"]
    with pytest.raises(ValueError):
        access.make_role("bad name", ["*"], ["x"], "t")
    with pytest.raises(ValueError):
        access.make_role("Empty", ["*"], [], "t")
    who = access.who_can_read("/Tables/dbo/orders", [_assignment("1", "Chris", "Admin"), _assignment("9", "Ann", "Viewer")],
                              [role, access.make_role("Other", ["/Tables/sales"], ["oid-8"], "t")])
    assert [(w["name"], w["how"]) for w in who] == [("oid-9", "Data access role SalesReaders"), ("Chris", "Workspace Admin")]


def test_access_review_uses_the_environment_workspaces(svc, monkeypatch):
    svc.meta_set_environment("test", "ws-test", "Sales Test")
    svc.meta_set_keys_workspace("test", "ws-keys", "Keys Test")

    class Fake:
        def list_role_assignments(self, ws):
            return [_assignment("1", "Chris", "Admin")]
    monkeypatch.setattr(type(svc), "fabric", property(lambda self: Fake()))
    out = svc.access_review()
    assert [w["key"] for w in out["workspaces"]] == ["test", "test-keys"]
    assert out["people"][0]["roles"]["test-keys"]["role"] == "Admin"


# ---- tests that gate promote

def test_failing_project_test_blocks_promote(svc):
    root = svc._project().root
    result = svc.meta_tests(run=True)
    assert result["passed"] == 1 and result["failed"] == 0 and result["tests"][0]["id"] == "tests/test_common.py::test_round_money"
    svc.meta_set_environment("test", target="local")
    assert svc.meta_promote_check("dev", "test")["errors"] == []

    (root / "tests" / "test_more.py").write_text(
        "def test_math():\n    assert 1 + 1 == 3, 'math is off'\n\n"
        "def test_folder(tmp_path):\n    (tmp_path / 'x').write_text('y')\n\n"
        "def test_fixture(capsys):\n    pass\n", encoding="utf-8")
    check = svc.meta_promote_check("dev", "test")
    assert check["tests"]["failed"] == 2
    assert any("test_more.py::test_math failed: AssertionError: math is off" in e for e in check["errors"])
    assert any("test_fixture errored" in e and "capsys" in e for e in check["errors"])
    with pytest.raises(GateError):
        svc.meta_promote("dev", "test")
    saved = svc.meta_tests()
    assert saved["failed"] == 2 and saved["stale"] is False
    assert {t["name"]: t["status"] for t in saved["tests"]}["test_folder"] == "passed"

    (root / "tests" / "test_more.py").write_text("def test_math():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    assert svc.meta_tests()["stale"] is True
    assert svc.meta_promote("dev", "test")["env"] == "test"


def test_hook_tests_are_linked_to_the_code_that_names_them(svc):
    text = svc.meta_entity("silver.orders")["text"]
    hook = '\n[hooks]\nafter_clean = "transforms.common:noop"\nowner = "me"\ntest = "tests/test_common.py::test_noop"\n'
    root = svc._project().root
    (root / "transforms" / "common.py").write_text(
        (root / "transforms" / "common.py").read_text() + "\n\ndef noop(df, ctx):\n    return df\n")
    (root / "tests" / "test_common.py").write_text(
        (root / "tests" / "test_common.py").read_text() + "\n\ndef test_noop():\n    assert True\n")
    svc.meta_save_entity("silver.orders", text + hook)
    out = svc.meta_tests(run=True)
    noop = next(t for t in out["tests"] if t["name"] == "test_noop")
    assert noop["status"] == "passed" and noop["used_by"] == ["silver.orders hook after_clean transforms.common:noop"]
    assert out["missing"] == []


# ---- VL_Sources

def test_variable_library_definition_has_a_value_set_per_environment(svc):
    project = svc._project()
    sources = project.source_systems
    sources[0].connections["prod"] = {"connection_type": "Files", "file_path": "prod/customers"}
    from fabric_desktop.meta import keys
    keys.write_source_systems(project.root, sources)
    project.reload()
    rows = {r["name"]: r for r in varlib.values(project)}
    assert rows["CRM_file_path"] == {"name": "CRM_file_path", "note": "Customer export: file path", "dev": "landing/customers",
                                     "test": "", "prod": "prod/customers"}
    assert rows["ENVIRONMENT"]["prod"] == "prod"
    parts = varlib.definition(project)
    variables = json.loads(parts["variables.json"])["variables"]
    assert {"name": "CRM_file_path", "note": "Customer export: file path", "type": "String", "value": "landing/customers"} in variables
    prod = json.loads(parts["valueSets/Prod.json"])
    assert {"name": "CRM_file_path", "value": "prod/customers"} in prod["variableOverrides"]
    assert json.loads(parts["settings.json"])["valueSetsOrder"] == ["Test", "Prod"]
    assert varlib.resolve(project, "prod")["CRM_file_path"] == "prod/customers"


def test_publish_variable_library_and_read_it_in_a_notebook(svc, monkeypatch):
    svc.meta_set_environment("test", "ws-test", "Sales Test")
    calls = []

    class Fake:
        def save_variable_library(self, ws, name, parts, description=""):
            calls.append(("save", ws, name, sorted(parts)))
            return {"id": "vl1", "created": True}

        def set_active_value_set(self, ws, lib, value_set):
            calls.append(("active", ws, lib, value_set))
    monkeypatch.setattr(type(svc), "fabric", property(lambda self: Fake()))
    out = svc.meta_publish_varlib("test")
    assert out["created"] and out["value_set"] == "Test" and "variablelibraries/vl1" in out["url"]
    assert calls == [("save", "ws-test", "VL_Sources", ["settings.json", "valueSets/Prod.json", "valueSets/Test.json", "variables.json"]),
                     ("active", "ws-test", "vl1", "Test")]
    assert "test" in svc.meta_varlib()["published"]

    nbu = build(Host(variables=svc._notebook_variables))
    lib = nbu.variableLibrary.getLibrary("VL_Sources")
    assert lib.CRM_file_path == "landing/customers" and lib.ENVIRONMENT == "dev"
    assert nbu.variableLibrary.get("$(/**/VL_Sources/SHOP_file_path)") == "landing/orders"
    with pytest.raises(LookupError):
        nbu.variableLibrary.getLibrary("VL_Other")
    with pytest.raises(AttributeError):
        lib.NOPE


def test_variable_library_rest_calls():
    c, s = client([Resp(200, {"value": [{"id": "v1", "displayName": "VL_Sources"}]}), Resp(200, {}), Resp(200, {})])
    out = c.save_variable_library("ws", "VL_Sources", {"variables.json": "{}"})
    assert out["created"] is False and s.calls[1][1].endswith("/variableLibraries/v1/updateDefinition")
    c.set_active_value_set("ws", "v1", "Prod")
    assert s.calls[2] == ("PATCH", "https://api.fabric.microsoft.com/v1/workspaces/ws/variableLibraries/v1",
                          {"properties": {"activeValueSetName": "Prod"}})


def test_layer_default_retention():
    assert Layer("x").retention_days == 7 and Layer("x").to_dict()["retention_days"] == 7
