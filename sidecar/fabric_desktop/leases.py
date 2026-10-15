"""Per-table write leases, so two writers never read-merge-overwrite the same table at once.

The metadata engine writes a table by reading it, merging new rows in memory and committing
the result. Two writers doing that at once can each miss the other's rows. A lease makes the
second writer wait: before running an entity, a worker creates a small lease file for its
table, keeps renewing it while it works, and deletes it when done. A worker that dies stops
renewing; once the lease expires, the next writer takes it over.

For an environment in OneLake the lease files live in its ``LH_Meta`` lakehouse under
``Files/_fabric_desktop/leases/`` and every change is conditional (``If-None-Match: *`` to
create, ``If-Match: <etag>`` to renew, take over or delete), so agents on different machines
see the same leases with no extra service. For a local environment they are files in its
``.local`` folder.

Expiry times are the holder's clock, so keep agent clocks in sync (NTP); the lease length
(default 120 s) is far longer than normal clock drift.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

import requests

from .onelake import DFS_HOST

LEASE_DIR = "Files/_fabric_desktop/leases"


class LeaseBusy(RuntimeError):
    """Another writer holds the table. Worth retrying later."""


class LeaseLost(RuntimeError):
    """This writer's lease expired or was taken over while it worked."""


@dataclass
class Stored:
    data: bytes
    etag: str
    modified: float  # epoch seconds


class Backend(Protocol):
    def create(self, key: str, data: bytes) -> str | None: ...           # new etag, or None if it exists
    def read(self, key: str) -> Stored | None: ...
    def replace(self, key: str, data: bytes, etag: str) -> str | None: ...  # new etag, or None if it changed
    def delete(self, key: str, etag: str) -> bool: ...


class LocalBackend:
    """Lease files in a folder on this machine (local environments, and tests)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        return self.folder / (key.replace("/", "__") + ".json")

    @staticmethod
    def _etag(data: bytes) -> str:
        return hashlib.sha1(data).hexdigest()

    def create(self, key: str, data: bytes) -> str | None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return self._etag(data)

    def read(self, key: str) -> Stored | None:
        path = self._path(key)
        try:
            data = path.read_bytes()
            return Stored(data, self._etag(data), path.stat().st_mtime)
        except FileNotFoundError:
            return None

    def replace(self, key: str, data: bytes, etag: str) -> str | None:
        with self._lock:
            now = self.read(key)
            if now is None or now.etag != etag:
                return None
            tmp = self._path(key).with_suffix(f".{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, self._path(key))
            return self._etag(data)

    def delete(self, key: str, etag: str) -> bool:
        with self._lock:
            now = self.read(key)
            if now is None or now.etag != etag:
                return False
            self._path(key).unlink(missing_ok=True)
            return True


class OneLakeBackend:
    """Lease files in a lakehouse's Files area, changed only with ADLS Gen2 conditional requests."""

    def __init__(self, workspace_id: str, lakehouse_id: str, token: Callable[[], str],
                 session: requests.Session | None = None):
        self.workspace_id, self.lakehouse_id = workspace_id, lakehouse_id
        self._token = token
        self._http = session or requests.Session()

    def _url(self, key: str) -> str:
        return f"https://{DFS_HOST}/{self.workspace_id}/{self.lakehouse_id}/{LEASE_DIR}/{key}.json"

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "x-ms-version": "2023-11-03"} | extra

    def _write(self, key: str, data: bytes, condition: dict[str, str]) -> str | None:
        url = self._url(key)
        resp = self._http.put(url, params={"resource": "file"}, headers=self._headers(**condition), timeout=60)
        if resp.status_code in (409, 412):
            return None  # it exists already (create), or changed since we read it (replace)
        resp.raise_for_status()
        etag = resp.headers.get("ETag", "")
        resp = self._http.patch(url, params={"action": "append", "position": "0"}, data=data,
                                headers=self._headers(), timeout=60)
        resp.raise_for_status()
        resp = self._http.patch(url, params={"action": "flush", "position": str(len(data))},
                                headers=self._headers(**({"If-Match": etag} if etag else {})), timeout=60)
        if resp.status_code == 412:
            return None  # someone replaced it between our create and flush
        resp.raise_for_status()
        return resp.headers.get("ETag", etag)

    def create(self, key: str, data: bytes) -> str | None:
        return self._write(key, data, {"If-None-Match": "*"})

    def read(self, key: str) -> Stored | None:
        resp = self._http.get(self._url(key), headers=self._headers(), timeout=60)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        try:
            modified = parsedate_to_datetime(resp.headers["Last-Modified"]).timestamp()
        except (KeyError, TypeError, ValueError):
            modified = time.time()
        return Stored(resp.content, resp.headers.get("ETag", ""), modified)

    def replace(self, key: str, data: bytes, etag: str) -> str | None:
        return self._write(key, data, {"If-Match": etag})

    def delete(self, key: str, etag: str) -> bool:
        resp = self._http.delete(self._url(key), headers=self._headers(**{"If-Match": etag}), timeout=60)
        if resp.status_code in (404, 409, 412):
            return False
        resp.raise_for_status()
        return True


class Leases:
    def __init__(self, backend: Backend, owner: str, ttl: float = 120, wait: float = 120,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.backend, self.owner, self.ttl, self.wait = backend, owner, ttl, wait
        self.clock, self.sleep = clock, sleep

    def _body(self) -> bytes:
        now = self.clock()
        return json.dumps({"owner": self.owner, "host": socket.gethostname(), "pid": os.getpid(),
                           "acquired_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
                           "expires": now + self.ttl}).encode()

    def _expires(self, stored: Stored) -> tuple[float, str]:
        try:
            body = json.loads(stored.data)
            return float(body["expires"]), str(body.get("owner", "?"))
        except (ValueError, KeyError, TypeError):
            # Empty while its writer is between create and flush: treat it as fresh.
            return stored.modified + self.ttl, "?"

    def try_acquire(self, key: str) -> str | None:
        """The new lease's etag, or None while someone else holds a live lease."""
        etag = self.backend.create(key, self._body())
        if etag:
            return etag
        stored = self.backend.read(key)
        if stored is None:
            return self.backend.create(key, self._body())  # released just now
        expires, _ = self._expires(stored)
        if expires < self.clock():
            return self.backend.replace(key, self._body(), stored.etag)  # take over an expired lease
        return None

    def holder(self, key: str) -> str:
        stored = self.backend.read(key)
        return self._expires(stored)[1] if stored else ""

    @contextmanager
    def hold(self, key: str) -> Iterator["Held"]:
        """Hold ``key`` for the block: wait up to ``wait`` seconds for it, renew it every third of
        its length, release it after. Raises LeaseBusy when it stays taken."""
        deadline = self.clock() + self.wait
        delay = 1.0
        while (etag := self.try_acquire(key)) is None:
            if self.clock() >= deadline:
                raise LeaseBusy(f"{key} is being written by {self.holder(key) or 'another writer'}; "
                                f"will try again later")
            self.sleep(delay)
            delay = min(delay * 2, 15)
        held = Held(self, key, etag)
        held.start()
        try:
            yield held
        finally:
            held.stop()
        if held.lost:
            raise LeaseLost(f"The lease on {key} expired while writing, so another writer may have overlapped. "
                            f"Check the table's history before running it again.")


class Held:
    def __init__(self, leases: Leases, key: str, etag: str):
        self.leases, self.key, self.etag = leases, key, etag
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._renew, daemon=True, name=f"lease-{key}")

    def start(self) -> None:
        self._thread.start()

    def _renew(self) -> None:
        while not self._stop.wait(self.leases.ttl / 3):
            try:
                etag = self.leases.backend.replace(self.key, self.leases._body(), self.etag)
            except Exception:  # a network blip: try again at the next beat, the lease still has time
                continue
            if etag is None:
                self.lost = True
                return
            self.etag = etag

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
        if not self.lost:
            try:
                self.leases.backend.delete(self.key, self.etag)
            except Exception:  # it expires on its own
                pass


def table_key(lakehouse: str, schema: str, table: str) -> str:
    return f"{lakehouse}/{schema}.{table}"
