"""Schedules that run on this PC: a notebook, or a metadata environment's tables.

Windows Task Scheduler starts each run, so a schedule fires with the app closed, as long as
you are signed in to Windows. The task runs this sidecar's own ``pythonw.exe -m
fabric_desktop.cli run-schedule <id>``; the app re-points every task at the current engine
when it starts (an update installs a new engine folder). Each run is logged to
``schedule_runs.jsonl`` so Monitor can show the last result of every schedule.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .fabric_run import WEEKDAYS, describe_schedule, schedule_configuration

TASK_FOLDER = "FabricDesktop"
RUNS_FILE = "schedule_runs.jsonl"
SCHEDULES_FILE = "schedules.json"


def target_label(target: dict[str, Any]) -> str:
    if target.get("kind") == "notebook":
        return target.get("name", "")
    names = target.get("entities") or []
    what = ", ".join(names) if names else "every table"
    return f"{str(target.get('env', '')).title()} metadata: {what}"


def check_target(target: dict[str, Any]) -> dict[str, Any]:
    kind = target.get("kind")
    if kind == "notebook":
        if not target.get("name"):
            raise ValueError("Pick the notebook to run")
        return {"kind": "notebook", "name": target["name"]}
    if kind == "meta":
        if target.get("env") not in ("dev", "test", "prod"):
            raise ValueError("Pick the environment to run")
        return {"kind": "meta", "env": target["env"], "entities": list(target.get("entities") or [])}
    raise ValueError("A schedule runs a notebook or a metadata environment")


def local_configuration(schedule: dict[str, Any]) -> dict[str, Any]:
    """The same shape as Fabric's schedule configuration, in this PC's own time zone."""
    config = schedule_configuration(schedule.get("kind", "daily"), schedule.get("times"),
                                    schedule.get("every_minutes"), schedule.get("weekdays"), "Local")
    now = dt.datetime.now().replace(second=0, microsecond=0)
    config["startDateTime"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    config.pop("endDateTime", None)
    return config


def next_run(config: dict[str, Any], now: dt.datetime | None = None) -> dt.datetime | None:
    """When the schedule fires next, in local time (what Task Scheduler does with these triggers)."""
    now = (now or dt.datetime.now()).replace(second=0, microsecond=0)
    kind = config.get("type")
    if kind == "Cron":
        start = dt.datetime.fromisoformat(config.get("startDateTime") or now.isoformat())
        step = dt.timedelta(minutes=int(config["interval"]))
        if start > now:
            return start
        return start + step * ((now - start) // step + 1)
    times = sorted(config.get("times") or [])
    days = set(range(7)) if kind == "Daily" else {WEEKDAYS.index(d) for d in config.get("weekdays") or []}
    if not times or not days:
        return None
    for offset in range(8):
        day = now.date() + dt.timedelta(days=offset)
        if day.weekday() not in days:
            continue
        for t in times:
            h, m = map(int, t.split(":"))
            at = dt.datetime.combine(day, dt.time(h, m))
            if at > now:
                return at
    return None


# ---- Task Scheduler

def task_name(schedule_id: str) -> str:
    return f"\\{TASK_FOLDER}\\{schedule_id}"


def pythonw(executable: str | None = None) -> str:
    """The windowless interpreter next to this one, so a scheduled run never flashes a console."""
    exe = Path(executable or sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def _triggers(config: dict[str, Any]) -> str:
    start = config.get("startDateTime") or dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    day = start[:10]
    if config.get("type") == "Cron":
        return (f"<TimeTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>"
                f"<Repetition><Interval>PT{int(config['interval'])}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd>"
                "</Repetition></TimeTrigger>")
    out = []
    for t in config.get("times") or []:
        boundary = f"{day}T{t}:00"
        if config.get("type") == "Daily":
            schedule = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
        else:
            days = "".join(f"<{d} />" for d in config.get("weekdays") or [])
            schedule = f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
        out.append(f"<CalendarTrigger><StartBoundary>{boundary}</StartBoundary><Enabled>true</Enabled>{schedule}</CalendarTrigger>")
    return "".join(out)


def task_xml(schedule: dict[str, Any], executable: str, workdir: str) -> str:
    """Task Scheduler's definition: run as you, only while you are signed in to Windows (your
    Fabric sign-in lives in your profile), start a missed run when the PC wakes, never two at once."""
    args = f"-m fabric_desktop.cli run-schedule {schedule['id']}"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape("Fabric Desktop: " + target_label(schedule["target"]))}</Description></RegistrationInfo>
  <Triggers>{_triggers(schedule["config"])}</Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
    <Enabled>{"true" if schedule.get("enabled", True) else "false"}</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(executable)}</Command><Arguments>{escape(args)}</Arguments>
    <WorkingDirectory>{escape(workdir)}</WorkingDirectory></Exec></Actions>
</Task>
"""


class TaskScheduler:
    """``schtasks.exe``. On other systems schedules are saved but nothing starts them."""

    available = sys.platform == "win32"

    def _run(self, *args: str) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=flags)
        if res.returncode != 0:
            raise RuntimeError(f"Task Scheduler: {(res.stderr or res.stdout).strip()}")

    def register(self, schedule: dict[str, Any], executable: str, workdir: str) -> None:
        if not self.available:
            return
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as f:
            f.write(task_xml(schedule, executable, workdir))
        try:
            self._run("/Create", "/TN", task_name(schedule["id"]), "/XML", f.name, "/F")
        finally:
            os.unlink(f.name)

    def unregister(self, schedule_id: str) -> None:
        if self.available:
            try:
                self._run("/Delete", "/TN", task_name(schedule_id), "/F")
            except RuntimeError:
                pass  # already gone

    def run_now(self, schedule_id: str) -> bool:
        if not self.available:
            return False
        self._run("/Run", "/TN", task_name(schedule_id))
        return True


class LocalSchedules:
    def __init__(self, root: Path, scheduler: TaskScheduler | None = None, executable: str | None = None):
        self.root = root
        self.scheduler = scheduler or TaskScheduler()
        self.executable = pythonw(executable)

    @property
    def file(self) -> Path:
        return self.root / SCHEDULES_FILE

    @property
    def runs_file(self) -> Path:
        return self.root / RUNS_FILE

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {"engine": "", "schedules": []}

    def _save(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def get(self, schedule_id: str) -> dict[str, Any]:
        found = next((s for s in self._load()["schedules"] if s["id"] == schedule_id), None)
        if found is None:
            raise LookupError(f"There is no schedule {schedule_id} on this PC")
        return found

    def add(self, target: dict[str, Any], schedule: dict[str, Any]) -> dict[str, Any]:
        item = {"id": uuid.uuid4().hex[:12], "target": check_target(target), "config": local_configuration(schedule),
                "enabled": True, "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        self.scheduler.register(item, self.executable, str(self.root))
        data = self._load()
        data["schedules"].append(item)
        data["engine"] = self.executable
        self._save(data)
        return item

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        data = self._load()
        for s in data["schedules"]:
            if s["id"] == schedule_id:
                s["enabled"] = bool(enabled)
                self.scheduler.register(s, self.executable, str(self.root))
                self._save(data)
                return s
        raise LookupError(f"There is no schedule {schedule_id} on this PC")

    def delete(self, schedule_id: str) -> None:
        data = self._load()
        data["schedules"] = [s for s in data["schedules"] if s["id"] != schedule_id]
        self.scheduler.unregister(schedule_id)
        self._save(data)

    def sync(self) -> int:
        """Point every task at this engine (after an update). Returns how many were re-registered."""
        data = self._load()
        if not data["schedules"] or data.get("engine") == self.executable:
            return 0
        for s in data["schedules"]:
            self.scheduler.register(s, self.executable, str(self.root))
        data["engine"] = self.executable
        self._save(data)
        return len(data["schedules"])

    def runs(self, limit: int = 500) -> list[dict[str, Any]]:
        try:
            lines = self.runs_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out[::-1]

    def log_run(self, row: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.runs_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def listing(self, now: dt.datetime | None = None) -> list[dict[str, Any]]:
        """Every schedule with its next run and its latest result."""
        latest: dict[str, dict[str, Any]] = {}
        for r in self.runs():
            latest.setdefault(r["schedule_id"], r)
        out = []
        for s in self._load()["schedules"]:
            nxt = next_run(s["config"], now) if s.get("enabled", True) else None
            out.append(s | {"label": target_label(s["target"]), "text": describe_schedule({"configuration": s["config"]}).replace(" Local", ""),
                            "next_run": nxt.isoformat() if nxt else None, "last": latest.get(s["id"])})
        return out
