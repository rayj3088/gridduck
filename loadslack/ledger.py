"""
The receipt ledger. This is the part anyone actually pays for.

The mechanism (compression, ladder, queue) is worth having but is not
defensible. What a facility under a curtailment tariff needs at 9am the
morning after an event is an artifact that says: the signal arrived at
14:02:11, we began reducing at 14:02:31, we held an average 31% reduction for
47 minutes, here is the per-decision trail, and here is proof that none of it
was edited afterward.

Tamper-evidence is a hash chain: every row's digest covers the previous row's
digest, so altering or deleting any row invalidates every row after it. The
chain head is then HMAC-signed with a key the operator holds, which makes
silent rewriting of the whole chain detectable too.

This is not a blockchain and does not want to be. It is an append-only log
with a checkable invariant, which is what an auditor actually asks for.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional

GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    region      TEXT,
    stress      REAL,
    required    REAL,
    achieved    REAL,
    shortfall   REAL,
    limiting    INTEGER NOT NULL DEFAULT 0,
    order_id    TEXT,
    wh_point    REAL,
    wh_lo       REAL,
    wh_hi       REAL,
    body        TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_receipts_ts ON receipts(ts);
CREATE TABLE IF NOT EXISTS attestations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    head_seq    INTEGER NOT NULL,
    head_hash   TEXT NOT NULL,
    signature   TEXT NOT NULL,
    key_id      TEXT NOT NULL
);
"""


@dataclass
class Attestation:
    ts: float
    head_seq: int
    head_hash: str
    signature: str
    key_id: str

    def as_dict(self) -> dict:
        return {"ts": self.ts, "head_seq": self.head_seq,
                "head_hash": self.head_hash, "signature": self.signature,
                "key_id": self.key_id}


def _digest(prev_hash: str, ts: float, kind: str, body: str) -> str:
    h = hashlib.sha256()
    h.update(prev_hash.encode())
    h.update(f"|{ts:.6f}|{kind}|".encode())
    h.update(body.encode())
    return h.hexdigest()


class Ledger:
    def __init__(self, path: str = "loadslack-receipts.db",
                 hmac_key: Optional[bytes] = None, key_id: str = "default"):
        self.path = path
        self.key_id = key_id
        self._key = hmac_key or self._load_or_create_key(path)
        self._lock = threading.Lock()
        need_dir = os.path.dirname(os.path.abspath(path))
        if need_dir:
            os.makedirs(need_dir, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------ keying

    @staticmethod
    def _load_or_create_key(db_path: str) -> bytes:
        key_path = os.path.splitext(db_path)[0] + ".key"
        env = os.environ.get("LOADSLACK_HMAC_KEY")
        if env:
            return env.encode()
        if os.path.exists(key_path):
            with open(key_path, "rb") as fh:
                return fh.read().strip()
        key = hashlib.sha256(os.urandom(32)).hexdigest().encode()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key

    # ------------------------------------------------------------ writing

    def head(self) -> tuple:
        cur = self._conn.execute(
            "SELECT seq, hash FROM receipts ORDER BY seq DESC LIMIT 1")
        row = cur.fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    def append(self, kind: str, payload: dict,
               ts: Optional[float] = None) -> dict:
        ts = time.time() if ts is None else ts
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self._lock:
            _, prev = self.head()
            digest = _digest(prev, ts, kind, body)
            wh = payload.get("energy_avoided_wh") or {}
            self._conn.execute(
                "INSERT INTO receipts (ts, kind, region, stress, required,"
                " achieved, shortfall, limiting, order_id, wh_point, wh_lo,"
                " wh_hi, body, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, kind, payload.get("region"), payload.get("stress"),
                 payload.get("required"), payload.get("achieved"),
                 payload.get("shortfall"),
                 1 if payload.get("limiting") else 0,
                 payload.get("order_id"), wh.get("point"), wh.get("lo"),
                 wh.get("hi"), body, prev, digest))
            self._conn.commit()
        return {"ts": ts, "kind": kind, "hash": digest, "prev_hash": prev}

    # ------------------------------------------------------------ reading

    def rows(self, since: float = 0.0, until: Optional[float] = None,
             kind: Optional[str] = None) -> Iterator[sqlite3.Row]:
        q = "SELECT * FROM receipts WHERE ts >= ?"
        args: List = [since]
        if until is not None:
            q += " AND ts < ?"
            args.append(until)
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY seq ASC"
        self._conn.row_factory = sqlite3.Row
        return self._conn.execute(q, args)

    # ---------------------------------------------------------- integrity

    def verify(self) -> dict:
        """Recompute the whole chain. Returns first break, if any."""
        prev = GENESIS
        n = 0
        self._conn.row_factory = sqlite3.Row
        for row in self._conn.execute("SELECT * FROM receipts ORDER BY seq"):
            if row["prev_hash"] != prev:
                return {"ok": False, "checked": n, "break_at_seq": row["seq"],
                        "reason": "prev_hash mismatch"}
            calc = _digest(prev, row["ts"], row["kind"], row["body"])
            if calc != row["hash"]:
                return {"ok": False, "checked": n, "break_at_seq": row["seq"],
                        "reason": "row digest mismatch"}
            prev = row["hash"]
            n += 1
        bad = []
        for a in self.attestations():
            expect = hmac.new(self._key,
                              f"{a.head_seq}:{a.head_hash}".encode(),
                              hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expect, a.signature):
                bad.append(a.head_seq)
        return {"ok": not bad, "checked": n, "head_hash": prev,
                "bad_attestations": bad}

    def attest(self) -> Attestation:
        seq, h = self.head()
        sig = hmac.new(self._key, f"{seq}:{h}".encode(),
                       hashlib.sha256).hexdigest()
        ts = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO attestations (ts, head_seq, head_hash,"
                " signature, key_id) VALUES (?,?,?,?,?)",
                (ts, seq, h, sig, self.key_id))
            self._conn.commit()
        return Attestation(ts, seq, h, sig, self.key_id)

    def attestations(self) -> List[Attestation]:
        self._conn.row_factory = sqlite3.Row
        return [Attestation(r["ts"], r["head_seq"], r["head_hash"],
                            r["signature"], r["key_id"])
                for r in self._conn.execute(
                    "SELECT * FROM attestations ORDER BY id")]

    # ------------------------------------------------------------ reports

    def event_report(self, since: float, until: Optional[float] = None) -> dict:
        until = time.time() if until is None else until
        rows = list(self.rows(since, until, kind="decision"))
        if not rows:
            return {"decisions": 0, "window": [since, until]}
        dur = max(1e-9, until - since)
        wsum = sum((r["achieved"] or 0.0) for r in rows)
        wh = [sum((r[k] or 0.0) for r in rows)
              for k in ("wh_point", "wh_lo", "wh_hi")]
        short = [r for r in rows if (r["shortfall"] or 0) > 1e-6]
        return {
            "window": [since, until],
            "duration_s": round(dur, 1),
            "decisions": len(rows),
            "mean_reduction": round(wsum / len(rows), 4),
            "peak_reduction": round(max((r["achieved"] or 0) for r in rows), 4),
            "decisions_with_shortfall": len(short),
            "max_shortfall": round(
                max([(r["shortfall"] or 0) for r in rows] + [0.0]), 4),
            "energy_avoided_wh": {"point": round(wh[0], 3),
                                  "lo": round(wh[1], 3),
                                  "hi": round(wh[2], 3)},
            "under_order": sum(1 for r in rows if r["limiting"]),
            "integrity": self.verify(),
        }

    def close(self) -> None:
        self._conn.close()
