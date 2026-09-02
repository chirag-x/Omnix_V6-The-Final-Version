"""
Omnix V6 — Execution Cycle for Stage 19.3.

Implements the ExecutionCycle class that orchestrates the
PRECONDITION → OBSERVE → GROUND → ACT → SYNCHRONIZE → VERIFY cycle as a
deterministic, reusable primitive with strict phase ordering, timeouts,
cancellation, observation invalidation, state tracking, and bounded
state-settling synchronization.

Stage 19.3 adds:

  * A new SYNCHRONIZE phase between ACT and VERIFY, driven by a
    pluggable :class:`SynchronizationProvider`.
  * ExecutionPolicy fields for synchronization timeout, poll interval,
    and policy enable/disable.
  * Cache invalidation of the pre-action observation id so stale
    observations cannot be mistaken for fresh settlement evidence.
  * Structured SynchronizationResult on ExecutionResult for diagnostics.
  * Observability events for SYNCHRONIZATION_* transitions.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Optional
from dataclasses import dataclass, field
from uuid import uuid4

from ..orchestration.cancellation import CancellationToken
from vision.perception_contract import (
    PerceptionProvider,
    PerceptionRequest,
    PerceptionResult,
    PerceptionStatus,
)
from core.grounding.target_resolver import TargetResolver, TargetResolutionResult, TargetResolutionStatus
from core.results import CapabilityResult, CapabilityStatus
from .step import ExecutionStep, StepAction
from .expectation import VerificationExpectation, ExpectationKind
from .result import (
    ExecutionResult,
    ExecutionStatus,
    ExecutionTrace,
    VerificationResult,
    VerificationStatus,
)
from .state import ExecutionState
from .preconditions import (
    Precondition,
    PreconditionResult,
    PreconditionStatus,
    PreconditionProvider,
)
from .provider import (
    VerificationProvider,
    ActionExecutor,
    GroundingProvider,
)
from .sync import (
    SynchronizationProvider,
    SynchronizationResult,
    SynchronizationStatus,
    SynchronizationContext,
)
from .errors import (
    ExecutionError,
    ObservationFailedError,
    GroundingFailedError,
    ActionFailedError,
    VerificationFailedError,
)


@dataclass
class ExecutionPolicy:
    """Configuration for execution phase timeouts and behavior."""
    observation_timeout_s: float = 5.0
    grounding_timeout_s: float = 2.0
    action_timeout_s: float = 30.0
    verification_timeout_s: float = 5.0
    verification_max_attempts: int = 3
    verification_poll_interval_s: float = 0.25
    invalidate_observation_after_action: bool = True
    # Stage 19.2 extensions
    precondition_timeout_s: float = 5.0
    require_preconditions: bool = False  # When True, missing preconditions fail the step
    minimum_confidence: float = 0.5      # Used for state transition validation
    # Stage 19.3 extensions — synchronization / state-settling
    enable_synchronization: bool = True
    synchronization_timeout_s: float = 5.0
    synchronization_poll_interval_s: float = 0.05
    require_settlement: bool = True     # When True, non-SETTLED results fail the step


class ExecutionCycle:
    """
    Orchestrates the OBSERVE → GROUND → ACT → VERIFY cycle as a deterministic primitive.

    The cycle executes exactly one iteration of:
        ExecutionStep → OBSERVE → GROUND → ACT → VERIFY → ExecutionResult

    It does NOT:
        - Make LLM calls (0 LLM calls during execution)
        - Implement application-specific executors
        - Use hard-coded UI coordinates
        - Report blind success (success requires verification to pass)
        - Retry failed phases automatically
        - Implement recovery strategies
    """

    def __init__(
        self,
        *,
        perception_provider: PerceptionProvider,
        target_resolver: TargetResolver,
        action_executor: ActionExecutor,
        verification_provider: VerificationProvider,
        perception_cache: Optional[Any] = None,  # CachedPerceptionProvider or similar
        precondition_provider: Optional[PreconditionProvider] = None,  # Stage 19.2
        synchronization_provider: Optional[SynchronizationProvider] = None,  # Stage 19.3
        policy: Optional[ExecutionPolicy] = None,
        observability_sink: Optional[Callable[[str, dict], None]] = None,
    ) -> None:
        self._perception_provider = perception_provider
        self._target_resolver = target_resolver
        self._action_executor = action_executor
        self._verification_provider = verification_provider
        self._perception_cache = perception_cache
        self._precondition_provider = precondition_provider
        self._synchronization_provider = synchronization_provider
        self._policy = policy or ExecutionPolicy()
        self._observability_sink = observability_sink

    async def execute(
        self,
        step: ExecutionStep,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> ExecutionResult:
        """
        Execute one PRECONDITION → OBSERVE → GROUND → ACT → VERIFY cycle.

        Args:
            step: The execution step to perform
            cancellation_token: Optional token for cancelling the operation

        Returns:
            ExecutionResult with the outcome of the cycle
        """
        execution_id = str(uuid4())
        start_time = time.time()

        # Initialize execution-local state
        # initial_pre_state is the true pre-execution state (before any observation).
        # This is what the ExecutionResult exposes as pre_state.
        initial_pre_state = ExecutionState.initial_state()
        # execution_state tracks state during execution phases and gets updated with observations
        execution_state = initial_pre_state

        # Emit execution started event
        self._emit_observability(
            "EXECUTION_STARTED",
            {
                "execution_id": execution_id,
                "step_id": step.step_id,
                "description": step.description,
                "action": step.action.value if hasattr(step.action, 'value') else str(step.action),
            },
        )

        # Pre-flight cancellation check
        if self._is_cancelled(cancellation_token):
            return self._create_cancelled_result(
                execution_id, step.step_id, start_time, "Cancelled before execution"
            )

        # Phase 0: PRECONDITION (optional)
        precondition_results: List[PreconditionResult] = []
        if step.preconditions and (self._precondition_provider is not None or self._policy.require_preconditions):
            precondition_results, precondition_status = await self._check_preconditions(
                step, execution_state, cancellation_token, start_time
            )
            if precondition_status != ExecutionStatus.SUCCESS:
                # Precondition check failed - return structured failure
                completed_time = time.time()
                result = ExecutionResult(
                    execution_id=execution_id,
                    step_id=step.step_id,
                    status=precondition_status,
                    observation=None,
                    started_at=start_time,
                    completed_at=completed_time,
                    duration_ms=(completed_time - start_time) * 1000,
                    error=self._format_precondition_error(precondition_results),
                    trace=ExecutionTrace(pre_state=initial_pre_state),
                    pre_state=initial_pre_state,
                    post_state=None,
                    precondition_results=tuple(precondition_results),
                    metadata={
                        "precondition_results": [pr.to_dict() if hasattr(pr, 'to_dict') else str(pr) for pr in precondition_results],
                    },
                )
                self._emit_observability(
                    "PRECONDITION_FAILED",
                    {
                        "execution_id": execution_id,
                        "step_id": step.step_id,
                        "precondition_results": len(precondition_results),
                    },
                )
                return result

        # Phase 1: OBSERVE
        observe_result = await self._observe(step, cancellation_token, start_time)
        if observe_result.status != ExecutionStatus.SUCCESS:
            return observe_result

        # Update execution state with observation
        execution_state = ExecutionState.from_observation(
            observe_result.observation,
            resolved_targets=[observe_result.resolved_target] if observe_result.resolved_target else [],
        )

        # Phase 2: GROUND
        ground_result = await self._ground(step, observe_result, cancellation_token, start_time)
        if ground_result.status != ExecutionStatus.SUCCESS:
            return ground_result

        # Phase 3: ACT
        act_result = await self._act(step, ground_result, cancellation_token, start_time, observe_result)
        if act_result.status != ExecutionStatus.SUCCESS:
            return act_result

        # Phase 3.5: SYNCHRONIZE (Stage 19.3 — wait for environment to settle)
        sync_result = await self._synchronize(
            step,
            observe_result,
            act_result,
            execution_state,
            cancellation_token,
            start_time,
        )
        if sync_result is not None and self._policy.require_settlement:
            # If sync ran and produced a non-SETTLED outcome, fail the step
            if sync_result.status != SynchronizationStatus.SETTLED:
                completed_time = time.time()
                result = ExecutionResult(
                    execution_id=execution_id,
                    step_id=step.step_id,
                    status=self._map_synchronization_status(sync_result.status),
                    observation=observe_result.observation,
                    resolved_target=ground_result.resolved_target,
                    action_result=act_result.action_result,
                    synchronization_result=sync_result,
                    pre_state=initial_pre_state,
                    precondition_results=tuple(precondition_results),
                    trace=ExecutionTrace(
                        observation_id=getattr(observe_result.observation, 'observation_id', None),
                        observation_timestamp=getattr(observe_result.observation, 'timestamp', None).timestamp() if hasattr(getattr(observe_result.observation, 'timestamp', None), 'timestamp') else None,
                        action_id=getattr(act_result.action_result, 'capability_name', None) if act_result.action_result else None,
                        action_started_at=act_result.started_at,
                        action_completed_at=act_result.completed_at,
                        pre_state=initial_pre_state,
                        synchronization_status=sync_result.status.value,
                        synchronization_observation_id=sync_result.observation_id,
                        synchronization_elapsed_ms=sync_result.elapsed_ms,
                        synchronization_poll_count=sync_result.poll_count,
                    ),
                    started_at=start_time,
                    completed_at=completed_time,
                    duration_ms=(completed_time - start_time) * 1000,
                    error=sync_result.reason or f"Synchronization {sync_result.status.value}",
                    metadata={
                        "synchronization_status": sync_result.status.value,
                        "synchronization_poll_count": sync_result.poll_count,
                    },
                )
                self._emit_observability(
                    "SYNCHRONIZATION_FAILED",
                    {
                        "execution_id": execution_id,
                        "step_id": step.step_id,
                        "status": sync_result.status.value,
                        "reason": sync_result.reason,
                        "poll_count": sync_result.poll_count,
                    },
                )
                return result

        # Phase 4: VERIFY (always uses fresh observation per cache invalidation)
        verify_result = await self._verify(step, observe_result, act_result, cancellation_token, start_time, ground_result)
        if verify_result.status != ExecutionStatus.SUCCESS:
            return verify_result

        # All phases succeeded - build post-state
        post_state = ExecutionState.from_observation(
            verify_result.observation if hasattr(verify_result, 'observation') and verify_result.observation else observe_result.observation,
            resolved_targets=[verify_result.resolved_target] if hasattr(verify_result, 'resolved_target') and verify_result.resolved_target else [ground_result.resolved_target] if ground_result.resolved_target else [],
        )

        # All phases succeeded
        completed_time = time.time()
        result = ExecutionResult(
            execution_id=execution_id,
            step_id=step.step_id,
            status=ExecutionStatus.SUCCESS,
            observation=observe_result.observation,
            resolved_target=ground_result.resolved_target,
            action_result=act_result.action_result,
            verification_result=verify_result.verification_result,
            synchronization_result=sync_result,
            pre_state=initial_pre_state,
            post_state=post_state,
            precondition_results=tuple(precondition_results),
            trace=ExecutionTrace(
                observation_id=getattr(observe_result.observation, 'observation_id', None),
                observation_timestamp=getattr(observe_result.observation, 'timestamp', None).timestamp() if hasattr(getattr(observe_result.observation, 'timestamp', None), 'timestamp') else None,
                action_id=getattr(act_result.action_result, 'capability_name', None) if act_result.action_result else None,
                action_started_at=act_result.started_at,
                action_completed_at=act_result.completed_at,
                verification_id=getattr(verify_result.verification_result, 'verification_id', None),
                verification_attempts=verify_result.verification_result.attempt if hasattr(verify_result.verification_result, 'attempt') else 1,
                pre_state=initial_pre_state,
                post_state=post_state,
                synchronization_status=(sync_result.status.value if sync_result else None),
                synchronization_observation_id=(sync_result.observation_id if sync_result else None),
                synchronization_elapsed_ms=(sync_result.elapsed_ms if sync_result else 0.0),
                synchronization_poll_count=(sync_result.poll_count if sync_result else 0),
            ),
            started_at=start_time,
            completed_at=completed_time,
            duration_ms=(completed_time - start_time) * 1000,
            metadata={
                "observation_status": observe_result.observation.status.value if hasattr(observe_result.observation, 'status') else None,
                "grounding_status": ground_result.resolved_target.status.value if hasattr(ground_result.resolved_target, 'status') else None,
                "action_status": act_result.action_result.status.value if hasattr(act_result.action_result, 'status') else None,
                "verification_status": verify_result.verification_result.status.value if hasattr(verify_result.verification_result, 'status') else None,
                "synchronization_status": (sync_result.status.value if sync_result else None),
                "synchronization_poll_count": (sync_result.poll_count if sync_result else 0),
            },
        )

        self._emit_observability(
            "EXECUTION_COMPLETED",
            {
                "execution_id": execution_id,
                "step_id": step.step_id,
                "status": ExecutionStatus.SUCCESS.value,
                "duration_ms": result.duration_ms,
            },
        )

        return result

    async def _check_preconditions(
        self,
        step: ExecutionStep,
        pre_state: ExecutionState,
        cancellation_token: Optional[CancellationToken],
        start_time: float,
    ) -> tuple[List[PreconditionResult], ExecutionStatus]:
        """
        Check all preconditions for a step.

        Returns:
            (precondition_results, status) - status is SUCCESS if all preconditions
            are satisfied, otherwise the appropriate failure status
        """
        results: List[PreconditionResult] = []

        self._emit_observability(
            "PRECONDITION_STARTED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
                "precondition_count": len(step.preconditions),
            },
        )

        # If no precondition provider is configured but preconditions exist, we can't check them
        if self._precondition_provider is None:
            if self._policy.require_preconditions:
                # Policy requires preconditions but no provider - return failed
                return [], ExecutionStatus.PRECONDITION_FAILED
            # Otherwise, skip precondition checking
            return [], ExecutionStatus.SUCCESS

        for precondition in step.preconditions:
            if self._is_cancelled(cancellation_token):
                # Create a cancelled result
                cancelled_result = PreconditionResult(
                    precondition_id=precondition.precondition_id,
                    status=PreconditionStatus.CANCELLED,
                    satisfied=False,
                    reason="Cancelled during precondition check",
                    elapsed_ms=(time.time() - start_time) * 1000,
                )
                results.append(cancelled_result)
                return results, ExecutionStatus.CANCELLED

            try:
                # Build a synthetic observation for the precondition check
                # In a full implementation, this would use cached or fresh observation
                observation = self._build_precondition_observation(step)

                # Check the precondition with timeout
                pc_result = await asyncio.wait_for(
                    self._precondition_provider.check(
                        precondition,
                        observation,
                        pre_state,
                        cancellation_token,
                    ),
                    timeout=self._policy.precondition_timeout_s,
                )
            except asyncio.TimeoutError:
                pc_result = PreconditionResult(
                    precondition_id=precondition.precondition_id,
                    status=PreconditionStatus.TIMEOUT,
                    satisfied=False,
                    reason="Precondition check timed out",
                    elapsed_ms=self._policy.precondition_timeout_s * 1000,
                )
            except Exception as e:
                pc_result = PreconditionResult(
                    precondition_id=precondition.precondition_id,
                    status=PreconditionStatus.ERROR,
                    satisfied=False,
                    reason=f"Precondition check error: {str(e)}",
                    elapsed_ms=(time.time() - start_time) * 1000,
                )

            results.append(pc_result)

            # Stop on first failure for safety
            if pc_result.status != PreconditionStatus.SATISFIED:
                if pc_result.status == PreconditionStatus.INCONCLUSIVE:
                    return results, ExecutionStatus.INCONCLUSIVE
                elif pc_result.status == PreconditionStatus.TIMEOUT:
                    return results, ExecutionStatus.TIMEOUT
                elif pc_result.status == PreconditionStatus.CANCELLED:
                    return results, ExecutionStatus.CANCELLED
                elif pc_result.status == PreconditionStatus.ERROR:
                    return results, ExecutionStatus.OBSERVATION_FAILED
                else:
                    return results, ExecutionStatus.PRECONDITION_FAILED

        self._emit_observability(
            "PRECONDITION_COMPLETED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
                "all_satisfied": all(r.satisfied for r in results),
            },
        )

        return results, ExecutionStatus.SUCCESS

    def _format_precondition_error(self, results: List[PreconditionResult]) -> str:
        """Format precondition results into a human-readable error message."""
        if not results:
            return "Preconditions required but not satisfied"
        failures = [r for r in results if not r.satisfied]
        if not failures:
            return "Precondition check failed"
        reasons = [f"{r.precondition_id}: {r.reason}" for r in failures]
        return f"Precondition(s) not satisfied: {'; '.join(reasons)}"

    def _build_precondition_observation(self, step: ExecutionStep) -> PerceptionResult:
        """
        Build a synthetic observation for precondition checking.
        In a full implementation, this would use cached perception data
        or request a minimal observation from the provider.
        """
        from vision.perception_contract import ScreenInfo
        from datetime import datetime
        return PerceptionResult(
            observation_id="precondition-obs",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            status=PerceptionStatus.SUCCESS,
        )

    async def _observe(
        self,
        step: ExecutionStep,
        cancellation_token: Optional[CancellationToken],
        start_time: float,
    ) -> ExecutionResult:
        """Perform the OBSERVE phase."""
        self._emit_observability(
            "OBSERVATION_STARTED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
            },
        )

        # Build perception request from step
        request = self._build_perception_request(step)

        # Execute observation with timeout and cancellation
        try:
            perception_result = await asyncio.wait_for(
                self._perception_provider.observe(request, cancellation_token),
                timeout=self._policy.observation_timeout_s,
            )
        except asyncio.TimeoutError:
            perception_result = PerceptionResult(
                observation_id="",
                timestamp=time.time(),
                screen=self._get_fake_screen_info(),
                status=PerceptionStatus.TIMEOUT,
                duration_ms=self._policy.observation_timeout_s * 1000,
            )
        except Exception as e:
            perception_result = PerceptionResult(
                observation_id="",
                timestamp=time.time(),
                screen=self._get_fake_screen_info(),
                status=PerceptionStatus.FAILED,
                duration_ms=0.0,
                metadata={"error": str(e)},
            )

        # Check if cancelled during observation
        if self._is_cancelled(cancellation_token):
            return self._create_cancelled_result(
                str(uuid4()), step.step_id, start_time, "Cancelled during observation"
            )

        # Determine execution status from perception result
        if perception_status_is_success(perception_result.status) or perception_result.status == PerceptionStatus.PARTIAL:
            # PARTIAL is acceptable for observation phase as long as we have candidates to work with
            status = ExecutionStatus.SUCCESS
        elif perception_result.status == PerceptionStatus.TIMEOUT:
            status = ExecutionStatus.TIMEOUT
        elif perception_result.status == PerceptionStatus.CANCELLED:
            status = ExecutionStatus.CANCELLED
        else:
            status = ExecutionStatus.OBSERVATION_FAILED

        self._emit_observability(
            "OBSERVATION_COMPLETED" if status == ExecutionStatus.SUCCESS else "OBSERVATION_FAILED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
                "status": status.value,
                "perception_status": perception_result.status.value,
            },
        )

        return ExecutionResult(
            execution_id=str(uuid4()),
            step_id=step.step_id,
            status=status,
            observation=perception_result,
            started_at=start_time,
            completed_at=time.time(),
            duration_ms=(time.time() - start_time) * 1000,
            trace=ExecutionTrace(
                observation_id=getattr(perception_result, 'observation_id', None),
                observation_timestamp=getattr(perception_result, 'timestamp', None).timestamp() if hasattr(getattr(perception_result, 'timestamp', None), 'timestamp') else None,
            ),
        )

    async def _ground(
        self,
        step: ExecutionStep,
        observe_result: ExecutionResult,
        cancellation_token: Optional[CancellationToken],
        start_time: float,
    ) -> ExecutionResult:
        """Perform the GROUND phase."""
        self._emit_observability(
            "GROUNDING_STARTED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
            },
        )

        # If no target query, grounding succeeds trivially
        if not step.target_query and not step.target_hint:
            resolved_target = TargetResolutionResult(
                status=TargetResolutionStatus.RESOLVED,
                target=None,  # No target needed
                reason="No target specified",
                details={"reason": "No target specified"},
            )
        else:
            # Determine what to ground
            target_input = step.target_hint if step.target_hint is not None else step.target_query

            # Check if target is a non-UI target (e.g., app_name, text) that doesn't require grounding
            if step.target_kind in ("app_name", "text", "path", "url"):
                resolved_target = TargetResolutionResult(
                    status=TargetResolutionStatus.RESOLVED,
                    target=None,
                    reason=f"Target kind '{step.target_kind}' does not require grounding",
                    details={"reason": f"Target kind '{step.target_kind}' does not require grounding"},
                )
            else:
                # Execute grounding
                try:
                    resolved_target = self._target_resolver.resolve(
                        target_input,
                        screen_width=getattr(observe_result.observation.screen, 'width', None) if hasattr(observe_result.observation, 'screen') else None,
                        screen_height=getattr(observe_result.observation.screen, 'height', None) if hasattr(observe_result.observation, 'screen') else None,
                    )
                except Exception as e:
                    resolved_target = TargetResolutionResult(
                        status=TargetResolutionStatus.UNSUPPORTED,
                        target=None,
                        reason=str(target_input),
                        details={"error": str(e)},
                    )

        # Check if cancelled during grounding
        if self._is_cancelled(cancellation_token):
            return self._create_cancelled_result(
                str(uuid4()), step.step_id, start_time, "Cancelled during grounding"
            )

        # Determine execution status from grounding result
        if resolved_target.status == TargetResolutionStatus.RESOLVED:
            status = ExecutionStatus.SUCCESS
        elif resolved_target.status in (TargetResolutionStatus.NOT_FOUND, TargetResolutionStatus.UNSUPPORTED):
            status = ExecutionStatus.GROUNDING_FAILED
        elif resolved_target.status == TargetResolutionStatus.TIMEOUT:
            status = ExecutionStatus.TIMEOUT
        else:
            status = ExecutionStatus.GROUNDING_FAILED  # AMBIGUOUS, LOW_CONFIDENCE, OUT_OF_BOUNDS, WINDOW_MISMATCH, etc.

        self._emit_observability(
            "GROUNDING_COMPLETED" if status == ExecutionStatus.SUCCESS else "GROUNDING_FAILED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
                "status": status.value,
                "grounding_status": resolved_target.status.value,
            },
        )

        return ExecutionResult(
            execution_id=str(uuid4()),
            step_id=step.step_id,
            status=status,
            observation=observe_result.observation,
            resolved_target=resolved_target.target,
            started_at=start_time,
            completed_at=time.time(),
            duration_ms=(time.time() - start_time) * 1000,
            trace=ExecutionTrace(
                observation_id=observe_result.trace.observation_id,
                observation_timestamp=observe_result.trace.observation_timestamp,
            ),
        )

    async def _act(
        self,
        step: ExecutionStep,
        ground_result: ExecutionResult,
        cancellation_token: Optional[CancellationToken],
        start_time: float,
        observe_result: ExecutionResult = None,
    ) -> ExecutionResult:
        """Perform the ACT phase."""
        self._emit_observability(
            "ACTION_STARTED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
            },
        )

        # Build capability request from step
        capability_name = step.capability_name or self._infer_capability_name(step.action)
        parameters = dict(step.parameters)
        target = ground_result.resolved_target

        # Execute action with timeout and cancellation
        try:
            capability_result = await asyncio.wait_for(
                self._action_executor.execute(
                    capability_name,
                    parameters,
                    target,
                    timeout_s=step.timeout_s or self._policy.action_timeout_s,
                    cancellation_token=cancellation_token,
                ),
                timeout=step.timeout_s or self._policy.action_timeout_s,
            )
        except asyncio.TimeoutError:
            capability_result = CapabilityResult(
                capability_name=capability_name,
                status=CapabilityStatus.TIMED_OUT,
                attempted=True,
                executed=False,
                verified=False,
                failed=False,
                duration_ms=(step.timeout_s or self._policy.action_timeout_s) * 1000,
                details={"error": "Action timeout"},
            )
        except Exception as e:
            capability_result = CapabilityResult(
                capability_name=capability_name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                executed=False,
                verified=False,
                failed=True,
                duration_ms=0.0,
                details={"error": str(e)},
                error=str(e),
            )

        # Check if cancelled during action
        if self._is_cancelled(cancellation_token):
            return self._create_cancelled_result(
                str(uuid4()), step.step_id, start_time, "Cancelled during action"
            )

        # Determine execution status from capability result
        if capability_result.status == CapabilityStatus.EXECUTED:
            status = ExecutionStatus.SUCCESS
        elif capability_result.status == CapabilityStatus.FAILED:
            status = ExecutionStatus.ACTION_FAILED
        elif capability_result.status == CapabilityStatus.TIMED_OUT:
            status = ExecutionStatus.TIMEOUT
        elif capability_result.status == CapabilityStatus.CANCELLED:
            status = ExecutionStatus.CANCELLED
        else:
            # ATTEMPTED, VERIFIED, SKIPPED - treat as failure for the cycle
            status = ExecutionStatus.ACTION_FAILED

        self._emit_observability(
            "ACTION_COMPLETED" if status == ExecutionStatus.SUCCESS else "ACTION_FAILED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
                "status": status.value,
                "capability_status": capability_result.status.value,
            },
        )

        # Invalidate observation after action if policy says so
        if self._policy.invalidate_observation_after_action and self._perception_cache is not None:
            try:
                # Invalidate all cache entries (simple approach)
                if hasattr(self._perception_cache, 'invalidate'):
                    # Try async invalidate first
                    invalidate_method = getattr(self._perception_cache, 'invalidate')
                    if asyncio.iscoroutinefunction(invalidate_method):
                        await invalidate_method(key=None)  # Invalidate all
                    else:
                        invalidate_method(key=None)  # Sync version
            except Exception:
                # Ignore cache invalidation errors - don't fail the cycle for cache issues
                pass

        return ExecutionResult(
            execution_id=str(uuid4()),
            step_id=step.step_id,
            status=status,
            observation=observe_result.observation if observe_result else None,
            resolved_target=ground_result.resolved_target,
            action_result=capability_result,
            started_at=start_time,
            completed_at=time.time(),
            duration_ms=(time.time() - start_time) * 1000,
            trace=ExecutionTrace(
                observation_id=observe_result.trace.observation_id if observe_result else None,
                observation_timestamp=observe_result.trace.observation_timestamp if observe_result else None,
                action_id=getattr(capability_result, 'request_id', None),
                action_started_at=start_time,  # Simplified
                action_completed_at=time.time(),
            ),
        )

    async def _synchronize(
        self,
        step: ExecutionStep,
        observe_result: ExecutionResult,
        act_result: ExecutionResult,
        execution_state: Optional[ExecutionState],
        cancellation_token: Optional[CancellationToken],
        start_time: float,
    ) -> Optional[SynchronizationResult]:
        """Perform the synchronization phase (Stage 19.3).

        Returns None if synchronization is disabled by policy, otherwise
        returns a SynchronizationResult. The caller must interpret
        the result according to the policy.require_settlement flag.
        """
        if not self._policy.enable_synchronization:
            return None

        if self._synchronization_provider is None:
            # No provider configured - skip synchronization
            return None

        # Build the synchronization context
        expectation = getattr(step, 'expectation', None)
        if expectation is None:
            expectation = VerificationExpectation.none()

        context = SynchronizationContext(
            step_id=step.step_id,
            before_observation_id=getattr(observe_result.observation, 'observation_id', None) if observe_result else None,
            expectation=expectation,
            pre_state=execution_state,
            metadata={
                "action_result_status": getattr(act_result.action_result, 'status', None).value if act_result.action_result else None,
            }
        )

        # Emit observability event for sync start
        self._emit_observability(
            "SYNCHRONIZATION_STARTED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
                "expectation_kind": expectation.kind.value if expectation else "NONE",
                "has_pre_state": execution_state is not None,
            }
        )

        # Perform the synchronization
        try:
            sync_result = await asyncio.wait_for(
                self._synchronization_provider.wait_until_settled(
                    context,
                    timeout_s=self._policy.synchronization_timeout_s,
                    poll_interval_s=self._policy.synchronization_poll_interval_s,
                    cancellation_token=cancellation_token,
                ),
                timeout=self._policy.synchronization_timeout_s + 1.0,  # Small buffer
            )
        except asyncio.TimeoutError:
            # This shouldn't happen due to internal timeouts, but guard anyway
            sync_result = SynchronizationResult(
                status=SynchronizationStatus.TIMEOUT,
                settled=False,
                elapsed_ms=(time.time() - start_time) * 1000,
                reason="Synchronization provider timeout (external)",
                poll_count=0,
                metadata={"context_step_id": step.step_id},
            )

        # Emit observability event for sync completion
        self._emit_observability(
            "SYNCHRONIZATION_COMPLETED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
                "status": sync_result.status.value,
                "settled": sync_result.settled,
                "poll_count": sync_result.poll_count,
                "elapsed_ms": sync_result.elapsed_ms,
            }
        )

        return sync_result

    def _map_synchronization_status(self, status: SynchronizationStatus) -> ExecutionStatus:
        """Map a SynchronizationStatus to an ExecutionStatus for the cycle result."""
        mapping = {
            SynchronizationStatus.SETTLED: ExecutionStatus.SUCCESS,  # This shouldn't happen in practice when called
            SynchronizationStatus.TIMEOUT: ExecutionStatus.TIMEOUT,
            SynchronizationStatus.CANCELLED: ExecutionStatus.CANCELLED,
            SynchronizationStatus.INCONCLUSIVE: ExecutionStatus.INCONCLUSIVE,
            SynchronizationStatus.ERROR: ExecutionStatus.SYNCHRONIZATION_FAILED,
        }
        return mapping.get(status, ExecutionStatus.SYNCHRONIZATION_FAILED)

    async def _verify(
        self,
        step: ExecutionStep,
        observe_result: ExecutionResult,
        act_result: ExecutionResult,
        cancellation_token: Optional[CancellationToken],
        start_time: float,
        ground_result: ExecutionResult = None,
    ) -> ExecutionResult:
        """Perform the VERIFY phase."""
        self._emit_observability(
            "VERIFICATION_STARTED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
            },
        )

        # If expectation is NONE, verification succeeds trivially
        if step.expectation.kind == ExpectationKind.NONE:
            verification_result = VerificationResult(
                verification_id="",
                status=VerificationStatus.SUCCESS,
                success=True,
                confidence=1.0,
                evidence=observe_result.observation,  # Use pre-action observation as evidence
                observation_id=getattr(observe_result.observation, 'observation_id', None),
                elapsed_ms=0.0,
                reason="No verification requested",
                attempt=1,
            )
        else:
            # Perform verification with retries for INCONCLUSIVE results
            verification_result = None
            for attempt in range(1, self._policy.verification_max_attempts + 1):
                # Get fresh observation for verification
                try:
                    fresh_perception = await asyncio.wait_for(
                        self._perception_provider.observe(
                            self._build_perception_request(step),
                            cancellation_token,
                        ),
                        timeout=self._policy.verification_timeout_s,
                    )
                except asyncio.TimeoutError:
                    fresh_perception = PerceptionResult(
                        observation_id="",
                        timestamp=time.time(),
                        screen=self._get_fake_screen_info(),
                        status=PerceptionStatus.TIMEOUT,
                        duration_ms=self._policy.verification_timeout_s * 1000,
                    )
                except Exception as e:
                    fresh_perception = PerceptionResult(
                        observation_id="",
                        timestamp=time.time(),
                        screen=self._get_fake_screen_info(),
                        status=PerceptionStatus.FAILED,
                        duration_ms=0.0,
                        metadata={"error": str(e)},
                    )

                # Check if cancelled during verification attempt
                if self._is_cancelled(cancellation_token):
                    return self._create_cancelled_result(
                        str(uuid4()), step.step_id, start_time, f"Cancelled during verification attempt {attempt}"
                    )

                # Perform verification
                try:
                    verification_result = await asyncio.wait_for(
                        self._verification_provider.verify(
                            step.expectation,
                            fresh_perception,
                            cancellation_token,
                        ),
                        timeout=self._policy.verification_timeout_s,
                    )
                except asyncio.TimeoutError:
                    verification_result = VerificationResult(
                        verification_id="",
                        status=VerificationStatus.TIMEOUT,
                        success=False,
                        evidence=fresh_perception,
                        observation_id=getattr(fresh_perception, 'observation_id', None),
                        elapsed_ms=self._policy.verification_timeout_s * 1000,
                        reason="Verification timeout",
                        attempt=attempt,
                    )
                except Exception as e:
                    verification_result = VerificationResult(
                        verification_id="",
                        status=VerificationStatus.FAILED,
                        success=False,
                        evidence=fresh_perception,
                        observation_id=getattr(fresh_perception, 'observation_id', None),
                        elapsed_ms=0.0,
                        reason=f"Verification error: {str(e)}",
                        attempt=attempt,
                    )

                # Check if we should retry
                if verification_result.status == VerificationStatus.INCONCLUSIVE and attempt < self._policy.verification_max_attempts:
                    # Wait before retrying
                    await asyncio.sleep(self._policy.verification_poll_interval_s)
                    continue
                else:
                    # Either succeeded, failed, timeout, cancelled, or max attempts reached
                    break

            # If we never got a result (shouldn't happen), create a failed one
            if verification_result is None:
                verification_result = VerificationResult(
                    verification_id="",
                    status=VerificationStatus.FAILED,
                    success=False,
                    evidence=observe_result.observation,
                    observation_id=getattr(observe_result.observation, 'observation_id', None),
                    elapsed_ms=(time.time() - start_time) * 1000,
                    reason="Verification did not produce a result",
                    attempt=1,
                )

        # Determine execution status from verification result
        if verification_result.status == VerificationStatus.SUCCESS:
            status = ExecutionStatus.SUCCESS
        elif verification_result.status == VerificationStatus.FAILED:
            status = ExecutionStatus.VERIFICATION_FAILED
        elif verification_result.status == VerificationStatus.TIMEOUT:
            status = ExecutionStatus.TIMEOUT
        elif verification_result.status == VerificationStatus.CANCELLED:
            status = ExecutionStatus.CANCELLED
        else:
            # INCONCLUSIVE - treat as failure for the cycle
            status = ExecutionStatus.VERIFICATION_FAILED

        self._emit_observability(
            "VERIFICATION_COMPLETED" if status == ExecutionStatus.SUCCESS else "VERIFICATION_FAILED",
            {
                "execution_id": str(uuid4()),
                "step_id": step.step_id,
                "status": status.value,
                "verification_status": verification_result.status.value,
                "attempts": verification_result.attempt if hasattr(verification_result, 'attempt') else 1,
            },
        )

        return ExecutionResult(
            execution_id=str(uuid4()),
            step_id=step.step_id,
            status=status,
            observation=observe_result.observation,
            resolved_target=ground_result.resolved_target if ground_result else None,
            action_result=act_result.action_result,
            verification_result=verification_result,
            started_at=start_time,
            completed_at=time.time(),
            duration_ms=(time.time() - start_time) * 1000,
            trace=ExecutionTrace(
                observation_id=observe_result.trace.observation_id,
                observation_timestamp=observe_result.trace.observation_timestamp,
                action_id=act_result.trace.action_id,
                action_started_at=act_result.trace.action_started_at,
                action_completed_at=act_result.trace.action_completed_at,
                verification_id=getattr(verification_result, 'verification_id', None),
                verification_attempts=verification_result.attempt if hasattr(verification_result, 'attempt') else 1,
            ),
        )

    def _is_cancelled(self, cancellation_token: Optional[CancellationToken]) -> bool:
        """Check if cancellation has been requested."""
        return cancellation_token is not None and getattr(cancellation_token, 'is_cancelled', False)

    def _create_cancelled_result(
        self,
        execution_id: str,
        step_id: str,
        start_time: float,
        reason: str,
    ) -> ExecutionResult:
        """Create a cancelled execution result."""
        return ExecutionResult(
            execution_id=execution_id,
            step_id=step_id,
            status=ExecutionStatus.CANCELLED,
            started_at=start_time,
            completed_at=time.time(),
            duration_ms=(time.time() - start_time) * 1000,
            error=reason,
            trace=ExecutionTrace(),
        )

    def _build_perception_request(self, step: ExecutionStep) -> PerceptionRequest:
        """Build a PerceptionRequest from an ExecutionStep."""
        # For now, use default request - could be enhanced based on step properties
        return PerceptionRequest(
            include_screenshot=True,
            include_vision=True,
            include_ocr=False,  # OCR is expensive, enable only when needed
            include_ui_elements=False,  # UI elements are expensive, enable only when needed
            include_window_context=True,
            region=None,
            max_age_ms=None,  # Let the provider/cache handle freshness
        )

    def _get_fake_screen_info(self):
        """Get fake screen info for error cases."""
        from vision.perception_contract import ScreenInfo
        return ScreenInfo(
            width=1920,
            height=1080,
            dpi_scale_x=1.0,
            dpi_scale_y=1.0,
            monitor_id="fake-monitor",
            coordinate_space="screen",
        )

    def _infer_capability_name(self, action: StepAction) -> str:
        """Infer capability name from StepAction."""
        action_map = {
            StepAction.CLICK: "desktop.mouse.click",
            StepAction.DOUBLE_CLICK: "desktop.mouse.double_click",
            StepAction.RIGHT_CLICK: "desktop.mouse.right_click",
            StepAction.TYPE_TEXT: "desktop.keyboard.type",
            StepAction.PRESS_KEY: "desktop.keyboard.press",
            StepAction.HOTKEY: "desktop.keyboard.hotkey",
            StepAction.SCROLL: "desktop.mouse.scroll",
            StepAction.MOVE: "desktop.mouse.move",
            StepAction.DRAG: "desktop.mouse.drag",
            StepAction.OPEN_APPLICATION: "desktop.application.open",
            StepAction.FOCUS_WINDOW: "desktop.window.focus",
            StepAction.WAIT: "desktop.wait",
            StepAction.SCREENSHOT: "desktop.screenshot",
        }
        return action_map.get(action, "unknown.action")

    def _emit_observability(self, event_name: str, data: dict[str, Any]) -> None:
        """Emit an observability event if sink is configured."""
        if self._observability_sink is not None:
            try:
                self._observability_sink(event_name, data)
            except Exception:
                # Don't let observability errors break the cycle
                pass


def perception_status_is_success(status: PerceptionStatus) -> bool:
    """Check if a perception status indicates success."""
    return status == PerceptionStatus.SUCCESS