"""Schedules on this PC (Task Scheduler) and the one list of every schedule."""

import datetime as dt
import time

import pytest

from fabric_desktop import schedule_service
from fabric_desktop.local_schedules import LocalSchedules, local_configuration, next_run, pythonw, task_xml
from fabric_desktop.service import Service


class FakeScheduler:
    available = True

    def __init__(self):
        self.tasks = {}

    def register(self, schedule, executable, workdir):
        self.tasks[schedule["id"]] = (schedule["enabled"], executable)

    def unregister(self, schedule_id):
        self.tasks.pop(schedule_id, None)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(Service, "pc_schedules", property(lambda self: LocalSchedules(self.root, fake, "C:/engine/python.exe")))
    s = Service(root=tmp_path / "app")
    s.fake_scheduler = fake
    return s


def test_next_run_for_each_kind():
    now = dt.datetime(2026, 10, 15, 7, 30)  # a Thursday
    daily = local_configuration({"kind": "daily", "times": ["06:00", "18:00"]})
    assert next_run(daily, now) == dt.datetime(2026, 10, 15, 18, 0)
    weekly = local_configuration({"kind": "weekly", "times": ["06:00"], "weekdays": ["Monday"]})
    assert next_run(weekly, now) == dt.datetime(2026, 10, 19, 6, 0)
    cron = {"type": "Cron", "interval": 45, "startDateTime": "2026-10-15T06:00:00"}
    assert next_run(cron, now) == dt.datetime(2026, 10, 15, 8, 15)
    with pytest.raises(ValueError):
        local_configuration({"kind": "daily", "times": ["25:00"]})


def test_task_definition_runs_the_sidecar_quietly():
    s = {"id": "abc", "enabled": True, "target": {"kind": "notebook", "name": "Load <sales>"},
         "config": {"type": "Weekly", "weekdays": ["Monday", "Friday"], "times": ["06:00", "18:30"],
                    "startDateTime": "2026-10-15T07:00:00"}}
    xml = task_xml(s, r"C:\engine\pythonw.exe", r"C:\Users\c\AppData\Local\FabricDesktop")
    assert xml.count("<CalendarTrigger>") == 2 and "<Monday /><Friday />" in xml
    assert "2026-10-15T18:30:00" in xml and "run-schedule abc" in xml
    assert "Load &lt;sales&gt;" in xml and "<StartWhenAvailable>true" in xml and "IgnoreNew" in xml
    cron = task_xml(s | {"config": {"type": "Cron", "interval": 30, "startDateTime": "2026-10-15T07:00:00"}}, "p", "w")
    assert "<Interval>PT30M</Interval>" in cron


def test_add_list_pause_and_remove_a_notebook_schedule(svc):
    svc.notebooks.save("Daily load", [{"type": "code", "source": "x = 1"}])
    with pytest.raises(FileNotFoundError):
        svc.add_pc_schedule({"kind": "notebook", "name": "Missing"}, {"kind": "daily", "times": ["06:00"]})
    added = svc.add_pc_schedule({"kind": "notebook", "name": "Daily load"}, {"kind": "daily", "times": ["06:00"]})
    sid = added["schedule"]["id"]
    assert svc.fake_scheduler.tasks[sid] == (True, pythonw("C:/engine/python.exe"))  # Windows writes C:\engine
    [row] = svc.pc_schedules_for("notebook", "Daily load")
    assert row["text"] == "Daily at 06:00" and row["next_run"] and row["last"] is None
    svc.set_pc_schedule_enabled(sid, False)
    assert svc.fake_scheduler.tasks[sid][0] is False
    assert svc.pc_schedules_for("notebook", "Daily load")[0]["next_run"] is None
    svc.delete_pc_schedule(sid)
    assert sid not in svc.fake_scheduler.tasks and svc.pc_schedules_for("notebook", "Daily load") == []


def test_tasks_follow_the_engine_after_an_update(tmp_path):
    fake = FakeScheduler()
    old = LocalSchedules(tmp_path, fake, "C:/engine/old/python.exe")
    old.add({"kind": "notebook", "name": "n"}, {"kind": "cron", "every_minutes": 15})
    assert LocalSchedules(tmp_path, fake, "C:/engine/old/python.exe").sync() == 0
    assert LocalSchedules(tmp_path, fake, "C:/engine/new/python.exe").sync() == 1
    assert list(fake.tasks.values()) == [(True, pythonw("C:/engine/new/python.exe"))]


def test_scheduled_notebook_runs_and_logs_its_result(svc):
    svc.notebooks.save("Good", [{"type": "markdown", "source": "# hi"}, {"type": "code", "source": "a = 2"},
                                {"type": "code", "source": "b = a * 3"}])
    svc.notebooks.save("Bad", [{"type": "code", "source": "a = 1"}, {"type": "code", "source": "1 / 0"},
                               {"type": "code", "source": "never = True"}])
    good = svc.add_pc_schedule({"kind": "notebook", "name": "Good"}, {"kind": "daily", "times": ["06:00"]})["schedule"]
    bad = svc.add_pc_schedule({"kind": "notebook", "name": "Bad"}, {"kind": "daily", "times": ["06:00"]})["schedule"]
    ok = svc.run_schedule(good["id"])
    assert ok["status"] == "ok" and ok["message"] == "2 cells ran" and ok["trigger"] == "schedule"
    failed = svc.run_schedule(bad["id"], "manual")
    assert failed["status"] == "failed" and failed["message"].startswith("Cell 2 of 3 failed: ZeroDivisionError")
    rows = {r["label"]: r for r in svc.pc_schedules.listing()}
    assert rows["Good"]["last"]["status"] == "ok" and rows["Bad"]["last"]["status"] == "failed"


def test_a_run_still_going_is_not_started_twice(svc):
    svc.notebooks.save("Slow", [{"type": "code", "source": "x = 1"}])
    sid = svc.add_pc_schedule({"kind": "notebook", "name": "Slow"}, {"kind": "daily", "times": ["06:00"]})["schedule"]["id"]
    assert svc._lock(sid) is not None
    assert svc.run_schedule(sid)["status"] == "skipped"


def test_scheduled_metadata_run_on_this_pc(svc, tmp_path):
    svc.meta_init(str(tmp_path / "proj"), "3")
    with pytest.raises(ValueError):
        svc.add_pc_schedule({"kind": "meta", "env": "dev", "entities": ["nope"]}, {"kind": "daily", "times": ["06:00"]})
    sid = svc.add_pc_schedule({"kind": "meta", "env": "dev"}, {"kind": "daily", "times": ["06:00"]})["schedule"]["id"]
    out = svc.run_schedule(sid)
    assert out["status"] == "ok" and out["tables"] == 6 and out["label"] == "Dev metadata: every table"
    assert len(svc.meta_runs("dev")) == 6


class FakeFabric:
    def __init__(self, token):
        pass

    def list_workspaces(self):
        return [{"id": "ws1", "displayName": "Sales Dev"}, {"id": "ws2", "displayName": "Other"}]

    def list_items(self, ws):
        return {"ws1": [{"id": "nb1", "type": "Notebook", "displayName": "Load"},
                        {"id": "lh1", "type": "Lakehouse", "displayName": "LH"}],
                "ws2": [{"id": "pl1", "type": "DataPipeline", "displayName": "Copy"}]}[ws]

    def list_schedules(self, ws, item, job):
        return [{"id": f"s-{item}", "enabled": True, "configuration": {"type": "Daily", "times": ["05:00"],
                                                                       "localTimeZoneId": "UTC"}}]

    def list_job_instances(self, ws, item):
        return [{"status": "Failed", "startTimeUtc": "2026-10-15T05:00:00", "failureReason": {"message": "boom"}}]


def test_one_list_has_this_pc_and_fabric(svc, monkeypatch):
    monkeypatch.setattr(schedule_service, "FabricClient", FakeFabric)
    monkeypatch.setattr(Service, "auth", property(lambda self: type("A", (), {"fabric_token": lambda _s: "tok"})()))
    svc.set_workspace_mark("ws1", "pins", True)
    first = svc.all_schedules()
    for _ in range(100):
        out = svc.all_schedules()
        if not out["fabric"]["pending"]:
            break
        time.sleep(0.02)
    [row] = out["fabric"]["items"]  # only the pinned workspace
    assert first["fabric"]["pending"] or first["fabric"]["items"]
    assert (row["name"], row["type"], row["job_type"], row["last"]["message"]) == ("Load", "Notebook", "RunNotebook", "boom")
    every = svc.all_schedules(all_workspaces=True)
    for _ in range(100):
        if not every["fabric"]["pending"]:
            break
        time.sleep(0.02)
        every = svc.all_schedules(all_workspaces=True)
    assert {r["type"] for r in every["fabric"]["items"]} == {"Notebook", "Pipeline"}
