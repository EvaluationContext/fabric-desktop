"""Undo a bad load: put a managed table back to an earlier Delta version.

Every run writes a new Delta version, so the table as it was before a bad load is still there
until VACUUM removes the files it needs. VACUUM keeps removed files for the layer's
``retention_days``; a version is restorable while every file it reads is still kept.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from deltalake import CommitProperties, DeltaTable

from .. import delta_profile

_MAINTENANCE = ("OPTIMIZE", "VACUUM START", "VACUUM END", "SET TBLPROPERTIES")
_KEEPS_FILES = ("VACUUM", "SET TBLPROPERTIES")


def _ts(ms: Any) -> dt.datetime | None:
    if ms is None:
        return None
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc)


def _rows(commit: dict[str, Any]) -> int | None:
    m = commit.get("operationMetrics") or {}
    for key in ("num_added_rows", "numOutputRows", "num_output_rows", "numTargetRowsInserted"):
        if m.get(key) not in (None, ""):
            try:
                return int(m[key])
            except (TypeError, ValueError):
                return None
    return None


def versions(uri: str, options: dict[str, str] | None, retention_days: int, limit: int = 50) -> list[dict[str, Any]]:
    """The table's versions, newest first, each saying whether it can still be restored.

    VACUUM deletes files that were removed more than its retention ago (the VACUUM commit says
    how long; ``retention_days`` is the fallback). The files of version v start being removed at
    version v+1, so v survives a VACUUM at time T when v+1 is newer than T minus the retention.
    Without any VACUUM, every version still in the log can be restored."""
    table = DeltaTable(uri, storage_options=options)
    history = table.history(limit)  # newest first
    current = table.version()
    horizon = None
    for h in table.history(1000):
        if h.get("operation") != "VACUUM START" or not h.get("timestamp"):
            continue
        params = h.get("operationParameters") or {}
        kept = params.get("specifiedRetentionMillis") or params.get("defaultRetentionMillis")
        hours = int(kept) / 3_600_000 if str(kept or "").isdigit() else int(retention_days) * 24
        cut = _ts(h["timestamp"]) - dt.timedelta(hours=hours)  # files removed before this are gone
        horizon = cut if horizon is None or cut > horizon else horizon
    # When each version's files started being removed: the next commit that removes files
    # (VACUUM's and table property commits don't).
    superseded_at: dict[int, dt.datetime | None] = {}
    nxt: dt.datetime | None = None
    for h in sorted((h for h in history if "version" in h), key=lambda h: int(h["version"]), reverse=True):
        superseded_at[int(h["version"])] = nxt
        if not str(h.get("operation", "")).startswith(_KEEPS_FILES):
            nxt = _ts(h.get("timestamp"))
    out = []
    for h in history:
        if "version" not in h:
            continue
        v = int(h["version"])
        superseded = superseded_at.get(v)
        restorable = v != current and (horizon is None or superseded is None or superseded > horizon)
        out.append({"version": v, "at": _ts(h.get("timestamp")).isoformat(timespec="seconds") if h.get("timestamp") else None,
                    "operation": h.get("operation"), "run_id": h.get("fabric_desktop_run_id"),
                    "restored_to": h.get("fabric_desktop_restored_to"), "rows": _rows(h),
                    "maintenance": h.get("operation") in _MAINTENANCE, "current": v == current,
                    "restorable": restorable,
                    "why_not": None if restorable or v == current else
                    f"VACUUM has removed its files (history is kept {retention_days} days)"})
    return out


def restore(uri: str, options: dict[str, str] | None, version: int, metadata: dict[str, str] | None = None) -> dict[str, Any]:
    """Restore the table to ``version`` as a new commit. Fails, changing nothing, if files it needs are gone."""
    table = DeltaTable(uri, storage_options=options)
    before = table.version()
    if int(version) >= before:
        raise ValueError(f"Version {version} isn't earlier than the current version ({before}).")
    metrics = table.restore(int(version), ignore_missing_files=False,
                            commit_properties=CommitProperties(custom_metadata=metadata) if metadata else None)
    after = DeltaTable(uri, storage_options=options)
    delta_profile.check_table(after)
    return {"from_version": before, "to_version": int(version), "new_version": after.version(),
            "files_restored": int(metrics.get("numRestoredFile", 0) or 0),
            "files_removed": int(metrics.get("numRemovedFile", 0) or 0)}
