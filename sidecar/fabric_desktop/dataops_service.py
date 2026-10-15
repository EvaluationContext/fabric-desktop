"""Day-to-day data engineering, mixed into ``Service``: compare a table across environments or
versions before a promote, review schema drift, fix and replay quarantined rows, and freshness
promises with alerts.

Each feature's logic lives in its own module under ``meta/`` (compare, drift, freshness) or in the
engine (replay); this file only connects them to the project, the stores and the UI.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import polars as pl
import requests

from .meta import Engine, impact
from .meta import compare as compare_mod
from .meta import drift as drift_mod
from .meta import freshness as freshness_mod
from .meta.project import ENVIRONMENTS


def _rows(df: pl.DataFrame, limit: int) -> list[dict[str, Any]]:
    return json.loads(df.head(limit).write_json())


class DataOpsMixin:

    # ---- compare

    def _table(self, project: Any, name: str, env: str, version: int | None) -> pl.DataFrame | None:
        e = project.entities[name]
        store = self._store(project, env)  # type: ignore[attr-defined]
        return store.read(project.layer(e.layer).lakehouse, e.schema, e.table,
                          None if version in (None, "") else int(version))

    def meta_versions(self, name: str, env: str = "dev", limit: int = 15) -> list[dict[str, Any]]:
        """The table's Delta versions, newest first, for picking "before" in a compare."""
        project = self._project()  # type: ignore[attr-defined]
        e = project.entities[name]
        hist = self._store(project, env).history(project.layer(e.layer).lakehouse, e.schema, e.table, limit)  # type: ignore[attr-defined]
        return [{"version": h.get("version"), "at": dt.datetime.fromtimestamp(h["timestamp"] / 1000, dt.timezone.utc).isoformat()
                 if h.get("timestamp") else None, "operation": h.get("operation"), "run_id": h.get("fabric_desktop_run_id")}
                for h in hist]

    def meta_compare(self, name: str, left_env: str, right_env: str, left_version: int | None = None,
                     right_version: int | None = None) -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        e = project.entities[name]
        left = self._table(project, name, left_env, left_version)
        right = self._table(project, name, right_env, right_version)
        return compare_mod.compare(e, left, right) | {
            "left": {"env": left_env, "version": left_version}, "right": {"env": right_env, "version": right_version}}

    def meta_promote_impact(self, source: str, target: str) -> dict[str, Any]:
        """Which tables will be built differently in ``target`` after this promote: their logic in the
        project differs from the logic of their last good run there. Reads only the run log."""
        project = self._project()  # type: ignore[attr-defined]
        try:
            log = self._store(project, target).meta("run_log")  # type: ignore[attr-defined]
        except Exception as exc:  # the target isn't set up or can't be read: say so, don't block
            return {"source": source, "target": target, "tables": [], "problem": str(exc)}
        ok = log.filter(pl.col("status") == "ok")
        last = {r["entity"]: r for r in ok.iter_rows(named=True)}  # sorted by time, so the last one wins
        tables = []
        for name in sorted(project.entities):
            run = last.get(name)
            fp = project.fingerprint(project.entities[name])
            state = "new" if run is None else "changed" if run["logic"] != fp else "same"
            tables.append({"entity": name, "state": state, "last_run": run["started_at"] if run else None,
                           "rows": run["rows_written"] if run else None})
        return {"source": source, "target": target, "tables": tables, "problem": None}

    # ---- schema drift

    def meta_drift(self, name: str, env: str = "dev") -> dict[str, Any]:
        """How the entity's source differs from what its contract expects, right now."""
        project = self._project()  # type: ignore[attr-defined]
        e = project.entities[name]
        src = e.spec.get("source", {})
        source = None
        problem = None
        try:
            store = self._store(project, env)  # type: ignore[attr-defined]
            if "entity" in src:
                up = project.entities[src["entity"]]
                source = store.read(project.layer(up.layer).lakehouse, up.schema, up.table)
                if source is None:
                    problem = f"{up.name} hasn't been loaded in {env} yet"
            elif "files" in src:
                source, _ = store.read_files(project.layer(e.layer).lakehouse, src["files"])
                if source is None:
                    problem = f"no files match {src['files']} yet"
        except Exception as exc:  # the config side is still useful
            problem = str(exc)
        history = []
        try:
            h = store.meta("schema_history").filter(pl.col("entity") == name).reverse()
            history = _rows(h, 10)
        except Exception:
            pass
        return drift_mod.detect(project, e, source) | {"env": env, "problem": problem, "history": history}

    def meta_drift_apply(self, name: str, add: dict[str, str] | None = None, retype: dict[str, str] | None = None,
                         rename: dict[str, str] | None = None, remove: list[str] | None = None,
                         policy: str | None = None) -> dict[str, Any]:
        """Write the drift decisions into the entity file, through the save gate."""
        project = self._project()  # type: ignore[attr-defined]
        text = project.entities[name].path.read_text(encoding="utf-8")
        new = drift_mod.apply(text, add=add, retype=retype, rename=rename, remove=remove, policy=policy)
        return self.meta_save_entity(name, new)  # type: ignore[attr-defined]

    # ---- quarantine

    def _quarantine_table(self, project: Any, name: str, env: str) -> tuple[Any, str, str, pl.DataFrame | None]:
        e = project.entities[name]
        store = self._store(project, env)  # type: ignore[attr-defined]
        lh, table = project.layer(e.layer).lakehouse, f"{e.schema}_{e.table}"
        q = store.read(lh, "quarantine", table)
        if q is not None:
            # Rows held before ids existed get one from their position, the same on every read.
            ids = pl.Series([f"old-{i}" for i in range(q.height)], dtype=pl.String)
            q = q.with_columns(pl.col("_qid").fill_null(ids) if "_qid" in q.columns else ids.alias("_qid"))
        return store, lh, table, q

    def meta_quarantine(self, name: str, env: str = "dev", limit: int = 500) -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        e = project.entities[name]
        _, _, _, q = self._quarantine_table(project, name, env)
        meta = ["_qid", "_check", "_run_id", "_loaded_at"]
        if q is None or not q.height:
            return {"entity": name, "env": env, "columns": [], "rows": [], "total": 0, "checks": {},
                    "downstream": impact(project, name)}
        q = q.sort("_loaded_at", descending=True, nulls_last=True) if "_loaded_at" in q.columns else q
        columns = [c for c in q.columns if c not in meta]
        checks = q.group_by("_check").len().sort("len", descending=True) if "_check" in q.columns else None
        return {"entity": name, "env": env, "columns": columns, "editable": [c for c in columns if c in e.columns],
                "rows": _rows(q.select([c for c in meta if c in q.columns] + columns), limit), "total": q.height,
                "checks": {r["_check"] or "": r["len"] for r in checks.iter_rows(named=True)} if checks is not None else {},
                "downstream": impact(project, name)}

    def meta_replay(self, name: str, env: str, ids: list[str], edits: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Replay the chosen quarantined rows, with any fixed values, into the table. Rows that pass are
        written and leave quarantine; rows that fail a check again stay, with the new reason."""
        project = self._project()  # type: ignore[attr-defined]
        store, lh, table, q = self._quarantine_table(project, name, env)
        if q is None:
            raise LookupError(f"{name} has no quarantined rows in {env}")
        wanted = set(ids)
        picked = q.filter(pl.col("_qid").is_in(list(wanted)))
        if not picked.height:
            raise LookupError("Those rows are no longer in quarantine. Refresh and try again.")
        for qid, changes in (edits or {}).items():
            for col, val in changes.items():
                if col not in picked.columns:
                    continue
                new = None if val is None or val == "" else str(val)
                picked = picked.with_columns(pl.when(pl.col("_qid") == qid).then(pl.lit(new, dtype=pl.String))
                                             .otherwise(pl.col(col)).alias(col))
        res, held = Engine(project, store).replay(name, picked)
        out = res.to_dict()
        out["held"] = 0
        if res.status == "ok":
            rest = q.filter(~pl.col("_qid").is_in(list(wanted)))
            remaining = pl.concat([rest, held], how="diagonal_relaxed") if held is not None else rest
            store.write(lh, "quarantine", table, remaining, mode="overwrite")
            out["held"] = 0 if held is None else held.height
        out["downstream"] = impact(project, name)
        return out

    def meta_discard_quarantine(self, name: str, env: str, ids: list[str]) -> dict[str, Any]:
        """Remove quarantined rows that should never load (test data, true duplicates)."""
        project = self._project()  # type: ignore[attr-defined]
        store, lh, table, q = self._quarantine_table(project, name, env)
        if q is None:
            return self.meta_quarantine(name, env)
        store.write(lh, "quarantine", table, q.filter(~pl.col("_qid").is_in(list(ids))), mode="overwrite")
        return self.meta_quarantine(name, env)

    # ---- freshness

    def meta_set_freshness(self, name: str, within_hours: float | None = None, by: str | None = None,
                           envs: list[str] | None = None) -> dict[str, Any]:
        project = self._project()  # type: ignore[attr-defined]
        text = project.entities[name].path.read_text(encoding="utf-8")
        new = freshness_mod.set_promise(text, within_hours or None, by or None, envs or None)
        return self.meta_save_entity(name, new)  # type: ignore[attr-defined]

    def meta_freshness(self, notify: bool = False) -> dict[str, Any]:
        """Every freshness promise in every environment that has runs, late ones first. With
        ``notify``, newly late or failed tables are posted to the Teams channel, once each."""
        try:
            project = self._project()  # type: ignore[attr-defined]
        except Exception:
            return {"items": [], "unavailable": {}, "alerts": bool(self._settings().get("teams_webhook"))}  # type: ignore[attr-defined]
        data = self.meta_runs_all(limit=100000)  # type: ignore[attr-defined]
        envs = [env for env in ENVIRONMENTS if any(r["env"] == env for r in data["runs"])]
        items = freshness_mod.report({n: e.spec for n, e in project.entities.items()}, data["runs"], envs)
        out: dict[str, Any] = {"items": items, "unavailable": data["unavailable"],
                               "alerts": bool(self._settings().get("teams_webhook"))}  # type: ignore[attr-defined]
        if notify:
            out["alerted"], out["alert_error"] = self._freshness_alerts(items)
        return out

    def _alert_state(self) -> dict[str, str]:
        try:
            return json.loads((self.root / "freshness_alerts.json").read_text(encoding="utf-8"))  # type: ignore[attr-defined]
        except (FileNotFoundError, ValueError):
            return {}

    def _freshness_alerts(self, items: list[dict[str, Any]]) -> tuple[int, str | None]:
        url = self._settings().get("teams_webhook")  # type: ignore[attr-defined]
        if not url:
            return 0, None
        sent = self._alert_state()
        lines, keys = [], []
        for x in items:
            env = x["env"].title()
            if x["status"] == "late":
                key = f"late:{x['env']}:{x['entity']}:{x['last_ok']}"
                text = f"**{x['entity']}** in {env} is late: {'; '.join(x['reasons'])}."
            elif x.get("failed_run"):
                key = f"failed:{x['env']}:{x['entity']}:{x['failed_run']}"
                text = f"**{x['entity']}** in {env} failed: {x.get('failure') or 'see the run in Fabric Desktop'}."
            else:
                continue
            if key not in sent:
                lines.append(text)
                keys.append(key)
        if not lines:
            return 0, None
        try:
            post_teams(url, f"{len(lines)} table{'s' if len(lines) != 1 else ''} need attention", lines)
        except Exception as exc:  # the app still shows them; try again on the next check
            return 0, f"{type(exc).__name__}: {exc}"
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        sent.update({k: now for k in keys})
        (self.root / "freshness_alerts.json").write_text(json.dumps(sent, indent=1), encoding="utf-8")  # type: ignore[attr-defined]
        return len(lines), None

    def meta_alerts(self) -> dict[str, Any]:
        url = self._settings().get("teams_webhook", "")  # type: ignore[attr-defined]
        return {"teams_webhook": url}

    def meta_set_alerts(self, teams_webhook: str = "", test: bool = False) -> dict[str, Any]:
        url = (teams_webhook or "").strip()
        if url and not url.startswith("https://"):
            raise ValueError("The Teams webhook address starts with https://")
        if url and test:
            post_teams(url, "Fabric Desktop alerts are on",
                       ["Late or failed tables will be posted here, once each."])
        self._save_settings(teams_webhook=url)  # type: ignore[attr-defined]
        return self.meta_alerts()


def teams_card(title: str, lines: list[str]) -> dict[str, Any]:
    """A message for a Teams Workflows webhook ("When a Teams webhook request is received")."""
    body: list[dict[str, Any]] = [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True}]
    body += [{"type": "TextBlock", "text": line, "wrap": True, "spacing": "Small"} for line in lines[:20]]
    if len(lines) > 20:
        body.append({"type": "TextBlock", "text": f"and {len(lines) - 20} more in Fabric Desktop", "wrap": True})
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                        "version": "1.4", "body": body}}]}


def post_teams(url: str, title: str, lines: list[str]) -> None:
    r = requests.post(url, json=teams_card(title, lines), timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"Teams answered {r.status_code}: {r.text[:200]}")
