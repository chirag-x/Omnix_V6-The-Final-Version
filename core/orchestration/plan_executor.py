"""
Omnix V6 — PlanExecutor (Phase 6A+6B).

The single component authorized to cross the brain → execution
boundary.  It is the only place in the V6 architecture that takes a
validated :class:`Plan`, walks its :class:`PlanStep` graph, and
constructs :class:`ActionRequest` objects for the
:class:`core.capability_router.CapabilityRouter`.

Flow::

    Validated Plan
        ↓
    PlanExecutor.execute(context)
        ├── _preconditions()                 (validate plan, lock idempotency)
        ├── _initialise_execution_result()   (start the audit trail)
        ├── _build_ready_queue()             (topological order over the DAG)
        └── for each ready step:
              ├── _resolve_dependencies()    (BLOCKED if upstream failed)
              ├── _build_action_request()   (PlanStep → ActionRequest)
              ├── _dispatch()                (router.route(...))
              ├── _classify()                (CapabilityResult → StepState)
              └── _emit_event()              (structured observability)
        ↓
    ExecutionResult

Architectural rules honored here:

- R-1   — single boot path; the executor is constructed once at
          engine boot and shared.
- R-3   — the executor owns *no* registry; it reads from the
          canonical :class:`core.capability_registry.CapabilityRegistry`
          through the :class:`core.capability_router.CapabilityRouter`.
- R-8   — typed status enums throughout; no bare booleans.
- R-10  — input :class:`ExecutionContext` is never mutated; the
          executor returns a fresh terminal context.
- R-13  — the executor never invents capability names; the closed
          registry set is the only valid surface.
- R-17  — ``loguru`` is the only logger; no ``logging`` imports.
- R-21  — the executor is the *only* place that constructs an
          :class:`ActionRequest`; downstream of the executor the
          boundary is the router.
- R-23  — the executor is the only writer of the per-execution
          audit trail; the orchestrator decides whether to commit
          the result to the global :class:`ContextService`.
- AD-21 — the four capability phase flags surface through the
          embedded :class:`core.results.CapabilityResult`.

What this module deliberately does NOT do:

- Implement a recovery / replan engine (Phase 6C territory).
- Implement vision / browser / voice (future phases).
- Spawn a long-running asyncio loop; the router handles
  sync→async bridging, the executor stays synchronous.
- Call the :class:`ContextService` directly.  The executor returns
  a fresh :class:`ExecutionContext`; the orchestrator decides
  whether to commit it.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from loguru import logger

from .execution_result import (
    ExecutionOutcome,
    ExecutionResult,
    StepResult,
    StepState,
    make_blank_execution_result,
    new_correlation_id,
)
from .models import (
    ActionKind,
    ActionRequest,
    ExecutionContext,
    Plan,
    PlanStep,
)

# Stage 19.3: ExecutionCycle integration — minimal wrapper at dispatch boundary
try:
    from core.execution import (
        ExecutionCycle,
        ExecutionStep,
        StepAction,
        ExecutionPolicy,
        DefaultActionExecutor,
        DefaultVerificationProvider,
        DefaultGroundingProvider,
    )
    _STAGE19_AVAILABLE = True
except Exception:
    _STAGE19_AVAILABLE = False


# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

# A function that the executor consults to check whether a dangerous
# capability is authorised *for this plan run*.  Returning ``True``
# means the executor will set ``authorized_dangerous=True`` on the
# router call.  Returning ``False`` causes the step to be SKIPPED.
# The default implementation refuses everything; the engine can
# install a more permissive one.
DangerousAuthorizer = Callable[[str, ActionRequest], bool]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PlanExecutorError(Exception):
    """Base class for executor-side errors.

    The executor surfaces a *typed* error (R-8) so the orchestrator
    can route the failure without parsing strings.
    """

    code: str = "PLAN_EXECUTOR_ERROR"

    def __init__(self, message: str, *, context: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.context: Dict[str, Any] = dict(context) if context else {}


class InvalidPlanError(PlanExecutorError):
    """The plan itself is invalid (empty steps, broken DAG, ...)."""

    code = "PLAN_INVALID"


class IdempotencyViolation(PlanExecutorError):
    """The executor was asked to run an execution_id that is already in flight."""

    code = "PLAN_IDEMPOTENCY_VIOLATION"


class CancellationRequested(PlanExecutorError):
    """The executor was cancelled mid-flight."""

    code = "PLAN_CANCELLED"


# ---------------------------------------------------------------------------
# Default policies
# ---------------------------------------------------------------------------

def _refuse_all_dangerous(name: str, request: ActionRequest) -> bool:
    """Default :class:`DangerousAuthorizer` — refuse every dangerous call."""
    return False


# ---------------------------------------------------------------------------
# The PlanExecutor
# ---------------------------------------------------------------------------

@dataclass
class PlanExecutor:
    """The canonical V6 :class:`PlanExecutor`.

    Construction is cheap; the executor is thread-safe and re-entrant
    for *different* ``execution_id``s.  The same ``execution_id``
    cannot run twice concurrently — the idempotency lock will raise
    :class:`IdempotencyViolation`.

    Parameters
    ----------
    router:
        The single authorized entry point for invoking capabilities
        (R-21).  Must be supplied.
    dangerous_authorizer:
        Optional callback that decides whether a *dangerous*
        capability is allowed to run.  Defaults to refusing
        everything (safe-by-default).
    default_step_timeout_s:
        The fallback per-step timeout when a :class:`PlanStep` does
        not declare one.  Mirrors the engine's default capability
        timeout (60s).
    default_plan_timeout_s:
        A wall-clock cap on a single ``execute()`` call.  ``0.0``
        disables it.
    observability_sink:
        Optional callable invoked once per executor event (the
        executor emits ``plan_started`` / ``step_started`` /
        ``step_finished`` / ``plan_finished``).  Tests use it to
        assert the event stream.
    event_bus:
        Optional :class:`core.events.event_bus.EventBus`.  When
        supplied, the executor ALSO publishes a :class:`RequestEvent`
        for every action executed (``REQUEST_ACTION_EXECUTED``),
        every observation captured (``REQUEST_OBSERVATION_CAPTURED``),
        and every recovery action (``REQUEST_RECOVERY_STARTED``).
        Phase 12 wires this so the canonical pipeline can observe
        real execution at the bus level.  The executor never
        requires a bus — bus is purely additive observability.
    """

    router: Any
    dangerous_authorizer: DangerousAuthorizer = _refuse_all_dangerous
    default_step_timeout_s: float = 60.0
    default_plan_timeout_s: float = 0.0
    observability_sink: Optional[Callable[[Dict[str, Any]], None]] = None
    event_bus: Optional[Any] = None
    # Stage 19.3: optional ExecutionCycle instance.  When provided,
    # ``_run_step`` routes through the cycle (PRECONDITION → OBSERVE
    # → GROUND → ACT → SYNCHRONIZE → VERIFY).  When ``None``, the
    # executor falls back to the legacy direct ``router.route()``
    # call so older wiring continues to work unchanged.  The engine
    # builds and injects the cycle at boot time.
    execution_cycle: Optional[Any] = None
    # ----- internal state --------------------------------------------------
    _idempotency_lock: threading.RLock = field(default_factory=threading.RLock)
    _inflight: Set[str] = field(default_factory=set)
    _name: str = field(default="plan_executor", init=False)

    # --------------------------------------------------------- identity
    @property
    def name(self) -> str:
        return self._name

    # ============================================================== api
    def execute(self, context: ExecutionContext) -> ExecutionResult:
        """Run the plan in ``context`` and return a terminal result.

        This is the public entry point.  It:

        1. Reserves the ``execution_id`` for idempotency.
        2. Initialises the :class:`ExecutionResult`.
        3. Walks the plan's DAG, dispatching each ready step.
        4. Releases the reservation in ``finally``.
        5. Returns the result; the *caller* decides what to do with it.

        The input :class:`ExecutionContext` is never mutated.

        Phase 4: a ``cancellation_token`` is read from
        ``context.cancellation_token`` (optional) and checked at
        the top of the step-dispatch loop.  When the token is
        cancelled, the executor returns a result with outcome
        :class:`ExecutionOutcome.CANCELLED` and per-step
        :class:`StepState.CANCELLED` so the Agent can finalise
        the run with :class:`AgentState.CANCELLED`.
        """
        self._acquire_idempotency_lock(context.execution_id)
        try:
            return self._execute_locked(context)
        finally:
            self._release_idempotency_lock(context.execution_id)

    def execute_step(
        self,
        context: ExecutionContext,
        step: PlanStep,
    ) -> StepResult:
        """Run a single step outside the plan loop.

        Used by the recovery layer and by hand-driven tests.  The
        caller is responsible for the idempotency lock and the
        plan-level accounting; this method is intentionally a thin
        wrapper around :meth:`_run_step` for a single step.

        Returns
        -------
        :class:`StepResult`
            The per-step outcome.  The function does NOT raise for
            routine failures (unknown capability, refused dangerous
            call, etc.) — those surface as :class:`StepState.FAILED`
            or :class:`StepState.SKIPPED` on the returned
            :class:`StepResult`.  It only raises for
            *executor-internal* errors.

        Phase 4: when ``context.cancellation_token`` is set and
        cancelled, the method returns a :class:`StepResult` with
        :class:`StepState.CANCELLED` instead of running the
        step.
        """
        token = getattr(context, "cancellation_token", None)
        if token is not None and getattr(token, "is_cancelled", False):
            return StepResult(
                step_id=step.step_id,
                capability_name=step.capability_name,
                status=StepState.CANCELLED,
                error=(
                    getattr(token, "reason", "")
                    or "cancelled before step dispatch"
                ),
            )
        return self._run_step(
            context=context,
            step=step,
            upstream_failed=set(),
            correlation_id=new_correlation_id(),
            attempt=1,
        )

    # =========================================================== execute
    def _execute_locked(self, context: ExecutionContext) -> ExecutionResult:
        plan: Plan = context.plan
        goal = context.goal

        # ---- 1. preconditions ------------------------------------------
        self._check_plan_preconditions(plan)

        # ---- 2. initialise the audit trail ------------------------------
        correlation_id = new_correlation_id()
        result = make_blank_execution_result(
            execution_id=context.execution_id,
            plan_id=plan.plan_id,
            goal_id=goal.goal_id,
            correlation_id=correlation_id,
        )
        self._emit_event(
            kind="plan_started",
            plan_id=plan.plan_id,
            goal_id=goal.goal_id,
            execution_id=context.execution_id,
            correlation_id=correlation_id,
            step_count=plan.step_count,
        )

        plan_started = time.time()
        plan_deadline = (
            plan_started + self.default_plan_timeout_s
            if self.default_plan_timeout_s > 0.0
            else None
        )

        # ---- 3. walk the plan -------------------------------------------
        completed: Set[str] = set(context.completed_step_ids)
        failed: Set[str] = set(context.failed_step_ids)
        upstream_failed: Set[str] = set()  # populated as we go

        # Plan was already running; carry over completed/failed from
        # the input context.
        for sid in completed:
            upstream_failed.discard(sid)  # completed is not "failed"

        plan_plan = plan
        step_order = self._topological_order(plan_plan)
        # Phase 4: pull the optional cancellation token from the
        # context once.  When the token is cancelled, mark every
        # remaining step CANCELLED and return early.
        ctx_token = getattr(context, "cancellation_token", None)
        for step_id in step_order:
            # Phase 4: cooperative cancellation.  When the token
            # is cancelled, mark the remaining steps CANCELLED
            # and exit the dispatch loop with outcome CANCELLED.
            if ctx_token is not None and getattr(ctx_token, "is_cancelled", False):
                for sid in step_order:
                    if sid in completed or sid in failed:
                        continue
                    s_obj = plan_plan.find_step(sid)
                    cancelled = StepResult(
                        step_id=sid,
                        capability_name=s_obj.capability_name if s_obj else "",
                        status=StepState.CANCELLED,
                        error=(
                            getattr(ctx_token, "reason", "")
                            or "cancelled during plan execution"
                        ),
                    )
                    result = result.with_step_result(cancelled)
                result = result.with_outcome(
                    ExecutionOutcome.CANCELLED,
                    completed_at=time.time(),
                    error=(
                        getattr(ctx_token, "reason", "")
                        or "cancelled during plan execution"
                    ),
                )
                break
            # Plan-level deadline check.
            if plan_deadline is not None and time.time() > plan_deadline:
                result = result.with_outcome(
                    ExecutionOutcome.TIMED_OUT,
                    completed_at=time.time(),
                    error=f"plan deadline exceeded after {self.default_plan_timeout_s}s",
                )
                # Mark remaining steps as BLOCKED.
                for sid in step_order:
                    if sid in completed or sid in failed:
                        continue
                    blocked = StepResult(
                        step_id=sid,
                        capability_name=plan_plan.find_step(sid).capability_name if plan_plan.find_step(sid) else "",
                        status=StepState.BLOCKED,
                        error="plan-level deadline exceeded",
                    )
                    result = result.with_step_result(blocked)
                break

            step = plan_plan.find_step(step_id)
            if step is None:
                continue  # already validated; this branch is defensive

            # Dependency check.
            if self._dependencies_failed(step, failed):
                blocked = StepResult(
                    step_id=step.step_id,
                    capability_name=step.capability_name,
                    status=StepState.BLOCKED,
                    error="upstream dependency failed",
                )
                result = result.with_step_result(blocked)
                failed.add(step.step_id)
                upstream_failed.add(step.step_id)
                continue

            # Skip already-completed steps (resume support).
            if step.step_id in completed:
                # Synthesize a "ghost" StepResult for audit.
                result = result.with_step_result(
                    StepResult(
                        step_id=step.step_id,
                        capability_name=step.capability_name,
                        status=StepState.SUCCEEDED,
                        error="resumed from prior context",
                    )
                )
                continue

            # Dispatch the step.
            step_result = self._run_step(
                context=context,
                step=step,
                upstream_failed=upstream_failed,
                correlation_id=correlation_id,
                attempt=1,
            )
            result = result.with_step_result(step_result)

            if step_result.ok:
                completed.add(step.step_id)
            else:
                failed.add(step.step_id)
                upstream_failed.add(step.step_id)
                # Hard failure short-circuits the plan: the executor
                # does NOT continue to steps whose only purpose is to
                # run after this one.  Phase 6C will add recovery.
                blocked_ids = self._descendants(plan_plan, step.step_id) - completed
                for blocked_id in blocked_ids:
                    blocked_step = plan_plan.find_step(blocked_id)
                    if blocked_step is None:
                        continue
                    blocked_result = StepResult(
                        step_id=blocked_id,
                        capability_name=blocked_step.capability_name,
                        status=StepState.BLOCKED,
                        error=f"upstream step {step.step_id} failed",
                    )
                    result = result.with_step_result(blocked_result)
                    failed.add(blocked_id)
                    upstream_failed.add(blocked_id)
                # Phase 6A: stop on first hard failure.  The future
                # recovery engine will decide whether to retry /
                # replan / ask the user.
                break

        # ---- 4. compute the outcome -------------------------------------
        result = self._finalize_outcome(result, started_at=plan_started)
        self._emit_event(
            kind="plan_finished",
            plan_id=plan.plan_id,
            goal_id=goal.goal_id,
            execution_id=context.execution_id,
            correlation_id=correlation_id,
            outcome=result.outcome.value,
            step_count=result.step_count,
            succeeded=result.succeeded_step_count,
            failed=result.failed_step_count,
            skipped=result.skipped_step_count,
            duration_ms=result.duration_ms,
        )
        return result

    # =========================================================== step
    def _run_step(
        self,
        *,
        context: ExecutionContext,
        step: PlanStep,
        upstream_failed: Set[str],
        correlation_id: str,
        attempt: int,
    ) -> StepResult:
        """Run one step and return a :class:`StepResult`.

        The function never raises for *plan*-level failures (unknown
        capability, refused dangerous call, capability returned
        failure).  It only raises for *executor-internal* errors
        (a malformed plan, a registry that disappeared, ...).
        """
        started_at = time.time()
        step_timeout = step.timeout_s if step.timeout_s > 0 else self.default_step_timeout_s

        self._emit_event(
            kind="step_started",
            plan_id=context.plan.plan_id,
            goal_id=context.goal.goal_id,
            execution_id=context.execution_id,
            correlation_id=correlation_id,
            step_id=step.step_id,
            capability_name=step.capability_name,
            action=step.action.value,
            attempt=attempt,
        )

        # ---- precondition: dependency check ----------------------------
        if self._dependencies_failed(step, upstream_failed):
            sr = StepResult(
                step_id=step.step_id,
                capability_name=step.capability_name,
                status=StepState.BLOCKED,
                started_at=started_at,
                completed_at=time.time(),
                duration_ms=_ms_since(started_at),
                error="upstream dependency failed",
                attempt=attempt,
            )
            self._emit_step_finished(context, correlation_id, sr)
            return sr

        # ---- precondition: action kind --------------------------------
        if step.action is not ActionKind.CAPABILITY_CALL:
            # Phase 6A only implements CAPABILITY_CALL.  Other
            # action kinds (OBSERVE, VERIFY, WAIT, ASK_USER) are
            # short-circuited to SKIPPED with an explicit reason;
            # the recovery engine can route them to a future
            # Phase 6C handler.
            sr = StepResult(
                step_id=step.step_id,
                capability_name=step.capability_name,
                status=StepState.SKIPPED,
                started_at=started_at,
                completed_at=time.time(),
                duration_ms=_ms_since(started_at),
                error=f"action kind {step.action.value!r} is not executable in Phase 6A",
                attempt=attempt,
            )
            self._emit_step_finished(context, correlation_id, sr)
            return sr

        # ---- precondition: capability exists --------------------------
        registry = self._registry()
        if registry is None or not registry.has(step.capability_name):
            sr = StepResult(
                step_id=step.step_id,
                capability_name=step.capability_name,
                status=StepState.FAILED,
                started_at=started_at,
                completed_at=time.time(),
                duration_ms=_ms_since(started_at),
                error=f"unknown capability: {step.capability_name!r}",
                attempt=attempt,
            )
            self._emit_step_finished(context, correlation_id, sr)
            return sr

        # ---- precondition: dangerous capability authorization ---------
        cap = registry.get(step.capability_name)
        authorized_dangerous = False
        safety_metadata: Dict[str, Any] = {
            "step_id": step.step_id,
            "plan_id": context.plan.plan_id,
        }
        if getattr(cap.spec, "dangerous", False):
            safety_metadata["dangerous"] = True
            # Build a *preview* request so the authorizer can inspect
            # the parameters without us paying the construction cost
            # twice.
            preview = ActionRequest(
                capability_name=step.capability_name,
                parameters=dict(step.parameters),
                request_id=_new_request_id(),
                expected_effect=step.expected_effect,
                plan_id=context.plan.plan_id,
                step_id=step.step_id,
                timeout_s=step_timeout,
                correlation_id=correlation_id,
                safety_metadata=safety_metadata,
            )
            if not self.dangerous_authorizer(step.capability_name, preview):
                sr = StepResult(
                    step_id=step.step_id,
                    capability_name=step.capability_name,
                    status=StepState.SKIPPED,
                    started_at=started_at,
                    completed_at=time.time(),
                    duration_ms=_ms_since(started_at),
                    error="dangerous capability not authorized",
                    action_request=preview,
                    attempt=attempt,
                )
                self._emit_step_finished(context, correlation_id, sr)
                return sr
            authorized_dangerous = True

        # ---- build the ActionRequest ----------------------------------
        action_request = ActionRequest(
            capability_name=step.capability_name,
            parameters=dict(step.parameters),
            request_id=_new_request_id(),
            expected_effect=step.expected_effect,
            issued_at=time.time(),
            plan_id=context.plan.plan_id,
            step_id=step.step_id,
            timeout_s=step_timeout,
            safety_metadata=safety_metadata,
            correlation_id=correlation_id,
            metadata={
                "execution_id": context.execution_id,
                "goal_id": context.goal.goal_id,
                "step_description": step.description,
            },
        )

        # ---- dispatch ---------------------------------------------------
        # Stage 19.3: route through the ExecutionCycle when one is
        # wired in.  This makes the canonical execution path
        # ``main.py → Brain → Agent → PlanExecutor → ExecutionCycle
        # → CapabilityRouter``, so every real action traverses the
        # PRECONDITION → OBSERVE → GROUND → ACT → SYNCHRONIZE →
        # VERIFY phases before the router is called.  When no cycle
        # is wired (older hosts, tests) we fall through to the
        # legacy direct router.route() call so the executor remains
        # backwards compatible.
        ctx_token = getattr(context, "cancellation_token", None)
        if _STAGE19_AVAILABLE and self.execution_cycle is not None:
            cap_result = self._dispatch_via_execution_cycle(
                step=step,
                context=context,
                correlation_id=correlation_id,
                step_timeout=step_timeout,
                cancellation_token=ctx_token,
            )
        else:
            # Legacy / fallback path.  Preserved exactly so older
            # wiring continues to work; the cycle is additive.
            cap_result = self.router.route(
                step.capability_name,
                dict(step.parameters),
                authorized_dangerous=authorized_dangerous,
                cancellation_token=ctx_token,
            )

        # ---- classify ---------------------------------------------------
        step_state = self._classify_capability_result(cap_result)
        sr = StepResult(
            step_id=step.step_id,
            capability_name=step.capability_name,
            status=step_state,
            capability_result=cap_result,
            action_request=action_request,
            started_at=started_at,
            completed_at=time.time(),
            duration_ms=_ms_since(started_at),
            error=_extract_error_message(cap_result),
            attempt=attempt,
        )
        self._emit_step_finished(context, correlation_id, sr)
        return sr

    # =========================================================== helpers
    def _registry(self) -> Any:
        """Return the underlying registry from the router, if any."""
        return getattr(self.router, "registry", None)

    # -------------------------------------------------------- Stage 19.3
    def _dispatch_via_execution_cycle(
        self,
        *,
        step: PlanStep,
        context: ExecutionContext,
        correlation_id: str,
        step_timeout: float,
        cancellation_token: Optional[Any],
    ) -> Any:
        """Build an :class:`ExecutionStep` and run it through the cycle.

        Returns a :class:`core.results.CapabilityResult` so the
        existing :meth:`_classify_capability_result` path can map
        cycle outcomes to :class:`StepState` without any change.
        ``ExecutionResult`` carries a structured ``action_result``
        (``CapabilityResult``) populated by the cycle's ACT phase.
        """
        from core.results import (
            CapabilityResult as _CapResult,
            CapabilityStatus as _CapStatus,
            ActionResult as _ActionResult,
            ActionStatus as _ActionStatus,
            VerificationResult as _CapVerification,
            VerificationStatus as _CapVerificationStatus,
        )

        # Build the typed ExecutionStep from the PlanStep.  The
        # action is forced to OPEN_APPLICATION for app-opener
        # capabilities and CAPABILITY_CALL for everything else so
        # the cycle knows the kind of physical interaction it is
        # about to coordinate.  ``capability_name`` carries the
        # router-level capability; ``parameters`` is the closed
        # parameter dict.
        capability_name = str(step.capability_name or "")
        parameters = dict(step.parameters or {})
        target_query, target_kind = self._extract_target_hint(parameters)

        exec_step = ExecutionStep(
            step_id=str(step.step_id),
            action=StepAction.OPEN_APPLICATION
            if "open" in capability_name.lower() or "launch" in capability_name.lower()
            else StepAction.WAIT,  # generic fallback for non-mouse steps
            description=str(step.description or capability_name),
            capability_name=capability_name,
            parameters=parameters,
            target_query=target_query,
            target_kind=target_kind,
            target_hint=None,
            timeout_s=float(step_timeout or 30.0),
            correlation_id=str(correlation_id or ""),
            metadata={
                "plan_id": context.plan.plan_id,
                "goal_id": context.goal.goal_id,
                "execution_id": context.execution_id,
            },
        )

        # Run the cycle.  The cycle is async; the executor stays
        # synchronous so we bridge the gap with asyncio.run() on a
        # fresh loop when the calling thread has none.  When the
        # host is already inside a loop (e.g. agent event loop),
        # use a thread to avoid re-entering the same loop.
        import asyncio
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False

        if in_loop:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                cycle_result = pool.submit(
                    asyncio.run,
                    self.execution_cycle.execute(
                        exec_step,
                        cancellation_token=cancellation_token,
                    ),
                ).result()
        else:
            cycle_result = asyncio.run(
                self.execution_cycle.execute(
                    exec_step,
                    cancellation_token=cancellation_token,
                )
            )

        # Map the ExecutionResult back into a CapabilityResult so
        # downstream _classify_capability_result and the Agent
        # closed loop keep working unchanged.
        return self._execution_result_to_capability_result(
            cycle_result=cycle_result,
            capability_name=capability_name,
            parameters=parameters,
        )

    @staticmethod
    def _extract_target_hint(parameters: Dict[str, Any]) -> Tuple[str, str]:
        """Pull a human-readable target string from a parameter dict.

        The function is generic — it looks for keys commonly used
        across Omnix capabilities (``app_name``, ``target``,
        ``query``, ``text``, ``path``, ``url``) and returns the
        first match.  No application-specific logic.
        """
        for key in ("app_name", "target", "query", "text", "path", "url", "name"):
            v = parameters.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip(), key
        return "", ""

    @staticmethod
    def _execution_result_to_capability_result(
        *,
        cycle_result: Any,
        capability_name: str,
        parameters: Dict[str, Any],
    ) -> Any:
        """Translate an :class:`ExecutionResult` into a
        :class:`CapabilityResult`.

        The mapping preserves all existing executor semantics:

        * ``ExecutionStatus.SUCCESS`` + verified → ``VERIFIED``
        * ``ExecutionStatus.SUCCESS`` (unverified) → ``EXECUTED``
        * any failure code → ``FAILED`` with the cycle's reason
        * ``TIMEOUT`` → ``TIMED_OUT``
        * ``CANCELLED`` → ``CANCELLED``

        When the cycle's ACT phase produced a :class:`CapabilityResult`
        we forward it as-is — the cycle is the source of truth and
        ``_classify_capability_result`` already understands every
        ``CapabilityStatus`` value.  This is the common case for
        ``"open notepad"`` and other app-opener commands.
        """
        from core.results import (
            CapabilityResult as _CapResult,
            CapabilityStatus as _CapStatus,
            ActionResult as _ActionResult,
            ActionStatus as _ActionStatus,
            OmnixError,
        )
        from core.execution.result import (
            ExecutionStatus as _ExecStatus,
            VerificationStatus as _VerStatus,
        )

        # If the cycle's ACT phase already produced a real
        # CapabilityResult, prefer it — the underlying capability
        # is the only source of truth for status / verification.
        action_result = getattr(cycle_result, "action_result", None)
        if isinstance(action_result, _CapResult) or (
            action_result is not None
            and hasattr(action_result, "status")
            and isinstance(getattr(action_result, "status", None), _CapStatus)
        ):
            return action_result

        status = getattr(cycle_result, "status", None)
        verification = getattr(cycle_result, "verification_result", None)
        error_msg = str(getattr(cycle_result, "error", "") or "")

        if status is _ExecStatus.SUCCESS:
            ok = (
                verification is not None
                and getattr(verification, "success", False)
            )
            cap_status = _CapStatus.VERIFIED if ok else _CapStatus.EXECUTED
        elif status is _ExecStatus.TIMEOUT:
            cap_status = _CapStatus.TIMED_OUT
        elif status is _ExecStatus.CANCELLED:
            cap_status = _CapStatus.CANCELLED
        else:
            cap_status = _CapStatus.FAILED

        err = None
        if cap_status is not _CapStatus.VERIFIED:
            err = OmnixError(
                message=error_msg or f"execution cycle status={status.value if status else 'unknown'}",
                code=str(getattr(status, "value", "EXECUTION_FAILED")).upper(),
            )

        # Build a minimal ActionResult + VerificationResult so the
        # shape matches the rest of the system.
        action = _ActionResult(
            status=(
                _ActionStatus.SUCCEEDED
                if cap_status in (_CapStatus.VERIFIED, _CapStatus.EXECUTED)
                else _ActionStatus.FAILED
            ),
            action_name=capability_name,
            details={
                "execution_id": getattr(cycle_result, "execution_id", ""),
                "step_id": getattr(cycle_result, "step_id", ""),
                "cycle_status": getattr(status, "value", "unknown") if status else "unknown",
            },
        )

        return _CapResult(
            capability_name=capability_name,
            status=cap_status,
            attempted=True,
            executed=cap_status in (_CapStatus.VERIFIED, _CapStatus.EXECUTED),
            verified=cap_status is _CapStatus.VERIFIED,
            failed=cap_status in (_CapStatus.FAILED, _CapStatus.TIMED_OUT, _CapStatus.CANCELLED),
            action=action,
            error=err,
            details={
                "execution_id": getattr(cycle_result, "execution_id", ""),
                "step_id": getattr(cycle_result, "step_id", ""),
                "cycle_status": getattr(status, "value", "unknown") if status else "unknown",
            },
        )

    def _check_plan_preconditions(self, plan: Plan) -> None:
        if plan.step_count == 0:
            raise InvalidPlanError(
                "Plan has no steps",
                context={"plan_id": plan.plan_id},
            )
        seen: Set[str] = set()
        for step in plan.steps:
            if step.step_id in seen:
                raise InvalidPlanError(
                    f"Duplicate step id {step.step_id!r}",
                    context={"plan_id": plan.plan_id, "step_id": step.step_id},
                )
            seen.add(step.step_id)
        # validate dependency references
        for step in plan.steps:
            for dep in step.depends_on:
                if dep not in seen:
                    raise InvalidPlanError(
                        f"Step {step.step_id!r} depends on unknown step {dep!r}",
                        context={"plan_id": plan.plan_id, "step_id": step.step_id, "dep": dep},
                    )
                if dep == step.step_id:
                    raise InvalidPlanError(
                        f"Step {step.step_id!r} depends on itself",
                        context={"plan_id": plan.plan_id, "step_id": step.step_id},
                    )

    def _topological_order(self, plan: Plan) -> List[str]:
        """Return the steps in dependency order (parents before children).

        Steps with no dependencies come first; ties are broken by the
        plan's natural order.  Cycles (which the plan validator would
        have rejected) raise :class:`InvalidPlanError` defensively.
        """
        order: List[str] = []
        visited: Set[str] = set()
        in_progress: Set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visited:
                return
            if step_id in in_progress:
                raise InvalidPlanError(
                    f"Dependency cycle detected at step {step_id!r}",
                    context={"plan_id": plan.plan_id, "step_id": step_id},
                )
            in_progress.add(step_id)
            step = plan.find_step(step_id)
            if step is not None:
                for dep in step.depends_on:
                    visit(dep)
            in_progress.discard(step_id)
            visited.add(step_id)
            order.append(step_id)

        for step in plan.steps:
            visit(step.step_id)
        return order

    def _dependencies_failed(self, step: PlanStep, failed: Set[str]) -> bool:
        return any(dep in failed for dep in step.depends_on)

    def _descendants(self, plan: Plan, step_id: str) -> Set[str]:
        """Return the set of step_ids that transitively depend on ``step_id``."""
        dependents: Dict[str, Set[str]] = {s.step_id: set() for s in plan.steps}
        for s in plan.steps:
            for dep in s.depends_on:
                if dep in dependents:
                    dependents[dep].add(s.step_id)
        result: Set[str] = set()
        stack: List[str] = list(dependents.get(step_id, ()))
        while stack:
            cur = stack.pop()
            if cur in result:
                continue
            result.add(cur)
            stack.extend(dependents.get(cur, ()))
        return result

    def _classify_capability_result(self, cap_result: Any) -> StepState:
        """Map a :class:`core.results.CapabilityResult` to a :class:`StepState`."""
        from core.results import CapabilityStatus
        if cap_result is None:
            return StepState.FAILED
        status = getattr(cap_result, "status", None)
        if status is CapabilityStatus.VERIFIED:
            return StepState.SUCCEEDED
        if status is CapabilityStatus.EXECUTED:
            # The capability ran but the verification step did not
            # confirm; treat as failure (R-8 + AD-21 — verified is
            # the only "succeeded" signal).
            return StepState.FAILED
        if status is CapabilityStatus.FAILED:
            return StepState.FAILED
        if status is CapabilityStatus.TIMED_OUT:
            return StepState.TIMED_OUT
        if status is CapabilityStatus.CANCELLED:
            return StepState.CANCELLED
        if status is CapabilityStatus.SKIPPED:
            return StepState.SKIPPED
        if status is CapabilityStatus.ATTEMPTED:
            return StepState.FAILED
        return StepState.FAILED

    def _finalize_outcome(
        self, result: ExecutionResult, *, started_at: float
    ) -> ExecutionResult:
        completed_at = time.time()
        # Recompute the terminal outcome based on step results.
        any_failed = any(
            r.status in (StepState.FAILED, StepState.TIMED_OUT)
            for r in result.step_results
        )
        any_cancelled = any(
            r.status is StepState.CANCELLED for r in result.step_results
        )
        any_blocked = any(
            r.status is StepState.BLOCKED for r in result.step_results
        )
        any_skipped = any(
            r.status is StepState.SKIPPED for r in result.step_results
        )
        total = result.step_count
        succeeded = result.succeeded_step_count

        if any_cancelled:
            outcome = ExecutionOutcome.CANCELLED
            err = "at least one step was cancelled"
        elif any_failed and succeeded < total:
            outcome = ExecutionOutcome.FAILED
            err = "at least one step failed"
        elif any_blocked and succeeded == 0:
            outcome = ExecutionOutcome.BLOCKED
            err = "all ready steps were blocked by upstream failures"
        elif any_skipped and succeeded == 0:
            outcome = ExecutionOutcome.FAILED
            err = "all steps were skipped"
        elif any_failed or any_blocked:
            outcome = ExecutionOutcome.PARTIAL
            err = ""
        else:
            outcome = ExecutionOutcome.COMPLETED
            err = ""

        return result.with_outcome(outcome, completed_at=completed_at, error=err)

    # =========================================================== observability
    def _emit_event(self, **fields: Any) -> None:
        if self.observability_sink is None:
            return
        try:
            self.observability_sink(fields)
        except Exception as exc:  # noqa: BLE001
            # An observability sink must never break execution.
            logger.warning(
                "PlanExecutor.observability_sink raised: {err!r}",
                err=exc,
            )

    def _publish_bus(self, **kwargs: Any) -> None:
        """Best-effort publish to the canonical event bus (Phase 12).

        The executor never *requires* a bus; if publishing fails or
        the bus is missing, the executor must keep running.  All
        published events are :class:`RequestEvent` records with the
        executor's correlation_id, so consumers can correlate them
        with the pipeline.
        """
        if self.event_bus is None:
            return
        try:
            from core.events.event_types import RequestEvent, make_event
            self.event_bus.publish(
                make_event(
                    RequestEvent,
                    source="plan_executor",
                    **kwargs,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # Bus must never break execution.
            logger.debug(
                "PlanExecutor.event_bus publish failed: {err!r}",
                err=exc,
            )

    def _emit_step_finished(
        self,
        context: ExecutionContext,
        correlation_id: str,
        step_result: StepResult,
    ) -> None:
        cap = step_result.capability_result
        # Status → phase mapping for the bus.  We use the canonical
        # request-pipeline stage names so the trace is uniform end-to-end.
        try:
            from core.events.event_types import (
                REQUEST_ACTION_EXECUTED,
                REQUEST_OBSERVATION_CAPTURED,
            )
            cap_status = (
                getattr(cap, "status", None).value
                if cap is not None and getattr(cap, "status", None) is not None
                else ""
            )
            self._emit_event(
                kind="step_finished",
                plan_id=context.plan.plan_id,
                goal_id=context.goal.goal_id,
                execution_id=context.execution_id,
                correlation_id=correlation_id,
                step_id=step_result.step_id,
                capability_name=step_result.capability_name,
                status=step_result.status.value,
                capability_status=cap_status,
                duration_ms=step_result.duration_ms,
                error=step_result.error,
            )
            # Publish the canonical ACTION_EXECUTED stage on the bus.
            self._publish_bus(
                correlation_id=correlation_id,
                stage=REQUEST_ACTION_EXECUTED,
                plan_id=context.plan.plan_id,
                plan_step_count=context.plan.step_count,
                status=cap_status,
                error=(step_result.error or "")[:200],
                metadata={
                    "step_id": step_result.step_id,
                    "capability": step_result.capability_name,
                    "duration_ms": step_result.duration_ms,
                },
            )
            # If the capability produced an observation (details with
            # observation-like content), publish OBSERVATION_CAPTURED.
            details = getattr(cap, "details", None) if cap is not None else None
            if isinstance(details, dict) and details:
                self._publish_bus(
                    correlation_id=correlation_id,
                    stage=REQUEST_OBSERVATION_CAPTURED,
                    plan_id=context.plan.plan_id,
                    metadata={
                        "step_id": step_result.step_id,
                        "capability": step_result.capability_name,
                        "details_keys": sorted(details.keys()),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            # Phase 1 / D8: replace silent ``pass`` with a DEBUG
            # log entry.  The bus is *non-essential* to execution
            # (a sink failure must never break a step) but it
            # must be observable.  Production code that depends
            # on bus events can now grep for "plan_executor:
            # step_finished bus publish failed" to find broken
            # wiring.  The log is at DEBUG so the default log
            # level is unaffected.
            logger.debug(
                "plan_executor: step_finished bus publish failed: {!r}",
                exc,
            )

    def publish_recovery_started(
        self,
        *,
        correlation_id: str,
        plan_id: str,
        reason: str = "",
        attempt: int = 0,
    ) -> None:
        """Publish a ``REQUEST_RECOVERY_STARTED`` event on the bus.

        Called by the recovery engine (and by tests) when a step
        failure triggers a recovery action.  The executor itself does
        not run recovery — the orchestration layer (Agent + recovery
        engine) decides what to do — but the executor is the
        canonical place for "the plan is recovering" because that is
        where the recovery action will be re-issued.
        """
        from core.events.event_types import REQUEST_RECOVERY_STARTED
        self._publish_bus(
            correlation_id=correlation_id,
            stage=REQUEST_RECOVERY_STARTED,
            plan_id=plan_id,
            metadata={"reason": reason, "attempt": attempt},
        )

    # =========================================================== idempotency
    def _acquire_idempotency_lock(self, execution_id: str) -> None:
        with self._idempotency_lock:
            if execution_id in self._inflight:
                raise IdempotencyViolation(
                    f"Execution {execution_id!r} is already in flight",
                    context={"execution_id": execution_id},
                )
            self._inflight.add(execution_id)

    def _release_idempotency_lock(self, execution_id: str) -> None:
        with self._idempotency_lock:
            self._inflight.discard(execution_id)

    def inflight_count(self) -> int:
        with self._idempotency_lock:
            return len(self._inflight)

    # =========================================================== diagnostics
    def statistics(self) -> Dict[str, Any]:
        with self._idempotency_lock:
            return {
                "type": "PlanExecutor",
                "name": self._name,
                "default_step_timeout_s": self.default_step_timeout_s,
                "default_plan_timeout_s": self.default_plan_timeout_s,
                "inflight": len(self._inflight),
            }

    def __repr__(self) -> str:
        return (
            f"PlanExecutor(name={self._name!r}, "
            f"inflight={self.inflight_count()}, "
            f"default_step_timeout_s={self.default_step_timeout_s})"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ms_since(t0: float) -> float:
    return round((time.time() - t0) * 1000.0, 3)


def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def _extract_error_message(cap_result: Any) -> str:
    if cap_result is None:
        return ""
    err = getattr(cap_result, "error", None)
    if err is None:
        return ""
    code = getattr(err, "code", None) or ""
    msg = getattr(err, "message", None) or str(err)
    if code and code not in msg:
        return f"{code}: {msg}"
    return msg
