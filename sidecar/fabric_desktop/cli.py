"""Command line entry point: `fabric-desktop serve | preview JOB | run JOB | history`."""

from __future__ import annotations

import argparse
import json

from .server import main as serve
from .service import Service


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fabric-desktop")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the JSON-lines server for the desktop app")
    sub.add_parser("sign-in", help="sign in to Fabric and OneLake")
    for name in ("preview", "run"):
        p = sub.add_parser(name, help=f"{name} a job file")
        p.add_argument("job")
    sub.add_parser("history", help="show recent runs")
    p = sub.add_parser("run-schedule", help="run one of this PC's schedules (Windows Task Scheduler calls this)")
    p.add_argument("schedule_id")
    p.add_argument("--manual", action="store_true", help="started with Run now, not by the schedule")
    args = parser.parse_args(argv)

    if args.command == "serve":
        serve()
        return
    if args.command == "run-schedule":
        _run_schedule(args.schedule_id, "manual" if args.manual else "schedule")
        return
    service = Service()
    result = {
        "sign-in": lambda: service.sign_in(),
        "preview": lambda: service.preview_job(args.job, limit=10),
        "run": lambda: service.run_job(args.job),
        "history": lambda: service.history(),
    }[args.command]()
    print(json.dumps(result, indent=2, default=str))


def _run_schedule(schedule_id: str, trigger: str) -> None:
    """No console and no one watching: log to a file, never open a sign-in window."""
    import sys

    from .server import _log_file
    if sys.stdout is None or sys.stderr is None or sys.executable.lower().endswith("pythonw.exe"):
        sys.stdout = sys.stderr = _log_file()
    result = Service(headless=True).run_schedule(schedule_id, trigger)
    print(json.dumps(result, default=str))
    raise SystemExit(0 if result["status"] != "failed" else 1)


if __name__ == "__main__":
    main()
