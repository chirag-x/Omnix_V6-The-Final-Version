"""
Omnix V6 — System 2 Brain failure classification (Phase 17).

The Brain needs to know *what kind* of failure it is looking at
before deciding how to recover.  A ``TARGET_NOT_FOUND`` failure has
nothing in common with a ``TIMEOUT`` failure: the former needs a
re-plan or a user clarification, the latter needs a retry with
backoff.

This module provides:

    * :class:`FailureKind` — the closed set of failure categories
      the Brain recognises.  Mirrors but does not duplicate
      :class:`core.orchestration.FailureKind`: the Brain's view
      is *narrower* and *user-facing* (it groups internal kinds
      into user-actionable buckets).
    * :class:`RecoveryStrategy` — the action the Brain takes.
    * :class:`RecoveryClassifier` — the deterministic mapping
      from (kind, attempt, history) to :class:`RecoveryStrategy`.

The classifier is **deterministic and explainable** — there is no
"AI" in the loop, only a small rule table.  When the classifier
cannot decide, it falls back to ``RecoveryStrategy.ASK_USER``
rather than guessing.

This module is pure data + a single classification function.  It
must never import:

    * :mod:`subprocess`
    * :mod:`pyautogui`
    * :mod:`win32gui` / :mod:`win32api`
    * :mod:`core.capability_router`
    * :mod:`core.omnix_engine`
    * :mod:`ai.provider.*`
    * any V6 *Windows service* (e.g. ``system.windows.*``)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# FailureKind
# ---------------------------------------------------------------------------


class FailureKind(str, Enum):
    """Closed set of user-facing failure categories.

    These are *narrower* than the executor's internal
    :class:`core.orchestration.FailureKind`.  The Brain groups
    internal kinds into user-actionable buckets:

      * ``EXECUTION``         — the capability raised a runtime error.
      * ``VERIFICATION``      — the capability ran but the post-
                                condition failed.
      * ``TIMEOUT``           — the capability exceeded its budget.
      * ``CANCELLED``         — the user cancelled the step.
      * ``SAFETY``            — a safety policy rejected the step.
      * ``UNKNOWN_CAPABILITY` — the executor was asked to call a
                                capability that is not in the
                                registry.  Should never happen for
                                a Brain-issued step; a Brain bug
                                if it does.
      * ``INVALID_PARAMETERS`— a parameter was rejected by the
                                capability's own validator.
      * ``TARGET_NOT_FOUND``  — the *target* the user asked for
                                (e.g. an application name) was not
                                in the catalog.  This is the
                                "Did you mean …?" case.
      * ``APP_NOT_RUNNING``   — the *target* the user asked for is
                                known but the app is not running
                                (e.g. "close Notepad" when Notepad
                                is not open).  This is a
                                no-op-with-truth, not an error.
      * ``APP_ALREADY_RUNNING`— the user asked to open an app that
                                is already running.  Re-focus
                                instead of launch.
      * ``PLAN_INFEASIBLE`    — the planner cannot produce a plan
                                (e.g. unsupported intent kind).
      * ``INTERNAL`           — an internal error in the Brain or
                                the executor (programming bug).
    """

    EXECUTION = "execution"
    VERIFICATION = "verification"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SAFETY = "safety"
    UNKNOWN_CAPABILITY = "unknown_capability"
    INVALID_PARAMETERS = "invalid_parameters"
    TARGET_NOT_FOUND = "target_not_found"
    APP_NOT_RUNNING = "app_not_running"
    APP_ALREADY_RUNNING = "app_already_running"
    PLAN_INFEASIBLE = "plan_infeasible"
    INTERNAL = "internal"


# ---------------------------------------------------------------------------
# RecoveryStrategy
# ---------------------------------------------------------------------------


class RecoveryStrategy(str, Enum):
    """The action the Brain takes in response to a failure.

    The set is small and closed.  Each value maps to a single
    observable behaviour:

      * ``RETRY``            — try the same step again immediately.
      * ``RETRY_WITH_BACKOFF`— wait ``backoff_s`` then retry.
      * ``REPLAN`            — ask the planner to produce a new
                               plan; the failed step is dropped or
                               replaced.
      * ``FOCUS_INSTEAD`     — re-focus the running app instead
                               of opening it.
      * ``OPEN_FIRST`        — open the app then re-run the step.
      * ``ASK_USER`          — surface a clarification question to
                               the user.  Terminal until they
                               respond.
      * ``GIVE_UP`           — give up; mark the task FAILED.
      * ``NO_OP`             — the failure is informational; the
                               step is treated as succeeded (e.g.
                               "close Notepad" when Notepad is
                               not running).
    """

    RETRY = "retry"
    RETRY_WITH_BACKOFF = "retry_with_backoff"
    REPLAN = "replan"
    FOCUS_INSTEAD = "focus_instead"
    OPEN_FIRST = "open_first"
    ASK_USER = "ask_user"
    GIVE_UP = "give_up"
    NO_OP = "no_op"


@dataclass(frozen=True)
class RecoveryDecision:
    """A concrete recovery decision.

    The Brain attaches this to a :class:`Task` so the executor
    knows what to do next.
    """

    strategy: RecoveryStrategy
    failure_kind: FailureKind = FailureKind.EXECUTION
    backoff_s: float = 0.0
    rationale: str = ""
    user_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


# Heuristic patterns that map an error string to a failure kind.
# These are *intentionally* small.  The Brain never tries to parse
# English — it only matches a few stable substrings that the
# capabilities produce.
_ERROR_PATTERNS: Sequence[Tuple[FailureKind, re.Pattern[str]]] = (
    (FailureKind.TARGET_NOT_FOUND,
     re.compile(r"\b(APP_NOT_FOUND|app_not_found|not found|cannot find"
                r"|could not find|unknown app|application not found)\b",
                re.IGNORECASE)),
    (FailureKind.APP_NOT_RUNNING,
     re.compile(r"\b(NOT_RUNNING|not running|is not running|process not"
                r" found|window not found|app_closed)\b",
                re.IGNORECASE)),
    (FailureKind.APP_ALREADY_RUNNING,
     re.compile(r"\b(ALREADY_RUNNING|already running|already open|"
                r"already started|process_exists)\b",
                re.IGNORECASE)),
    (FailureKind.UNKNOWN_CAPABILITY,
     re.compile(r"\b(UNKNOWN_CAPABILITY|unknown_capability|unknown"
                r" capability|capability not registered|no such"
                r" capability)\b",
                re.IGNORECASE)),
    (FailureKind.INVALID_PARAMETERS,
     re.compile(r"\b(INVALID_PARAMETERS|invalid parameters|invalid"
                r" argument|missing parameter|required parameter)\b",
                re.IGNORECASE)),
    (FailureKind.SAFETY,
     re.compile(r"\b(SAFETY|safety_rejected|safety violation|"
                r"policy violation|requires-grant)\b",
                re.IGNORECASE)),
    (FailureKind.TIMEOUT,
     re.compile(r"\b(TIMEOUT|timed out|deadline exceeded)\b",
                re.IGNORECASE)),
    (FailureKind.VERIFICATION,
     re.compile(r"\b(VERIFICATION_FAILED|verification failed|"
                r"post[-_ ]condition failed|expected.*not met|"
                r"check failed)\b",
                re.IGNORECASE)),
    (FailureKind.CANCELLED,
     re.compile(r"\b(CANCELLED|cancelled|user_cancelled|"
                r"abort|aborted)\b",
                re.IGNORECASE)),
)


# Hard mapping for capabilities.  When the failed step's capability
# is one of these, the Brain uses the mapped kind *without*
# scanning the error message.
_CAPABILITY_KIND_HINTS: Mapping[str, FailureKind] = {
    "desktop.application.open": FailureKind.APP_ALREADY_RUNNING,
    "desktop.application.close": FailureKind.APP_NOT_RUNNING,
    "desktop.application.focus": FailureKind.APP_NOT_RUNNING,
}


class RecoveryClassifier:
    """The single, deterministic failure classifier.

    The classifier is a *value* — it is safe to share a single
    instance across the Brain, the Agent, and the request pipeline.
    It carries no mutable state.
    """

    def __init__(self, *, max_attempts: int = 3, default_backoff_s: float = 1.0) -> None:
        if max_attempts < 1:
            max_attempts = 1
        self._max_attempts = int(max_attempts)
        self._default_backoff_s = float(default_backoff_s)

    # --------------------------------------------------------------- API
    def classify(
        self,
        *,
        capability_name: str = "",
        error_code: str = "",
        error_message: str = "",
        attempt: int = 1,
        plan: Optional[Any] = None,
    ) -> RecoveryDecision:
        """Map a failure into a :class:`RecoveryDecision`.

        The classifier:

          1. Tries the ``error_code`` against :data:`_ERROR_PATTERNS`
             and the explicit :data:`_CAPABILITY_KIND_HINTS`.
          2. Falls back to scanning ``error_message`` against the
             same patterns.
          3. Defaults to :class:`FailureKind.EXECUTION`.
        """
        kind = self._classify_kind(
            capability_name=capability_name,
            error_code=error_code,
            error_message=error_message,
        )
        strategy = self._strategy_for(kind, attempt=attempt, plan=plan)
        user_message = _user_message_for(kind, error_message=error_message)
        backoff = 0.0
        if strategy is RecoveryStrategy.RETRY_WITH_BACKOFF:
            backoff = self._default_backoff_s * max(1, attempt)
        return RecoveryDecision(
            strategy=strategy,
            failure_kind=kind,
            backoff_s=backoff,
            rationale=f"kind={kind.value} attempt={attempt}",
            user_message=user_message,
        )

    # --------------------------------------------------------------- helpers
    def _classify_kind(
        self,
        *,
        capability_name: str,
        error_code: str,
        error_message: str,
    ) -> FailureKind:
        # Explicit kind hint based on capability + known error_code.
        if error_code == "APP_NOT_FOUND":
            return FailureKind.TARGET_NOT_FOUND
        if error_code == "APP_NOT_RUNNING":
            return FailureKind.APP_NOT_RUNNING
        if error_code == "APP_ALREADY_RUNNING":
            return FailureKind.APP_ALREADY_RUNNING
        if error_code == "CAPABILITY_NOT_FOUND":
            return FailureKind.UNKNOWN_CAPABILITY
        if error_code in ("INVALID_PARAMETERS", "PARAMETER_VALIDATION"):
            return FailureKind.INVALID_PARAMETERS
        if error_code == "TIMEOUT":
            return FailureKind.TIMEOUT
        if error_code in ("VERIFICATION_FAILED",):
            return FailureKind.VERIFICATION
        if error_code in ("CANCELLED", "USER_CANCELLED"):
            return FailureKind.CANCELLED
        if error_code == "SAFETY_REJECTED":
            return FailureKind.SAFETY
        # Capability hints.
        hint = _CAPABILITY_KIND_HINTS.get(capability_name or "")
        if hint is not None:
            # For ``close`` / ``focus`` we need the error code to
            # confirm the app was not running; otherwise it's a
            # generic execution failure.
            if hint is FailureKind.APP_NOT_RUNNING and not error_code:
                pass  # let the message scan decide
            else:
                return hint
        # Message scan.
        for kind, pattern in _ERROR_PATTERNS:
            if error_code and pattern.search(error_code):
                return kind
            if error_message and pattern.search(error_message):
                return kind
        return FailureKind.EXECUTION

    def _strategy_for(
        self,
        kind: FailureKind,
        *,
        attempt: int,
        plan: Optional[Any],
    ) -> RecoveryStrategy:
        # User-actionable kinds.
        if kind in (
            FailureKind.TARGET_NOT_FOUND,
            FailureKind.PLAN_INFEASIBLE,
            FailureKind.INVALID_PARAMETERS,
            FailureKind.SAFETY,
            FailureKind.NEEDS_USER if hasattr(FailureKind, "NEEDS_USER") else FailureKind.SAFETY,
        ):
            return RecoveryStrategy.ASK_USER
        # Cancellation: give up.
        if kind is FailureKind.CANCELLED:
            return RecoveryStrategy.GIVE_UP
        # Target-state failures are honest no-ops.
        if kind is FailureKind.APP_NOT_RUNNING:
            return RecoveryStrategy.NO_OP
        if kind is FailureKind.APP_ALREADY_RUNNING:
            return RecoveryStrategy.FOCUS_INSTEAD
        # Retries.
        if kind in (FailureKind.TIMEOUT, FailureKind.EXECUTION, FailureKind.VERIFICATION):
            if attempt >= self._max_attempts:
                return RecoveryStrategy.REPLAN
            if kind is FailureKind.TIMEOUT:
                return RecoveryStrategy.RETRY_WITH_BACKOFF
            return RecoveryStrategy.RETRY
        # Unknown capability is a programming error; replan and
        # let the planner drop the step.
        if kind is FailureKind.UNKNOWN_CAPABILITY:
            return RecoveryStrategy.REPLAN
        # Default: ask the user.
        return RecoveryStrategy.ASK_USER


def _user_message_for(kind: FailureKind, *, error_message: str) -> str:
    if kind is FailureKind.TARGET_NOT_FOUND:
        return "I couldn't find that application."
    if kind is FailureKind.APP_NOT_RUNNING:
        return "That application isn't running."
    if kind is FailureKind.APP_ALREADY_RUNNING:
        return "It's already open; switching to it."
    if kind is FailureKind.TIMEOUT:
        return "That step took too long; I'll try again."
    if kind is FailureKind.VERIFICATION:
        return "The step didn't quite finish. Let me try once more."
    if kind is FailureKind.INVALID_PARAMETERS:
        return "I didn't understand the request."
    if kind is FailureKind.SAFETY:
        return "I can't do that for safety reasons."
    if kind is FailureKind.CANCELLED:
        return "Okay, I stopped."
    if kind is FailureKind.PLAN_INFEASIBLE:
        return "I don't know how to do that."
    if error_message:
        return "Something went wrong."
    return "Something went wrong."


__all__ = [
    "FailureKind",
    "RecoveryClassifier",
    "RecoveryDecision",
    "RecoveryStrategy",
]
