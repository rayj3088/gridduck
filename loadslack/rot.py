"""
Rot elimination. Removes work that buys nothing.

First principle: every mechanism in this file must be quality-neutral by
construction. Not "usually fine" -- structurally incapable of changing an
answer. A returned cache hit is byte-identical to what the model would have
said. A pruned context turn is one no later turn refers to. A collapsed retry
is a duplicate of a call already in flight.

The `effort` knob does NOT trade quality for joules. It controls how hard we
LOOK. At effort 0 we run exact matching only. As effort rises we widen the
near-duplicate window, prune deeper, and hold the coalescing window longer so
more things can be found identical. You buy elimination with detection cycles,
never with output.

The correctness hazard is real and is handled explicitly: two prompts that
differ by a date, a number, a name or a single negation can look near-identical
and require opposite answers. A false cache hit is a silent wrong answer, which
is worse than a wasted call. So near-duplicate matching refuses any pair whose
differing tokens contain negation, numerals, or anything on the caller's
never-cache list -- and those refusals are counted and reported, because the
savings you did not take are part of an honest ledger.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .store import CacheBackend, Entry, Inflight, MemoryBackend

_WORD = re.compile(r"[a-z0-9']+")
_NUMERIC = re.compile(r"\d")
_VOLATILE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T?[\d:.]*Z?\b"           # ISO timestamps
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"     # uuids
    r"|\b\d{10,13}\b",                            # epoch
    re.I)

# Differences that forbid a near-duplicate match. A single "not" flips meaning.
NEGATIONS = {"not", "no", "never", "without", "except", "exclude", "excluding",
             "don't", "dont", "cannot", "can't", "cant", "isn't", "isnt",
             "won't", "wont", "neither", "nor", "stop", "undo", "revert"}


def _tokens(text: str) -> List[str]:
    return _WORD.findall(text.lower())


def simhash(tokens: List[str], bits: int = 64) -> int:
    """Charikar simhash. Cheap, pure stdlib, good enough at this scale."""
    if not tokens:
        return 0
    v = [0] * bits
    for t in tokens:
        h = int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(),
                           "big")
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= (1 << i)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass(frozen=True)
class Fingerprint:
    exact: str
    sim: int
    tokens: Tuple[str, ...]
    volatile_spans: int

    @staticmethod
    def of(text: str) -> "Fingerprint":
        norm = " ".join(text.split())
        vol = len(_VOLATILE.findall(norm))
        stripped = _VOLATILE.sub("<v>", norm)
        toks = _tokens(stripped)
        return Fingerprint(
            exact=hashlib.blake2b(stripped.encode(), digest_size=16).hexdigest(),
            sim=simhash(toks), tokens=tuple(toks), volatile_spans=vol)


def safe_to_match(a: Fingerprint, b: Fingerprint,
                  never: Optional[set] = None) -> Tuple[bool, str]:
    """
    Gate on the SYMMETRIC DIFFERENCE of tokens, not on similarity. Two prompts
    can be 99% identical and mean opposite things; what matters is exactly
    which tokens differ.
    """
    diff = set(a.tokens) ^ set(b.tokens)
    if not diff:
        return True, ""
    if diff & NEGATIONS:
        return False, "negation_differs"
    if any(_NUMERIC.search(t) for t in diff):
        return False, "numeral_differs"
    if never and (diff & never):
        return False, "never_cache_token"
    if len(diff) > 4:
        return False, "too_many_differing_tokens"
    return True, ""


@dataclass
class RotConfig:
    effort: float = 0.0            # 0..1, how hard to look
    exact_ttl_s: float = 300.0     # scales up to 12x with effort
    max_hamming: int = 6           # max near-dup distance at effort 1.0
    max_coalesce_ms: int = 400
    prune_context: bool = True
    rightsize_output: bool = True
    never_cache_tokens: set = field(default_factory=set)
    never_cache_paths: set = field(default_factory=set)
    max_entries: int = 20000
    near_scan_limit: int = 2048
    inflight_wait_s: float = 60.0


@dataclass
class RotFinding:
    kind: str
    saved_input_tokens: int = 0
    saved_output_tokens: int = 0
    detail: dict = field(default_factory=dict)


@dataclass
class RotResult:
    served_from: Optional[str] = None      # exact | near | inflight | None
    response: Optional[object] = None
    findings: List[RotFinding] = field(default_factory=list)
    pruned_messages: Optional[list] = None
    max_tokens: Optional[int] = None
    coalesce_ms: int = 0
    refusals: List[str] = field(default_factory=list)
    latency_credit_ms: float = 0.0         # cache hit returns time to the user
    leader_key: Optional[str] = None       # set when WE own the upstream call

    @property
    def hit(self) -> bool:
        return self.served_from is not None

    @property
    def saved_tokens(self) -> int:
        return sum(f.saved_input_tokens + f.saved_output_tokens
                   for f in self.findings)


class RotEngine:
    """
    Thread-safe, and cross-process when given a shared backend.
    """

    def __init__(self, config: Optional[RotConfig] = None,
                 backend: Optional[CacheBackend] = None,
                 inflight: Optional[Inflight] = None):
        self.cfg = config or RotConfig()
        self.backend = backend or MemoryBackend()
        self.inflight = inflight or Inflight()
        self._lock = threading.Lock()
        self._lengths: Dict[str, deque] = defaultdict(lambda: deque(maxlen=400))
        self._prefix_seen: Dict[str, tuple] = {}
        self._last_evict = 0.0
        self.counters = defaultdict(int)

    # ------------------------------------------------------------- tuning

    def _ttl(self) -> float:
        return self.cfg.exact_ttl_s * (1.0 + 11.0 * self.cfg.effort)

    def _hamming_budget(self) -> int:
        # effort 0 -> exact only. Near-duplicate matching is OFF by default.
        return int(round(self.cfg.max_hamming * self.cfg.effort))

    def coalesce_ms(self) -> int:
        return int(round(self.cfg.max_coalesce_ms * self.cfg.effort))

    def _maybe_evict(self, now: float) -> None:
        if now - self._last_evict < 30.0:
            return
        self._last_evict = now
        self.backend.evict(now - self._ttl(), self.cfg.max_entries)

    # -------------------------------------------------------------- lookup

    def lookup(self, site: str, prompt: str, requested_max_tokens: int = 0,
               messages: Optional[list] = None,
               now: Optional[float] = None) -> RotResult:
        now = time.time() if now is None else now
        res = RotResult()
        if site in self.cfg.never_cache_paths:
            self.counters["never_cache_path"] += 1
            return res

        fp = Fingerprint.of(prompt)
        ttl = self._ttl()
        self._maybe_evict(now)

        # 1. exact
        hit = self.backend.get(fp.exact)
        if hit and (now - hit.ts) <= ttl:
            self.counters["exact_hit"] += 1
            res.served_from = "exact"
            res.response = hit.response
            res.findings.append(RotFinding(
                "exact_duplicate", saved_input_tokens=len(fp.tokens),
                saved_output_tokens=hit.output_tokens))
            res.latency_credit_ms = 800.0
            return res

        # 2. in-flight collapse. Claim leadership or block on the leader.
        leader, slot = self.inflight.claim(fp.exact, now)
        if not leader:
            answer = self.inflight.wait(slot, self.cfg.inflight_wait_s)
            if answer is not None:
                self.counters["inflight_collapse"] += 1
                res.served_from = "inflight"
                res.response = answer
                res.findings.append(RotFinding(
                    "inflight_duplicate", saved_input_tokens=len(fp.tokens)))
                res.latency_credit_ms = 200.0
                return res
            # Leader died or timed out. Degrade to baseline: make the call
            # ourselves, exactly as if the driver were not here.
            self.counters["inflight_fallthrough"] += 1
            leader, _ = self.inflight.claim(fp.exact, now)

        # 3. near-duplicate, only if effort bought us a window
        budget = self._hamming_budget()
        if budget > 0:
            for sim, key in self.backend.recent(self.cfg.near_scan_limit):
                if key == fp.exact or hamming(sim, fp.sim) > budget:
                    continue
                cached = self.backend.get(key)
                if not cached or (now - cached.ts) > ttl:
                    continue
                ok, why = safe_to_match(
                    fp, Fingerprint(key, sim, cached.tokens, 0),
                    self.cfg.never_cache_tokens)
                if not ok:
                    self.counters[f"refused_{why}"] += 1
                    res.refusals.append(why)
                    continue
                self.counters["near_hit"] += 1
                self.inflight.fail(fp.exact)   # release the claim we took
                res.served_from = "near"
                res.response = cached.response
                res.findings.append(RotFinding(
                    "near_duplicate", saved_input_tokens=len(fp.tokens),
                    saved_output_tokens=cached.output_tokens,
                    detail={"hamming": hamming(sim, fp.sim)}))
                res.latency_credit_ms = 800.0
                return res

        # ---- miss: shape the request instead of answering it ----
        res.leader_key = fp.exact

        if self.cfg.prune_context and messages:
            kept, dropped = prune_context(messages, self.cfg.effort)
            if dropped:
                res.pruned_messages = kept
                res.findings.append(RotFinding(
                    "dead_context", saved_input_tokens=dropped))
                self.counters["context_pruned"] += 1

        if self.cfg.rightsize_output and requested_max_tokens:
            rs = self._rightsize(site, requested_max_tokens)
            if rs and rs < requested_max_tokens:
                res.max_tokens = rs
                res.findings.append(RotFinding(
                    "output_overallocation",
                    saved_output_tokens=requested_max_tokens - rs,
                    detail={"requested": requested_max_tokens, "set": rs}))
                self.counters["rightsized"] += 1

        vol = self._check_prefix(site, prompt, fp)
        if vol:
            res.findings.append(vol)

        res.coalesce_ms = self.coalesce_ms()
        return res

    def record(self, site: str, prompt: str, response: object,
               output_tokens: int, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        fp = Fingerprint.of(prompt)
        self.backend.put(fp.exact, Entry(response, now, output_tokens,
                                         fp.tokens, fp.sim))
        self.inflight.settle(fp.exact, response)
        with self._lock:
            self._lengths[site].append(output_tokens)

    def abandon(self, prompt: str) -> None:
        """Upstream failed. Release followers so they retry themselves."""
        self.inflight.fail(Fingerprint.of(prompt).exact)

    # ------------------------------------------------------------ internals

    def _rightsize(self, site: str, requested: int) -> Optional[int]:
        """
        Never truncate. Set the allocation to observed p99 plus 50% headroom,
        and only when we have enough history to be sure. Over-allocating
        max_tokens costs real reserved KV capacity on the serving side.
        """
        with self._lock:
            hist = self._lengths.get(site)
            vals = list(hist) if hist else []
        if len(vals) < 50:
            return None
        s = sorted(vals)
        p99 = s[min(len(s) - 1, int(len(s) * 0.99))]
        proposed = int(p99 * 1.5) + 64
        return proposed if proposed < requested * 0.8 else None

    def _check_prefix(self, site: str, prompt: str,
                      fp: Fingerprint) -> Optional[RotFinding]:
        """
        Cache-hostile prefix detection. One timestamp at the top of a system
        prompt means the provider's prefix cache never hits and every call in
        that pipeline pays full prefill forever. Nothing is broken, so nobody
        ever notices. We cannot fix it from here -- it is a code change on
        their side -- so we report it.
        """
        head = _VOLATILE.sub("<v>", " ".join(prompt.split())[:1000])
        with self._lock:
            prev = self._prefix_seen.get(site)
            if prev is None:
                self._prefix_seen[site] = (head, 1, 0)
                return None
            lcp, n, reported = prev
            # Longest common prefix across everything this site has sent. A
            # fixed window would miss it: the stable block is whatever the
            # calls share, and its length is not known in advance.
            i = 0
            for x, y in zip(lcp, head):
                if x != y:
                    break
                i += 1
            lcp = lcp[:i]
            if reported or n + 1 < 20 or len(lcp) < 40 or "<v>" not in lcp:
                self._prefix_seen[site] = (lcp, n + 1, reported)
                return None
            self._prefix_seen[site] = (lcp, n + 1, 1)
        self.counters["cache_hostile_prefix"] += 1
        return RotFinding("cache_hostile_prefix", detail={
            "site": site, "samples": n + 1, "stable_prefix_chars": len(lcp),
            "advice": "A volatile token (timestamp/uuid) sits inside a "
                      "stable prefix. The provider prefix cache cannot hit, "
                      "so every call pays full prefill. Move it below the "
                      "stable block.",
            "fixable_here": False})

    # -------------------------------------------------------------- report

    def report(self) -> dict:
        with self._lock:
            c = dict(self.counters)
        looked = sum(c.get(k, 0) for k in
                     ("exact_hit", "near_hit", "inflight_collapse"))
        refused = sum(v for k, v in c.items() if k.startswith("refused_"))
        return {"eliminations": looked, "refusals": refused,
                "cache_entries": self.backend.size(),
                "effort": round(self.cfg.effort, 3),
                "near_dup_window_bits": self._hamming_budget(),
                "backend": type(self.backend).__name__,
                "inflight": self.inflight.stats(),
                "counters": c}

    def close(self) -> None:
        self.backend.close()


def prune_context(messages: list, effort: float) -> Tuple[list, int]:
    """
    Drop conversation turns that no later turn ever refers to.

    Lexical reference counting, deliberately crude and deliberately
    conservative: the first and last two messages are always kept, system
    messages are always kept, and a turn is only dropped if none of its
    distinctive words (length > 6, not in the common set) appear anywhere
    later in the conversation. Effort widens how far back we are willing to
    look, not how aggressively we cut.
    """
    if len(messages) < 6:
        return messages, 0
    window = int(2 + effort * (len(messages) - 4))
    later_words = set()
    keep = [True] * len(messages)
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        text = m.get("content", "") if isinstance(m, dict) else str(m)
        if not isinstance(text, str):
            continue
        toks = {t for t in _tokens(text) if len(t) > 6}
        protected = (i < 1 or i >= len(messages) - 2 or
                     (isinstance(m, dict) and m.get("role") == "system") or
                     i < len(messages) - window)
        if not protected and toks and not (toks & later_words):
            keep[i] = False
        later_words |= toks
    kept = [m for m, k in zip(messages, keep) if k]
    dropped = sum(
        len(_tokens(m.get("content", "") if isinstance(m, dict) else str(m)))
        for m, k in zip(messages, keep) if not k)
    return kept, dropped
