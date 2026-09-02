"""
Omnix V6 — Local-first fast path dispatcher (Phase 15).

For trivially-classifiable single-step app commands
(``"open notepad"``, ``"close chrome"``, ``"launch spotify"``) we
use the :class:`LocalActionDecisionEngine` to produce a fully
resolved :class:`Plan` *without* consulting the Brain or the
Agent.  The plan is then dispatched through the
:class:`CapabilityRouter` so the underlying capability is actually
executed.

The dispatcher is the *only* caller of
:func:`LocalActionDecisionEngine.classify`.  It returns a
:class:`core.results.CapabilityResult` wrapping the plan's outcome.

The dispatcher never claims a hit unless:

  * the engine matched the input verb+target;
  * the catalog resolved the app;
  * the corresponding capability is registered.

When the engine matches but the app is not in the catalog, the
dispatcher surfaces a structured FAILED result so the speech layer
narrates ``"I couldn't find that."``  When the engine does not
match, the dispatcher returns ``None`` and the request pipeline
falls through to the Brain + Agent.

Architectural rules:

  * The dispatcher is generic.  It never references a specific
    application name.
  * The dispatcher executes the capability through the router — it
    never fabricates a VERIFIED result.  The capability's own
    verification (e.g. ``app_launched``, ``app_closed``) is the only
    source of truth.
"""

from __future__ import annotations

import logging
import time
from typing import Any, List, Optional

from loguru import logger

from core.capability_registry import CapabilityRegistry
from core.capability_router import CapabilityRouter
from core.orchestration.execution_result import (
    ExecutionOutcome,
    ExecutionResult,
    StepResult,
    StepState,
    make_blank_execution_result,
)
from core.orchestration.plan_executor import PlanExecutor
from core.orchestration.models import (
    ExecutionContext,
    Goal,
    Plan,
    PlanStatus,
)
from core.results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
    VerificationStatus,
)
from core.errors import OmnixError

from .local_decision_engine import LocalActionDecisionEngine, LocalDecision


class FastPathDispatcher:
    """Execute trivially-classifiable commands locally.

    The dispatcher wires the :class:`LocalActionDecisionEngine` to
    the :class:`CapabilityRouter` and a :class:`PlanExecutor` (or a
    synchronous shortcut for single-step plans).  The two
    construction paths (executor present / not) are equivalent: the
    executor wraps the same dispatch and adds observability.
    """

    def __init__(
        self,
        *,
        resolver: Any,
        registry: Optional[CapabilityRegistry] = None,
        router: Optional[CapabilityRouter] = None,
        plan_executor: Optional[PlanExecutor] = None,
        cancellation_token: Any = None,
    ) -> None:
        if resolver is None:
            raise TypeError("FastPathDispatcher requires a resolver")
        self._resolver = resolver
        # Lazy default registry when no router/executor is supplied.
        self._registry = registry
        self._router = router
        self._executor = plan_executor
        # Phase 17: cooperative-cancellation token forwarded to
        # every router.route() call.  The token lives on the
        # dispatcher so callers (Agent, voice loop) can set it once
        # per run and the input layer can react to it.
        self._cancellation_token = cancellation_token
        # Build the local engine eagerly so the pattern table is
        # constructed once.
        if registry is None and router is not None:
            self._registry = router.registry
        if self._registry is None:
            # Defer until first use.
            self._engine: Optional[LocalActionDecisionEngine] = None
        else:
            self._engine = LocalActionDecisionEngine(
                registry=self._registry,
                resolver=resolver,
            )

    def _ensure_engine(self) -> LocalActionDecisionEngine:
        if self._engine is None:
            if self._registry is None:
                raise RuntimeError(
                    "FastPathDispatcher has no CapabilityRegistry; "
                    "pass registry= or router= at construction"
                )
            self._engine = LocalActionDecisionEngine(
                registry=self._registry,
                resolver=self._resolver,
            )
        return self._engine

    # ----------------------------------------------------------- public API
    def set_cancellation_token(self, token: Any) -> None:
        """Install (or replace) the cooperative-cancellation token.

        The token is forwarded to every router.route() call made by
        this dispatcher.  Setting ``token=None`` clears it.
        """
        self._cancellation_token = token

    def try_dispatch(self, text: str) -> Optional[CapabilityResult]:
        """Try to dispatch a trivially-classifiable command.

        Returns a :class:`CapabilityResult` for trivially-classifiable
        inputs, or ``None`` when the input needs the full pipeline.
        """
        if not text or not isinstance(text, str):
            return None
        engine = self._ensure_engine()
        try:
            decision = engine.classify(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"LocalDecisionEngine.classify raised: {exc!r}")
            return None
        if not decision.matched:
            return None
        if decision.not_found:
            return _not_found_result(decision)
        if decision.plan is None:
            return None
        return self._execute_plan(decision)

    # ----------------------------------------------------------- internals
    def _execute_plan(self, decision: LocalDecision) -> CapabilityResult:
        plan = decision.plan
        if plan is None:
            return None  # type: ignore[return-value]
        # Single-step plan: dispatch directly through the router.
        if len(plan.steps) == 1 and self._router is not None:
            return self._execute_single_step(plan)
        # Multi-step plan: use the executor when available.
        if self._executor is not None:
            return self._execute_via_executor(plan)
        if self._router is not None:
            # No executor; run the steps one at a time.
            return self._execute_sequential(plan)
        return _cannot_dispatch(plan)

    def _execute_single_step(self, plan: Plan) -> CapabilityResult:
        step = plan.steps[0]
        # Stage 19.3: when the executor was wired with an
        # ``ExecutionCycle`` instance, route the dispatch through
        # the cycle so the fast path benefits from the same
        # PRECONDITION → OBSERVE → GROUND → ACT → SYNCHRONIZE →
        # VERIFY phases as the full Agent path.  When no cycle is
        # available (legacy wiring, tests), the dispatcher falls
        # back to a direct ``router.route()`` call so it remains
        # backwards compatible.
        cycle = getattr(self._executor, "execution_cycle", None) if self._executor else None
        if cycle is not None:
            cap_result = self._executor._dispatch_via_execution_cycle(
                step=step,
                context=self._build_fast_path_context(plan, step),
                correlation_id=str(plan.plan_id),
                step_timeout=float(getattr(step, "timeout_s", 30.0) or 30.0),
                cancellation_token=self._cancellation_token,
            )
        else:
            # Dispatch through the router so the underlying capability
            # is the only source of truth for status / verification.
            cap_result = self._router.route(
                step.capability_name,
                dict(step.parameters),
                cancellation_token=self._cancellation_token,
            )
        # Decorate the result with the local-first metadata so the
        # audit log can attribute the dispatch to the fast path.
        # ``CapabilityResult`` is a frozen dataclass; use
        # ``dataclasses.replace`` to produce a new instance.
        from dataclasses import replace
        try:
            details = dict(cap_result.details or {})
            details["local_first"] = True
            details["plan_id"] = plan.plan_id
            details["step_id"] = step.step_id
            cap_result = replace(cap_result, details=details)
        except Exception:
            pass
        return cap_result

    def _build_fast_path_context(self, plan: Plan, step: Any) -> Any:
        """Build a minimal :class:`ExecutionContext` for fast-path
        cycle dispatch.  Keeps the fast path self-contained — the
        Agent's full context is not required because each fast
        path is a single-step plan with no upstream state.
        """
        from core.orchestration.models import ExecutionContext, Goal
        goal = Goal(
            goal_id=f"goal-fast-{plan.plan_id}",
            description="fast-path plan",
            metadata={"source": "fast_path_dispatcher"},
        )
        return ExecutionContext(
            execution_id=f"exec-fast-{plan.plan_id}",
            goal=goal,
            plan=plan,
        )

    def _execute_via_executor(self, plan: Plan) -> CapabilityResult:
        goal = Goal(
            goal_id=f"goal-local-{plan.plan_id}",
            description="local-first plan",
            metadata={"source": "local_decision_engine"},
        )
        ctx = ExecutionContext(
            execution_id=f"exec-{plan.plan_id}",
            goal=goal,
            plan=plan,
        )
        result: ExecutionResult = self._executor.execute(ctx)
        return _result_from_execution(plan, result)

    def _execute_sequential(self, plan: Plan) -> CapabilityResult:
        # No executor: run each step through the router in order.
        for step in plan.steps:
            cr = self._router.route(
                step.capability_name,
                dict(step.parameters),
                cancellation_token=self._cancellation_token,
            )
            if cr.status in (
                CapabilityStatus.FAILED,
                CapabilityStatus.SKIPPED,
                CapabilityStatus.CANCELLED,
            ):
                return cr
        # All steps succeeded; return the last one as the canonical
        # result.
        last_step = plan.steps[-1]
        return self._router.route(
            last_step.capability_name,
            dict(last_step.parameters),
            cancellation_token=self._cancellation_token,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _not_found_result(decision: LocalDecision) -> CapabilityResult:
    if not decision.not_found:
        return CapabilityResult(
            capability_name="unknown",
            status=CapabilityStatus.FAILED,
            failed=True,
            error=OmnixError(
                "Application not found",
                code="APP_NOT_FOUND",
            ),
        )
    return CapabilityResult(
        capability_name="desktop.application.open",
        status=CapabilityStatus.FAILED,
        failed=True,
        action=ActionResult(
            status=ActionStatus.FAILED,
            action_name="desktop.application.open",
            details={
                "app_name": decision.not_found[0],
                "reason": "not_found",
            },
        ),
        error=OmnixError(
            message=f"Application not found: {decision.not_found[0]}",
            code="APP_NOT_FOUND",
        ),
        details={"app_name": decision.not_found[0]},
    )


def _cannot_dispatch(plan: Plan) -> CapabilityResult:
    return CapabilityResult(
        capability_name=plan.steps[0].capability_name if plan.steps else "unknown",
        status=CapabilityStatus.SKIPPED,
        failed=True,
        error=OmnixError(
            "FastPathDispatcher has no router or executor; cannot dispatch",
            code="FAST_PATH_NO_DISPATCH",
        ),
        details={"plan_id": plan.plan_id, "step_count": len(plan.steps)},
    )


def _result_from_execution(
    plan: Plan, result: ExecutionResult
) -> CapabilityResult:
    if result.outcome is ExecutionOutcome.COMPLETED:
        status = CapabilityStatus.VERIFIED
    elif result.outcome is ExecutionOutcome.PARTIAL:
        status = CapabilityStatus.EXECUTED
    elif result.outcome is ExecutionOutcome.CANCELLED:
        status = CapabilityStatus.CANCELLED
    elif result.outcome is ExecutionOutcome.TIMED_OUT:
        status = CapabilityStatus.TIMED_OUT
    else:
        status = CapabilityStatus.FAILED

    failed = status in (
        CapabilityStatus.FAILED,
        CapabilityStatus.TIMED_OUT,
        CapabilityStatus.CANCELLED,
    )

    cap_name = plan.steps[0].capability_name if plan.steps else "unknown"
    return CapabilityResult(
        capability_name=cap_name,
        status=status,
        attempted=bool(result.step_results),
        executed=any(r.status is StepState.SUCCEEDED for r in result.step_results),
        verified=status is CapabilityStatus.VERIFIED,
        failed=failed,
        error=_error_from_execution(result),
        details={
            "plan_id": plan.plan_id,
            "step_count": len(plan.steps),
            "succeeded": result.succeeded_step_count,
            "failed_steps": result.failed_step_count,
        },
    )


def _error_from_execution(result: ExecutionResult) -> Optional[OmnixError]:
    for step in result.step_results:
        if step.status in (StepState.FAILED, StepState.TIMED_OUT):
            return OmnixError(
                message=step.error or "step failed",
                code=step.status.value.upper(),
                metadata={"step_id": step.step_id},
            )
    if result.error:
        return OmnixError(message=result.error, code="EXECUTION_ERROR")
    return None


__all__ = ["FastPathDispatcher"]
