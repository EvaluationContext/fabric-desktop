"""Command line entry point: `fabric-desktop serve | preview JOB | run JOB | history | run-schedule ID | agent ...`."""

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
    agent = sub.add_parser("agent", help="the headless agent: run, check, enqueue, status")
    agent.add_argument("--config", help="agent.toml (default: FABRIC_DESKTOP_AGENT_CONFIG, then "
                                        "%%ProgramData%%\\FabricDesktop\\agent.toml or /etc/fabric-desktop/agent.toml)")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("run", help="run until stopped (the service command)")
    agent_sub.add_parser("check", help="check agent.toml, the project and the sign-in, then exit")
    agent_sub.add_parser("status", help="queue counts, schedules and the latest jobs")
    p = agent_sub.add_parser("enqueue", help="queue a run of an environment")
    p.add_argument("env")
    p.add_argument("entities", nargs="*", help="entities to run (default: all)")
    p.add_argument("--backfill", default="", choices=["", "full"], help="full: rebuild from the whole source")
    p.add_argument("--cascade", action="store_true", help="with one entity: rebuild it and everything downstream")
    args = parser.parse_args(argv)

    if args.command == "serve":
        serve()
        return
    if args.command == "agent":
        raise SystemExit(_agent(args))
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


def _agent(args: argparse.Namespace) -> int:
    from . import agent_config, logs
    from .agent import Agent

    try:
        config = agent_config.load(args.config)
    except (FileNotFoundError, agent_config.ConfigError) as exc:
        print(exc)
        return 2
    if args.agent_command == "run":
        log = logs.setup(config.name, config.home / "logs", config.log_level)
        Agent(config, log=log).run_forever()
        return 0
    if args.agent_command == "check":
        from .runtime import Runtime
        try:
            print(json.dumps(Runtime(config).check(), indent=2, default=str))
        except Exception as exc:
            print(f"Check failed: {type(exc).__name__}: {exc}")
            return 1
        return 0
    agent = Agent(config)
    if args.agent_command == "enqueue":
        result: object = agent.enqueue(args.env, args.entities, args.backfill, args.cascade, source="cli")
    else:
        result = agent.status()
    print(json.dumps(result, indent=2, default=str))
    return 0


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
