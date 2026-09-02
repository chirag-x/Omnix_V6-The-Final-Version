"""
Relative / first-result target refinement (Phase 8).

A :class:`BrowserTarget` may carry ``label`` (a human-readable
description, never used for resolution) and ``nth`` (which match
to take).  This module contains a *pure* helper that, given a
:class:`BrowserTarget`, returns the *refined* CSS / accessibility
hint the session should try.

It is intentionally simple: it only validates the inputs and
chooses an ordering; the heavy lifting (locator dispatch) lives
in :mod:`browser.session.session`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from browser.models.contracts import BrowserTarget, LocatorKind


@dataclass(frozen=True)
class RelativeTargetHint:
    """A pre-resolution hint derived from a :class:`BrowserTarget`."""

    primary_kind: LocatorKind
    nth: Optional[int]
    selector_hint: str
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "primary_kind": self.primary_kind.value,
            "nth": self.nth,
            "selector_hint": self.selector_hint,
            "label": self.label,
        }


class RelativeTargetResolver:
    """Static, pure helpers for refining a :class:`BrowserTarget`."""

    @staticmethod
    def refine(target: BrowserTarget) -> RelativeTargetHint:
        if not isinstance(target, BrowserTarget):
            raise TypeError(
                f"RelativeTargetResolver.refine expected a BrowserTarget, "
                f"got {type(target).__name__}"
            )
        if target.nth is not None and (
            not isinstance(target.nth, int) or target.nth < 0
        ):
            raise ValueError(
                f"target.nth must be a non-negative int or None, "
                f"got {target.nth!r}"
            )
        return RelativeTargetHint(
            primary_kind=target.kind,
            nth=target.nth,
            selector_hint=target.value,
            label=target.label,
        )
