"""
Omnix V6 — Phase 14: bounded scrolling helper.

A "scroll until target found" loop is the most common fallback in a
real desktop / browser workflow: the user asks the Agent to click
"Settings" but Settings is below the fold.  Phase 14's rule (R-7
"never loop unboundedly") applies — the Agent MUST cap both the
number of scrolls and the wall-clock time it is willing to spend
finding a target.

This module is the *data* half of that loop.  It produces a
:class:`ScrollPlan` that the executor consumes; the executor then
performs the actual scroll (a registered capability call) and
re-grounds after each iteration.  This module does not call any
real capability, service, or screen-capture.

Architectural isolation:
    This module MUST NOT import:
        * :mod:`core.omnix_engine`
        * :mod:`core.pipeline`
        * :mod:`core.capability_router`
        * :mod:`core.services.*` (vision / browser / memory / voice)
        * any V6 *Windows service* (e.g. ``system.windows.*``)
        * any V6 *AI provider* (e.g. ``ai.provider.*``)

    The scroll plan is a pure value type.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class ScrollDirection(str, Enum):
    """The closed set of scroll directions."""

    DOWN = "down"
    UP = "up"
    LEFT = "left"
    RIGHT = "right"


class ScrollSurface(str, Enum):
    """The closed set of surfaces a scroll targets."""

    DESKTOP = "desktop"
    BROWSER = "browser"
    APPLICATION = "application"


@dataclass(frozen=True)
class ScrollStep:
    """A single bounded scroll step in a :class:`ScrollPlan`."""

    direction: ScrollDirection
    surface: ScrollSurface
    amount: int = 3        # mouse wheel notches; bounded default
    target_id: Optional[str] = None    # the application / window to scroll
    selector: Optional[str] = None     # browser-only CSS selector to scroll within
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.amount, int) or self.amount <= 0:
            raise ValueError(
                f"ScrollStep.amount must be a positive int, got {self.amount!r}"
            )
        if self.amount > 50:
            # A single scroll of > 50 notches is almost certainly a
            # bug; the bounded loop should re-ground instead.
            raise ValueError(
                f"ScrollStep.amount={self.amount} is too large; "
                f"the bounded loop caps single scrolls at 50 notches."
            )

    def to_capability_parameters(self) -> Dict[str, Any]:
        """Project this scroll step into capability parameters.

        The executor uses this to build an ActionRequest.  We
        intentionally do not import any capability spec here —
        the projection is a plain dict the executor validates.
        """
        params: Dict[str, Any] = {
            "direction": self.direction.value,
            "amount": self.amount,
            "surface": self.surface.value,
        }
        if self.target_id:
            params["target_id"] = self.target_id
        if self.selector:
            params["selector"] = self.selector
        return params

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ScrollStep",
            "direction": self.direction.value,
            "surface": self.surface.value,
            "amount": self.amount,
            "target_id": self.target_id,
            "selector": self.selector,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ScrollPlan:
    """A bounded, deterministic scroll-then-ground plan.

    A :class:`ScrollPlan` describes a sequence of scroll steps the
    Agent may take when a target is initially NOT_FOUND.  The plan
    is bounded by both ``max_steps`` and ``max_total_amount`` — the
    executor must refuse to take more steps once either is reached.

    Attributes
    ----------
    target_query:
        The human-readable target the executor is scrolling to find.
    steps:
        The bounded sequence of :class:`ScrollStep`.
    max_steps:
        Hard cap on the number of scrolls.  Phase 14 §6 keeps this
        small (5 by default) — a target that requires more than a
        few scrolls to surface is probably not on this page.
    max_total_amount:
        Hard cap on the cumulative notches scrolled.  Even with
        small individual scrolls, the executor must not exceed this
        total.
    re_ground_after_each:
        If True, the executor re-grounds after every step.  This is
        the recommended default — re-grounding early stops the
        loop the moment the target becomes visible.
    surface:
        The surface the executor is scrolling on.  Stored on the
        plan for audit; each step also carries a surface for cases
        where the loop crosses surfaces.
    """

    target_query: str
    steps: Tuple[ScrollStep, ...] = ()
    max_steps: int = 5
    max_total_amount: int = 25
    re_ground_after_each: bool = True
    surface: ScrollSurface = ScrollSurface.DESKTOP
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.target_query, str) or not self.target_query.strip():
            raise ValueError("ScrollPlan.target_query must be a non-empty string")
        if not isinstance(self.max_steps, int) or self.max_steps <= 0:
            raise ValueError(
                f"ScrollPlan.max_steps must be a positive int, got {self.max_steps!r}"
            )
        if not isinstance(self.max_total_amount, int) or self.max_total_amount <= 0:
            raise ValueError(
                "ScrollPlan.max_total_amount must be a positive int, "
                f"got {self.max_total_amount!r}"
            )
        if self.max_total_amount < self.max_steps:
            # If the cap is smaller than the step count, the plan
            # is incoherent.  Reject loudly so the caller fixes
            # the configuration rather than discovering the bug
            # at runtime.
            raise ValueError(
                "ScrollPlan.max_total_amount must be >= max_steps; got "
                f"max_total_amount={self.max_total_amount}, "
                f"max_steps={self.max_steps}."
            )

    def is_within_bounds(self, current_total: int) -> bool:
        return current_total < self.max_total_amount

    def remaining_steps(self, current_index: int) -> int:
        return max(0, self.max_steps - current_index)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "ScrollPlan",
            "target_query": self.target_query,
            "steps": [s.to_dict() for s in self.steps],
            "max_steps": self.max_steps,
            "max_total_amount": self.max_total_amount,
            "re_ground_after_each": self.re_ground_after_each,
            "surface": self.surface.value,
            "metadata": dict(self.metadata),
        }


def build_default_scroll_plan(
    *,
    target_query: str,
    surface: ScrollSurface = ScrollSurface.DESKTOP,
    direction: ScrollDirection = ScrollDirection.DOWN,
    max_steps: int = 5,
    amount_per_step: int = 3,
    target_id: Optional[str] = None,
    selector: Optional[str] = None,
) -> ScrollPlan:
    """Build a deterministic :class:`ScrollPlan` with bounded defaults.

    Helper for callers that do not need to customise the per-step
    shape.  The defaults match Phase 14 §6's guidance: at most 5
    scrolls of 3 notches each, for a maximum of 15 notches total.
    """
    steps = tuple(
        ScrollStep(
            direction=direction,
            surface=surface,
            amount=amount_per_step,
            target_id=target_id,
            selector=selector,
        )
        for _ in range(max_steps)
    )
    return ScrollPlan(
        target_query=target_query,
        steps=steps,
        max_steps=max_steps,
        max_total_amount=amount_per_step * max_steps,
        re_ground_after_each=True,
        surface=surface,
    )


__all__ = [
    "ScrollDirection",
    "ScrollSurface",
    "ScrollStep",
    "ScrollPlan",
    "build_default_scroll_plan",
]
