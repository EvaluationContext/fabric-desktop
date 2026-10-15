"""Table health and maintenance.

Tables written from this PC carry no V-Order, which Direct Lake and the SQL analytics
endpoint read fastest, and every run adds a Delta version whose old files stay until
VACUUM. Fabric's own table maintenance job (OPTIMIZE with V-Order, then VACUUM) fixes
both on Fabric capacity; for tables on this PC the same compaction runs locally with
delta-rs, without V-Order.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from deltalake import DeltaTable
from deltalake.exceptions import TableNotFoundError

SMALL_FILE_BYTES = 32 * 1024 * 1024
SMALL_FILE_COUNT = 20     # this many files averaging under SMALL_FILE_BYTES is worth compacting
VERSIONS_BEFORE_VACUUM = 30
RETENTION_HOURS = 7 * 24 + 1  # Fabric's default retention is 7 days
LOG_RETENTION_DAYS = 30  # Delta's default: older log entries go at the next checkpoint


def _when(ms: Any) -> str | None:
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).isoformat(timespec="seconds")


def _by_fabric(commit: dict[str, Any]) -> bool:
    """Spark in Fabric writes the commit; delta-rs (this app) stamps its own clientVersion."""
    return "delta-rs" not in str(commit.get("clientVersion", "")) and bool(commit.get("engineInfo"))


def health(uri: str, options: dict[str, str] | None = None, local: bool = False) -> dict[str, Any]:
    """File, size and history numbers for one Delta table, plus what maintenance would help.

    V-Order only matters to Fabric readers, so it isn't advised for a table on this PC."""
    try:
        table = DeltaTable(uri, storage_options=options)
    except TableNotFoundError:
        return {"exists": False, "advice": [], "status": "missing"}
    actions = table.get_add_actions(flatten=True)
    sizes = actions.column("size_bytes").to_pylist() if actions.num_rows else []
    files, total = len(sizes), sum(sizes)
    history = table.history(200)  # newest first
    version = table.version()
    last_optimize = next((h for h in history if h.get("operation") == "OPTIMIZE"), None)
    last_vacuum = next((h for h in history if str(h.get("operation", "")).startswith("VACUUM")), None)
    last_write = next((h for h in history if h.get("operation") not in ("OPTIMIZE", "VACUUM START", "VACUUM END")), None)
    v_ordered = bool(last_optimize and _by_fabric(last_optimize) and last_write
                     and int(last_optimize.get("timestamp", 0)) >= int(last_write.get("timestamp", 0)))
    vacuum_version = int(last_vacuum.get("version", -1)) if last_vacuum and "version" in last_vacuum else None
    since_vacuum = version - vacuum_version if vacuum_version is not None else version + 1
    advice = []
    if files >= SMALL_FILE_COUNT and total / max(files, 1) < SMALL_FILE_BYTES:
        advice.append(f"{files} small files: compacting them speeds up every read")
    if not v_ordered and files and not local:
        advice.append("No V-Order since the last write: Direct Lake and the SQL endpoint read V-Ordered files faster")
    if since_vacuum >= VERSIONS_BEFORE_VACUUM:
        advice.append(f"{since_vacuum} versions since the last VACUUM: old files still take up storage")
    return {"exists": True, "version": version, "files": files, "bytes": total,
            "avg_file_bytes": total // files if files else 0, "v_ordered": v_ordered,
            "last_optimize": _when(last_optimize.get("timestamp")) if last_optimize else None,
            "last_vacuum": _when(last_vacuum.get("timestamp")) if last_vacuum else None,
            "last_write": _when(last_write.get("timestamp")) if last_write else None,
            "versions_since_vacuum": since_vacuum, "advice": advice,
            "status": "needs_maintenance" if advice else "healthy"}


def retention_hours(days: int) -> int:
    return int(days) * 24 + 1


def fabric_retention(days: int) -> str:
    """Retention as Fabric's table maintenance job takes it: d.hh:mm:ss, an hour of margin."""
    return f"{int(days)}.01:00:00"


def retention_properties(days: int) -> dict[str, str]:
    """Table properties that keep ``days`` of history for every engine: VACUUM keeps removed
    files that long, and the log keeps its entries at least that long (Delta's default is 30)."""
    return {"delta.deletedFileRetentionDuration": f"interval {int(days)} days",
            "delta.logRetentionDuration": f"interval {max(int(days), LOG_RETENTION_DAYS)} days"}


def set_retention(uri: str, days: int, options: dict[str, str] | None = None) -> bool:
    """Write the retention properties onto the table if they differ. True when it changed."""
    table = DeltaTable(uri, storage_options=options)
    wanted = retention_properties(days)
    config = table.metadata().configuration or {}
    if all(config.get(k) == v for k, v in wanted.items()):
        return False
    table.alter.set_table_properties(wanted, raise_if_not_exists=False)
    return True


def compact_local(uri: str, days: int = 7) -> dict[str, Any]:
    """OPTIMIZE (compaction, no V-Order) and VACUUM for a table on this PC, keeping ``days`` of history."""
    set_retention(uri, days)
    table = DeltaTable(uri)
    metrics = table.optimize.compact()
    removed = table.vacuum(retention_hours=retention_hours(days), dry_run=False, enforce_retention_duration=False)
    return {"files_added": metrics.get("numFilesAdded", 0), "files_removed": metrics.get("numFilesRemoved", 0),
            "vacuumed": len(removed)}
