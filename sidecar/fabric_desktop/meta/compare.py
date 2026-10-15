"""Compare one table in two places: Dev against Test or Prod, or now against an earlier Delta version.

The answer is what a data engineer checks before a promote: row counts, keys that are only on
one side, which columns changed value for the same key, and how the numeric totals moved.
Columns each environment issues for itself (surrogate keys, run stamps, Type 2 dates) are
left out, since they always differ and say nothing about the data.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl

from .project import SCD2_COLUMNS, SYSTEM_COLUMNS, Entity

SAMPLE = 20


def _rows(df: pl.DataFrame) -> list[dict[str, Any]]:
    return json.loads(df.head(SAMPLE).write_json())


def ignored_columns(e: Entity) -> set[str]:
    """Columns that differ between environments by design."""
    out = set(SYSTEM_COLUMNS) | set(SCD2_COLUMNS) | set(e.key_columns)
    if e.surrogate_key:
        out.add(e.surrogate_key)
    for lk in e.spec.get("lookup", []):
        out.add(lk["as"])  # a surrogate key looked up from a dimension
    return out


def _current(df: pl.DataFrame | None) -> pl.DataFrame | None:
    if df is not None and "is_current" in df.columns:
        return df.filter(pl.col("is_current"))
    return df


def compare(e: Entity, left: pl.DataFrame | None, right: pl.DataFrame | None) -> dict[str, Any]:
    left, right = _current(left), _current(right)
    out: dict[str, Any] = {
        "entity": e.name, "keys": e.keys,
        "rows": {"left": None if left is None else left.height, "right": None if right is None else right.height},
        "columns": {"only_left": [], "only_right": [], "type_changed": []},
        "only_left": {"count": 0, "rows": []}, "only_right": {"count": 0, "rows": []},
        "changed": {"count": 0, "by_column": {}, "rows": []}, "totals": [], "compared_on_keys": False,
    }
    if left is None or right is None:
        out["note"] = "The table doesn't exist on " + ("either side" if left is None and right is None
                                                        else "the left" if left is None else "the right") + " yet."
        return out
    skip = ignored_columns(e)
    lcols = [c for c in left.columns if c not in skip]
    rcols = [c for c in right.columns if c not in skip]
    out["columns"]["only_left"] = [c for c in lcols if c not in rcols]
    out["columns"]["only_right"] = [c for c in rcols if c not in lcols]
    common = [c for c in lcols if c in rcols]
    out["columns"]["type_changed"] = [{"column": c, "left": str(left.schema[c]), "right": str(right.schema[c])}
                                      for c in common if left.schema[c] != right.schema[c]]

    # How each number column's total moved: the quickest sign that a change changes the numbers.
    # Keys and ids are numbers too, but their totals mean nothing.
    for c in common:
        if c in e.keys or c.lower().endswith("id"):
            continue
        if left.schema[c].is_numeric() and right.schema[c].is_numeric():
            a, b = left[c].sum(), right[c].sum()
            out["totals"].append({"column": c, "left": a, "right": b, "diff": (b or 0) - (a or 0)})

    keys = [k for k in e.keys if k in common]
    if not keys or len(keys) != len(e.keys):
        out["note"] = "This table has no contract keys, so rows can't be matched one to one; counts and totals only."
        return out
    out["compared_on_keys"] = True
    # Compare as text so an int on one side and a float on the other still match on equal values.
    as_text = lambda df: df.select([pl.col(c).cast(pl.String) for c in common]).unique(subset=keys, keep="first")
    l, r = as_text(left), as_text(right)
    only_l = l.join(r.select(keys), on=keys, how="anti")
    only_r = r.join(l.select(keys), on=keys, how="anti")
    out["only_left"] = {"count": only_l.height, "rows": _rows(only_l)}
    out["only_right"] = {"count": only_r.height, "rows": _rows(only_r)}

    values = [c for c in common if c not in keys]
    both = l.join(r, on=keys, how="inner", suffix="__r")
    if not values or not both.height:
        return out
    diffs = both.select(keys + [pl.col(c).ne_missing(pl.col(f"{c}__r")).alias(c) for c in values])
    by_column = {c: int(diffs[c].sum()) for c in values}
    out["changed"]["by_column"] = {c: n for c, n in by_column.items() if n}
    changed_mask = pl.any_horizontal([pl.col(c) for c in values])
    changed_keys = diffs.filter(changed_mask).select(keys)
    out["changed"]["count"] = changed_keys.height
    sample = both.join(changed_keys.head(SAMPLE), on=keys, how="semi")
    rows = []
    for row in sample.iter_rows(named=True):
        rows.append({"key": {k: row[k] for k in keys},
                     "changes": [{"column": c, "left": row[c], "right": row[f"{c}__r"]}
                                 for c in values if row[c] != row[f"{c}__r"]]})
    out["changed"]["rows"] = rows
    return out
