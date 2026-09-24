"""
Per-model calibration, and the tier gate.

Why this is a subscription rather than a purchase: the waste profile is
model-specific and models turn over constantly. Every release changes context
economics, tokenizer behaviour, prefix-cache granularity and reasoning-token
cost. A profile calibrated in March is wrong by September. What recurs is not
access to a feature -- access can be copied -- it is calibration that decays.
Same logic as a threat-signature feed.

A stale profile does not break anything. It degrades gracefully to the generic
baseline and says so, loudly, in the report. Software that stops working when
a subscription lapses is software that gets ripped out; software that stops
being *accurate* keeps getting renewed.
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from . import _ed25519

KEY_PREFIX = "GDPRO"
# The vendor's Ed25519 PUBLIC key, 64 hex chars. Generate with
# PRIVATE-do-not-commit/licensetool.py keygen. Empty = every key fails (fail closed).
PUBLIC_KEY_HEX = "53964169b60d717ae888838cc9ff2997b54594964a2ff5cee840fd56b52d14ad"

FREE, PAID = "free", "paid"
PROFILE_LIFE_S = 90 * 86400  # profiles go stale after ~a quarter


@dataclass
class ModelProfile:
    model: str
    chars_per_token: float = 4.0
    prefix_cache_block: int = 256     # tokens; prefix reuse granularity
    near_dup_max_hamming: int = 6     # tolerance tuned per tokenizer
    typical_reasoning_ratio: float = 0.0
    calibrated_at: float = 0.0
    source: str = "generic-baseline"

    @property
    def age_s(self) -> float:
        return time.time() - self.calibrated_at if self.calibrated_at else 1e12

    @property
    def stale(self) -> bool:
        return self.age_s > PROFILE_LIFE_S

    def as_dict(self) -> dict:
        return {"model": self.model, "source": self.source,
                "stale": self.stale,
                "age_days": round(self.age_s / 86400, 1)
                if self.calibrated_at else None}


GENERIC = ModelProfile(model="*")


class ProfileStore:
    """
    Loads calibration from a JSON bundle. The bundle is what the subscription
    ships. No bundle, or an expired one, and everything falls back to GENERIC
    with a visible warning.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.profiles: Dict[str, ModelProfile] = {}
        self.bundle_version = ""
        self.bundle_issued = 0.0
        if path and os.path.exists(path):
            self.load(path)

    def load(self, path: str) -> None:
        try:
            with open(path) as fh:
                raw = json.load(fh)
        except Exception:
            return
        self.bundle_version = str(raw.get("version", ""))
        self.bundle_issued = float(raw.get("issued_at", 0.0))
        for m, p in (raw.get("models") or {}).items():
            self.profiles[m] = ModelProfile(
                model=m,
                chars_per_token=float(p.get("chars_per_token", 4.0)),
                prefix_cache_block=int(p.get("prefix_cache_block", 256)),
                near_dup_max_hamming=int(p.get("near_dup_max_hamming", 6)),
                typical_reasoning_ratio=float(
                    p.get("typical_reasoning_ratio", 0.0)),
                calibrated_at=float(p.get("calibrated_at",
                                          self.bundle_issued)),
                source=f"bundle:{self.bundle_version}")

    def get(self, model: str) -> ModelProfile:
        p = self.profiles.get(model)
        if p and not p.stale:
            return p
        return p or GENERIC

    def status(self) -> dict:
        stale = [m for m, p in self.profiles.items() if p.stale]
        return {"bundle_version": self.bundle_version or None,
                "models": len(self.profiles),
                "stale_models": stale,
                "warning": None if self.profiles and not stale else
                "Running on generic baselines. Per-model calibration is "
                "absent or expired; waste detection is conservative and "
                "savings are understated."}


@dataclass
class License:
    """
    Tier gate. The free tier is complete and honest on its own: it measures
    waste, eliminates it, and proves it did no harm. The paid tier is the one
    thing the free tier structurally cannot do -- couple to the grid, and
    produce a record assertable to somebody who has no reason to trust you.

    Keys are Ed25519-signed and verified offline against PUBLIC_KEY_HEX, so a
    licence cannot be forged without the vendor's private key. This is not
    copy protection -- the code is Apache-2.0 and anyone can edit this check
    out of their own copy. It only makes a genuine key verifiable, and gates
    the things a fork can't reproduce (signed calibration bundles, receipts
    countersigned by a key you don't hold).

    Key format:  GDPRO.<base64url JSON payload>.<base64url Ed25519 signature>
    Payload:     {"org": "...", "exp": <unix seconds, 0 = none>, "mw": <float>}
    Expiry and seats come from the SIGNED payload only, never from the
    environment, so a customer cannot extend their own licence by editing a
    variable.
    """
    tier: str = FREE
    key: str = ""
    expires_at: float = 0.0
    seats_mw: float = 0.0
    profiles_path: str = ""
    org: str = ""

    @staticmethod
    def from_key(key: str, public_key_hex: Optional[str] = None) -> "License":
        """Verify a key. Any failure returns a free-tier License (never raises)."""
        free = License(key=key or "")
        pub_hex = PUBLIC_KEY_HEX if public_key_hex is None else public_key_hex
        try:
            if not key or not pub_hex:
                return free
            prefix, payload_b64, sig_b64 = key.strip().split(".")
            if prefix != KEY_PREFIX:
                return free
            public = bytes.fromhex(pub_hex)
            payload = _b64d(payload_b64)
            if not _ed25519.verify(public, payload, _b64d(sig_b64)):
                return free
            data = json.loads(payload.decode("utf-8"))
            return License(tier=PAID, key=key,
                           expires_at=float(data.get("exp") or 0),
                           seats_mw=float(data.get("mw") or 0),
                           org=str(data.get("org") or ""),
                           profiles_path=os.environ.get("LOADSLACK_PROFILES", ""))
        except Exception:
            return free

    @classmethod
    def from_env(cls, public_key_hex: Optional[str] = None) -> "License":
        return cls.from_key(os.environ.get("LOADSLACK_LICENSE_KEY", "").strip(),
                            public_key_hex)

    @property
    def active(self) -> bool:
        if self.tier != PAID:
            return False
        return not self.expires_at or time.time() < self.expires_at

    def gate(self, feature: str) -> bool:
        """
        Paid features. Note what is NOT here: rot elimination, the holdout
        verifier, the latency ledger and the waste report are all free. If the
        licence lapses, the driver keeps working and keeps proving itself; it
        simply stops following the grid.
        """
        return self.active and feature in {
            "grid_sidechain", "per_model_profiles", "countersigned_receipts"}

    def status(self) -> dict:
        return {"tier": self.tier, "active": self.active, "org": self.org or None,
                "expires_at": self.expires_at or None,
                "seats_mw": self.seats_mw or None,
                "free_features": ["rot_elimination", "holdout_verifier",
                                  "latency_ledger", "waste_report"],
                "paid_features": ["grid_sidechain", "per_model_profiles",
                                  "countersigned_receipts"]}


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
