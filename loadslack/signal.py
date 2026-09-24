"""
Grid signal types and sources.

GridSignal.stress is the sidechain's only real input: 0.0 (calm) to 1.0
(extreme). Everything else rides alongside it -- carbon intensity, region,
and an optional hard curtailment order from a utility, ISO, or demand
response aggregator.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CurtailmentOrder:
    """A hard instruction from a utility/ISO/DR aggregator: reduce load now."""
    source: str = "unknown"
    issued_at: float = 0.0
    expires_at: float = 0.0
    reduction_fraction: Optional[float] = None  # 0..1, e.g. 0.3 = cut 30%
    target_mw: Optional[float] = None           # absolute cap
    shed_mw: Optional[float] = None             # absolute amount to shed
    event_id: str = ""

    def active(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return self.issued_at <= now < self.expires_at

    def as_fraction(self, baseline_mw: Optional[float] = None) -> Optional[float]:
        """Express the order as a 0..1 reduction fraction, whatever units it
        was issued in. None if it can't be resolved (an absolute target/shed
        amount given with no known baseline)."""
        if self.reduction_fraction is not None:
            return max(0.0, min(1.0, self.reduction_fraction))
        if baseline_mw and baseline_mw > 0:
            if self.shed_mw is not None:
                return max(0.0, min(1.0, self.shed_mw / baseline_mw))
            if self.target_mw is not None:
                return max(0.0, min(1.0, 1.0 - (self.target_mw / baseline_mw)))
        return None


@dataclass
class GridSignal:
    stress: float = 0.0                       # 0..1; the compressor's real input
    region: str = "local"
    carbon_g_per_kwh: Optional[float] = None
    order: Optional[CurtailmentOrder] = None
    stale: bool = False
    timestamp: float = field(default_factory=time.time)


class SignalSource:
    def read(self) -> GridSignal:
        raise NotImplementedError

    def close(self) -> None:
        pass


class StaticSource(SignalSource):
    """A fixed stress value that never changes on its own. The free-tier
    default is stress=0.0 -- calm, so the driver never ducks anything until
    something real is wired in."""
    def __init__(self, stress: float = 0.0, region: str = "local"):
        self.stress = stress
        self.region = region

    def read(self) -> GridSignal:
        return GridSignal(stress=self.stress, region=self.region)


class WebhookSource(SignalSource):
    """Holds whatever signal was last pushed to it -- typically by proxy.py's
    /signal endpoint, itself driven by a utility webhook or a demand-response
    feed. Thread-safe: push() and read() are called from different threads
    (the HTTP handler thread and the request-serving thread)."""
    def __init__(self, initial: Optional[GridSignal] = None):
        self._lock = threading.Lock()
        self._current = initial or GridSignal()

    def push(self, signal: GridSignal) -> None:
        with self._lock:
            self._current = signal

    def read(self) -> GridSignal:
        with self._lock:
            return self._current
