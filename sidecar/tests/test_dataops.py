"""Compare before promote, quarantine replay and freshness promises."""

import datetime as dt
import shutil
import time

import polars as pl
import pytest

from fabric_desktop import dataops_service
from fabric_desktop.meta import freshness
from fabric_desktop.meta.project import validate
from fabric_desktop.service import Service


@pytest.fixture
def svc(tmp_path):
    s = Service(root=tmp_path / "app")
    s.meta_init(str(tmp_path / "proj"), "3")
    assert [r["status"] for r in s.meta_run()] == ["ok"] * 6
    return s


def edit_entity(svc, name, old, new):
    text = svc.meta_entity(name)["text"]
    assert old in text
    return svc.meta_save_entity(name, text.replace(old, new, 1))


# ---- compare

def test_compare_dev_with_test_shows_what_a_logic_change_does(svc):
    svc.meta_set_environment("test", target="local")
    svc.meta_promote("dev", "test")
    data = svc._project().local_data  # Test reads the same landing files as Dev
    shutil.copytree(data / "dev" / "LH_Bronze" / "Files", data / "test" / "LH_Bronze" / "Files")
    assert all(r["status"] == "ok" for r in svc.meta_run(env="test"))
    assert {t["state"] for t in svc.meta_promote_impact("dev", "test")["tables"]} == {"same"}

    edit_entity(svc, "silver.orders", 'expr = "amount - coalesce(discount, 0)"', 'expr = "amount"')
    impact = {t["entity"]: t["state"] for t in svc.meta_promote_impact("dev", "test")["tables"]}
    assert impact["silver.orders"] == "changed" and impact["silver.customers"] == "same"
    svc.meta_run(["silver.orders"], backfill="full")

    out = svc.meta_compare("silver.orders", "test", "dev")
    assert out["compared_on_keys"] and out["rows"]["left"] == out["rows"]["right"]
    assert set(out["changed"]["by_column"]) == {"net_amount"}  # surrogate keys and run stamps are left out
    assert out["only_left"]["count"] == out["only_right"]["count"] == 0
    change = out["changed"]["rows"][0]["changes"]
    assert change[0]["column"] == "net_amount" and change[0]["left"] != change[0]["right"]
    totals = {t["column"]: t for t in out["totals"]}
    assert totals["net_amount"]["diff"] > 0 and totals["amount"]["diff"] == 0
    assert "order_id" not in totals and "customer_id" not in totals


def test_compare_with_an_earlier_version_of_the_same_table(svc):
    before = svc.meta_versions("silver.customers")[0]["version"]
    proj = svc._project()
    store = svc._store(proj, "dev")
    df = store.read("LH_Silver", "dbo", "customers")
    store.write("LH_Silver", "dbo", "customers", df.filter(pl.col("customer_id") != 1), mode="overwrite")
    out = svc.meta_compare("silver.customers", "dev", "dev", left_version=before)
    assert out["only_left"]["count"] == 1 and out["only_left"]["rows"][0]["customer_id"] == "1"
    assert out["rows"]["left"] - out["rows"]["right"] == 1


# ---- quarantine replay

def test_fix_and_replay_a_quarantined_row(svc):
    q = svc.meta_quarantine("silver.orders")
    assert q["total"] == 1 and q["checks"] == {"returns_are_negative": 1}
    assert "gold.fact_sales" in q["downstream"] and "amount" in q["editable"]
    row = q["rows"][0]
    assert row["order_id"] == "105"

    # Replayed as it is, the row fails the same check again and stays held.
    again = svc.meta_replay("silver.orders", "dev", [row["_qid"]])
    assert again["status"] == "ok" and again["held"] == 1 and again["rows_written"] == 0
    held = svc.meta_quarantine("silver.orders")["rows"][0]
    assert held["order_id"] == "105" and held["_qid"] != row["_qid"]

    # Fixed by hand (a return is a negative amount), it loads, and net_amount is worked out again.
    amount = -abs(float(held["amount"]))
    res = svc.meta_replay("silver.orders", "dev", [held["_qid"]], {held["_qid"]: {"amount": str(amount)}})
    assert res["status"] == "ok" and res["rows_written"] == 1 and res["held"] == 0
    assert svc.meta_quarantine("silver.orders")["total"] == 0
    proj = svc._project()
    orders = svc._store(proj, "dev").read("LH_Silver", "dbo", "orders")
    r105 = orders.filter(pl.col("order_id") == 105)
    assert r105.height == 1 and r105["amount"].item() == amount
    assert r105["net_amount"].item() == pytest.approx(amount - (r105["discount"].item() or 0))
    assert r105["order_sk"].item() > 0  # the key it was given before it was held
    log = svc.meta_runs("dev")[0]
    assert log["backfill"] == "replay" and log["entity"] == "silver.orders"
    # The watermark stays where the normal runs left it.
    wm = svc._store(proj, "dev").meta("watermarks").filter(pl.col("entity") == "silver.orders")
    assert wm["run_id"][-1] != log["run_id"]


def test_discard_quarantined_rows(svc):
    q = svc.meta_quarantine("silver.orders")
    assert svc.meta_discard_quarantine("silver.orders", "dev", [q["rows"][0]["_qid"]])["total"] == 0


def test_replay_of_a_full_reload_table_adds_rows_without_replacing_the_table(svc):
    edit_entity(svc, "bronze.customers", "[contract]\n",
                '[[check]]\nname = "no_ada"\nexpr = "customer_id <> 1"\non_fail = "quarantine"\n\n[contract]\n')
    assert svc.meta_entity("bronze.customers")["mode"] == "full"
    assert svc.meta_run(["bronze.customers"])[0]["quarantined"] == 1
    proj = svc._project()
    before = svc._store(proj, "dev").read("LH_Bronze", "dbo", "customers").height
    q = svc.meta_quarantine("bronze.customers")
    assert q["total"] == 1
    edit_entity(svc, "bronze.customers", 'expr = "customer_id <> 1"', 'expr = "customer_id <> 999"')
    res = svc.meta_replay("bronze.customers", "dev", [q["rows"][0]["_qid"]])
    assert res["status"] == "ok" and res["rows_written"] == 1
    assert svc._store(proj, "dev").read("LH_Bronze", "dbo", "customers").height == before + 1


# ---- freshness

NOW = dt.datetime(2026, 10, 15, 9, 0).astimezone()


def run(hours_ago, status="ok", run_id="r1"):
    t = (NOW - dt.timedelta(hours=hours_ago)).astimezone(dt.timezone.utc)
    return {"status": status, "started_at": t.isoformat(), "seconds": 0, "run_id": run_id, "message": "boom"}


def test_freshness_within_hours():
    p = freshness.promise({"freshness": {"within_hours": 24}})
    assert p["envs"] == ["test", "prod"]
    assert freshness.status(p, [run(2)], NOW)["status"] == "fresh"
    late = freshness.status(p, [run(30)], NOW)
    assert late["status"] == "late" and "30 h ago" in late["reasons"][0]
    assert freshness.status(p, [], NOW)["status"] == "never"
    failed = freshness.status(p, [run(1, "failed", "r2"), run(3)], NOW)
    assert failed["status"] == "fresh" and failed["last_failed"] and failed["failed_run"] == "r2"


def test_freshness_by_a_time_of_day():
    p = freshness.promise({"freshness": {"by": "07:00"}})
    assert freshness.status(p, [run(1)], NOW)["status"] == "fresh"  # loaded at 08:00 today
    assert freshness.status(p, [run(12)], NOW)["status"] == "late"  # last loaded yesterday evening
    early = NOW.replace(hour=6)
    assert freshness.status(p, [run(9)], early)["status"] == "fresh"  # not 07:00 yet


def test_freshness_is_checked_by_the_save_gate_and_saved_from_the_app(svc):
    res = svc.meta_set_freshness("gold.fact_sales", within_hours=26, by="07:00", envs=["dev", "prod"])
    assert res["errors"] == [] and res["freshness"] == {"within_hours": 26, "by": "07:00", "envs": ["dev", "prod"]}
    assert "[freshness]" in res["text"]
    items = svc.meta_freshness()["items"]
    assert [(x["entity"], x["env"]) for x in items] == [("gold.fact_sales", "dev")]
    cleared = svc.meta_set_freshness("gold.fact_sales")
    assert "[freshness]" not in cleared["text"] and cleared["freshness"] is None

    bad = edit_entity(svc, "gold.fact_sales", 'name = "gold.fact_sales"', 'name = "gold.fact_sales"\n\n[freshness]\nby = "7am"\nenvs = ["qa"]')
    assert any("freshness by" in e for e in bad["errors"]) and any("freshness envs" in e for e in bad["errors"])


def test_late_tables_are_posted_to_teams_once(svc, monkeypatch):
    posts = []
    monkeypatch.setattr(dataops_service, "post_teams", lambda url, title, lines: posts.append((title, lines)))
    svc.meta_set_alerts("https://example.webhook.office.com/x")
    svc.meta_set_freshness("gold.fact_sales", within_hours=0.0001, envs=["dev"])
    time.sleep(0.5)  # 0.0001 h is 0.36 s
    out = svc.meta_freshness(notify=True)
    assert out["items"][0]["status"] == "late" and out["alerted"] == 1
    assert "gold.fact_sales" in posts[0][1][0]
    assert svc.meta_freshness(notify=True)["alerted"] == 0  # already told
    with pytest.raises(ValueError):
        svc.meta_set_alerts("http://not-secure")


def test_teams_card_shape():
    card = dataops_service.teams_card("t", ["a"])
    content = card["attachments"][0]["content"]
    assert content["type"] == "AdaptiveCard" and content["body"][1]["text"] == "a"
