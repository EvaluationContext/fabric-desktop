"""Schedules in one place: runs on this PC (Task Scheduler) and schedules in Fabric's job scheduler."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .fabric_api import FabricClient
from .fabric_run import describe_schedule
from .local_schedules import LocalSchedules, target_label

# Fabric item types that have schedules, and the job type each one runs.
SCHEDULED_TYPES = {"Notebook": "RunNotebook", "DataPipeline": "Pipeline", "SparkJobDefinition": "sparkjob"}
TYPE_LABELS = {"Notebook": "Notebook", "DataPipeline": "Pipeline", "SparkJobDefinition": "Spark job"}
STALE_LOCK_SECONDS = 12 * 3600


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class ScheduleMixin:
    _fabric_schedules: dict[str, Any] | None = None
    _fabric_schedules_lock = threading.Lock()

    @property
    def pc_schedules(self) -> LocalSchedules:
        return LocalSchedules(self.root)  # type: ignore[attr-defined]

    # ---- schedules on this PC

    def add_pc_schedule(self, target: dict[str, Any], schedule: dict[str, Any]) -> dict[str, Any]:
        if target.get("kind") == "notebook":
            self.notebooks.load(target.get("name", ""))  # type: ignore[attr-defined]  # it exists
        elif target.get("kind") == "meta":
            project = self._project()  # type: ignore[attr-defined]
            unknown = [n for n in target.get("entities") or [] if n not in project.entities]
            if unknown:
                raise ValueError(f"Not in the project: {', '.join(unknown)}")
        item = self.pc_schedules.add(target, schedule)
        return {"schedule": item, "windows": self.pc_schedules.scheduler.available}

    def pc_schedules_for(self, kind: str, name: str = "", env: str = "") -> list[dict[str, Any]]:
        """This PC's schedules for one notebook (kind "notebook"), one environment (kind "meta"), or all."""
        if kind == "all":
            return self.pc_schedules.listing()
        return [s for s in self.pc_schedules.listing()
                if s["target"]["kind"] == kind and (s["target"].get("name") == name if kind == "notebook"
                                                    else s["target"].get("env") == env)]

    def delete_pc_schedule(self, schedule_id: str) -> dict[str, Any]:
        self.pc_schedules.delete(schedule_id)
        return {"deleted": schedule_id}

    def set_pc_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        return self.pc_schedules.set_enabled(schedule_id, enabled)

    def run_pc_schedule_now(self, schedule_id: str) -> dict[str, Any]:
        """Start the run in its own process, as Task Scheduler would, so the app stays free."""
        self.pc_schedules.get(schedule_id)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([sys.executable, "-m", "fabric_desktop.cli", "run-schedule", schedule_id, "--manual"],
                         cwd=str(self.root), creationflags=flags, env=os.environ.copy(),  # type: ignore[attr-defined]
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"started": schedule_id}

    def pc_schedule_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.pc_schedules.runs(limit)

    def sync_pc_schedules(self) -> dict[str, Any]:
        """Called when the app starts: point every task at this engine."""
        try:
            return {"updated": self.pc_schedules.sync()}
        except Exception as exc:  # Task Scheduler refused: the list still shows them
            return {"updated": 0, "error": f"{type(exc).__name__}: {exc}"}

    def _lock(self, schedule_id: str) -> Path | None:
        folder = self.root / "schedule_locks"  # type: ignore[attr-defined]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{schedule_id}.lock"
        if path.exists() and time.time() - path.stat().st_mtime > STALE_LOCK_SECONDS:
            path.unlink(missing_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return path

    def run_schedule(self, schedule_id: str, trigger: str = "schedule") -> dict[str, Any]:
        """One scheduled run on this PC (Task Scheduler calls this through the CLI)."""
        schedules = self.pc_schedules
        s = schedules.get(schedule_id)
        lock = self._lock(schedule_id)
        row: dict[str, Any] = {"schedule_id": schedule_id, "label": target_label(s["target"]), "trigger": trigger,
                               "started": _now(), "pid": os.getpid()}
        if lock is None:
            row |= {"ended": _now(), "seconds": 0, "status": "skipped",
                    "message": "The previous run of this schedule was still going"}
            schedules.log_run(row)
            return row
        t0 = time.monotonic()
        try:
            if s["target"]["kind"] == "notebook":
                row |= self._run_notebook_here(s["target"]["name"])
            else:
                row |= self._run_meta_here(s["target"]["env"], s["target"].get("entities") or None)
        except Exception as exc:
            row |= {"status": "failed", "message": f"{type(exc).__name__}: {exc}"}
        finally:
            lock.unlink(missing_ok=True)
        row |= {"ended": _now(), "seconds": round(time.monotonic() - t0, 1)}
        schedules.log_run(row)
        if row["status"] == "failed":
            self._alert_failed_schedule(row)
        return row

    def _run_notebook_here(self, name: str) -> dict[str, Any]:
        nb = self.notebooks.load(name)  # type: ignore[attr-defined]
        self._attach(nb["lakehouse"])  # type: ignore[attr-defined]
        code = [c for c in nb["cells"] if c.get("type") != "markdown" and c.get("source", "").strip()]
        for i, cell in enumerate(code, 1):
            res = self.kernel.run(cell["source"])  # type: ignore[attr-defined]
            if res["status"] not in ("ok",):
                err = next((o for o in res["outputs"] if o.get("type") == "error"), {})
                why = f"{err.get('ename')}: {err.get('evalue')}" if err else res["status"]
                return {"status": "failed", "message": f"Cell {i} of {len(code)} failed: {why}", "cells": i}
        return {"status": "ok", "message": f"{len(code)} cell{'s' if len(code) != 1 else ''} ran", "cells": len(code)}

    def _run_meta_here(self, env: str, names: list[str] | None) -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        results = self._run_here(project, env, names)  # type: ignore[attr-defined]
        failed = [r for r in results if r["status"] not in ("ok", "skipped")]
        rows = sum(r.get("rows_written") or 0 for r in results)
        if failed:
            first = failed[0]
            return {"status": "failed", "tables": len(results), "rows": rows,
                    "message": f"{len(failed)} of {len(results)} tables didn't finish. {first['entity']}: {first['message']}"}
        return {"status": "ok", "tables": len(results), "rows": rows,
                "message": f"{len(results)} tables, {rows:,} rows written"}

    def _alert_failed_schedule(self, row: dict[str, Any]) -> None:
        url = self._settings().get("teams_webhook")  # type: ignore[attr-defined]
        if not url:
            return
        from .dataops_service import post_teams
        try:
            post_teams(url, f"Scheduled run failed on this PC: {row['label']}", [row.get("message") or "See Monitor > Schedules"])
        except Exception:
            pass  # Monitor still shows it

    # ---- every schedule, in one list

    def _schedule_workspaces(self, all_workspaces: bool, token: str) -> list[dict[str, str]]:
        """Workspaces whose Fabric schedules are listed: every one, or the ones this app works in."""
        listed = FabricClient(lambda: token).list_workspaces()
        if all_workspaces:
            return [{"id": w["id"], "name": w["displayName"]} for w in listed]
        names = {w["id"]: w["displayName"] for w in listed}
        wanted: set[str] = set(self.workspace_marks().get("pins", []))  # type: ignore[attr-defined]
        try:
            project = self._project()  # type: ignore[attr-defined]
            wanted |= {c.get("workspace_id") for c in project.environments.values() if c.get("workspace_id")}
        except Exception:
            pass
        for n in self.notebooks.list():  # type: ignore[attr-defined]
            try:
                nb = self.notebooks.load(n["name"])  # type: ignore[attr-defined]
            except Exception:
                continue
            ws = (nb.get("workspace") or {}).get("workspace_id") or (nb.get("lakehouse") or {}).get("workspace_id")
            if ws:
                wanted.add(ws)
        return [{"id": w, "name": names[w]} for w in wanted if w in names]

    def all_schedules(self, refresh: bool = False, all_workspaces: bool = False) -> dict[str, Any]:
        """This PC's schedules now; Fabric's schedules from a background scan ({"pending": True} meanwhile)."""
        pc = self.pc_schedules.listing()
        with self._fabric_schedules_lock:
            cached = self._fabric_schedules
            scope = cached.get("all_workspaces") if cached else None
            if cached is None or refresh or (scope != all_workspaces and not cached.get("pending")):
                try:
                    token = self.auth.fabric_token()  # type: ignore[attr-defined]  # here: MSAL isn't thread-safe
                except Exception as exc:
                    self._fabric_schedules = {"pending": False, "items": [], "all_workspaces": all_workspaces,
                                              "error": f"{type(exc).__name__}: {exc}"}
                else:
                    self._fabric_schedules = {"pending": True, "items": [], "all_workspaces": all_workspaces}
                    threading.Thread(target=self._scan_fabric_schedules, args=(token, all_workspaces), daemon=True).start()
            fabric = dict(self._fabric_schedules or {})
        return {"pc": pc, "windows": self.pc_schedules.scheduler.available, "fabric": fabric,
                "runs": self.pc_schedules.runs(200)}

    def _scan_fabric_schedules(self, token: str, all_workspaces: bool) -> None:
        client = FabricClient(lambda: token)
        try:
            workspaces = self._schedule_workspaces(all_workspaces, token)
        except Exception as exc:
            with self._fabric_schedules_lock:
                self._fabric_schedules = {"pending": False, "items": [], "all_workspaces": all_workspaces,
                                          "error": f"{type(exc).__name__}: {exc}"}
            return

        def items(ws: dict[str, str]) -> list[dict[str, Any]]:
            try:
                return [{"workspace_id": ws["id"], "workspace_name": ws["name"], **it}
                        for it in client.list_items(ws["id"]) if it.get("type") in SCHEDULED_TYPES]
            except Exception:
                return []

        def schedules(it: dict[str, Any]) -> list[dict[str, Any]]:
            job = SCHEDULED_TYPES[it["type"]]
            try:
                found = client.list_schedules(it["workspace_id"], it["id"], job)
            except Exception:
                return []
            if not found:
                return []
            last = None
            try:
                runs = client.list_job_instances(it["workspace_id"], it["id"])
                last = runs[0] if runs else None
            except Exception:
                pass
            return [{"id": s["id"], "enabled": s.get("enabled", False), "text": describe_schedule(s),
                     "configuration": s.get("configuration") or {}, "job_type": job,
                     "workspace_id": it["workspace_id"], "workspace_name": it["workspace_name"],
                     "item_id": it["id"], "name": it.get("displayName"), "type": TYPE_LABELS[it["type"]], "item_type": it["type"],
                     "last": {"status": last.get("status"), "started": last.get("startTimeUtc"),
                              "ended": last.get("endTimeUtc"),
                              "message": (last.get("failureReason") or {}).get("message", "")} if last else None}
                    for s in found]

        with ThreadPoolExecutor(max_workers=8) as pool:
            found = [it for chunk in pool.map(items, workspaces) for it in chunk]
            rows = [r for chunk in pool.map(schedules, found) for r in chunk]
        rows.sort(key=lambda r: (r["workspace_name"].lower(), str(r["name"]).lower()))
        with self._fabric_schedules_lock:
            self._fabric_schedules = {"pending": False, "items": rows, "all_workspaces": all_workspaces,
                                      "workspaces": len(workspaces), "scanned": _now()}

    def set_fabric_schedule_enabled(self, workspace_id: str, item_id: str, job_type: str, schedule_id: str,
                                    enabled: bool, configuration: dict[str, Any]) -> dict[str, Any]:
        out = self.fabric_jobs.update_schedule(workspace_id, item_id, job_type, schedule_id,  # type: ignore[attr-defined]
                                               enabled, configuration)
        self._forget_fabric_schedule(schedule_id, enabled=enabled)
        return out

    def delete_fabric_schedule(self, workspace_id: str, item_id: str, job_type: str, schedule_id: str) -> dict[str, Any]:
        self.fabric_jobs.delete_schedule(workspace_id, item_id, job_type, schedule_id)  # type: ignore[attr-defined]
        self._forget_fabric_schedule(schedule_id)
        return {"deleted": schedule_id}

    def _forget_fabric_schedule(self, schedule_id: str, enabled: bool | None = None) -> None:
        with self._fabric_schedules_lock:
            cached = self._fabric_schedules
            if not cached:
                return
            if enabled is None:
                cached["items"] = [r for r in cached["items"] if r["id"] != schedule_id]
            else:
                for r in cached["items"]:
                    if r["id"] == schedule_id:
                        r["enabled"] = enabled

    def run_fabric_item_now(self, workspace_id: str, item_id: str, job_type: str, name: str,
                            workspace_name: str = "") -> dict[str, Any]:
        job_id = self.fabric_jobs.run_item_job(workspace_id, item_id, job_type)  # type: ignore[attr-defined]
        return self._log_fabric_run({"kind": "Scheduled item run", "name": name, "workspace_id": workspace_id,  # type: ignore[attr-defined]
                                     "workspace_name": workspace_name, "item_id": item_id, "job_id": job_id,
                                     "engine": "spark"})

