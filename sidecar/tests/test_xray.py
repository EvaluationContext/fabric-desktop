"""Estate X-ray over local folders laid out like OneLake, and adopting a lakehouse into metadata."""

import datetime as dt
import os
from pathlib import Path

import polars as pl
import pytest
from deltalake import write_deltalake

from fabric_desktop import maintenance, xray
from fabric_desktop.service import Service
from fabric_desktop.xray_service import adopted_entity_text, guess_keys


def _lake(root: Path):
    """Two workspaces: Sales (a lakehouse with schemas, one without, one empty) and Finance."""
    lh = root / "ws-sales" / "lh-raw" / "Tables"
    for i in range(25):  # 25 tiny commits: small files
        write_deltalake(str(lh / "dbo" / "orders"), pl.DataFrame({"order_id": [i], "amount": [1.0]}).to_arrow(), mode="append")
    write_deltalake(str(lh / "dbo" / "customers"), pl.DataFrame({"customer_id": [1, 2], "name": ["a", "b"],
                                                                 "updated_at": ["2026-01-01", "2026-01-02"]}).to_arrow())
    (lh / "dbo" / "dumped_csv").mkdir(parents=True)
    (lh / "dbo" / "dumped_csv" / "part.csv").write_text("a,b\n1,2\n")
    flat = root / "ws-sales" / "lh-flat" / "Tables"
    write_deltalake(str(flat / "events"), pl.DataFrame({"id": [1]}).to_arrow())
    (root / "ws-sales" / "lh-empty" / "Tables").mkdir(parents=True)
    (root / "ws-sales" / "lh-empty" / "Files").mkdir(parents=True)
    write_deltalake(str(root / "ws-fin" / "lh-gl" / "Tables" / "dbo" / "ledger"), pl.DataFrame({"id": [1]}).to_arrow())


class FakeFabric:
    def list_workspaces(self):
        return [{"id": "ws-sales", "displayName": "Sales"}, {"id": "ws-fin", "displayName": "Finance"}]

    def list_lakehouses(self, ws):
        return {"ws-sales": [{"id": "lh-raw", "displayName": "LH_Raw"}, {"id": "lh-flat", "displayName": "LH_Flat"},
                             {"id": "lh-empty", "displayName": "LH_Empty"}],
                "ws-fin": [{"id": "lh-gl", "displayName": "LH_GL"}]}[ws]

    def list_role_assignments(self, ws):
        if ws == "ws-fin":
            raise PermissionError("not an admin")
        return [{"id": "a1", "role": "Admin", "principal": {"id": "u1", "displayName": "Chris", "type": "User",
                                                            "userDetails": {"userPrincipalName": "chris@kratosbi.com"}}},
                {"id": "a2", "role": "Viewer", "principal": {"id": "g1", "displayName": "Pat", "type": "User",
                                                             "userDetails": {"userPrincipalName": "pat_x.com#EXT#@kratosbi.com"}}},
                {"id": "a3", "role": "Admin", "principal": {"id": "sp1", "displayName": "etl-app", "type": "ServicePrincipal"}}]


def _files(root: Path):
    def files(ws, lh, directory):
        p = root / ws / lh / directory
        if not p.exists():
            return []
        return [{"name": c.name, "is_dir": c.is_dir()} for c in sorted(p.iterdir())]
    return files


def _health(root: Path):
    def health(ws, lh, schema, table):
        return maintenance.health(str(root / ws / lh / "Tables" / (f"{schema}/{table}" if schema else table)))
    return health


def test_scan_finds_what_needs_attention(tmp_path):
    _lake(tmp_path)
    seen = []
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=45)
    out = xray.Scanner(FakeFabric(), _files(tmp_path), _health(tmp_path), seen.append).scan(now=later)
    assert (out["workspaces"], out["lakehouses"], out["tables"]) == (2, 4, 5)
    kinds = [(f["kind"], f.get("table") or f.get("lakehouse_name") or f.get("person")) for f in out["findings"]]
    assert kinds[0] == ("small_files", "orders")  # most urgent first
    assert ("unidentified", "dumped_csv") in kinds and ("empty", "LH_Empty") in kinds
    assert ("stale", "ledger") in kinds and ("stale", "events") in kinds  # 45 days later
    access = [f["title"] for f in out["findings"] if f["kind"] == "access"]
    assert access == ["App etl-app is Admin in 1 workspace", "Guest Pat has access to 1 workspace"]
    raw = next(r for r in out["lakehouse_rows"] if r["lakehouse_name"] == "LH_Raw")
    assert raw["tables"] == 3 and raw["issues"] >= 3
    assert {p["phase"] for p in seen} >= {"Finding lakehouses", "Reading table logs"}


def test_cost_findings_point_at_local_notebooks():
    report = {"days": 30, "by_item": [
        {"kind": "Notebook run", "name": "Daily load", "env": "", "cost": 42.5, "fabric_runs": 30, "cu_hours": 236.0},
        {"kind": "Notebook run", "name": "Elsewhere", "env": "", "cost": 1.2, "fabric_runs": 3, "cu_hours": 6.6},
        {"kind": "Metadata run", "name": "silver.x", "env": "prod", "cost": 9.0, "fabric_runs": 3, "cu_hours": 50.0}]}
    out = xray.cost_findings(report, {"Daily load"})
    assert [(f["notebook"], f["action"], f["severity"]) for f in out] == [("Daily load", "run_local", "medium"),
                                                                        ("Elsewhere", "cost", "low")]


def test_adopted_entity_loads_the_original_table(tmp_path, monkeypatch):
    source = tmp_path / "lake" / "Tables" / "dbo" / "customers"
    write_deltalake(str(source), pl.DataFrame({"CustomerID": [1, 2], "Name": ["a", "b"],
                                               "ModifiedDate": ["2026-01-01", "2026-01-02"]}).to_arrow())
    svc = Service(root=tmp_path / "app")
    svc.meta_init(str(tmp_path / "proj"), "3")
    project = svc._project()
    schema = pl.read_delta(str(source)).schema
    assert guess_keys(list(schema)) == ["CustomerID"]
    text = adopted_entity_text("bronze.crm_customers", "chris", str(source), "Sales / LH_Raw / dbo.customers",
                               dict(schema), ["CustomerID"])
    (project.entities_dir / "bronze.crm_customers.toml").write_text(text, encoding="utf-8")
    assert svc.meta_project()["errors"] == []
    [res] = svc.meta_run(["bronze.crm_customers"])
    assert res["status"] == "ok" and res["rows_written"] == 2
    assert svc.meta_entity("bronze.crm_customers")["source"] == "Sales / LH_Raw / dbo.customers"
    # New rows only, by the timestamp it found.
    write_deltalake(str(source), pl.DataFrame({"CustomerID": [3], "Name": ["c"], "ModifiedDate": ["2026-02-01"]}).to_arrow(),
                    mode="append")
    [again] = svc.meta_run(["bronze.crm_customers"])
    assert again["status"] == "ok" and again["rows_read"] == 1


def test_adopt_writes_entities_and_skips_what_exists(tmp_path, monkeypatch):
    _lake(tmp_path / "lake")
    svc = Service(root=tmp_path / "app")
    svc.meta_init(str(tmp_path / "proj"), "3")
    lake = tmp_path / "lake"

    def source_table(self, ws, lh, schema, table):
        from deltalake import DeltaTable
        uri = str(lake / ws / lh / "Tables" / (f"{schema}/{table}" if schema else table))
        return uri, DeltaTable(uri)

    monkeypatch.setattr(Service, "_source_table", source_table)
    monkeypatch.setattr(Service, "auth", property(lambda self: type("A", (), {
        "signed_in_user": lambda _s: "chris@kratosbi.com", "storage_token": lambda _s: "tok"})()))
    import fabric_desktop.xray_service as xs
    monkeypatch.setattr(xs, "FileStore", lambda _t: type("F", (), {"list": staticmethod(_files(lake))})())
    plan = svc.xray_adopt_plan("ws-sales", "lh-raw", "LH_Raw", "Sales")
    by = {t["table"]: t for t in plan["tables"]}
    assert plan["layer"] == "bronze" and by["orders"]["keys"] == ["order_id"] and by["orders"]["name"] == "bronze.orders"
    assert by["orders"]["exists"]  # the sample project already has bronze.orders
    assert not by["dumped_csv"]["ok"]
    out = svc.xray_adopt("ws-sales", "lh-raw", "LH_Raw", "Sales",
                         [{"schema": "dbo", "table": "orders", "name": "bronze.raw_orders"},
                          {"schema": "dbo", "table": "customers"}])
    assert out["adopted"] == ["bronze.raw_orders"] and out["problems"] == ["bronze.customers already exists"]
    p = svc.meta_project()
    assert p["errors"] == [] and "bronze.raw_orders" in {e["name"] for e in p["entities"]}
    again = {t["table"]: t for t in svc.xray_adopt_plan("ws-sales", "lh-raw", "LH_Raw", "Sales")["tables"]}
    assert again["orders"]["why"] == "Already adopted as bronze.raw_orders"


def test_scan_waits_while_paused(tmp_path):
    import threading
    _lake(tmp_path)
    gate = threading.Event()  # starts paused
    out = {}
    worker = threading.Thread(target=lambda: out.update(xray.Scanner(FakeFabric(), _files(tmp_path), _health(tmp_path),
                                                                     gate=gate).scan()))
    worker.start()
    worker.join(0.5)
    assert worker.is_alive() and not out  # paused before the first workspace
    gate.set()
    worker.join(30)
    assert out["tables"] == 5


def test_home_scans_by_itself_only_once(tmp_path, monkeypatch):
    svc = Service(root=tmp_path / "app")
    started = []
    monkeypatch.setattr(svc, "_xray_run", lambda *a: started.append(a))

    class Auth:
        def fabric_token(self):
            return "f"

        def storage_token(self):
            return "s"
    monkeypatch.setattr(Service, "auth", property(lambda self: Auth()), raising=False)
    assert svc.xray(auto=True)["running"] is True
    svc._xray_state = {"running": False, "error": "RuntimeError: offline"}  # the scan failed: nothing saved
    assert svc.xray(auto=True)["running"] is False  # a second Home visit doesn't scan again
    assert len(started) == 1
    assert svc.xray(refresh=True)["running"] is True  # the button does
    assert svc.xray_pause(True)["paused"] is True
    assert not svc._xray_gate.is_set()
    assert svc.xray_pause(False)["paused"] is False and svc._xray_gate.is_set()
