"""
Omnix V6 — Brain layer (Phase 5C+5D).

The :class:`Brain` is the orchestration seam between the Phase 5B
:class:`LLMIntentInterpreter` and the future Agent / PlanExecutor
loop.  It owns:

    * a :class:`CapabilityRegistry` (canonical, read-only);
    * an :class:`LLMIntentInterpreter` (text → Intent);
    * a :class:`Planner` (Goal → Plan).

The Brain is **never** allowed to:

    * import :mod:`core.omnix_engine`;
    * import :mod:`core.capability_router`;
    * call :class:`Capability.execute` directly;
    * call :class:`Planner.plan` and dispatch the result;
    * import any V6 *Windows service* (``system.windows.*``,
      ``system.applications.*``, ...).

The Brain's job is to produce a *trusted* :class:`Plan` from a user
utterance (or a system trigger).  Execution belongs to the future
Agent / PlanExecutor.

Two-stage AI pipeline (R-5 / AD-5):

    user text  →  IntentInterpreter  →  Intent
                                     ↘
                                       Planner.plan(goal)  →  Plan
                                     ↗
    Goal  ←  intent.to_goal()

Clarification, unknown, and error cases from the interpreter are
surfaced as :class:`BrainResult` with a non-OK status, not raised
as exceptions.  Hard failures (provider errors, plan validation
errors) are raised as :class:`BrainError` subclasses.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.capability_registry import CapabilityRegistry
from core.orchestration import (
    Failure,
    Goal,
    Intent,
    IntentKind,
    Plan,
)
from core.orchestration.interfaces import IntentInterpreter, Planner

from ai.intent import IntentResult, LLMIntentInterpreter

from .exceptions import (
    BrainError,
    CancelledError,
    CannotPlanError,
    ClarificationRequired,
    ProviderFailure,
)
from .validation import MAX_PLAN_STEPS


# ---------------------------------------------------------------------------
# Result wrapper
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrainResult:
    """The structured outcome of a :class:`Brain.handle_text` call.

    ``status`` is one of:

        * ``"ok"``             — a trusted :class:`Plan` is available.
        * ``"clarification"``  — the interpreter asked the user a
                                 question; no plan is produced.
        * ``"unknown"``        — the interpreter could not classify
                                 the input; no plan is produced.
        * ``"error"``          — a structured error with a stable
                                 ``error_code`` (mirrors a
                                 :class:`BrainError` but is *data*,
                                 not an exception).
    """

    status: str
    plan: Optional[Plan] = None
    goal: Optional[Goal] = None
    intent: Optional[Intent] = None
    clarifying_question: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "plan": self.plan.to_dict() if self.plan else None,
            "goal": self.goal.to_dict() if self.goal else None,
            "intent": self.intent.to_dict() if self.intent else None,
            "clarifying_question": self.clarifying_question,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Brain
# ---------------------------------------------------------------------------

class Brain:
    """The two-stage AI pipeline (Intent → Goal → Plan).

    The Brain holds:

        * a :class:`CapabilityRegistry` (read-only),
        * an :class:`IntentInterpreter`,
        * a :class:`Planner`.

    The public surface is intentionally small:

        * :meth:`handle_text(text)` — the full two-stage pipeline.
        * :meth:`plan(goal, intent=...)` — plan from a known goal
          (used by the orchestrator for replans and for hand-built
          goals).
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        interpreter: IntentInterpreter,
        planner: Planner,
        name: str = "brain",
        max_plan_steps: int = MAX_PLAN_STEPS,
    ) -> None:
        if registry is None or not isinstance(registry, CapabilityRegistry):
            raise TypeError(
                f"Brain expected a CapabilityRegistry, got {type(registry).__name__}"
            )
        if interpreter is None or not hasattr(interpreter, "interpret"):
            raise ValueError("Brain requires an IntentInterpreter")
        if planner is None or not hasattr(planner, "plan"):
            raise ValueError("Brain requires a Planner")
        self.registry = registry
        self.interpreter = interpreter
        self.planner = planner
        self.name = str(name)
        self.max_plan_steps = int(max_plan_steps)

    # ------------------------------------------------------------------
    # Two-stage pipeline
    # ------------------------------------------------------------------

    def handle_text(
        self,
        text: str,
        *,
        context_snapshot: Optional[Dict[str, Any]] = None,
    ) -> BrainResult:
        """Run the full two-stage pipeline on ``text``.

        The Brain is *strictly* read-only: it never executes, never
        dispatches.  If the interpreter asks for clarification, the
        Brain returns :class:`BrainResult` with
        ``status="clarification"``.  If the interpreter cannot
        classify the input, ``status="unknown"``.  If the planner
        fails, the Brain raises a :class:`BrainError` subclass; the
        caller (orchestrator) is expected to map that into a
        :class:`Failure` and route it to the recovery engine.
        """
        if not isinstance(text, str):
            raise TypeError(f"Brain.handle_text expected str, got {type(text).__name__}")

        # Stage 1: text → Intent
        intent_result: IntentResult = self.interpreter.interpret(
            text,
            context_snapshot=context_snapshot,
        )
        if intent_result.status == "clarification":
            return BrainResult(
                status="clarification",
                intent=intent_result.intent,
                clarifying_question=intent_result.clarifying_question
                or (intent_result.intent.parameters.get("question") if intent_result.intent else None),
                metadata={"stage": "intent"},
            )
        if intent_result.status == "unknown":
            return BrainResult(
                status="unknown",
                intent=intent_result.intent,
                metadata={"stage": "intent"},
            )
        if intent_result.status == "error":
            return BrainResult(
                status="error",
                error_code=intent_result.error_code or "INTENT_ERROR",
                error_message=intent_result.error_message or "Intent interpreter returned an error.",
                metadata={
                    "stage": "intent",
                    "intent_metadata": dict(intent_result.error_context) if intent_result.error_context else {},
                },
            )
        intent = intent_result.intent
        if intent is None:
            # Defensive: an "ok" status without an intent is a bug.
            return BrainResult(
                status="error",
                error_code="INTENT_MISSING",
                error_message="Intent interpreter returned ok but no intent.",
            )

        # Stage 2: Intent → Goal → Plan
        try:
            goal = intent.to_goal(goal_id=f"goal_{_short_id()}")
        except Exception as exc:  # noqa: BLE001 - defensive
            return BrainResult(
                status="error",
                intent=intent,
                error_code="GOAL_CONVERSION_FAILED",
                error_message=str(exc),
                metadata={"stage": "goal"},
            )

        try:
            plan = self.planner.plan(goal, intent=intent, context_snapshot=context_snapshot)
        except CannotPlanError as exc:
            # Soft planning failure: the orchestrator should re-prompt
            # the user with a clarification.  We surface it as a
            # structured result so the caller can decide.
            return BrainResult(
                status="error",
                intent=intent,
                goal=goal,
                error_code=exc.code if hasattr(exc, "code") else "BRAIN_CANNOT_PLAN",
                error_message=exc.message,
                metadata={"stage": "plan"},
            )
        except BrainError as exc:
            # Hard failures (provider, validation) bubble up; the
            # caller decides how to route them.
            raise
        except Exception as exc:  # noqa: BLE001
            # An unknown error in the planner.  Convert to a
            # structured failure so the caller does not have to
            # expect a bare ``Exception``.
            return BrainResult(
                status="error",
                intent=intent,
                goal=goal,
                error_code="BRAIN_INTERNAL",
                error_message=str(exc),
                metadata={"stage": "plan", "exception": type(exc).__name__},
            )

        return BrainResult(
            status="ok",
            intent=intent,
            goal=goal,
            plan=plan,
            metadata={"stage": "ok"},
        )

    # ------------------------------------------------------------------
    # Planning from a known goal (replan path, hand-built goals)
    # ------------------------------------------------------------------

    def plan(
        self,
        goal: Goal,
        *,
        intent: Optional[Intent] = None,
        context_snapshot: Optional[Dict[str, Any]] = None,
        prior_plan: Optional[Plan] = None,
        failure: Optional[Failure] = None,
    ) -> Plan:
        """Run only the planning stage, from a known goal.

        This is the entry point used by the future orchestrator for
        replans and for hand-built goals.  It does NOT call the
        interpreter; the caller is expected to already have a goal
        (and optionally an intent) in hand.
        """
        if not isinstance(goal, Goal):
            raise TypeError(f"Brain.plan expected a Goal, got {type(goal).__name__}")
        return self.planner.plan(
            goal,
            intent=intent,
            context_snapshot=context_snapshot,
            prior_plan=prior_plan,
            failure=failure,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _short_id() -> str:
    return uuid.uuid4().hex[:12]
