"""What runs cost in Fabric capacity, and what running on this PC saved.

Fabric bills compute in capacity units (CU). Spark and Python notebooks use 1 CU for every 2
vCores while they run, so a job's CU-seconds are its run time times the CUs of what it ran
on. Fabric Desktop knows each run's time; the CUs per kind of run and the price of a CU hour
are settings (defaults: a 2-vCore Python notebook is 1 CU, a Spark job on a starter pool's two
medium nodes is 8 CU, and pay-as-you-go is about $0.18 per CU hour). These are estimates; the
Capacity Metrics app in Fabric has the billed numbers.

Runs on this PC cost no capacity. What they would have cost is estimated as the same time
on a Python notebook, which is what Fabric Desktop's runner uses in Fabric.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

DEFAULTS: dict[str, Any] = {"price_per_cu_hour": 0.18, "currency": "USD", "python_cu": 1.0, "spark_cu": 8.0}
# F SKUs: F2 has 2 CUs, F64 has 64.
_SKU_CU = {f"F{n}": n for n in (2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048)}
_SKU_CU |= {"P1": 64, "P2": 128, "P3": 256, "P4": 512, "P5": 1024, "FT1": 64, "FTL64": 64}


def settings(saved: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for k, v in (saved or {}).items():
        if k in DEFAULTS and v not in (None, ""):
            out[k] = v if k == "currency" else float(v)
    return out


def sku_cu(sku: str | None) -> int | None:
    return _SKU_CU.get(str(sku or "").upper())


def _parse(at: Any) -> dt.datetime | None:
    if not at:
        return None
    try:
        t = dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _seconds(started: Any, finished: Any) -> float | None:
    a, b = _parse(started), _parse(finished)
    return max((b - a).total_seconds(), 0.0) if a and b else None


def activities(meta_runs: list[dict[str, Any]], fabric_runs: list[dict[str, Any]],
               notebook_writes: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Every run as one row: what it was, where it ran, how long, and its CU-seconds.

    Metadata runs in Fabric are counted per table from the run log; the runner job that holds
    them is left out so its time isn't counted twice."""
    py, spark = float(cfg["python_cu"]), float(cfg["spark_cu"])
    rows: list[dict[str, Any]] = []
    for r in meta_runs:
        secs = float(r.get("seconds") or 0)
        fabric = r.get("compute") == "fabric"
        rows.append({"kind": "Metadata run", "name": r.get("entity") or "", "env": r.get("env") or "",
                     "where": "fabric" if fabric else "local", "at": r.get("started_at"), "seconds": secs,
                     "cu_seconds": secs * py if fabric else 0.0, "would_cu_seconds": 0.0 if fabric else secs * py})
    for r in fabric_runs:
        if r.get("kind") == "Metadata run":
            continue
        secs = _seconds(r.get("started_at"), r.get("finished_at"))
        if secs is None:
            continue  # still running, or never started
        cu = py if r.get("engine") == "python" else spark
        rows.append({"kind": r.get("kind") or "Fabric job", "name": r.get("table") or r.get("name") or "",
                     "env": r.get("env") or "", "where": "fabric", "at": r.get("started_at"), "seconds": secs,
                     "cu_seconds": secs * cu, "would_cu_seconds": 0.0})
    for r in notebook_writes:
        secs = float(r.get("seconds") or 0)
        rows.append({"kind": "Notebook write", "name": r.get("job") or "", "env": "", "where": "local",
                     "at": r.get("finished_at"), "seconds": secs, "cu_seconds": 0.0, "would_cu_seconds": secs * py})
    return rows


def report(rows: list[dict[str, Any]], cfg: dict[str, Any], days: int, now: dt.datetime | None = None,
           capacities: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Totals for the last ``days``: overall, per environment, per table or notebook, per day.

    ``capacities`` maps an environment to its capacity ({"name", "sku"}), to show what share of
    the capacity its runs used over the period."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=days)
    price = float(cfg["price_per_cu_hour"])
    kept = [r for r in rows if (t := _parse(r["at"])) is not None and t >= since]

    def money(cu_seconds: float) -> float:
        return round(cu_seconds / 3600 * price, 2)

    def total(group: list[dict[str, Any]]) -> dict[str, Any]:
        cu = sum(r["cu_seconds"] for r in group)
        would = sum(r["would_cu_seconds"] for r in group)
        return {"runs": len(group), "seconds": round(sum(r["seconds"] for r in group), 1),
                "fabric_runs": sum(r["where"] == "fabric" for r in group),
                "local_runs": sum(r["where"] == "local" for r in group),
                "cu_hours": round(cu / 3600, 3), "cost": money(cu),
                "saved_cu_hours": round(would / 3600, 3), "saved": money(would)}

    def grouped(key) -> list[dict[str, Any]]:
        groups: dict[Any, list[dict[str, Any]]] = {}
        for r in kept:
            groups.setdefault(key(r), []).append(r)
        return [{"key": k} | total(g) for k, g in groups.items()]

    items = sorted(grouped(lambda r: (r["kind"], r["name"], r["env"])),
                   key=lambda g: -(g["cost"] + g["saved"] + g["seconds"] / 1e6))
    by_item = [{"kind": g["key"][0], "name": g["key"][1], "env": g["key"][2]} | {k: v for k, v in g.items() if k != "key"}
               for g in items]
    by_env = []
    for g in sorted(grouped(lambda r: r["env"]), key=lambda g: str(g["key"])):
        env = g.pop("key")
        cap = (capacities or {}).get(env) or {}
        cus = sku_cu(cap.get("sku"))
        share = round(g["cu_hours"] * 3600 / (cus * days * 86400) * 100, 3) if cus else None
        by_env.append({"env": env, "capacity": cap.get("name"), "sku": cap.get("sku"), "capacity_share": share} | g)
    by_day = sorted(({"day": g.pop("key")} | g for g in grouped(lambda r: _parse(r["at"]).date().isoformat())),
                    key=lambda g: g["day"])
    return {"days": days, "since": since.isoformat(timespec="seconds"), "settings": cfg, "total": total(kept),
            "by_env": by_env, "by_item": by_item, "by_day": by_day}
