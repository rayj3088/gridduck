"""
Durable deferral queue, plus the part everyone skips: side-effect safety.

Deferring a call is easy. Deferring an agent loop that has already sent an
email, charged a card, or opened a pull request is not, because resumption
naively replays the side effect. You cannot un-send an email, so the queue
does not pretend it can.

Two-phase intent log:

    reserve(task, key, kind, payload)   -> before the side effect fires
    commit(task, key, result)           -> after it demonstrably succeeded

On resume, any intent that is RESERVED but not COMMITTED is in an unknown
state. The queue does not replay it and does not drop it. It surfaces it in
`orphans()` for reconciliation, because a human or an idempotency check at the
downstream service is the only correct arbiter. An honest unknown beats a
confident duplicate.

Leases prevent two workers draining the same task. Attempt counts and a
dead-letter state prevent poison tasks from cycling forever.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Iterator, List, Optional

PENDING, LEASED, DONE, FAILED, DEAD = "pending", "leased", "done", "failed", "dead"
RESERVED, COMMITTED = "reserved", "committed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    not_before    REAL NOT NULL,
    deadline      REAL,
    request_class TEXT NOT NULL,
    state         TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    lease_until   REAL,
    lease_owner   TEXT,
    payload       TEXT NOT NULL,
    checkpoint    TEXT,
    last_error    TEXT,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_ready ON tasks(state, not_before);
CREATE TABLE IF NOT EXISTS intents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL,
    payload     TEXT,
    result      TEXT,
    reserved_at REAL NOT NULL,
    settled_at  REAL,
    UNIQUE(task_id, key)
);
"""


@dataclass
class Task:
    id: str
    payload: dict
    request_class: str
    attempts: int
    checkpoint: Optional[dict]
    not_before: float
    deadline: Optional[float]

    @property
    def overdue(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline


class DeferQueue:
    def __init__(self, path: str = "loadslack-queue.db",
                 lease_s: float = 300.0, max_attempts: int = 5):
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self.lease_s = lease_s
        self.max_attempts = max_attempts
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -------------------------------------------------------------- enqueue

    def push(self, payload: dict, request_class: str = "background",
             delay_s: float = 0.0, deadline: Optional[float] = None,
             task_id: Optional[str] = None) -> str:
        tid = task_id or uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks (id, created_at, not_before,"
                " deadline, request_class, state, attempts, payload,"
                " updated_at) VALUES (?,?,?,?,?,?,0,?,?)",
                (tid, now, now + max(0.0, delay_s), deadline, request_class,
                 PENDING, json.dumps(payload), now))
            self._conn.commit()
        return tid

    # ---------------------------------------------------------------- lease

    def lease(self, owner: str = "worker", limit: int = 1) -> List[Task]:
        now = time.time()
        out: List[Task] = []
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE not_before <= ? AND"
                " (state = ? OR (state = ? AND lease_until < ?))"
                " ORDER BY not_before ASC LIMIT ?",
                (now, PENDING, LEASED, now, limit)).fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE tasks SET state=?, attempts=attempts+1,"
                    " lease_until=?, lease_owner=?, updated_at=? WHERE id=?",
                    (LEASED, now + self.lease_s, owner, now, r["id"]))
                out.append(Task(
                    id=r["id"], payload=json.loads(r["payload"]),
                    request_class=r["request_class"],
                    attempts=r["attempts"] + 1,
                    checkpoint=json.loads(r["checkpoint"])
                    if r["checkpoint"] else None,
                    not_before=r["not_before"], deadline=r["deadline"]))
            self._conn.commit()
        return out

    def checkpoint(self, task_id: str, state: dict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET checkpoint=?, lease_until=?, updated_at=?"
                " WHERE id=?",
                (json.dumps(state), time.time() + self.lease_s, time.time(),
                 task_id))
            self._conn.commit()

    def complete(self, task_id: str) -> None:
        self._set(task_id, DONE)

    def release(self, task_id: str, error: str = "",
                retry_in_s: float = 60.0) -> None:
        """Hand the task back. Dead-letters it past max_attempts."""
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute(
                "SELECT attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
            attempts = row["attempts"] if row else 0
            state = DEAD if attempts >= self.max_attempts else PENDING
            self._conn.execute(
                "UPDATE tasks SET state=?, not_before=?, last_error=?,"
                " lease_until=NULL, lease_owner=NULL, updated_at=? WHERE id=?",
                (state, time.time() + retry_in_s, error[:2000], time.time(),
                 task_id))
            self._conn.commit()

    def _set(self, task_id: str, state: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET state=?, lease_until=NULL,"
                " lease_owner=NULL, updated_at=? WHERE id=?",
                (state, time.time(), task_id))
            self._conn.commit()

    # --------------------------------------------------------- intent log

    def reserve(self, task_id: str, key: str, kind: str,
                payload: Optional[dict] = None) -> bool:
        """
        Call before an irreversible side effect. Returns False if this key was
        already committed, which is your idempotency check on resume.
        """
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute(
                "SELECT state FROM intents WHERE task_id=? AND key=?",
                (task_id, key)).fetchone()
            if row and row["state"] == COMMITTED:
                return False
            if row:
                return True  # already reserved, still unsettled: caller decides
            self._conn.execute(
                "INSERT INTO intents (task_id, key, kind, state, payload,"
                " reserved_at) VALUES (?,?,?,?,?,?)",
                (task_id, key, kind, RESERVED,
                 json.dumps(payload or {}), time.time()))
            self._conn.commit()
            return True

    def commit(self, task_id: str, key: str,
               result: Optional[dict] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE intents SET state=?, result=?, settled_at=?"
                " WHERE task_id=? AND key=?",
                (COMMITTED, json.dumps(result or {}), time.time(),
                 task_id, key))
            self._conn.commit()

    def orphans(self, older_than_s: float = 0.0) -> List[dict]:
        """
        Intents reserved but never committed. Unknown state. These are handed
        to a human or to a downstream idempotency check; the queue will not
        guess.
        """
        cut = time.time() - older_than_s
        self._conn.row_factory = sqlite3.Row
        return [dict(r) for r in self._conn.execute(
            "SELECT i.* FROM intents i JOIN tasks t ON t.id = i.task_id"
            " WHERE i.state=? AND i.reserved_at <= ?"
            " ORDER BY i.reserved_at", (RESERVED, cut))]

    # -------------------------------------------------------------- status

    def stats(self) -> dict:
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute(
            "SELECT state, COUNT(*) c FROM tasks GROUP BY state").fetchall()
        out = {k: 0 for k in (PENDING, LEASED, DONE, FAILED, DEAD)}
        out.update({r["state"]: r["c"] for r in rows})
        out["orphan_intents"] = len(self.orphans())
        nxt = self._conn.execute(
            "SELECT MIN(not_before) m FROM tasks WHERE state=?",
            (PENDING,)).fetchone()
        out["next_ready_at"] = nxt["m"] if nxt else None
        overdue = self._conn.execute(
            "SELECT COUNT(*) c FROM tasks WHERE deadline IS NOT NULL"
            " AND deadline < ? AND state IN (?,?)",
            (time.time(), PENDING, LEASED)).fetchone()
        out["overdue"] = overdue["c"] if overdue else 0
        return out

    def iter_all(self, state: Optional[str] = None) -> Iterator[dict]:
        self._conn.row_factory = sqlite3.Row
        q = "SELECT * FROM tasks"
        args: List = []
        if state:
            q += " WHERE state=?"
            args.append(state)
        q += " ORDER BY created_at"
        for r in self._conn.execute(q, args):
            yield dict(r)

    def close(self) -> None:
        self._conn.close()
