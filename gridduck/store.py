"""
Cache backing and in-flight coordination.

Two problems solved here, both of which make the difference between a demo and
a deployment:

1. A per-process cache halves its own hit rate the moment you run two workers,
   which every real deployment does. `SqliteBackend` shares state across every
   process on a host with no daemon and no dependency. Multi-host needs a
   network store; the `CacheBackend` interface is the seam for that, and
   `RedisBackend` is forty lines whenever somebody needs it.

2. In-flight collapse has to actually collapse. Detecting that an identical
   call is already upstream is useless unless the second caller can wait for
   the first one's answer. That needs a leader election and a waiter, and it
   needs a timeout, because the leader can die.

The in-flight coordinator is deliberately in-process only. Cross-process
leader election needs locks with fencing tokens and is a much worse trade:
worst case here is that two workers each make one upstream call, which is
exactly what would have happened without the driver.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


_SIGN = 1 << 63
_WRAP = 1 << 64


def _to_sqlite_int(v: int) -> int:
    """
    Simhashes are UNSIGNED 64-bit. SQLite INTEGER is SIGNED 64-bit, so any
    hash with the top bit set overflows on write. Wrap into signed range here
    and unwrap on read; the bit pattern -- which is all Hamming distance
    cares about -- is preserved exactly.
    """
    v &= _WRAP - 1
    return v - _WRAP if v >= _SIGN else v


def _from_sqlite_int(v: int) -> int:
    return v + _WRAP if v < 0 else v


@dataclass
class Entry:
    response: Any
    ts: float
    output_tokens: int
    tokens: Tuple[str, ...]
    sim: int


class CacheBackend:
    """Interface. Implementations must be safe for concurrent use."""

    def get(self, key: str) -> Optional[Entry]: ...

    def put(self, key: str, entry: Entry) -> None: ...

    def recent(self, limit: int) -> List[Tuple[int, str]]:
        """(simhash, key) newest first, for near-duplicate scanning."""
        ...

    def evict(self, older_than: float, max_entries: int) -> None: ...

    def size(self) -> int: ...

    def close(self) -> None:
        pass


class MemoryBackend(CacheBackend):
    def __init__(self, max_sims: int = 4096):
        self._d: Dict[str, Entry] = {}
        self._order: List[Tuple[int, str]] = []
        self._max_sims = max_sims
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Entry]:
        with self._lock:
            return self._d.get(key)

    def put(self, key: str, entry: Entry) -> None:
        with self._lock:
            self._d[key] = entry
            self._order.append((entry.sim, key))
            if len(self._order) > self._max_sims:
                self._order = self._order[-self._max_sims:]

    def recent(self, limit: int) -> List[Tuple[int, str]]:
        with self._lock:
            return list(reversed(self._order[-limit:]))

    def evict(self, older_than: float, max_entries: int) -> None:
        with self._lock:
            dead = [k for k, v in self._d.items() if v.ts < older_than]
            for k in dead:
                self._d.pop(k, None)
            if len(self._d) > max_entries:
                keep = sorted(self._d, key=lambda k: self._d[k].ts,
                              reverse=True)[:max_entries]
                self._d = {k: self._d[k] for k in keep}
            live = set(self._d)
            self._order = [(s, k) for s, k in self._order if k in live]

    def size(self) -> int:
        with self._lock:
            return len(self._d)


_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS cache (
    key           TEXT PRIMARY KEY,
    sim           INTEGER NOT NULL,
    ts            REAL NOT NULL,
    output_tokens INTEGER NOT NULL,
    tokens        TEXT NOT NULL,
    response      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_ts ON cache(ts DESC);
"""


class SqliteBackend(CacheBackend):
    """
    Shared across every process on one host. WAL mode means readers never
    block the writer, which matters because the read path is on the hot line
    of every request.

    Responses are stored as JSON. Anything not JSON-serialisable is refused at
    write time rather than corrupting the cache -- a cache that silently drops
    writes is worse than no cache, because the hit-rate number lies.
    """

    def __init__(self, path: str = "gridduck-cache.db", timeout_s: float = 5.0):
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self._local = threading.local()
        self._timeout = timeout_s
        con = self._conn()
        con.executescript(_SQL)
        con.commit()
        self.write_failures = 0

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=self._timeout)
            c.row_factory = sqlite3.Row
            self._local.c = c
        return c

    def get(self, key: str) -> Optional[Entry]:
        try:
            r = self._conn().execute(
                "SELECT * FROM cache WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        if not r:
            return None
        try:
            return Entry(json.loads(r["response"]), r["ts"],
                         r["output_tokens"], tuple(json.loads(r["tokens"])),
                         _from_sqlite_int(r["sim"]))
        except (ValueError, TypeError):
            return None

    def put(self, key: str, entry: Entry) -> None:
        try:
            payload = json.dumps(entry.response)
        except (TypeError, ValueError):
            self.write_failures += 1
            return
        try:
            c = self._conn()
            c.execute(
                "INSERT OR REPLACE INTO cache (key, sim, ts, output_tokens,"
                " tokens, response) VALUES (?,?,?,?,?,?)",
                (key, _to_sqlite_int(entry.sim), entry.ts,
                 entry.output_tokens, json.dumps(list(entry.tokens)),
                 payload))
            c.commit()
        except (sqlite3.Error, OverflowError, ValueError):
            self.write_failures += 1

    def recent(self, limit: int) -> List[Tuple[int, str]]:
        try:
            return [(_from_sqlite_int(r["sim"]), r["key"])
                    for r in self._conn().execute(
                "SELECT sim, key FROM cache ORDER BY ts DESC LIMIT ?",
                (limit,))]
        except sqlite3.Error:
            return []

    def evict(self, older_than: float, max_entries: int) -> None:
        try:
            c = self._conn()
            c.execute("DELETE FROM cache WHERE ts < ?", (older_than,))
            c.execute(
                "DELETE FROM cache WHERE key NOT IN "
                "(SELECT key FROM cache ORDER BY ts DESC LIMIT ?)",
                (max_entries,))
            c.commit()
        except sqlite3.Error:
            pass

    def size(self) -> int:
        try:
            return self._conn().execute(
                "SELECT COUNT(*) c FROM cache").fetchone()["c"]
        except sqlite3.Error:
            return 0

    def close(self) -> None:
        c = getattr(self._local, "c", None)
        if c is not None:
            c.close()
            self._local.c = None


class Inflight:
    """
    Leader election for identical concurrent calls.

    `claim(key)` returns (True, None) for the first caller -- it owns the
    upstream call. Every subsequent caller gets (False, waiter) and blocks on
    `waiter.wait(timeout)`.

    The leader MUST call `settle()` or `fail()`. If it dies without doing
    either, followers wake on the timeout and proceed independently, which is
    exactly what would have happened with no driver at all. Degrading to
    baseline is the correct failure mode for an optimisation.
    """

    def __init__(self, lease_s: float = 120.0):
        self.lease_s = lease_s
        self._lock = threading.Lock()
        self._waiters: Dict[str, Tuple[threading.Event, float, list]] = {}
        self.collapsed = 0
        self.timeouts = 0

    def claim(self, key: str, now: Optional[float] = None):
        now = time.time() if now is None else now
        with self._lock:
            slot = self._waiters.get(key)
            if slot is not None and (now - slot[1]) < self.lease_s:
                self.collapsed += 1
                return False, slot
            ev = threading.Event()
            self._waiters[key] = (ev, now, [])
            return True, None

    def settle(self, key: str, response: Any) -> None:
        with self._lock:
            slot = self._waiters.pop(key, None)
        if slot:
            slot[2].append(response)
            slot[0].set()

    def fail(self, key: str) -> None:
        with self._lock:
            slot = self._waiters.pop(key, None)
        if slot:
            slot[0].set()

    def wait(self, slot, timeout: float = 60.0) -> Optional[Any]:
        ev, _started, box = slot
        if not ev.wait(timeout):
            self.timeouts += 1
            return None
        return box[0] if box else None

    def stats(self) -> dict:
        with self._lock:
            pending = len(self._waiters)
        return {"collapsed": self.collapsed, "timeouts": self.timeouts,
                "pending": pending}
