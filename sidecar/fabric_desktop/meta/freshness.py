"""Freshness promises: when each table should have been updated, and which ones are late.

An entity promises freshness in its own file::

    [freshness]
    within_hours = 26        # a successful run at most this long ago
    by = "07:00"             # and/or: updated today by this time (this PC's clock)
    envs = ["prod"]          # where the promise applies; Test and Prod when left out

The run log is the evidence: a table is fresh when its last successful run is recent enough.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

DEFAULT_ENVS = ["test", "prod"]
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def promise(spec: dict[str, Any]) -> dict[str, Any] | None:
    f = spec.get("freshness")
    if not isinstance(f, dict) or not (f.get("within_hours") or f.get("by")):
        return None
    return {"within_hours": f.get("within_hours"), "by": f.get("by"), "envs": list(f.get("envs") or DEFAULT_ENVS)}


def problems(name: str, spec: dict[str, Any], environments: tuple[str, ...]) -> list[str]:
    f = spec.get("freshness")
    if f is None:
        return []
    if not isinstance(f, dict):
        return [f"{name}: [freshness] must be a table, like within_hours = 24"]
    errors = []
    unknown = set(f) - {"within_hours", "by", "envs"}
    if unknown:
        errors.append(f"{name}: [freshness] doesn't know {', '.join(sorted(unknown))} (use within_hours, by, envs)")
    if not f.get("within_hours") and not f.get("by"):
        errors.append(f"{name}: [freshness] needs within_hours, by, or both")
    wh = f.get("within_hours")
    if wh is not None and (isinstance(wh, bool) or not isinstance(wh, (int, float)) or wh <= 0):
        errors.append(f"{name}: freshness within_hours must be a number of hours above 0")
    by = f.get("by")
    if by is not None and not (isinstance(by, str) and _TIME.match(by)):
        errors.append(f'{name}: freshness by must be a time like "07:00"')
    envs = f.get("envs")
    if envs is not None and (not isinstance(envs, list) or not envs or any(x not in environments for x in envs)):
        errors.append(f"{name}: freshness envs must list some of {', '.join(environments)}")
    return errors


def describe(p: dict[str, Any]) -> str:
    parts = []
    if p.get("within_hours"):
        parts.append(f"every {p['within_hours']:g} h")
    if p.get("by"):
        parts.append(f"by {p['by']} each day")
    return " and ".join(parts)


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    t = dt.datetime.fromisoformat(str(ts))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def status(p: dict[str, Any], runs: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    """How one table stands against its promise in one environment.

    ``runs`` are that table's runs there, newest first; ``now`` is aware, in local time."""
    now = now.astimezone()
    last_ok = next((r for r in runs if r.get("status") == "ok"), None)
    latest = runs[0] if runs else None
    out: dict[str, Any] = {"expected": describe(p), "last_ok": None, "age_hours": None, "status": "fresh",
                           "reasons": [], "due_at": None,
                           "last_failed": bool(latest and latest.get("status") in ("failed", "needs_decision")),
                           "failed_run": latest.get("run_id") if latest and latest.get("status") == "failed" else None,
                           "failure": latest.get("message") if latest and latest.get("status") != "ok" else None}
    if last_ok is None:
        out["status"], out["reasons"] = "never", ["never loaded here"]
        return out
    done = _parse(last_ok.get("started_at")) + dt.timedelta(seconds=float(last_ok.get("seconds") or 0))
    out["last_ok"] = done.isoformat()
    age = (now - done).total_seconds() / 3600
    out["age_hours"] = round(age, 2)
    dues = []
    if p.get("within_hours"):
        due = done + dt.timedelta(hours=float(p["within_hours"]))
        dues.append(due)
        if now > due:
            out["reasons"].append(f"last updated {age:.0f} h ago, promised every {p['within_hours']:g} h")
    if p.get("by"):
        hh, mm = (int(x) for x in p["by"].split(":"))
        deadline = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= deadline and done.astimezone() < midnight:
            out["reasons"].append(f"not updated today, promised by {p['by']}")
            dues.append(deadline)
        else:
            dues.append(deadline if done.astimezone() < midnight else deadline + dt.timedelta(days=1))
    out["due_at"] = min(dues).isoformat() if dues else None
    if out["reasons"]:
        out["status"] = "late"
    return out


def report(entities: dict[str, dict[str, Any]], runs: list[dict[str, Any]], envs: list[str],
           now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Every promise in every readable environment. ``runs`` carry ``env`` and are newest first."""
    now = now or dt.datetime.now().astimezone()
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in runs:
        by_key.setdefault((r.get("env", ""), r.get("entity", "")), []).append(r)
    out = []
    for name, spec in sorted(entities.items()):
        p = promise(spec)
        if not p:
            continue
        for env in p["envs"]:
            if env not in envs:
                continue
            out.append({"entity": name, "env": env} | status(p, by_key.get((env, name), []), now))
    order = {"late": 0, "never": 1, "fresh": 2}
    out.sort(key=lambda x: (order[x["status"]], x["env"], x["entity"]))
    return out


def set_promise(text: str, within_hours: float | None, by: str | None, envs: list[str] | None) -> str:
    """The entity file with its [freshness] table replaced (or removed when nothing is promised)."""
    lines = text.splitlines()
    out, skipping = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped == "[freshness]"
        if not skipping:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    if within_hours or by:
        block = ["", "[freshness]"]
        if within_hours:
            block.append(f"within_hours = {float(within_hours):g}")
        if by:
            block.append(f'by = "{by}"')
        if envs:
            block.append("envs = [" + ", ".join(f'"{e}"' for e in envs) + "]")
        out += block
    return "\n".join(out) + "\n"
