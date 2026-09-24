"""
The ladder: what gets ducked, and in what order.

This is where the claim "scale down without hurting performance" is either
honoured or exposed as a lie, so the module is built to expose it. Every stage
carries a perceptual_cost, request classes carry a cost ceiling, and the plan
reports `shortfall` when the required reduction cannot be met within the
ceiling. A driver that silently degrades interactive traffic to hit a number
is worse than useless; this one refuses and says so.

Savings compose MULTIPLICATIVELY, not additively. Two stages that each remove
20% remove 36% together, not 40%. Getting this wrong is the single most common
error in load-shedding arithmetic and it always errs optimistic.

Stage order is by ascending perceptual cost. The first two rungs cost the user
essentially nothing:

  0. batch_coalesce   - hold a request a few hundred ms so it rides a fuller
                        batch. Largest joules-per-token lever in serving and
                        perceptually invisible below ~300ms.
  1. cache_align      - reorder prompt prefixes to maximise KV-cache hits.
                        Removes prefill work, changes no output.
  2. sample_depth     - cut n-best / self-consistency sampling.
  3. reasoning_trim   - reduce thinking-token budget.
  4. model_downshift  - route to a smaller model.
  5. defer            - move the work in time. Only for deferrable classes.

The max_savings numbers are PRIORS with the same health warning as energy.py.
Replace them with measurements from your own traffic before you put the
receipt in front of a regulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple


class RequestClass(str, Enum):
    INTERACTIVE = "interactive"      # a human is watching a cursor blink
    NEAR_REALTIME = "near_realtime"  # a system is waiting, seconds matter
    BACKGROUND = "background"        # agent loops, enrichment, evals
    BATCH = "batch"                  # nightly, deadline hours away


# How much perceptual cost each class will tolerate. INTERACTIVE at 0.10 means
# only the two free rungs ever touch it.
CLASS_CEILING: Dict[RequestClass, float] = {
    RequestClass.INTERACTIVE: 0.10,
    RequestClass.NEAR_REALTIME: 0.30,
    RequestClass.BACKGROUND: 0.65,
    RequestClass.BATCH: 1.00,
}

# Perceptual cost is class-dependent, which most schedulers get wrong.
# Moving a nightly batch job six hours costs nobody anything; swapping its
# model changes the output. So for BATCH, deferring is the CHEAPEST rung, not
# the most expensive, and it sorts to the front. For INTERACTIVE it stays at
# 1.00 and is therefore permanently out of reach.
CLASS_COST_OVERRIDES: Dict[RequestClass, Dict[str, float]] = {
    RequestClass.BACKGROUND: {"defer": 0.34},
    RequestClass.BATCH: {"defer": 0.02, "model_downshift": 0.60},
}


@dataclass(frozen=True)
class Stage:
    name: str
    max_savings: float       # fraction of remaining energy removable at depth 1
    perceptual_cost: float   # 0 = invisible, 1 = user definitely notices
    directive: Callable[[float, dict], dict]
    note: str = ""


# ------------------------------------------------------------- directives

def _coalesce(depth: float, ctx: dict) -> dict:
    max_ms = ctx.get("max_coalesce_ms", 400)
    return {"coalesce_ms": int(round(depth * max_ms))}


def _cache_align(depth: float, ctx: dict) -> dict:
    return {"cache_align": depth > 0.0,
            "prefix_reorder": depth > 0.5}


def _sample_depth(depth: float, ctx: dict) -> dict:
    n = int(ctx.get("n", 1) or 1)
    if n <= 1:
        return {"n": 1}
    return {"n": max(1, int(round(n - depth * (n - 1))))}


def _reasoning_trim(depth: float, ctx: dict) -> dict:
    budget = ctx.get("reasoning_budget")
    if not budget:
        return {}
    floor = ctx.get("reasoning_floor", 0.25)
    keep = 1.0 - depth * (1.0 - floor)
    return {"reasoning_budget": max(1, int(round(budget * keep)))}


def _downshift(depth: float, ctx: dict) -> dict:
    chain: List[str] = ctx.get("downshift_chain") or []
    if not chain:
        return {}
    idx = min(len(chain) - 1, int(round(depth * len(chain) - 0.5)))
    if idx < 0:
        return {}
    return {"model": chain[idx], "downshift_step": idx + 1}


def _defer(depth: float, ctx: dict) -> dict:
    if depth <= 0:
        return {}
    window = ctx.get("defer_window_s", 3600)
    return {"defer": True, "defer_hint_s": int(round(depth * window))}


DEFAULT_STAGES: List[Stage] = [
    Stage("batch_coalesce", 0.22, 0.04, _coalesce,
          "hold for a fuller batch; invisible below ~300ms"),
    Stage("cache_align", 0.10, 0.06, _cache_align,
          "maximise KV prefix reuse; output unchanged"),
    Stage("sample_depth", 0.18, 0.22, _sample_depth,
          "fewer self-consistency samples; small accuracy cost"),
    Stage("reasoning_trim", 0.30, 0.28, _reasoning_trim,
          "smaller thinking budget; bites on hard prompts only"),
    Stage("model_downshift", 0.55, 0.58, _downshift,
          "smaller model; measurable quality change"),
    Stage("defer", 0.98, 1.00, _defer,
          "move the work in time; latency, not quality"),
]


@dataclass
class Plan:
    required: float
    achieved: float
    shortfall: float
    engaged: List[Tuple[str, float]] = field(default_factory=list)
    directives: Dict[str, object] = field(default_factory=dict)
    request_class: RequestClass = RequestClass.INTERACTIVE
    ceiling: float = 0.0

    @property
    def deferred(self) -> bool:
        return bool(self.directives.get("defer"))

    @property
    def max_perceptual_cost(self) -> float:
        return self.ceiling

    def as_dict(self) -> dict:
        return {
            "required": round(self.required, 4),
            "achieved": round(self.achieved, 4),
            "shortfall": round(self.shortfall, 4),
            "class": self.request_class.value,
            "engaged": [{"stage": n, "depth": round(d, 3)}
                        for n, d in self.engaged],
            "directives": dict(self.directives),
        }


class Ladder:
    def __init__(self, stages: Optional[List[Stage]] = None,
                 ceilings: Optional[Dict[RequestClass, float]] = None,
                 cost_overrides: Optional[Dict[RequestClass,
                                               Dict[str, float]]] = None):
        self.stages = list(stages or DEFAULT_STAGES)
        self.ceilings = dict(ceilings or CLASS_CEILING)
        self.cost_overrides = dict(cost_overrides or CLASS_COST_OVERRIDES)

    def cost(self, stage: Stage, rclass: RequestClass) -> float:
        return self.cost_overrides.get(rclass, {}).get(
            stage.name, stage.perceptual_cost)

    def ordered(self, rclass: RequestClass) -> List[tuple]:
        """Stages this class permits, cheapest perceptual cost first."""
        out = [(s, self.cost(s, rclass)) for s in self.stages]
        out = [(s, c) for s, c in out if c <= self.ceilings.get(rclass, 0.0)]
        return sorted(out, key=lambda sc: sc[1])

    def plan(self, required: float, rclass: RequestClass,
             ctx: Optional[dict] = None) -> Plan:
        ctx = dict(ctx or {})
        required = max(0.0, min(1.0, required))
        ceiling = self.ceilings.get(rclass, 0.0)
        target_factor = 1.0 - required

        remaining = 1.0
        engaged: List[Tuple[str, float]] = []
        directives: Dict[str, object] = {}

        for stage, _cost in self.ordered(rclass):
            if remaining <= target_factor + 1e-9:
                break
            needed_factor = target_factor / remaining  # still to remove
            full_factor = 1.0 - stage.max_savings
            if full_factor <= needed_factor:
                depth = (1.0 - needed_factor) / stage.max_savings
            else:
                depth = 1.0
            depth = max(0.0, min(1.0, depth))
            if depth <= 1e-6:
                continue
            d = stage.directive(depth, ctx)
            if not d and stage.name != "defer":
                # Stage is inapplicable to this request (e.g. no reasoning
                # budget to trim). Claim no savings for it.
                continue
            directives.update(d)
            remaining *= (1.0 - stage.max_savings * depth)
            engaged.append((stage.name, depth))

        achieved = 1.0 - remaining
        return Plan(required=required, achieved=achieved,
                    shortfall=max(0.0, required - achieved),
                    engaged=engaged, directives=directives,
                    request_class=rclass, ceiling=ceiling)

    def headroom(self, rclass: RequestClass) -> float:
        """Maximum reduction achievable for this class. Useful for planning."""
        remaining = 1.0
        for s, _ in self.ordered(rclass):
            remaining *= (1.0 - s.max_savings)
        return 1.0 - remaining
