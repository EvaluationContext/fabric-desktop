"""The agent's durable job queue: SQLite on the agent's own disk.

A job moves queued -> running -> succeeded | failed | dead. ``running`` is a lease: the worker
that claimed it must renew ``lease_until`` while it works. When a worker dies (crash, reboot,
killed service), its lease runs out and :meth:`JobQueue.recover` puts the job back in the queue,
so it runs again rather than staying "running" forever. A job that fails with an error worth
retrying waits ``backoff * 2^(attempt-1)`` (capped) and runs again until ``max_attempts``; after
that it is ``dead`` and needs a person. A job whose tables failed their checks is ``failed``:
running it again would fail the same way.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

STATES = ("queued", "running", "succeeded", "failed", "dead")
ACTIVE = ("queued", "running")

_SCHEMA = """
create table if not exists jobs (
    id text primary key,
    kind text not null,
    payload text not null,
    state text not null,
    source text not null default '',
    dedupe_key text,
    attempts integer not null default 0,
    max_attempts integer not null,
    next_at real not null,
    owner text,
    lease_until real,
    created_at real not null,
    started_at real,
    finished_at real,
    result text,
    error text
);
create index if not exists jobs_ready on jobs (state, next_at);
create table if not exists schedule_state (
    id text primary key,
    next_at real not null
);
"""


class JobQueue:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time,
                 backoff_seconds: float = 60, max_backoff_seconds: float = 1800):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.backoff_seconds, self.max_backoff_seconds = backoff_seconds, max_backoff_seconds
        self._local = threading.local()
        self._db().executescript(_SCHEMA)  # executescript commits on its own

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("pragma journal_mode=wal")
            db.execute("pragma synchronous=full")  # a job that says succeeded stays succeeded after a power cut
            self._local.db = db
        return db

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = self._db()
        db.execute("begin immediate")  # one writer at a time, also across processes
        try:
            yield db
        except BaseException:
            db.execute("rollback")
            raise
        db.execute("commit")

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        out = dict(r)
        out["payload"] = json.loads(out["payload"])
        out["result"] = json.loads(out["result"]) if out["result"] else None
        return out

    # ---- adding work

    def enqueue(self, kind: str, payload: dict[str, Any], source: str = "", max_attempts: int = 3,
                dedupe_key: str | None = None, delay: float = 0) -> dict[str, Any]:
        """Add a job. With ``dedupe_key``, a job with the same key that is still queued or running is
        returned instead, so a schedule that fires while its last run is going doesn't pile up."""
        now = self.clock()
        with self._tx() as db:
            if dedupe_key:
                same = db.execute("select * from jobs where dedupe_key = ? and state in ('queued', 'running')",
                                  (dedupe_key,)).fetchone()
                if same:
                    return self._row(same) | {"deduplicated": True}  # type: ignore[operator]
            job_id = f"job-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}-{uuid.uuid4().hex[:6]}"
            db.execute("insert into jobs (id, kind, payload, state, source, dedupe_key, max_attempts, next_at, created_at)"
                       " values (?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                       (job_id, kind, json.dumps(payload), source, dedupe_key, max(1, int(max_attempts)), now + delay, now))
            return self._row(db.execute("select * from jobs where id = ?", (job_id,)).fetchone())  # type: ignore[return-value]

    # ---- working

    def claim(self, owner: str, lease_seconds: float) -> dict[str, Any] | None:
        """The oldest job that is due, now leased to ``owner``; None when nothing is due."""
        now = self.clock()
        with self._tx() as db:
            row = db.execute("select id from jobs where state = 'queued' and next_at <= ? order by next_at, created_at limit 1",
                             (now,)).fetchone()
            if row is None:
                return None
            db.execute("update jobs set state = 'running', owner = ?, lease_until = ?, attempts = attempts + 1,"
                       " started_at = ?, error = null where id = ?", (owner, now + lease_seconds, now, row["id"]))
            return self._row(db.execute("select * from jobs where id = ?", (row["id"],)).fetchone())

    def heartbeat(self, job_id: str, owner: str, lease_seconds: float) -> bool:
        """Extend the lease. False when the job is no longer this owner's (it was recovered)."""
        with self._tx() as db:
            cur = db.execute("update jobs set lease_until = ? where id = ? and owner = ? and state = 'running'",
                             (self.clock() + lease_seconds, job_id, owner))
            return cur.rowcount == 1

    def _finish(self, job_id: str, owner: str, state: str, result: Any = None, error: str | None = None) -> bool:
        with self._tx() as db:
            cur = db.execute("update jobs set state = ?, finished_at = ?, lease_until = null, result = ?, error = ?"
                             " where id = ? and owner = ? and state = 'running'",
                             (state, self.clock(), json.dumps(result, default=str) if result is not None else None,
                              error, job_id, owner))
            return cur.rowcount == 1

    def succeed(self, job_id: str, owner: str, result: Any = None) -> bool:
        return self._finish(job_id, owner, "succeeded", result)

    def fail(self, job_id: str, owner: str, error: str, result: Any = None) -> bool:
        """A failure retrying won't fix (a table failed its checks): no more attempts."""
        return self._finish(job_id, owner, "failed", result, error)

    def retry_later(self, job_id: str, owner: str, error: str) -> dict[str, Any] | None:
        """A failure worth another try (network, throttling, a table busy): back in the queue after the
        backoff, or ``dead`` once the attempts are used up. Returns the job as it now is."""
        now = self.clock()
        with self._tx() as db:
            job = db.execute("select * from jobs where id = ? and owner = ? and state = 'running'", (job_id, owner)).fetchone()
            if job is None:
                return None
            self._requeue(db, job, error, now)
            return self._row(db.execute("select * from jobs where id = ?", (job_id,)).fetchone())

    def _requeue(self, db: sqlite3.Connection, job: sqlite3.Row, error: str, now: float) -> None:
        if job["attempts"] >= job["max_attempts"]:
            db.execute("update jobs set state = 'dead', finished_at = ?, lease_until = null, error = ? where id = ?",
                       (now, f"Gave up after {job['attempts']} attempts. Last error: {error}", job["id"]))
            return
        wait = min(self.backoff_seconds * 2 ** (job["attempts"] - 1), self.max_backoff_seconds)
        db.execute("update jobs set state = 'queued', owner = null, lease_until = null, next_at = ?, error = ? where id = ?",
                   (now + wait, error, job["id"]))

    def recover(self) -> list[str]:
        """Running jobs whose worker stopped renewing (it crashed or was killed) go back in the queue,
        counting the lost run as an attempt. Returns their ids."""
        now = self.clock()
        with self._tx() as db:
            stale = db.execute("select * from jobs where state = 'running' and lease_until < ?", (now,)).fetchall()
            for job in stale:
                self._requeue(db, job, f"{job['owner']} stopped while running it", now)
            return [j["id"] for j in stale]

    def set_payload(self, job_id: str, payload: dict[str, Any]) -> bool:
        """Change what a queued job will do (a retry narrowed to the tables that didn't finish)."""
        with self._tx() as db:
            cur = db.execute("update jobs set payload = ? where id = ? and state = 'queued'", (json.dumps(payload), job_id))
            return cur.rowcount == 1

    # ---- reading

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._row(self._db().execute("select * from jobs where id = ?", (job_id,)).fetchone())

    def list(self, limit: int = 50, state: str | None = None) -> list[dict[str, Any]]:
        sql, args = "select * from jobs", ()
        if state:
            sql, args = sql + " where state = ?", (state,)
        rows = self._db().execute(sql + " order by created_at desc limit ?", (*args, limit)).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def counts(self) -> dict[str, int]:
        rows = self._db().execute("select state, count(*) as n from jobs group by state").fetchall()
        return {s: 0 for s in STATES} | {r["state"]: r["n"] for r in rows}

    # ---- schedules: when each fires next, kept here so a restart neither skips nor doubles a run

    def schedule_next(self, schedule_id: str) -> float | None:
        row = self._db().execute("select next_at from schedule_state where id = ?", (schedule_id,)).fetchone()
        return row["next_at"] if row else None

    def set_schedule_next(self, schedule_id: str, next_at: float) -> None:
        with self._tx() as db:
            db.execute("insert into schedule_state (id, next_at) values (?, ?)"
                       " on conflict(id) do update set next_at = excluded.next_at", (schedule_id, next_at))

    def close(self) -> None:
        db = getattr(self._local, "db", None)
        if db is not None:
            db.close()
            self._local.db = None
