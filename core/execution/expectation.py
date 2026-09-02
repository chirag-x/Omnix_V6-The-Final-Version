"""
Omnix V6 — Verification Expectation Model for Stage 19.0.

Defines the VerificationExpectation and ExpectationKind models that represent
what to verify after an action in the OBSERVE → GROUND → ACT → VERIFY cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ExpectationKind(str, Enum):
    """Closed set of verification expectations the cycle can verify.
    This is an honest set — only what can be deterministically verified
    using existing perception/grounding primitives.
    """
    NONE = "none"
    WINDOW_EXISTS = "window_exists"
    WINDOW_FOCUSED = "window_focused"
    TARGET_VISIBLE = "target_visible"
    TARGET_PRESENT = "target_present"
    TARGET_ABSENT = "target_absent"
    TEXT_PRESENT = "text_present"
    TEXT_CHANGED = "text_changed"
    SCREEN_CHANGED = "screen_changed"
    FOCUS_CHANGED = "focus_changed"


@dataclass(frozen=True)
class VerificationExpectation:
    """A claim about what the world should look like after an action.
    Used by VerificationProvider to verify post-action state.
    """
    kind: ExpectationKind = ExpectationKind.NONE
    target_query: str = ""            # e.g. "Save button"
    expected_text: str = ""           # e.g. "AI agents"
    expected_window_title: str = ""   # e.g. "Chrome"
    expected_application: str = ""    # e.g. "chrome"
    target_id: str | None = None      # observation_id of the pre-action observation
    before_observation_id: str | None = None
    tolerance_ms: int = 1000          # how much drift is allowed
    timeout_s: float = 5.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def none(cls) -> "VerificationExpectation":
        """Create an expectation that always succeeds (no verification needed)."""
        return cls(kind=ExpectationKind.NONE)

    @classmethod
    def target_visible(cls, query: str) -> "VerificationExpectation":
        """Create an expectation that a target with the given query is visible."""
        return cls(kind=ExpectationKind.TARGET_VISIBLE, target_query=query)

    @classmethod
    def text_present(cls, text: str) -> "VerificationExpectation":
        """Create an expectation that the given text is present on screen."""
        return cls(kind=ExpectationKind.TEXT_PRESENT, expected_text=text)

    @classmethod
    def screen_changed(cls, before_obs_id: str) -> "VerificationExpectation":
        """Create an expectation that the screen has changed since the given observation."""
        return cls(
            kind=ExpectationKind.SCREEN_CHANGED,
            before_observation_id=before_obs_id,
        )