"""Gate 3: promote one environment's metadata to the next (Dev to Test, Test to Prod)."""

from __future__ import annotations

import datetime as dt
from typing import Any

import polars as pl

from .project import GateError, Project, validate
from .store import Store


def promote_checks(project: Project, source: Store, target_env: str, today: dt.date | None = None) -> list[str]:
    """Every reason the project can't be promoted from ``source`` yet."""
    errors = validate(project, today)
    version = project.version
    if source.env != "dev":
        deps = source.meta("deployments").filter(pl.col("env") == source.env)
        if not deps.height or deps["version"][-1] != version:
            errors.append(f"{source.env} is not on metadata {version}; promote it to {source.env} first")
    log = source.meta("run_log")
    for name in project.entities:
        runs = log.filter(pl.col("entity") == name)
        if not runs.height:
            errors.append(f"{name}: has never run in {source.env}")
            continue
        last = runs.row(-1, named=True)
        if last["logic"] != project.fingerprint(project.entities[name]):
            errors.append(f"{name}: changed since its last {source.env} run; run it again")
        elif last["status"] != "ok":
            errors.append(f"{name}: its last {source.env} run failed")
    if target_env == source.env:
        errors.append("pick a different environment to promote to")
    return errors


def promote(project: Project, source: Store, target: Store, today: dt.date | None = None) -> dict[str, Any]:
    errors = promote_checks(project, source, target.env, today)
    if errors:
        raise GateError("promote", errors)
    version = project.version
    target.meta_append("entities", [{"version": version, "name": e.name,
                                     "config": e.path.read_text(encoding="utf-8")}
                                    for e in project.entities.values()])
    target.meta_append("deployments", [{"env": target.env, "version": version, "source_env": source.env,
                                        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}])
    return {"env": target.env, "version": version, "entities": len(project.entities)}
