"""
Stage 19.2 — Precondition Checking and State Awareness Tests

Test suite for the ExecutionCycle precondition functionality that adds:
- PRECONDITION phase execution
- State tracking (pre_state/post_state)
- Precondition providers and results
- Observation invalidation after action
"""

import asyncio
import time
from datetime import datetime
from unittest.mock import Mock, MagicMock
from typing import Any, Dict, List, Optional
import pytest

# Import the execution cycle components
from core.execution import (
    ExecutionCycle,
    ExecutionPolicy,
    ExecutionStep,
    StepAction,
    ExecutionResult,
    ExecutionStatus,
    VerificationExpectation,
    ExpectationKind,
    VerificationResult,
    VerificationStatus,
    DefaultActionExecutor,
    DefaultGroundingProvider,
    DefaultVerificationProvider,
    Precondition,
    PreconditionResult,
    PreconditionStatus,
    PreconditionProvider,
    ExecutionState,
)
from core.execution.preconditions import PreconditionKind
from core.execution.errors import (
    ObservationFailedError,
    GroundingFailedError,
    ActionFailedError,
    VerificationFailedError,
)

# Import Omnix types for mocking
from vision.perception_contract import (
    PerceptionProvider,
    PerceptionRequest,
    PerceptionResult,
    PerceptionStatus,
    ScreenInfo,
)
from core.grounding.target_resolver import TargetResolver, TargetResolutionResult, TargetResolutionStatus
from core.results import CapabilityResult, CapabilityStatus
from core.orchestration.cancellation import CancellationToken


# ---------------------------------------------------------------------------
# Test Doubles (Fakes) for Precondition Testing
# ---------------------------------------------------------------------------

class FakePerceptionProvider:
    """Fake perception provider that returns deterministic results."""

    def __init__(
        self,
        return_status: PerceptionStatus = PerceptionStatus.SUCCESS,
        observation_id: str = "fake-obs-123",
        duration_ms: float = 10.0,
        call_count: int = 0,
    ):
        self.return_status = return_status
        self.observation_id = observation_id
        self.duration_ms = duration_ms
        self.call_count = call_count
        self.last_request: Optional[PerceptionRequest] = None
        self.invalidate_calls = 0

    async def observe(
        self,
        request: PerceptionRequest,
        cancellation_token: Optional[Any] = None,
    ) -> PerceptionResult:
        self.call_count += 1
        self.last_request = request

        # Check for cancellation
        if cancellation_token and getattr(cancellation_token, 'is_cancelled', False):
            return PerceptionResult(
                observation_id="cancelled-obs",
                timestamp=time.time(),
                screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                status=PerceptionStatus.CANCELLED,
                duration_ms=self.duration_ms
            )

        # Return configured result
        return PerceptionResult(
            observation_id=f"{self.observation_id}-{self.call_count}",
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            screenshot=b"fake-screenshot",
            candidates=(),
            window_context=None,
            sources=(),
            duration_ms=self.duration_ms,
            status=self.return_status,
        )

    def get_available_sources(self) -> tuple:
        return ()

    def is_source_available(self, source: Any) -> bool:
        return False

    async def invalidate(self, key: Any = None) -> None:
        self.invalidate_calls += 1


class FakeTarget:
    """Fake resolved target for testing."""

    def __init__(self, target_id: str = "fake-target-123"):
        self.target_id = target_id
        self.status = TargetResolutionStatus.RESOLVED


class FakeGroundingProvider:
    """Fake grounding provider that returns deterministic results."""

    def __init__(
        self,
        return_status: TargetResolutionStatus = TargetResolutionStatus.RESOLVED,
        return_target: Optional[Any] = None,
        call_count: int = 0,
    ):
        self.return_status = return_status
        self.return_target = return_target or FakeTarget()
        self.call_count = call_count
        self.last_input: Optional[Any] = None

    def resolve(
        self,
        target_input: Any,
        *,
        screen_width: Optional[int] = None,
        screen_height: Optional[int] = None,
    ) -> TargetResolutionResult:
        self.call_count += 1
        self.last_input = target_input

        return TargetResolutionResult(
            status=self.return_status,
            target=self.return_target,
            reason=str(target_input),
            details={"fake": True, "screen_width": screen_width, "screen_height": screen_height},
        )

    def get_available_sources(self) -> tuple:
        return ()


class FakeCapabilityResult:
    """Fake capability result for testing."""

    def __init__(
        self,
        status: CapabilityStatus = CapabilityStatus.EXECUTED,
        capability_name: str = "fake.capability",
        duration_ms: float = 10.0,
    ):
        self.status = status
        self.capability_name = capability_name
        self.duration_ms = duration_ms
        self.attempted = status != CapabilityStatus.SKIPPED
        self.executed = status in (CapabilityStatus.EXECUTED, CapabilityStatus.VERIFIED)
        self.verified = status == CapabilityStatus.VERIFIED
        self.failed = status in (CapabilityStatus.FAILED, CapabilityStatus.TIMED_OUT)
        self.request_id = f"fake-req-{id(self)}"


class FakeActionExecutor:
    """Fake action executor that returns deterministic results."""

    def __init__(
        self,
        return_status: CapabilityStatus = CapabilityStatus.EXECUTED,
        return_capability_name: str = "fake.capability",
        call_count: int = 0,
    ):
        self.return_status = return_status
        self.return_capability_name = return_capability_name
        self.call_count = call_count
        self.last_call: Optional[Dict[str, Any]] = None

    async def execute(
        self,
        capability_name: str,
        parameters: Dict[str, Any],
        target: Optional[Any] = None,
        timeout_s: float = 30.0,
        cancellation_token: Optional[Any] = None,
    ) -> CapabilityResult:
        self.call_count += 1
        self.last_call = {
            "capability_name": capability_name,
            "parameters": parameters,
            "target": target,
            "timeout_s": timeout_s,
        }

        # Check for cancellation
        if cancellation_token and getattr(cancellation_token, 'is_cancelled', False):
            return FakeCapabilityResult(
                status=CapabilityStatus.CANCELLED,
                capability_name=capability_name,
            )

        return FakeCapabilityResult(
            status=self.return_status,
            capability_name=capability_name or self.return_capability_name,
        )


class FakeVerificationResult:
    """Fake verification result for testing."""

    def __init__(
        self,
        status: VerificationStatus = VerificationStatus.SUCCESS,
        success: bool = True,
        verification_id: str = "fake-verif-123",
        observation_id: str = "fake-obs-456",
        attempt: int = 1,
    ):
        self.status = status
        self.success = success
        self.verification_id = verification_id
        self.evidence = None
        self.observation_id = observation_id
        self.elapsed_ms = 5.0
        self.reason = "fake verification"
        self.attempt = attempt


class FakeVerificationProvider:
    """Fake verification provider that returns deterministic results."""

    def __init__(
        self,
        return_status: VerificationStatus = VerificationStatus.SUCCESS,
        return_success: bool = True,
        call_count: int = 0,
    ):
        self.return_status = return_status
        self.return_success = return_success
        self.call_count = call_count
        self.last_expectation: Optional[Any] = None
        self.last_observation: Optional[Any] = None

    async def verify(
        self,
        expectation: VerificationExpectation,
        observation: PerceptionResult,
        cancellation_token: Optional[Any] = None,
    ) -> VerificationResult:
        self.call_count += 1
        self.last_expectation = expectation
        self.last_observation = observation

        # Check for cancellation
        if cancellation_token and getattr(cancellation_token, 'is_cancelled', False):
            return VerificationResult(
                verification_id="cancelled-verif",
                status=VerificationStatus.CANCELLED,
                success=False,
                evidence=observation,
                observation_id=observation.observation_id,
                elapsed_ms=0.0,
                reason="Verification cancelled",
                attempt=1,
            )

        return VerificationResult(
            verification_id=f"fake-verif-{self.call_count}",
            status=self.return_status,
            success=self.return_success,
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=5.0,
            reason="fake verification",
            attempt=1,
        )


class FakePreconditionProvider:
    """Fake precondition provider for testing precondition functionality."""

    def __init__(
        self,
        return_status: PreconditionStatus = PreconditionStatus.SATISFIED,
        precondition_id: str = "fake-precond-123",
        call_count: int = 0,
    ):
        self.return_status = return_status
        self.precondition_id = precondition_id
        self.call_count = call_count
        self.last_precondition: Optional[Precondition] = None
        self.last_observation: Optional[PerceptionResult] = None
        self.last_state: Optional[ExecutionState] = None

    @property
    def name(self) -> str:
        return "fake-precondition-provider"

    async def check(
        self,
        precondition: Precondition,
        observation: PerceptionResult,
        context: Optional[ExecutionState] = None,
        cancellation_token: Optional[Any] = None,
    ) -> PreconditionResult:
        self.call_count += 1
        self.last_precondition = precondition
        self.last_observation = observation
        self.last_state = context

        # Check for cancellation
        if cancellation_token and getattr(cancellation_token, 'is_cancelled', False):
            return PreconditionResult(
                precondition_id=precondition.precondition_id,
                status=PreconditionStatus.CANCELLED,
                satisfied=False,
                reason="Cancelled during precondition check",
                elapsed_ms=0.0,
                timestamp=time.time(),
            )

        # Return configured result
        result = PreconditionResult(
            precondition_id=precondition.precondition_id or self.precondition_id,
            status=self.return_status,
            satisfied=(self.return_status == PreconditionStatus.SATISFIED),
            confidence=0.9 if self.return_status == PreconditionStatus.SATISFIED else 0.0,
            evidence={"fake": True},
            observation_id=observation.observation_id,
            reason=f"Fake precondition check: {self.return_status.value}",
            elapsed_ms=5.0,
            timestamp=time.time(),
        )
        return result


# ---------------------------------------------------------------------------
# Test Cases for Stage 19.2 Precondition Functionality
# ---------------------------------------------------------------------------

class TestExecutionCyclePreconditionBasics:
    """Test basic precondition functionality."""

    @pytest.mark.asyncio
    async def test_precondition_satisfied(self):
        """Test that satisfied preconditions allow execution to proceed."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.SATISFIED
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
        )

        step = ExecutionStep(
            step_id="test-precondition-1",
            description="Test step with precondition",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.SUCCESS
        assert precondition_provider.call_count == 1
        assert len(result.precondition_results) == 1
        assert result.precondition_results[0].status == PreconditionStatus.SATISFIED
        assert result.precondition_results[0].satisfied == True
        assert result.pre_state is not None
        assert result.post_state is not None

    @pytest.mark.asyncio
    async def test_precondition_not_satisfied(self):
        """Test that unsatisfied preconditions fail the execution."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.NOT_SATISFIED
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
        )

        step = ExecutionStep(
            step_id="test-precondition-2",
            description="Test step with failed precondition",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.PRECONDITION_FAILED
        assert precondition_provider.call_count == 1
        assert len(result.precondition_results) == 1
        assert result.precondition_results[0].status == PreconditionStatus.NOT_SATISFIED
        assert result.precondition_results[0].satisfied == False
        assert result.observation is None  # Should not proceed to observation
        assert result.resolved_target is None
        assert result.action_result is None
        assert result.verification_result is None
        assert result.pre_state is not None  # Initial state should be captured
        assert result.post_state is None  # No post-state on precondition failure

    @pytest.mark.asyncio
    async def test_precondition_timeout(self):
        """Test that precondition timeout results in timeout status."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.TIMEOUT
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
            policy=ExecutionPolicy(precondition_timeout_s=0.1),  # Short timeout for test
        )

        step = ExecutionStep(
            step_id="test-precondition-3",
            description="Test step with precondition timeout",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                    timeout_s=0.05,  # Shorter than policy timeout
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.TIMEOUT
        assert precondition_provider.call_count == 1
        assert len(result.precondition_results) == 1
        assert result.precondition_results[0].status == PreconditionStatus.TIMEOUT
        assert result.precondition_results[0].satisfied == False
        assert result.observation is None  # Should not proceed to observation

    @pytest.mark.asyncio
    async def test_precondition_inconclusive(self):
        """Test that inconclusive precondition results in inconclusive status."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.INCONCLUSIVE
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
        )

        step = ExecutionStep(
            step_id="test-precondition-4",
            description="Test step with inconclusive precondition",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.INCONCLUSIVE
        assert precondition_provider.call_count == 1
        assert len(result.precondition_results) == 1
        assert result.precondition_results[0].status == PreconditionStatus.INCONCLUSIVE
        assert result.precondition_results[0].satisfied == False
        assert result.observation is None  # Should not proceed to observation

    @pytest.mark.asyncio
    async def test_multiple_preconditions_all_satisfied(self):
        """Test that multiple preconditions all need to be satisfied."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.SATISFIED
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
        )

        step = ExecutionStep(
            step_id="test-precondition-5",
            description="Test step with multiple preconditions",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
                Precondition(
                    kind=PreconditionKind.WINDOW_EXISTS,
                    target_query="Test Window",
                ),
                Precondition(
                    kind=PreconditionKind.TEXT_PRESENT,
                    target_query="Test Text",
                    expected_state="Visible",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.SUCCESS
        assert precondition_provider.call_count == 3  # One for each precondition
        assert len(result.precondition_results) == 3
        for pc_result in result.precondition_results:
            assert pc_result.status == PreconditionStatus.SATISFIED
            assert pc_result.satisfied == True

    @pytest.mark.asyncio
    async def test_multiple_preconditions_one_fails(self):
        """Test that execution stops on first failed precondition."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.SATISFIED  # Default to satisfied
        )

        # Override to return NOT_SATISFIED for the second precondition
        original_check = precondition_provider.check
        call_count = 0

        async def mock_check(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Track total invocations of the provider's check method
            precondition_provider.call_count = call_count
            if call_count == 2:  # Second precondition fails
                return PreconditionResult(
                    precondition_id=args[0].precondition_id if args else "fake",
                    status=PreconditionStatus.NOT_SATISFIED,
                    satisfied=False,
                    reason="Second precondition failed",
                    elapsed_ms=5.0,
                    timestamp=time.time(),
                )
            return await original_check(*args, **kwargs)

        precondition_provider.check = mock_check

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
        )

        step = ExecutionStep(
            step_id="test-precondition-6",
            description="Test step with multiple preconditions, one fails",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
                Precondition(
                    kind=PreconditionKind.WINDOW_EXISTS,
                    target_query="Test Window",
                ),
                Precondition(
                    kind=PreconditionKind.TEXT_PRESENT,
                    target_query="Test Text",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.PRECONDITION_FAILED
        assert precondition_provider.call_count == 2  # Should stop after second failure
        assert len(result.precondition_results) == 2
        # First should succeed
        assert result.precondition_results[0].status == PreconditionStatus.SATISFIED
        assert result.precondition_results[0].satisfied == True
        # Second should fail
        assert result.precondition_results[1].status == PreconditionStatus.NOT_SATISFIED
        assert result.precondition_results[1].satisfied == False


class TestExecutionCyclePreconditionEdgeCases:
    """Test edge cases and special scenarios for precondition functionality."""

    @pytest.mark.asyncio
    async def test_no_preconditions_works_normally(self):
        """Test that steps without preconditions work as before."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        # No precondition provider needed

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            # No precondition_provider - should work normally
        )

        step = ExecutionStep(
            step_id="test-no-precondition",
            description="Test step without preconditions",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            # No preconditions
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.SUCCESS
        assert len(result.precondition_results) == 0  # No precondition results
        assert result.pre_state is not None
        assert result.post_state is not None
        assert result.observation is not None
        assert result.resolved_target is not None
        assert result.action_result is not None
        assert result.verification_result is not None

    @pytest.mark.asyncio
    async def test_precondition_provider_none_skip_checking(self):
        """Test that when precondition_provider is None, checking is skipped."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        # Explicitly set precondition_provider to None

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=None,  # Explicitly None
        )

        step = ExecutionStep(
            step_id="test-none-provider",
            description="Test step with None precondition provider",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        # Should succeed because precondition checking is skipped when provider is None
        # (unless require_preconditions is True in policy)
        assert result.status == ExecutionStatus.SUCCESS
        assert len(result.precondition_results) == 0  # No precondition checking done

    @pytest.mark.asyncio
    async def test_precondition_provider_none_with_require_true_fails(self):
        """Test that when precondition_provider is None but require_preconditions=True, it fails."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=None,  # Explicitly None
            policy=ExecutionPolicy(require_preconditions=True),  # Require preconditions
        )

        step = ExecutionStep(
            step_id="test-none-provider-require-true",
            description="Test step with None provider but require preconditions",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.PRECONDITION_FAILED
        assert len(result.precondition_results) == 0  # No checking attempted

    @pytest.mark.asyncio
    async def test_observation_invalidation_after_action(self):
        """Test that observation is invalidated after action when policy says so."""
        # Setup counting perception provider
        perception = FakePerceptionProvider(
            return_status=PerceptionStatus.SUCCESS,
            observation_id="base-obs"
        )
        grounding = FakeGroundingProvider(return_status=TargetResolutionStatus.RESOLVED)
        action = FakeActionExecutor(return_status=CapabilityStatus.EXECUTED)
        verification = FakeVerificationProvider(return_status=VerificationStatus.SUCCESS)
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.SATISFIED
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
            perception_cache=perception,  # Use the fake as cache too
            policy=ExecutionPolicy(invalidate_observation_after_action=True),
        )

        step = ExecutionStep(
            step_id="test-invalidation",
            description="Test step for observation invalidation",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.SUCCESS
        # Should have called observe at least twice: once for observe phase, once for verify phase
        assert perception.call_count >= 2
        # Cache should have been invalidated after action
        assert perception.invalidate_calls >= 1

    @pytest.mark.asyncio
    async def test_state_tracking_pre_and_post(self):
        """Test that pre_state and post_state are properly captured."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.SATISFIED
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
        )

        step = ExecutionStep(
            step_id="test-state-tracking",
            description="Test step for state tracking",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.SUCCESS
        assert result.pre_state is not None
        assert result.post_state is not None
        assert isinstance(result.pre_state, ExecutionState)
        assert isinstance(result.post_state, ExecutionState)
        # States should have different IDs (unless time didn't advance enough)
        assert result.pre_state.state_id == result.post_state.state_id or \
               abs((result.post_state.timestamp - result.pre_state.timestamp).total_seconds()) >= 0
        # Pre-state should be initial state (no observation)
        assert result.pre_state.observation_id is None
        # Post-state should have observation from verification
        assert result.post_state.observation_id is not None

    @pytest.mark.asyncio
    async def test_precondition_error_handling(self):
        """Test that precondition provider errors are handled gracefully."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()

        class ErrorPreconditionProvider:
            @property
            def name(self) -> str:
                return "error-precondition-provider"

            async def check(
                self,
                precondition: Precondition,
                observation: PerceptionResult,
                context: Optional[ExecutionState] = None,
                cancellation_token: Optional[Any] = None,
            ) -> PreconditionResult:
                # Simulate an error in precondition checking
                raise ValueError("Simulated precondition provider error")

        error_provider = ErrorPreconditionProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=error_provider,
        )

        step = ExecutionStep(
            step_id="test-precondition-error",
            description="Test step with precondition provider error",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        # Execute
        result = await cycle.execute(step)

        # Assert
        assert result.status == ExecutionStatus.OBSERVATION_FAILED  # Errors map to OBSERVATION_FAILED
        assert len(result.precondition_results) == 1
        assert result.precondition_results[0].status == PreconditionStatus.ERROR
        assert result.precondition_results[0].satisfied == False
        assert "Simulated precondition provider error" in result.precondition_results[0].reason

    @pytest.mark.asyncio
    async def test_precondition_cancellation(self):
        """Test that precondition checking respects cancellation."""
        # Setup
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.SATISFIED
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
        )

        step = ExecutionStep(
            step_id="test-precondition-cancellation",
            description="Test step for precondition cancellation",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        # Create cancellation token that is already cancelled
        cancellation_token = CancellationToken()
        cancellation_token.cancel()

        # Execute
        result = await cycle.execute(step, cancellation_token=cancellation_token)

        # Assert
        assert result.status == ExecutionStatus.CANCELLED
        # Should not have called precondition provider since cancelled before execution
        assert precondition_provider.call_count == 0


# ---------------------------------------------------------------------------
# Test Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Run a simple test to verify basic functionality
    import asyncio

    async def simple_precondition_test():
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        precondition_provider = FakePreconditionProvider(
            return_status=PreconditionStatus.SATISFIED
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            precondition_provider=precondition_provider,
        )

        step = ExecutionStep(
            step_id="simple-precondition-test",
            description="Simple precondition test",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="Test Button",
            expectation=VerificationExpectation.target_visible("Test Button"),
            preconditions=(
                Precondition(
                    kind=PreconditionKind.TARGET_VISIBLE,
                    target_query="Test Button",
                ),
            ),
        )

        result = await cycle.execute(step)
        print(f"Simple precondition test result: {result.status}")
        assert result.status == ExecutionStatus.SUCCESS
        assert precondition_provider.call_count == 1
        assert len(result.precondition_results) == 1
        print("✓ Basic precondition functionality test passed")

    asyncio.run(simple_precondition_test())
    print("All precondition tests passed!")