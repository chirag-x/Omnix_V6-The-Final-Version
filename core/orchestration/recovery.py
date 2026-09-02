"""
Omnix V6 — Agent recovery engine (Phase 6C).

This module provides a concrete, deterministic
:class:`RecoveryEngine` (R-12 contract) that the Agent Orchestrator
uses to decide what to do when a step or a whole plan fails.

Policy summary
--------------

The engine respects a strict, bounded retry/replan policy so the
Agent can never enter an infinite recovery loop::

    max_attempts_per_step = 2     # (initial attempt + 1 retry by default)
    max_replans            = 2     # initial plan + at most 2 replans
    max_total_runtime_s    = 120.0 # hard wall-clock cap

When the engine runs out of attempts it returns
``RecoveryAction.GIVE_UP`` and the Agent transitions to
``AgentState.FAILED``.

Failure → action mapping
------------------------
The default mapping is::

    EXECUTION          → RETRY_WITH_BACKOFF (if attempts left)
                          else REPLAN           (if replans left)
                          else GIVE_UP

    VERIFICATION       → REPLAN              (if replans left)
                          else ASK_USER

    TIMEOUT            → REPLAN              (if replans left)
                          else GIVE_UP

    CANCELLED          → GIVE_UP             (no recovery from cancellation)

    SAFETY             → GIVE_UP             (safety refusals are final)

    UNKNOWN_CAPABILITY → GIVE_UP             (cannot retry a non-existent name)

    INVALID_PARAMETERS → GIVE_UP             (deterministic; retrying won't help)

    PLAN_INFEASIBLE    → REPLAN              (if replans left)
                          else GIVE_UP

    INTERNAL           → GIVE_UP             (orchestration layer bug)

The mapping is overridable via ``action_overrides`` so a developer
can experiment without forking the engine.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .models import (
    ExecutionContext,
    Failure,
    FailureKind,
    Plan,
    PlanStep,
    RecoveryAction,
    RecoveryDecision,
)


# ===========================================================================
# Recovery policy
# ===========================================================================

@dataclass(frozen=True)
class RecoveryPolicy:
    """The bounded policy the engine consults on every decision.

    All fields have safe defaults so the engine can be constructed
    with no arguments.  Tests can override individual fields to
    exercise the boundary cases (e.g. ``max_replans=0``).

    Phase 3 addition: ``per_kind_backoff_s`` lets each
    :class:`FailureKind` carry its own backoff duration.  The
    canonical entries are :data:`_DEFAULT_PER_KIND_BACKOFF` (the
    6 UI failure kinds have specific backoffs: 1.0s for
    WINDOW_NOT_READY, 2.0s for PROVIDER_FAILURE).  When a kind is
    not in the map, ``backoff_s`` is used.
    """

    max_attempts_per_step: int = 2
    max_replans: int = 2
    max_total_runtime_s: float = 120.0
    backoff_s: float = 0.0          # default backoff for RETRY_WITH_BACKOFF
    per_kind_backoff_s: Mapping[FailureKind, float] = field(default_factory=dict)
    ask_user_on_uncertain: bool = True

    def with_overrides(self, **kwargs: Any) -> "RecoveryPolicy":
        return replace(self, **kwargs)


# ===========================================================================
# Recovery engine
# ===========================================================================

@dataclass
class _EngineState:
    """Mutable counters the engine keeps while a run is in progress.

    The engine is *stateless* across runs (its ``decide`` method is
    a pure function over its arguments + the counters), but the
    Agent passes a single shared :class:`_EngineState` so the
    counters can be incremented as the run progresses.
    """

    attempts_by_step: Dict[str, int] = field(default_factory=dict)
    replans_used: int = 0
    started_at: float = field(default_factory=time.time)

    def attempt_count(self, step_id: str) -> int:
        return self.attempts_by_step.get(step_id, 0)

    def record_attempt(self, step_id: str) -> None:
        self.attempts_by_step[step_id] = self.attempt_count(step_id) + 1

    def record_replan(self) -> None:
        self.replans_used += 1

    def elapsed_s(self, now: Optional[float] = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.started_at)


class DefaultRecoveryEngine:
    """The concrete :class:`RecoveryEngine` for the Agent Orchestrator.

    The engine is constructed with a :class:`RecoveryPolicy`.  On
    every :meth:`decide` call the engine:

      1. Increments the attempt counter for the failed step.
      2. Checks the bounded policy (max attempts, max replans,
         max runtime).
      3. Maps the :class:`FailureKind` to a default
         :class:`RecoveryAction`.
      4. Applies any caller-supplied ``action_overrides`` for that
         kind.
      5. Returns a :class:`RecoveryDecision` carrying the
         rationale, a fresh ``decision_id``, and the backoff
         duration (if applicable).

    The engine is **deterministic**: the same inputs always
    produce the same decision.  No clock reads affect the outcome
    except ``max_total_runtime_s`` (which only triggers GIVE_UP).
    """

    name: str = "default-recovery"

    def __init__(
        self,
        policy: Optional[RecoveryPolicy] = None,
        *,
        action_overrides: Optional[Mapping[FailureKind, RecoveryAction]] = None,
    ) -> None:
        if policy is None:
            # Phase 3: pre-populate the per-kind backoff map with
            # the canonical Phase 3 defaults so a stock engine
            # returns WINDOW_NOT_READY=1.0s, PROVIDER_FAILURE=2.0s.
            policy = RecoveryPolicy(
                per_kind_backoff_s=dict(_DEFAULT_PER_KIND_BACKOFF),
            )
        self.policy = policy
        self.action_overrides: Dict[FailureKind, RecoveryAction] = dict(
            action_overrides or {}
        )
        self.state = _EngineState()

    # --------------------------------------------------------- public API
    def reset(self) -> None:
        """Reset all per-run counters (call between Agent runs)."""
        self.state = _EngineState()

    def record_replan(self) -> None:
        """Mark that a replan has been produced.

        Called by the Agent *after* it actually dispatches a replan
        so the engine's counter stays in sync.
        """
        self.state.record_replan()

    def record_attempt(self, step_id: str) -> None:
        """Mark that an attempt for ``step_id`` has been dispatched."""
        self.state.record_attempt(step_id)

    def attempts_for(self, step_id: str) -> int:
        """Return the number of attempts recorded for ``step_id``.

        Phase 1 / D4: the Agent's :meth:`_failure_from_step` reads
        this so :class:`Failure.attempt` reflects the actual
        attempt index rather than a hardcoded ``1``.  Callers
        that do not yet call :meth:`record_attempt` will see
        ``0``; the Agent clamps to ``1`` (initial attempt) so
        the audit log is never ambiguous.
        """
        try:
            return max(0, int(self.state.attempt_count(step_id)))
        except Exception:  # noqa: BLE001
            return 0

    def attempts_remaining(self, step_id: str) -> int:
        return max(0, self.policy.max_attempts_per_step - self.state.attempt_count(step_id))

    def replans_remaining(self) -> int:
        return max(0, self.policy.max_replans - self.state.replans_used)

    def decide(
        self,
        failure: Failure,
        context: ExecutionContext,
        *,
        history: Optional[List[RecoveryDecision]] = None,
        cancellation_token: Optional[Any] = None,
    ) -> RecoveryDecision:
        """Return a :class:`RecoveryDecision` for ``failure``.

        ``history`` is a list of recent decisions (oldest first) the
        Agent has made for the same goal.  It is currently used only
        to size the rationale; the policy itself is counters-based.

        ``cancellation_token`` (Phase 4) is checked first; when the
        token is cancelled the engine returns :class:`RecoveryAction.ABORT`
        so the Agent can finalise immediately rather than starting a
        new retry/replan cycle that will be cancelled anyway.
        """
        # Phase 4: cooperative cancellation.  When the token is
        # cancelled, return ABORT so the Agent transitions to
        # CANCELLED rather than FAILED.  This is checked first so
        # we never start a retry/replan cycle that will be
        # cancelled before it completes.
        if cancellation_token is not None and getattr(
            cancellation_token, "is_cancelled", False
        ):
            reason = getattr(cancellation_token, "reason", "") or \
                "cancelled before recovery decision"
            return self._decision(
                failure,
                action=RecoveryAction.ABORT,
                rationale=(
                    f"recovery aborted: cancellation token is set "
                    f"({reason!r})"
                ),
            )
        # Bounded runtime check first.
        elapsed = self.state.elapsed_s()
        if (
            self.policy.max_total_runtime_s > 0
            and elapsed > self.policy.max_total_runtime_s
        ):
            return self._decision(
                failure,
                action=RecoveryAction.GIVE_UP,
                rationale=(
                    f"recovery budget exhausted: runtime {elapsed:.1f}s "
                    f"> {self.policy.max_total_runtime_s:.1f}s"
                ),
            )

        step_id = failure.step_id or ""
        attempts_remaining = (
            self.attempts_remaining(step_id) if step_id else self.policy.max_attempts_per_step
        )
        replans_remaining = self.replans_remaining()

        # Caller-supplied override wins.
        override = self.action_overrides.get(failure.kind)
        if override is not None:
            return self._with_bounds(
                failure, action=override,
                attempts_remaining=attempts_remaining,
                replans_remaining=replans_remaining,
            )

        # Default kind → action mapping.
        default_action = _DEFAULT_KIND_TO_ACTION.get(
            failure.kind, RecoveryAction.GIVE_UP
        )

        return self._with_bounds(
            failure, action=default_action,
            attempts_remaining=attempts_remaining,
            replans_remaining=replans_remaining,
        )

    # --------------------------------------------------------- helpers
    def _with_bounds(
        self,
        failure: Failure,
        *,
        action: RecoveryAction,
        attempts_remaining: int,
        replans_remaining: int,
    ) -> RecoveryDecision:
        """Downgrade actions that have exhausted their budget."""
        if action in (RecoveryAction.RETRY, RecoveryAction.RETRY_WITH_BACKOFF):
            if attempts_remaining <= 0:
                if replans_remaining > 0:
                    return self._decision(
                        failure,
                        action=RecoveryAction.REPLAN,
                        rationale=(
                            "no step retries left; replanning instead"
                        ),
                    )
                return self._decision(
                    failure,
                    action=RecoveryAction.GIVE_UP,
                    rationale="no step retries and no replans left",
                )
        if action is RecoveryAction.REPLAN:
            if replans_remaining <= 0:
                # VERIFICATION + REPLAN-out-of-budget falls back to ASK_USER
                # (the user can sometimes unblock the verifier).
                if (
                    failure.kind is FailureKind.VERIFICATION
                    and self.policy.ask_user_on_uncertain
                ):
                    return self._decision(
                        failure,
                        action=RecoveryAction.ASK_USER,
                        ask_user_message=(
                            "I could not verify the expected effect, "
                            "and I cannot replan any further. "
                            "Could you tell me how to proceed?"
                        ),
                        rationale="replans exhausted; asking the user",
                    )
                return self._decision(
                    failure,
                    action=RecoveryAction.GIVE_UP,
                    rationale="replans exhausted",
                )
        return self._decision(failure, action=action)

    def _decision(
        self,
        failure: Failure,
        *,
        action: RecoveryAction,
        rationale: str = "",
        ask_user_message: str = "",
    ) -> RecoveryDecision:
        # Phase 3: per-kind backoff wins over the global default.
        # The canonical map (e.g. WINDOW_NOT_READY=1.0s,
        # PROVIDER_FAILURE=2.0s) is consulted first.
        backoff = 0.0
        if action is RecoveryAction.RETRY_WITH_BACKOFF:
            backoff = self.policy.per_kind_backoff_s.get(
                failure.kind, self.policy.backoff_s
            )
        return RecoveryDecision(
            decision_id=f"rd-{uuid.uuid4().hex[:10]}",
            action=action,
            failure_id=failure.failure_id,
            backoff_s=backoff,
            ask_user_message=ask_user_message,
            rationale=rationale or _DEFAULT_RATIONALE.get(
                action, f"recovery action {action.value}"
            ),
        )


# ===========================================================================
# Default kind → action mapping
# ===========================================================================

_DEFAULT_KIND_TO_ACTION: Dict[FailureKind, RecoveryAction] = {
    FailureKind.EXECUTION: RecoveryAction.RETRY_WITH_BACKOFF,
    FailureKind.VERIFICATION: RecoveryAction.REPLAN,
    FailureKind.TIMEOUT: RecoveryAction.REPLAN,
    FailureKind.CANCELLED: RecoveryAction.GIVE_UP,
    FailureKind.SAFETY: RecoveryAction.GIVE_UP,
    FailureKind.UNKNOWN_CAPABILITY: RecoveryAction.GIVE_UP,
    FailureKind.INVALID_PARAMETERS: RecoveryAction.GIVE_UP,
    FailureKind.PLAN_INFEASIBLE: RecoveryAction.REPLAN,
    FailureKind.INTERNAL: RecoveryAction.GIVE_UP,
    # Phase 1 / D23 + Phase 3: 6 UI failure kinds.  Each maps
    # to a deterministic recovery action chosen by the audit:
    #
    #   TARGET_NOT_FOUND   → REPLAN  (re-ground via vision)
    #   FOCUS_FAILED       → RETRY   (one immediate retry)
    #   WINDOW_NOT_READY   → RETRY_WITH_BACKOFF  (1.0s)
    #   STALE_TARGET       → REPLAN  (re-ground)
    #   PROVIDER_FAILURE   → RETRY_WITH_BACKOFF  (2.0s)
    #   PERMISSION_FAILURE → ASK_USER
    FailureKind.TARGET_NOT_FOUND: RecoveryAction.REPLAN,
    FailureKind.FOCUS_FAILED: RecoveryAction.RETRY,
    FailureKind.WINDOW_NOT_READY: RecoveryAction.RETRY_WITH_BACKOFF,
    FailureKind.STALE_TARGET: RecoveryAction.REPLAN,
    FailureKind.PROVIDER_FAILURE: RecoveryAction.RETRY_WITH_BACKOFF,
    FailureKind.PERMISSION_FAILURE: RecoveryAction.ASK_USER,
}

_DEFAULT_RATIONALE: Dict[RecoveryAction, str] = {
    RecoveryAction.RETRY: "transient failure; retrying the same step",
    RecoveryAction.RETRY_WITH_BACKOFF: "transient failure; retrying with backoff",
    RecoveryAction.SKIP: "non-essential step; skipping",
    RecoveryAction.REPLAN: "step failed irrecoverably; producing a new plan",
    RecoveryAction.ABORT: "aborting the whole plan",
    RecoveryAction.ASK_USER: "asking the user to clarify",
    RecoveryAction.GIVE_UP: "exhausted recovery budget; giving up",
}

# Phase 3: canonical per-kind backoff for the 6 UI failure kinds.
# Window-not-ready is a transient UIA race; provider failure is
# a longer transient (network/auth blip).  These are the
# production defaults; tests can override via
# ``RecoveryPolicy.per_kind_backoff_s``.
_DEFAULT_PER_KIND_BACKOFF: Dict[FailureKind, float] = {
    FailureKind.WINDOW_NOT_READY: 1.0,
    FailureKind.PROVIDER_FAILURE: 2.0,
}


# ===========================================================================
# Failure construction helper
# ===========================================================================

def make_failure(
    *,
    failure_id: Optional[str] = None,
    kind: FailureKind,
    step_id: str = "",
    plan_id: str = "",
    message: str = "",
    cause: Optional[str] = None,
    attempt: int = 1,
    is_retryable: bool = True,
    **metadata: Any,
) -> Failure:
    """Build a :class:`Failure` with sensible defaults.

    Centralised here so callers do not have to remember the field
    name ``failure_id`` (the model itself is strict on the field
    name to make audit log parsing easier).
    """
    return Failure(
        failure_id=failure_id or f"f-{uuid.uuid4().hex[:10]}",
        kind=kind,
        step_id=step_id or None,
        plan_id=plan_id or None,
        message=message,
        cause=cause,
        attempt=attempt,
        is_retryable=is_retryable,
        metadata=dict(metadata),
    )


__all__ = [
    "RecoveryPolicy",
    "DefaultRecoveryEngine",
    "make_failure",
]
