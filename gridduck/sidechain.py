"""
The driver.

    sc = Sidechain()                        # free tier, calm grid
    d  = sc.before(site="support-bot", prompt=p, messages=m, max_tokens=800)
    if d.served:                            # duplicate work, answered locally
        return d.response
    resp = call_upstream(**d.request)       # possibly pruned / right-sized
    sc.after(d, resp, output_tokens=n, ttft_ms=t)

Composition, in one paragraph:

The rot engine removes work that buys nothing -- quality-neutral by
construction. The verifier keeps a permanent bypass slice and proves,
continuously, that governed traffic is indistinguishable from ungoverned. The
latency ledger enforces that we only spend milliseconds we already earned. All
three are free, because they are the giveaway and because they are what makes
the thing installable at all.

The grid signal is the sidechain input, and it modulates ONE thing: effort --
how hard the rot engine looks. It never touches quality, never swaps a model,
never trims a reasoning budget. Higher grid stress means wider near-duplicate
windows, deeper pruning, longer coalescing. You buy elimination with detection
cycles.

That coupling, per-model calibration, and the countersigned receipt are the
paid tier. When a licence lapses the driver keeps working and keeps proving
itself. It simply stops following the grid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .compressor import Compressor, CompressorConfig
from .ledger import Ledger
from .profiles import License, ProfileStore
from .rot import RotConfig, RotEngine, RotResult
from .store import CacheBackend
from .signal import GridSignal, SignalSource, StaticSource
from .verify import Verifier

# Effort floor: what we do even on a perfectly calm grid, because eliminating
# duplicate work is correct regardless of what the grid is doing.
BASE_EFFORT = 0.20


@dataclass
class Turn:
    site: str
    prompt: str
    request: dict
    rot: RotResult
    held_out: bool
    effort: float
    stress: float
    started: float = field(default_factory=time.time)

    @property
    def served(self) -> bool:
        return self.rot.hit and self.rot.response is not None

    @property
    def response(self):
        return self.rot.response

    @property
    def coalesce_ms(self) -> int:
        return self.rot.coalesce_ms


class Sidechain:
    def __init__(self, source: Optional[SignalSource] = None,
                 license: Optional[License] = None,
                 rot_config: Optional[RotConfig] = None,
                 holdout_fraction: float = 0.03,
                 ledger_path: str = "gridduck-receipts.db",
                 compressor: Optional[CompressorConfig] = None,
                 profiles_path: str = "",
                 backend: Optional[CacheBackend] = None,
                 window_s: float = 3600.0):
        self.license = license or License.from_env()
        self.source = source or StaticSource(0.0)
        self.rot = RotEngine(rot_config or RotConfig(effort=BASE_EFFORT),
                             backend=backend)
        self.verifier = Verifier(holdout_fraction, window_s=window_s)
        self.compressor = Compressor(compressor or CompressorConfig(
            threshold=0.45, ratio=4.0, attack_s=45, release_s=900))
        self.profiles = ProfileStore(
            profiles_path or self.license.profiles_path or None)
        self.ledger = Ledger(ledger_path)
        self._sig = GridSignal()

    # ------------------------------------------------------------- effort

    def _effort(self, now: Optional[float] = None) -> tuple:
        try:
            self._sig = self.source.read()
        except Exception:
            self._sig = GridSignal(stress=0.0, stale=True)  # fail open
        if not self.license.gate("grid_sidechain"):
            return BASE_EFFORT, 0.0
        r = self.compressor.update(self._sig, now=now)
        # Normalise against the curve's own maximum so effort actually reaches
        # 1.0 at full stress. Ratio and threshold shape the RAMP; they should
        # not cap how hard we are willing to look, because looking harder
        # costs the user nothing.
        span = self.compressor.static_curve(1.0) or 1.0
        frac = min(1.0, r.applied / span)
        return min(1.0, BASE_EFFORT + (1.0 - BASE_EFFORT) * frac), \
            self._sig.stress

    # -------------------------------------------------------------- before

    def before(self, site: str, prompt: str, messages: Optional[list] = None,
               max_tokens: int = 0, request: Optional[dict] = None,
               key: Optional[str] = None,
               now: Optional[float] = None) -> Turn:
        effort, stress = self._effort(now)
        held = self.verifier.assign(key or f"{site}:{prompt[:256]}")

        if self.license.gate("per_model_profiles") and request:
            p = self.profiles.get(request.get("model", ""))
            self.rot.cfg.max_hamming = p.near_dup_max_hamming

        req = dict(request or {})

        if held:
            # The bypass button. Completely ungoverned, by construction.
            return Turn(site, prompt, req, RotResult(), True, 0.0, stress)

        self.rot.cfg.effort = effort
        res = self.rot.lookup(site, prompt, max_tokens, messages, now)

        # Latency ledger: only spend a coalescing window out of surplus.
        if res.coalesce_ms and not self.verifier.ledger.affordable(
                res.coalesce_ms):
            res.coalesce_ms = 0
        if res.coalesce_ms:
            self.verifier.ledger.debit(res.coalesce_ms)
        if res.latency_credit_ms:
            self.verifier.ledger.credit(res.latency_credit_ms)

        if res.pruned_messages is not None:
            req["messages"] = res.pruned_messages
        if res.max_tokens:
            req["max_tokens"] = res.max_tokens

        if res.findings or res.refusals:
            self.ledger.append("rot", {
                "site": site, "effort": round(effort, 3),
                "stress": round(stress, 4),
                "served_from": res.served_from,
                "saved_tokens": res.saved_tokens,
                "findings": [f.kind for f in res.findings],
                "refusals": res.refusals,
                "tier": self.license.tier})

        return Turn(site, prompt, req, res, False, effort, stress)

    # --------------------------------------------------------------- after

    def after(self, turn: Turn, response: object = None,
              output_tokens: int = 0, ttft_ms: float = 0.0,
              total_ms: float = 0.0) -> None:
        self.verifier.observe(turn.held_out, ttft_ms,
                              total_ms or ttft_ms)
        if turn.served:
            return
        if response is None:
            self.rot.abandon(turn.prompt)
            return
        if not turn.held_out:
            self.rot.record(turn.site, turn.prompt, response, output_tokens)

    # -------------------------------------------------------------- report

    def state(self) -> dict:
        return {
            "licence": self.license.status(),
            "grid": {"stress": round(self._sig.stress, 4),
                     "region": self._sig.region, "stale": self._sig.stale,
                     "coupled": self.license.gate("grid_sidechain")},
            "effort": round(self.rot.cfg.effort, 3),
            "rot": self.rot.report(),
            "proof": self.verifier.proof(),
            "profiles": self.profiles.status(),
        }

    def datasheet(self) -> str:
        return self.verifier.datasheet_line()

    def close(self) -> None:
        try:
            self.ledger.attest()
        except Exception:
            pass
        self.source.close()
        self.ledger.close()
        self.rot.close()
