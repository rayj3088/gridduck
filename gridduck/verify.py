"""
The verification layer. This is the product.

A mix engineer does not trust a compressor's numbers. They hit bypass, over
and over, and listen for whether they can tell. If you can hear it working,
it's wrong.

So the bypass button runs permanently. A deterministic slice of traffic passes
completely ungoverned, and the driver continuously compares the two
populations. The claim is never historical and never a benchmark someone ran
last quarter -- it is a live measurement that says: right now, governed and
ungoverned traffic are indistinguishable, and here is the number.

Two things are proven here, and the distinction matters because someone will
try to blur it:

  LATENCY      provable, continuously, cheaply. Governed vs holdout, with a
               confidence interval. We claim this.

  IDENTITY     provable for eliminated work specifically. A served duplicate
               is byte-identical to the stored answer, which is what the model
               produced. We claim this, scoped to the eliminated slice.

  QUALITY      NOT provable cheaply. We do not claim it. The driver's defence
               is structural -- no mechanism alters an answer -- not
               empirical. Anyone who tells you their middleware has been
               proven not to degrade quality is selling you something.

The latency ledger enforces the invariant that makes this installable: a cache
hit RETURNS time to the user, coalescing SPENDS it, and net perceived latency
change must stay zero or better. You only ever spend milliseconds you already
earned.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional, Tuple


def in_holdout(key: str, fraction: float) -> bool:
    """
    Deterministic on the request key, so the same logical call always lands on
    the same side. Random assignment would let a single slow site drift into
    one arm and poison the comparison.
    """
    if fraction <= 0:
        return False
    h = int.from_bytes(hashlib.blake2b(key.encode(), digest_size=4).digest(),
                       "big")
    return (h % 1_000_000) < int(fraction * 1_000_000)


@dataclass
class Sample:
    ttft_ms: float
    total_ms: float
    ts: float


class _Arm:
    def __init__(self, maxlen: int = 20000):
        self.s: Deque[Sample] = deque(maxlen=maxlen)

    def add(self, ttft: float, total: float, ts: float) -> None:
        self.s.append(Sample(ttft, total, ts))

    def expire(self, cutoff: float) -> None:
        while self.s and self.s[0].ts < cutoff:
            self.s.popleft()

    def stats(self) -> Tuple[int, float, float, float]:
        """n, mean ttft, variance, p95 ttft"""
        n = len(self.s)
        if n == 0:
            return 0, 0.0, 0.0, 0.0
        vals = [x.ttft_ms for x in self.s]
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / max(1, n - 1)
        srt = sorted(vals)
        p95 = srt[min(n - 1, int(n * 0.95))]
        return n, mean, var, p95


@dataclass
class LatencyLedger:
    """
    Running balance in milliseconds. Credits come from eliminated work that
    returned instantly; debits come from coalescing. The invariant is that the
    balance never goes negative -- we never spend latency we have not earned.
    """
    credits_ms: float = 0.0
    debits_ms: float = 0.0
    requests: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def credit(self, ms: float) -> None:
        with self._lock:
            self.credits_ms += max(0.0, ms)
            self.requests += 1

    def debit(self, ms: float) -> None:
        with self._lock:
            self.debits_ms += max(0.0, ms)
            self.requests += 1

    @property
    def net_ms(self) -> float:
        """Positive means we have returned more time than we spent."""
        return self.credits_ms - self.debits_ms

    @property
    def per_request_ms(self) -> float:
        return self.net_ms / self.requests if self.requests else 0.0

    def affordable(self, ms: float) -> bool:
        """May we spend this much? Only out of an existing surplus."""
        return (self.net_ms - ms) >= 0.0

    def as_dict(self) -> dict:
        return {"credits_ms": round(self.credits_ms, 1),
                "debits_ms": round(self.debits_ms, 1),
                "net_ms": round(self.net_ms, 1),
                "net_per_request_ms": round(self.per_request_ms, 2),
                "requests": self.requests,
                "invariant_holds": self.net_ms >= 0.0}


class Verifier:
    def __init__(self, holdout_fraction: float = 0.03,
                 min_samples: int = 200, window_s: float = 3600.0):
        self.fraction = holdout_fraction
        self.min_samples = min_samples
        # Rolling window. A good stretch last month must not prop up today's
        # verdict -- the claim is about right now, or it is worthless.
        self.window_s = window_s
        self.governed = _Arm()
        self.holdout = _Arm()
        self.ledger = LatencyLedger()
        self.identity_checks = 0
        self.identity_failures = 0
        self._lock = threading.Lock()

    def assign(self, key: str) -> bool:
        """True = this request is in the holdout and must pass ungoverned."""
        return in_holdout(key, self.fraction)

    def observe(self, held_out: bool, ttft_ms: float, total_ms: float,
                now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            (self.holdout if held_out else self.governed).add(
                ttft_ms, total_ms, now)

    def check_identity(self, served: object, canonical: object) -> bool:
        """
        Eliminated work must be byte-identical to what was stored. A failure
        here is a correctness bug in the cache, not a quality judgement.
        """
        with self._lock:
            self.identity_checks += 1
            ok = served == canonical
            if not ok:
                self.identity_failures += 1
        return ok

    # -------------------------------------------------------------- proof

    def proof(self) -> dict:
        """
        Welch's t on TTFT between arms, plus a 95% CI on the difference of
        means. Verdict is deliberately three-valued: a wide CI with few
        samples is INSUFFICIENT, not PASS. Declaring victory on thin data is
        how everyone else's benchmark lies.
        """
        with self._lock:
            cutoff = time.time() - self.window_s
            self.governed.expire(cutoff)
            self.holdout.expire(cutoff)
            ng, mg, vg, pg = self.governed.stats()
            nh, mh, vh, ph = self.holdout.stats()

        out = {
            "governed": {"n": ng, "mean_ttft_ms": round(mg, 1),
                         "p95_ttft_ms": round(pg, 1)},
            "holdout": {"n": nh, "mean_ttft_ms": round(mh, 1),
                        "p95_ttft_ms": round(ph, 1)},
            "holdout_fraction": self.fraction,
            "window_s": self.window_s,
            "latency_ledger": self.ledger.as_dict(),
            "identity": {"checked": self.identity_checks,
                         "failures": self.identity_failures},
            "claims": {
                "latency": "measured continuously against a live holdout",
                "identity": "byte-equality on eliminated work only",
                "quality": "NOT measured. Defence is structural: no mechanism "
                           "in this driver alters a model's answer.",
            },
        }

        if ng < self.min_samples or nh < max(30, self.min_samples // 10):
            out["verdict"] = "INSUFFICIENT_DATA"
            out["detail"] = (f"need >={self.min_samples} governed and "
                             f">=30 holdout samples")
            return out

        diff = mg - mh          # positive = governed is slower
        se = math.sqrt(vg / ng + vh / nh) or 1e-9
        ci = 1.96 * se
        out["ttft_delta_ms"] = {"point": round(diff, 2),
                                "lo": round(diff - ci, 2),
                                "hi": round(diff + ci, 2)}

        if self.identity_failures:
            out["verdict"] = "FAIL_IDENTITY"
        elif diff + ci <= 0:
            out["verdict"] = "PASS_FASTER"
        elif diff - ci <= 0 <= diff + ci:
            out["verdict"] = "PASS_INDISTINGUISHABLE"
        else:
            out["verdict"] = "FAIL_SLOWER"
        return out

    def datasheet_line(self) -> str:
        p = self.proof()
        v = p["verdict"]
        if v == "INSUFFICIENT_DATA":
            return "Not yet provable: insufficient samples."
        d = p.get("ttft_delta_ms", {})
        if v.startswith("PASS"):
            return (f"Net perceived latency change "
                    f"{d.get('point', 0):+.1f} ms "
                    f"(95% CI {d.get('lo', 0):+.1f} to {d.get('hi', 0):+.1f}), "
                    f"n={p['governed']['n']} governed vs "
                    f"{p['holdout']['n']} ungoverned. "
                    f"{p['identity']['failures']} identity failures.")
        return (f"FAILING: {v}. Governed traffic is measurably worse. "
                f"Reduce effort or disable.")
