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
    args = parser.parse_args(argv)

    if args.command == "serve":
        serve()
        return
    service = Service()
    result = {
        "sign-in": lambda: service.sign_in(),
        "preview": lambda: service.preview_job(args.job, limit=10),
        "run": lambda: service.run_job(args.job),
        "history": lambda: service.history(),
    }[args.command]()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
