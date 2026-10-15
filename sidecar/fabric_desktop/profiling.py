"""Profile a table on this PC and suggest cleaning rules and checks for it.

Every suggestion is a ``[[clean]]`` or ``[[check]]`` block the metadata framework
already runs, so accepting one is adding its TOML to the entity file. Profiling reads
every row locally, which is the point: it costs no Fabric capacity.
"""

from __future__ import annotations

import json
from typing import Any

import polars as pl

NULL_TOKENS = ["", "NULL", "null", "Null", "N/A", "n/a", "NA", "na", "None", "none", "-", "?"]
DATE_FORMATS = {"yyyy-MM-dd": "%Y-%m-%d", "MM/dd/yyyy": "%m/%d/%Y", "dd/MM/yyyy": "%d/%m/%Y",
                "yyyyMMdd": "%Y%m%d", "dd-MM-yyyy": "%d-%m-%Y"}
MAX_ACCEPTED = 12     # suggest accepted_values when a text column has this many values or fewer
TOP_VALUES = 5
_NUMERIC = (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
            pl.Float32, pl.Float64, pl.Decimal)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))


def to_toml(kind: str, rule: dict[str, Any]) -> str:
    return f"[[{kind}]]\n" + "\n".join(f"{k} = {_toml_value(v)}" for k, v in rule.items()) + "\n"


def _n(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _is_numeric(dtype: pl.DataType) -> bool:
    return isinstance(dtype, _NUMERIC) or dtype in _NUMERIC


def _keyish(col: str, keys: list[str]) -> bool:
    return col in keys or col.lower().endswith(("_id", "_key", "_code", "_sk"))


def profile(df: pl.DataFrame, keys: list[str] | None = None,
            existing: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Column statistics and suggestions. ``existing`` holds the entity's current clean and
    check blocks (``{"kind", **rule}``) so suggestions already in place aren't repeated."""
    keys = keys or []
    have = existing or []
    n = df.height
    columns, suggestions = [], []

    def already(kind: str, col: str, field: str) -> bool:
        return any(x.get("kind") == kind and x.get("column") == col and field in x for x in have)

    def suggest(kind: str, col: str, rule: dict[str, Any], why: str, field: str) -> bool:
        """Add a suggestion; False when the entity already has that rule."""
        if already(kind, col, field):
            return False
        full = {"column": col, **rule}
        if kind == "check":
            full.setdefault("on_fail", "warn")
        suggestions.append({"id": f"{kind}:{col}:{field}", "kind": kind, "column": col, "why": why,
                            "rule": full, "toml": to_toml(kind, full)})
        return True

    def issue(stat: dict[str, Any], text: str, new: bool) -> None:
        stat["issues"].append(text if new else f"{text} (a [[clean]] rule handles it)")

    for col, dtype in df.schema.items():
        s = df[col]
        nulls = int(s.null_count())
        nn = s.drop_nulls()
        distinct = int(nn.n_unique()) if len(nn) else 0
        stat: dict[str, Any] = {"name": col, "type": str(dtype), "nulls": nulls,
                                "null_pct": round(100 * nulls / n, 1) if n else 0.0,
                                "distinct": distinct, "issues": []}
        top = (nn.value_counts(sort=True).head(TOP_VALUES) if len(nn) else None)
        stat["top"] = [{"value": str(r[0]), "count": int(r[1])} for r in top.iter_rows()] if top is not None else []

        if dtype == pl.String and len(nn):
            lengths = nn.str.len_chars()
            stat["min_length"], stat["max_length"] = int(lengths.min()), int(lengths.max())
            padded = int((nn != nn.str.strip_chars()).sum())
            if padded:
                text = f"{_n(padded, 'value')} with spaces at the start or end"
                issue(stat, text, suggest("clean", col, {"trim": True}, text, "trim"))
            stripped = nn.str.strip_chars()
            tokens = sorted({t for t in stripped.unique().to_list() if t in NULL_TOKENS})
            if tokens:
                count = int(stripped.is_in(tokens).sum())
                new = suggest("clean", col, {"null_if": tokens}, f"{_n(count, 'value')} like {tokens[0]!r} that mean no value",
                              "null_if")
                issue(stat, f"{_n(count, 'value')} that mean null ({', '.join(repr(t) for t in tokens)})", new)
            real = stripped.filter(~stripped.is_in(NULL_TOKENS))
            folded = int(real.str.to_lowercase().n_unique()) if len(real) else 0
            case = None
            if len(real) and folded < int(real.n_unique()):
                case = "lower" if int((real == real.str.to_lowercase()).sum()) >= len(real) / 2 else "upper"
                new = suggest("clean", col, {"case": case},
                              f"{int(real.n_unique())} spellings collapse to {folded} values in {case} case", "case")
                issue(stat, f"the same value in different case ({int(real.n_unique()) - folded} extra spellings)", new)
            is_date = False
            if len(real):
                for label, fmt in DATE_FORMATS.items():
                    parsed = real.str.to_date(fmt, strict=False)
                    if parsed.null_count() == 0:
                        issue(stat, f"dates stored as text ({label})",
                              suggest("clean", col, {"parse_date": [label]}, f"every value is a {label} date", "parse_date"))
                        is_date = True
                        break
                else:
                    as_num = real.str.replace_all(",", "").cast(pl.Float64, strict=False)
                    if as_num.null_count() == 0 and not _keyish(col, keys):
                        stat["issues"].append("numbers stored as text")
                        suggestions.append({"id": f"info:{col}:numeric", "kind": "info", "column": col,
                                            "why": "every value is a number: type it as int64 or float64 in [contract.columns]",
                                            "rule": {}, "toml": ""})
            if 1 < folded <= MAX_ACCEPTED and n >= 3 * folded and not is_date and not _keyish(col, keys):
                # Checks run after cleaning, so the accepted values are the cleaned ones.
                cleaned = {"lower": real.str.to_lowercase(), "upper": real.str.to_uppercase()}.get(case, real)
                values = sorted(set(cleaned.to_list()))
                suggest("check", col, {"accepted_values": values},
                        f"only {len(values)} distinct values: a new one is worth a warning", "accepted_values")

        if _is_numeric(dtype) and len(nn):
            vals = nn.cast(pl.Float64)
            stat.update(min=vals.min(), max=vals.max(), mean=round(float(vals.mean()), 4))
            q1, q3 = vals.quantile(0.25), vals.quantile(0.75)
            if q1 is not None and q3 is not None and q3 > q1 and not _keyish(col, keys):
                iqr = q3 - q1
                outliers = int(((vals < q1 - 3 * iqr) | (vals > q3 + 3 * iqr)).sum())
                if outliers:
                    stat["issues"].append(f"{_n(outliers, 'far outlier')}")
            negatives = int((vals < 0).sum())
            if not _keyish(col, keys) and negatives == 0 and vals.min() is not None and len(nn) >= 3:
                suggest("check", col, {"range": {"min": 0}}, "no value is below 0 today", "range")
            elif 0 < negatives <= max(1, len(nn) // 100):
                stat["issues"].append(f"{_n(negatives, 'negative value')}")

        if dtype in (pl.Date, pl.Datetime) or isinstance(dtype, pl.Datetime):
            if len(nn):
                stat.update(min=str(nn.min()), max=str(nn.max()))

        if _keyish(col, keys) and n:
            if nulls == 0:
                suggest("check", col, {"not_null": True, "on_fail": "stop" if col in keys else "warn"},
                        "no nulls today, and it's a key", "not_null")
            dupes = int((nn.is_duplicated()).sum())
            if col in keys and dupes:
                stat["issues"].append(f"{_n(dupes, 'row')} sharing a key value")
            if dupes == 0 and distinct == len(nn) and len(nn) > 1 and (col in keys or col.lower().endswith("_id")) \
                    and (col in keys or len(keys) == 0):
                suggest("check", col, {"unique": True, "on_fail": "stop" if col in keys else "warn"},
                        "every value is different today", "unique")
        elif nulls and nulls < n and stat["null_pct"] <= 1:
            stat["issues"].append(_n(nulls, "null"))
        columns.append(stat)

    if keys and all(k in df.columns for k in keys) and n:
        dupes = n - df.select(keys).n_unique()
        if dupes:
            suggestions.append({"id": "info:keys:dupes", "kind": "info", "column": ", ".join(keys),
                                "why": f"{dupes} rows repeat a key: add dedupe_keep = \"newest:<column>\" in a [[clean]] block",
                                "rule": {}, "toml": ""})
    return {"rows": n, "columns": columns, "suggestions": suggestions}


def existing_rules(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"kind": "clean", **r} for r in spec.get("clean", [])] + [{"kind": "check", **c} for c in spec.get("check", [])]


def append_blocks(text: str, blocks: list[str]) -> str:
    """The entity file with the accepted blocks added at the end."""
    if not blocks:
        return text
    header = "\n# Added from Profile in Fabric Desktop\n"
    return text.rstrip("\n") + "\n" + header + "\n".join(blocks)
