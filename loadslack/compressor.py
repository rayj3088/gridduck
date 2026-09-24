"""
The sidechain compressor.

This is a real compressor, not a threshold with a fancy name. Grid stress is
the detector input; our own power draw is the signal being ducked. Threshold,
ratio, soft knee, attack, release and hold all mean what they mean in audio,
because the failure modes are the same ones: pumping, chatter, and holes you
can hear.

Static curve (soft knee, textbook form):

    slope = 1 - 1/ratio
    d     = stress - threshold

    2d < -W          ->  0
    |2d| <= W        ->  slope * (d + W/2)^2 / (2W)      [quadratic knee]
    otherwise        ->  slope * d

Then a one-pole envelope with separate attack and release time constants, and
a hold timer so a two-minute price spike doesn't leave the plant oscillating
for the rest of the afternoon.

A hard curtailment order switches the unit into limiter mode: the curve is
bypassed, the ordered reduction becomes the target directly, and attack runs
on `order_attack_s` because the utility gave you ten minutes, not an hour.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

from .signal import GridSignal


@dataclass
class CompressorConfig:
    threshold: float = 0.55
    ratio: float = 6.0
    knee: float = 0.20
    attack_s: float = 45.0
    release_s: float = 900.0
    hold_s: float = 180.0
    ceiling: float = 0.65  # never ask the ladder for more than it can deliver
    order_attack_s: float = 20.0
    order_ceiling: float = 0.95  # a hard order may exceed the soft ceiling

    def validate(self) -> None:
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("threshold must be in [0,1]")
        if self.ratio < 1.0:
            raise ValueError("ratio must be >= 1")
        if self.knee < 0.0:
            raise ValueError("knee must be >= 0")
        for name in ("attack_s", "release_s", "order_attack_s"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be > 0")
        if not 0.0 <= self.ceiling <= 1.0:
            raise ValueError("ceiling must be in [0,1]")


@dataclass(frozen=True)
class Reduction:
    """How hard we are currently ducking, and why."""

    target: float  # instantaneous curve output, pre-envelope
    applied: float  # post-envelope; this is what the ladder must deliver
    limiting: bool  # True when driven by a hard curtailment order
    stress: float
    holding: bool
    order_id: str = ""


class Compressor:
    def __init__(self, config: Optional[CompressorConfig] = None,
                 now: Optional[float] = None):
        self.cfg = config or CompressorConfig()
        self.cfg.validate()
        self._applied = 0.0
        self._peak = 0.0
        self._hold_until = 0.0
        self._last_t = time.time() if now is None else now

    # -------------------------------------------------------------- curve

    def static_curve(self, stress: float) -> float:
        cfg = self.cfg
        slope = 1.0 - 1.0 / cfg.ratio
        d = stress - cfg.threshold
        w = cfg.knee
        if w > 0 and abs(2.0 * d) <= w:
            gr = slope * (d + w / 2.0) ** 2 / (2.0 * w)
        elif 2.0 * d < -w:
            gr = 0.0
        else:
            gr = slope * d
        return max(0.0, min(cfg.ceiling, gr))

    # ------------------------------------------------------------ envelope

    def update(self, sig: GridSignal, baseline_mw: Optional[float] = None,
               now: Optional[float] = None) -> Reduction:
        now = time.time() if now is None else now
        dt = max(0.0, now - self._last_t)
        self._last_t = now
        cfg = self.cfg

        limiting = False
        order_id = ""
        target = self.static_curve(sig.stress)

        order = sig.order
        if order is not None and order.active(now):
            frac = order.as_fraction(baseline_mw)
            if frac is not None:
                limiting = True
                order_id = order.event_id or order.source
                target = max(target, min(cfg.order_ceiling, frac))

        tau = (cfg.order_attack_s if limiting else cfg.attack_s) \
            if target > self._applied else cfg.release_s

        # Hold: once we've come up, refuse to release for hold_s. Prevents the
        # plant from pumping on a spiky signal.
        holding = False
        if target >= self._peak - 1e-9 and target > 0:
            self._peak = target
            self._hold_until = now + cfg.hold_s
        elif now < self._hold_until and target < self._applied:
            holding = True
            target = self._applied
        elif now >= self._hold_until:
            self._peak = target

        # dt == 0 means no time has passed, so nothing moves. Snapping to
        # target here would make the envelope instantaneous whenever two
        # decisions share a timestamp -- which, under load, is most of them.
        coeff = 1.0 if dt <= 0 else math.exp(-dt / tau)
        self._applied = target + (self._applied - target) * coeff
        if abs(self._applied - target) < 1e-4:
            self._applied = target
        self._applied = max(0.0, min(1.0, self._applied))

        return Reduction(target=target, applied=self._applied,
                         limiting=limiting, stress=sig.stress,
                         holding=holding, order_id=order_id)

    @property
    def applied(self) -> float:
        return self._applied

    def reset(self) -> None:
        self._applied = 0.0
        self._peak = 0.0
        self._hold_until = 0.0
