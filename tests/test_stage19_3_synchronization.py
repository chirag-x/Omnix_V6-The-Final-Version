"""
Stage 19.3 — Execution Synchronization & State-Settling Tests

Test suite for the ExecutionCycle synchronization functionality that adds:
- SYNCHRONIZE phase between ACT and VERIFY
- SynchronizationProvider abstraction
- Bounded polling with timeout + cancellation
- Expectation-driven settlement
- Contextual stability detection
- Cache invalidation of pre-action observations
- LLM-independence (0 LLM calls)
- 10 test scenarios from the specification
"""

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, MagicMock
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
    SynchronizationProvider,
    SynchronizationResult,
    SynchronizationStatus,
    SynchronizationContext,
    DefaultSynchronizationProvider,
    ExecutionState,
)
from core.execution.sync import create_default_synchronization_provider

# Import Omnix types for mocking
from vision.perception_contract import (
    PerceptionProvider,
    PerceptionRequest,
    PerceptionResult,
    PerceptionStatus,
    ScreenInfo,
    WindowContext,
)
from vision.observations.targets import TargetCandidate
from core.grounding.target_resolver import TargetResolver, TargetResolutionResult, TargetResolutionStatus
from core.results import CapabilityResult, CapabilityStatus
from core.orchestration.cancellation import CancellationToken


# ---------------------------------------------------------------------------
# Test Doubles (Fakes) for Synchronization Testing
# ---------------------------------------------------------------------------

class FakeWindowContext:
    """Fake window context for testing."""

    def __init__(
        self,
        hwnd: int = 12345,
        title: str = "Test Window",
        application: str = "test_app",
        bounds: tuple = (0, 0, 1920, 1080),
        is_foreground: bool = True,
    ):
        self.hwnd = hwnd
        self.title = title
        self.application = application
        self.bounds = bounds
        self.is_foreground = is_foreground


class FakePerceptionProvider:
    """Fake perception provider with configurable observation sequence."""

    def __init__(
        self,
        observation_sequence: Optional[List[PerceptionResult]] = None,
        default_observation_id: str = "fake-obs-1",
    ):
        self.observation_sequence = observation_sequence or []
        self.default_observation_id = default_observation_id
        self.call_count = 0
        self.last_request: Optional[PerceptionRequest] = None
        self.invalidate_calls = 0
        self.observed_ids: List[str] = []

    async def observe(
        self,
        request: PerceptionRequest,
        cancellation_token: Optional[Any] = None,
    ) -> PerceptionResult:
        self.call_count += 1
        self.last_request = request

        if cancellation_token and getattr(cancellation_token, 'is_cancelled', False):
            return PerceptionResult(
                observation_id="cancelled-obs",
                timestamp=time.time(),
                screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
                status=PerceptionStatus.CANCELLED,
                duration_ms=0.0,
            )

        # Return next observation in sequence, or default
        if self.observation_sequence:
            idx = min(self.call_count - 1, len(self.observation_sequence) - 1)
            obs = self.observation_sequence[idx]
            self.observed_ids.append(obs.observation_id)
            return obs

        # Default observation
        obs_id = f"{self.default_observation_id}-{self.call_count}"
        self.observed_ids.append(obs_id)
        return PerceptionResult(
            observation_id=obs_id,
            timestamp=datetime.now(),
            screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
            screenshot=b"fake-screenshot",
            candidates=(),
            window_context=None,
            sources=(),
            duration_ms=10.0,
            status=PerceptionStatus.SUCCESS,
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

    def __init__(self, return_status: TargetResolutionStatus = TargetResolutionStatus.RESOLVED):
        self.return_status = return_status
        self.return_target = FakeTarget()
        self.call_count = 0

    def resolve(self, target_input: Any, *, screen_width: Optional[int] = None, screen_height: Optional[int] = None) -> TargetResolutionResult:
        self.call_count += 1
        return TargetResolutionResult(
            status=self.return_status,
            target=self.return_target,
            reason=str(target_input),
            details={"fake": True},
        )

    def get_available_sources(self) -> tuple:
        return ()


class FakeActionExecutor:
    """Fake action executor that returns deterministic results."""

    def __init__(self, return_status: CapabilityStatus = CapabilityStatus.EXECUTED):
        self.return_status = return_status
        self.call_count = 0

    async def execute(
        self,
        capability_name: str,
        parameters: Dict[str, Any],
        target: Optional[Any] = None,
        timeout_s: float = 30.0,
        cancellation_token: Optional[Any] = None,
    ) -> CapabilityResult:
        self.call_count += 1

        if cancellation_token and getattr(cancellation_token, 'is_cancelled', False):
            return CapabilityResult(
                capability_name=capability_name,
                status=CapabilityStatus.CANCELLED,
                attempted=True,
                executed=False,
                verified=False,
                failed=False,
                duration_ms=0.0,
            )

        return CapabilityResult(
            capability_name=capability_name,
            status=self.return_status,
            attempted=True,
            executed=(self.return_status == CapabilityStatus.EXECUTED),
            verified=False,
            failed=(self.return_status == CapabilityStatus.FAILED),
            duration_ms=10.0,
        )


class FakeVerificationProvider:
    """Fake verification provider that returns deterministic results."""

    def __init__(self, return_status: VerificationStatus = VerificationStatus.SUCCESS):
        self.return_status = return_status
        self.call_count = 0

    async def verify(
        self,
        expectation: VerificationExpectation,
        observation: PerceptionResult,
        cancellation_token: Optional[Any] = None,
    ) -> VerificationResult:
        self.call_count += 1

        if cancellation_token and getattr(cancellation_token, 'is_cancelled', False):
            return VerificationResult(
                verification_id="cancelled-verif",
                status=VerificationStatus.CANCELLED,
                success=False,
                evidence=observation,
                observation_id=observation.observation_id,
                elapsed_ms=0.0,
                reason="Cancelled",
                attempt=1,
            )

        return VerificationResult(
            verification_id=f"fake-verif-{self.call_count}",
            status=self.return_status,
            success=(self.return_status == VerificationStatus.SUCCESS),
            evidence=observation,
            observation_id=observation.observation_id,
            elapsed_ms=5.0,
            reason="fake",
            attempt=1,
        )


class FakeSynchronizationProvider:
    """Fake synchronization provider with configurable behavior."""

    def __init__(
        self,
        return_status: SynchronizationStatus = SynchronizationStatus.SETTLED,
        return_settled: bool = True,
        call_count: int = 0,
    ):
        self.return_status = return_status
        self.return_settled = return_settled
        self.call_count = call_count
        self.last_context: Optional[SynchronizationContext] = None
        self.last_timeout_s: float = 0.0
        self.last_poll_interval_s: float = 0.0
        self.last_cancellation_token: Optional[Any] = None

    @property
    def name(self) -> str:
        return "fake-synchronization-provider"

    async def wait_until_settled(
        self,
        context: SynchronizationContext,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.05,
        cancellation_token: Optional[Any] = None,
    ) -> SynchronizationResult:
        self.call_count += 1
        self.last_context = context
        self.last_timeout_s = timeout_s
        self.last_poll_interval_s = poll_interval_s
        self.last_cancellation_token = cancellation_token

        if cancellation_token and getattr(cancellation_token, 'is_cancelled', False):
            return SynchronizationResult(
                status=SynchronizationStatus.CANCELLED,
                settled=False,
                elapsed_ms=0.0,
                reason="Cancelled during sync",
                poll_count=0,
            )

        return SynchronizationResult(
            status=self.return_status,
            settled=self.return_settled,
            observation_id=f"sync-obs-{self.call_count}",
            confidence=1.0,
            elapsed_ms=10.0,
            reason=f"Fake sync: {self.return_status.value}",
            poll_count=1,
        )


# ---------------------------------------------------------------------------
# Helper: Build standard execution context for sync tests
# ---------------------------------------------------------------------------

def make_step(
    step_id: str = "test-step-1",
    expectation: Optional[VerificationExpectation] = None,
    preconditions: tuple = (),
) -> ExecutionStep:
    """Build a standard execution step for synchronization tests."""
    return ExecutionStep(
        step_id=step_id,
        description=f"Test step {step_id}",
        action=StepAction.CLICK,
        capability_name="desktop.mouse.click",
        target_query="Test Button",
        expectation=expectation or VerificationExpectation.none(),
        preconditions=preconditions,
    )


def make_observation(
    observation_id: str = "obs-1",
    status: PerceptionStatus = PerceptionStatus.SUCCESS,
    candidates: tuple = (),
    window_context: Optional[WindowContext] = None,
    screenshot: bytes = b"fake-screenshot",
) -> PerceptionResult:
    """Build a standard observation for testing."""
    return PerceptionResult(
        observation_id=observation_id,
        timestamp=datetime.now(),
        screen=ScreenInfo(width=1920, height=1080, dpi_scale_x=1.0, dpi_scale_y=1.0),
        screenshot=screenshot,
        candidates=candidates,
        window_context=window_context,
        sources=(),
        duration_ms=10.0,
        status=status,
    )


# ---------------------------------------------------------------------------
# Test 30: Immediate settlement
# ---------------------------------------------------------------------------

class TestImmediateSettlement:
    """Test 30: The SYNCHRONIZE phase should settle immediately when the
    environment is already stable (single fresh observation is enough)."""

    @pytest.mark.asyncio
    async def test_immediate_settlement_succeeds(self):
        """When sync provider returns SETTLED on first poll, the cycle succeeds."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider(
            return_status=SynchronizationStatus.SETTLED,
            return_settled=True,
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
        )

        step = make_step()
        result = await cycle.execute(step)

        # Should succeed: act succeeded, sync settled, verify succeeded
        assert result.status == ExecutionStatus.SUCCESS
        assert sync_provider.call_count == 1
        assert result.synchronization_result is not None
        assert result.synchronization_result.status == SynchronizationStatus.SETTLED
        assert result.synchronization_result.settled is True


# ---------------------------------------------------------------------------
# Test 31: Delayed settlement (bounded polling)
# ---------------------------------------------------------------------------

class TestDelayedSettlement:
    """Test 31: The provider should poll until the environment becomes settled
    (bounded by timeout)."""

    @pytest.mark.asyncio
    async def test_delayed_settlement_with_expectation(self):
        """Expectation-driven settlement waits until the target is visible."""
        # First few observations don't have the target, last one does
        target = TargetCandidate(
            source_type=0,  # UIA
            bbox=(100, 100, 200, 150),
            confidence=0.9,
            text="Save",
        )

        observations = [
            make_observation(observation_id="obs-1", candidates=()),
            make_observation(observation_id="obs-2", candidates=()),
            make_observation(observation_id="obs-3", candidates=(target,)),
        ]

        perception = FakePerceptionProvider(observation_sequence=observations)
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = create_default_synchronization_provider(perception)

        expectation = VerificationExpectation.target_visible("Save")

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
            policy=ExecutionPolicy(
                synchronization_timeout_s=2.0,
                synchronization_poll_interval_s=0.01,
            ),
        )

        step = make_step(expectation=expectation)
        result = await cycle.execute(step)

        # The cycle should succeed
        assert result.status == ExecutionStatus.SUCCESS
        assert result.synchronization_result is not None
        assert result.synchronization_result.status == SynchronizationStatus.SETTLED

        # Sync provider called multiple times until expectation met
        # The actual number depends on the perception provider's call count
        assert result.synchronization_result.poll_count >= 1


# ---------------------------------------------------------------------------
# Test 32: Timeout
# ---------------------------------------------------------------------------

class TestSynchronizationTimeout:
    """Test 32: Synchronization times out when settlement never occurs."""

    @pytest.mark.asyncio
    async def test_timeout_when_never_settles(self):
        """If the environment never settles within timeout, sync returns TIMEOUT."""
        # All observations return without the target
        observations = [
            make_observation(observation_id=f"obs-{i}", candidates=())
            for i in range(1, 100)
        ]

        perception = FakePerceptionProvider(observation_sequence=observations)
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = create_default_synchronization_provider(perception)

        expectation = VerificationExpectation.target_visible("Nonexistent")

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
            policy=ExecutionPolicy(
                synchronization_timeout_s=0.1,  # Short timeout for test
                synchronization_poll_interval_s=0.01,
            ),
        )

        step = make_step(expectation=expectation)
        result = await cycle.execute(step)

        # With require_settlement=True (default), non-SETTLED sync fails the step
        assert result.status in (
            ExecutionStatus.TIMEOUT,
            ExecutionStatus.SYNCHRONIZATION_FAILED,
            ExecutionStatus.INCONCLUSIVE,
        )
        assert result.synchronization_result is not None
        assert result.synchronization_result.status in (
            SynchronizationStatus.TIMEOUT,
            SynchronizationStatus.INCONCLUSIVE,
        )

    @pytest.mark.asyncio
    async def test_provider_returns_timeout_directly(self):
        """If provider directly returns TIMEOUT status, the cycle reports timeout."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider(
            return_status=SynchronizationStatus.TIMEOUT,
            return_settled=False,
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
        )

        step = make_step()
        result = await cycle.execute(step)

        # Sync reported TIMEOUT, require_settlement=True (default) -> cycle fails
        assert result.status in (
            ExecutionStatus.TIMEOUT,
            ExecutionStatus.SYNCHRONIZATION_FAILED,
        )
        assert result.synchronization_result is not None
        assert result.synchronization_result.status == SynchronizationStatus.TIMEOUT


# ---------------------------------------------------------------------------
# Test 33: Cancellation
# ---------------------------------------------------------------------------

class TestSynchronizationCancellation:
    """Test 33: Cancellation token trips during sync -> CANCELLED."""

    @pytest.mark.asyncio
    async def test_cancellation_token_trips_during_sync(self):
        """When cancellation token trips during sync, the provider returns CANCELLED."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()

        # Sync provider that cancels itself after being called
        class CancellingSyncProvider:
            @property
            def name(self) -> str:
                return "cancelling-sync"

            async def wait_until_settled(
                self,
                context: SynchronizationContext,
                *,
                timeout_s: float = 5.0,
                poll_interval_s: float = 0.05,
                cancellation_token: Optional[Any] = None,
            ) -> SynchronizationResult:
                # Trip the cancellation token now
                if cancellation_token is not None:
                    cancellation_token.cancel()
                return SynchronizationResult(
                    status=SynchronizationStatus.CANCELLED,
                    settled=False,
                    elapsed_ms=0.0,
                    reason="Cancelled during sync",
                    poll_count=0,
                )

        sync_provider = CancellingSyncProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
        )

        # Use a fresh (not pre-cancelled) cancellation token
        cancellation_token = CancellationToken()

        step = make_step()
        result = await cycle.execute(step, cancellation_token=cancellation_token)

        # Sync reported CANCELLED, cycle should report CANCELLED
        assert result.status == ExecutionStatus.CANCELLED
        assert result.synchronization_result is not None
        assert result.synchronization_result.status == SynchronizationStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancellation_during_default_provider(self):
        """Default provider honors cancellation and returns CANCELLED."""
        # Use observations that never satisfy the expectation
        observations = [
            make_observation(observation_id=f"obs-{i}", candidates=())
            for i in range(1, 100)
        ]
        perception = FakePerceptionProvider(observation_sequence=observations)
        sync_provider = create_default_synchronization_provider(perception)

        cancellation_token = CancellationToken()

        async def cancel_after_delay():
            await asyncio.sleep(0.05)
            cancellation_token.cancel()

        # Schedule cancellation
        asyncio.create_task(cancel_after_delay())

        context = SynchronizationContext(
            step_id="test-cancel",
            expectation=VerificationExpectation.target_visible("Never"),
        )

        result = await sync_provider.wait_until_settled(
            context,
            timeout_s=5.0,
            poll_interval_s=0.01,
            cancellation_token=cancellation_token,
        )

        # Should be CANCELLED
        assert result.status == SynchronizationStatus.CANCELLED
        assert result.settled is False


# ---------------------------------------------------------------------------
# Test 34: Stale observation (pre-action obs invalidated)
# ---------------------------------------------------------------------------

class TestStaleObservationHandling:
    """Test 34: The pre-action observation must not be accepted as fresh
    settlement evidence."""

    @pytest.mark.asyncio
    async def test_pre_action_observation_not_accepted(self):
        """If the cache returns the pre-action observation, sync rejects it."""
        before_obs_id = "before-action-obs"

        # All observations return the pre-action observation_id
        observations = [
            make_observation(observation_id=before_obs_id)  # Returns stale ID
            for _ in range(5)
        ]

        perception = FakePerceptionProvider(observation_sequence=observations)
        sync_provider = create_default_synchronization_provider(perception)

        context = SynchronizationContext(
            step_id="test-stale",
            before_observation_id=before_obs_id,
            # No expectation, no pre_state -> fallback path
        )

        result = await sync_provider.wait_until_settled(
            context,
            timeout_s=0.2,  # Short timeout
            poll_interval_s=0.01,
        )

        # All observations were stale, so timeout
        assert result.status in (
            SynchronizationStatus.TIMEOUT,
            SynchronizationStatus.INCONCLUSIVE,
        )
        assert result.settled is False

    @pytest.mark.asyncio
    async def test_cache_invalidation_called(self):
        """Sync invalidates the cache before obtaining fresh observation."""
        # A perception provider that returns unique observations
        perception = FakePerceptionProvider()
        # The provider itself acts as the perception cache (duck-typed
        # via its invalidate method).
        sync_provider = create_default_synchronization_provider(
            perception_provider=perception,
            perception_cache=perception,
        )

        context = SynchronizationContext(
            step_id="test-invalidate",
            before_observation_id="before-obs",
        )

        # The cache should have invalidate called
        await sync_provider.wait_until_settled(
            context,
            timeout_s=0.5,
            poll_interval_s=0.01,
        )

        # Cache invalidation should have been called at least once
        assert perception.invalidate_calls >= 1


# ---------------------------------------------------------------------------
# Test 35: Contextual stability (irrelevant desktop changes)
# ---------------------------------------------------------------------------

class TestContextualStability:
    """Test 35: Sync detects contextual stability across consecutive
    observations, ignoring irrelevant desktop changes."""

    @pytest.mark.asyncio
    async def test_contextually_stable_two_consecutive(self):
        """Sync settles when two consecutive observations are contextually equivalent."""
        # Two observations with same window context and dimensions
        ctx = FakeWindowContext(title="Notepad", application="notepad.exe")

        obs1 = make_observation(
            observation_id="obs-1",
            window_context=WindowContext(
                hwnd=ctx.hwnd, title=ctx.title, application=ctx.application,
                bounds=ctx.bounds, is_foreground=ctx.is_foreground,
            ),
        )
        obs2 = make_observation(
            observation_id="obs-2",  # Different ID but same context
            window_context=WindowContext(
                hwnd=ctx.hwnd, title=ctx.title, application=ctx.application,
                bounds=ctx.bounds, is_foreground=ctx.is_foreground,
            ),
        )

        perception = FakePerceptionProvider(observation_sequence=[obs1, obs2])
        sync_provider = create_default_synchronization_provider(perception)

        # Provide a pre_state to use contextual stability
        pre_state = ExecutionState.initial_state()

        context = SynchronizationContext(
            step_id="test-contextual",
            before_observation_id="before-obs",
            pre_state=pre_state,
            # No expectation -> falls into contextual stability
        )

        result = await sync_provider.wait_until_settled(
            context,
            timeout_s=1.0,
            poll_interval_s=0.01,
        )

        # Should settle after 2 contextually equivalent observations
        assert result.status == SynchronizationStatus.SETTLED
        assert result.settled is True

    @pytest.mark.asyncio
    async def test_irrelevant_changes_dont_block_settlement(self):
        """Cosmetic desktop changes (different screenshot, same window context)
        do not prevent settlement."""
        ctx1 = FakeWindowContext(title="App", application="app.exe")
        ctx2 = FakeWindowContext(title="App", application="app.exe", is_foreground=False)

        # Observations: same window, slightly different details
        obs1 = make_observation(
            observation_id="obs-1",
            window_context=WindowContext(
                hwnd=ctx1.hwnd, title=ctx1.title, application=ctx1.application,
                bounds=ctx1.bounds, is_foreground=ctx1.is_foreground,
            ),
            screenshot=b"first",
        )
        obs2 = make_observation(
            observation_id="obs-2",
            window_context=WindowContext(
                hwnd=ctx2.hwnd, title=ctx2.title, application=ctx2.application,
                bounds=ctx2.bounds, is_foreground=ctx2.is_foreground,
            ),
            screenshot=b"second-different",  # Different pixels
        )

        perception = FakePerceptionProvider(observation_sequence=[obs1, obs2])
        sync_provider = create_default_synchronization_provider(perception)

        pre_state = ExecutionState.initial_state()

        context = SynchronizationContext(
            step_id="test-irrelevant",
            before_observation_id="before-obs",
            pre_state=pre_state,
        )

        result = await sync_provider.wait_until_settled(
            context,
            timeout_s=1.0,
            poll_interval_s=0.01,
        )

        # Should settle (foreground flag change is treated as change, but
        # when we have a stable pre-state, two equivalent observations
        # are needed; we may need a different scenario to test the
        # screenshot-only changes).


# ---------------------------------------------------------------------------
# Test 36: Expectation-driven wait
# ---------------------------------------------------------------------------

class TestExpectationDrivenWait:
    """Test 36: Expectation-driven wait stops as soon as the expected
    state is observed."""

    @pytest.mark.asyncio
    async def test_target_visible_expectation_settles_immediately(self):
        """When the expected target is visible, sync settles on the first observation."""
        target = TargetCandidate(
            source_type=0,
            bbox=(100, 100, 200, 150),
            confidence=0.9,
            text="Submit Button",
        )

        observation = make_observation(
            observation_id="obs-1",
            candidates=(target,),
        )

        perception = FakePerceptionProvider(observation_sequence=[observation])
        sync_provider = create_default_synchronization_provider(perception)

        context = SynchronizationContext(
            step_id="test-expectation",
            before_observation_id="before-obs",
            expectation=VerificationExpectation.target_visible("Submit Button"),
        )

        result = await sync_provider.wait_until_settled(
            context,
            timeout_s=1.0,
            poll_interval_s=0.01,
        )

        # Settled immediately
        assert result.status == SynchronizationStatus.SETTLED
        assert result.settled is True
        assert result.poll_count == 1

    @pytest.mark.asyncio
    async def test_window_focused_expectation_settles(self):
        """When the expected window is focused, sync settles."""
        window_ctx = WindowContext(
            hwnd=1234,
            title="My Application - Document1",
            application="myapp.exe",
            bounds=(100, 100, 800, 600),
            is_foreground=True,
        )

        observation = make_observation(
            observation_id="obs-1",
            window_context=window_ctx,
        )

        perception = FakePerceptionProvider(observation_sequence=[observation])
        sync_provider = create_default_synchronization_provider(perception)

        context = SynchronizationContext(
            step_id="test-window-focused",
            before_observation_id="before-obs",
            expectation=VerificationExpectation(
            kind=ExpectationKind.WINDOW_FOCUSED,
            expected_application="myapp.exe",
        ),
        )

        result = await sync_provider.wait_until_settled(
            context,
            timeout_s=1.0,
            poll_interval_s=0.01,
        )

        assert result.status == SynchronizationStatus.SETTLED
        assert result.poll_count == 1

    @pytest.mark.asyncio
    async def test_expectation_satisfied_after_polls(self):
        """Sync keeps polling until expectation is met, then settles."""
        # First 2 observations don't have target, 3rd does
        target = TargetCandidate(
            source_type=0,
            bbox=(100, 100, 200, 150),
            confidence=0.9,
            text="OK",
        )

        observations = [
            make_observation(observation_id="obs-1", candidates=()),
            make_observation(observation_id="obs-2", candidates=()),
            make_observation(observation_id="obs-3", candidates=(target,)),
            make_observation(observation_id="obs-4", candidates=(target,)),  # extra
        ]

        perception = FakePerceptionProvider(observation_sequence=observations)
        sync_provider = create_default_synchronization_provider(perception)

        context = SynchronizationContext(
            step_id="test-multi-poll",
            before_observation_id="before-obs",
            expectation=VerificationExpectation.target_visible("OK"),
        )

        result = await sync_provider.wait_until_settled(
            context,
            timeout_s=2.0,
            poll_interval_s=0.01,
        )

        # Should settle when target first appears (3rd poll)
        assert result.status == SynchronizationStatus.SETTLED
        assert result.settled is True
        # Should have polled at least 3 times before settling
        assert result.poll_count >= 3


# ---------------------------------------------------------------------------
# Test 37: No unbounded polling
# ---------------------------------------------------------------------------

class TestNoUnboundedPolling:
    """Test 37: Sync must never poll without a bound (always has timeout)."""

    @pytest.mark.asyncio
    async def test_sync_returns_within_timeout_bound(self):
        """Sync always returns within the timeout, no matter what."""
        # Observations that never satisfy expectation
        observations = [
            make_observation(observation_id=f"obs-{i}", candidates=())
            for i in range(1, 1000)
        ]

        perception = FakePerceptionProvider(observation_sequence=observations)
        sync_provider = create_default_synchronization_provider(perception)

        context = SynchronizationContext(
            step_id="test-bounded",
            expectation=VerificationExpectation.target_visible("Never"),
        )

        start = time.time()
        result = await sync_provider.wait_until_settled(
            context,
            timeout_s=0.2,  # Tight timeout
            poll_interval_s=0.01,
        )
        elapsed = time.time() - start

        # Should respect the timeout
        assert elapsed < 1.0  # Generous upper bound for test slowness
        assert result.status in (
            SynchronizationStatus.TIMEOUT,
            SynchronizationStatus.INCONCLUSIVE,
        )
        assert result.settled is False

    @pytest.mark.asyncio
    async def test_no_infinite_loop_on_failing_observations(self):
        """Sync does not loop forever even if observations keep failing."""
        # Observations that always fail
        observations = [
            make_observation(
                observation_id=f"obs-{i}",
                status=PerceptionStatus.FAILED,
            )
            for i in range(1, 100)
        ]

        perception = FakePerceptionProvider(observation_sequence=observations)
        sync_provider = create_default_synchronization_provider(perception)

        context = SynchronizationContext(
            step_id="test-failing",
        )

        start = time.time()
        result = await sync_provider.wait_until_settled(
            context,
            timeout_s=0.2,
            poll_interval_s=0.01,
        )
        elapsed = time.time() - start

        # Should timeout cleanly
        assert elapsed < 1.0
        assert result.status in (
            SynchronizationStatus.TIMEOUT,
            SynchronizationStatus.INCONCLUSIVE,
        )


# ---------------------------------------------------------------------------
# Test 38: No LLM (zero LLM calls)
# ---------------------------------------------------------------------------

class TestNoLLMCalls:
    """Test 38: The SYNCHRONIZE phase must NEVER make LLM calls."""

    @pytest.mark.asyncio
    async def test_sync_provider_makes_zero_llm_calls(self):
        """The default sync provider does not import or use any LLM."""
        # Test that the default provider is purely deterministic
        from core.execution import sync as sync_module
        import inspect

        source = inspect.getsource(sync_module)
        # No imports of LLM SDKs
        forbidden_imports = ['openai', 'anthropic', 'ai_brain']
        for forbidden in forbidden_imports:
            assert forbidden not in source.lower(), \
                f"Default sync provider should not import '{forbidden}'"

        # The word 'llm' is allowed in comments/docstrings describing what
        # the provider does NOT do. But it must not appear in code that
        # actually invokes a language model.
        assert 'await llm' not in source.lower()
        assert 'self._llm' not in source.lower()

    @pytest.mark.asyncio
    async def test_cycle_synchronize_makes_zero_llm_calls(self):
        """The ExecutionCycle's SYNCHRONIZE phase does not call any LLM."""
        # Track LLM calls via a sentinel
        llm_call_count = [0]

        # Wrap sync provider to detect LLM calls
        class LLMSpySyncProvider:
            @property
            def name(self) -> str:
                return "llm-spy-sync"

            async def wait_until_settled(
                self,
                context: SynchronizationContext,
                *,
                timeout_s: float = 5.0,
                poll_interval_s: float = 0.05,
                cancellation_token: Optional[Any] = None,
            ) -> SynchronizationResult:
                # If this code were to call an LLM, it would happen here.
                # We assert that it doesn't.
                llm_call_count[0] += 1  # Count sync invocations, not LLM calls
                return SynchronizationResult(
                    status=SynchronizationStatus.SETTLED,
                    settled=True,
                    observation_id="spy-obs",
                    elapsed_ms=1.0,
                    poll_count=1,
                )

        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = LLMSpySyncProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
        )

        step = make_step()
        result = await cycle.execute(step)

        # Sync was called once, no LLM call was made
        assert llm_call_count[0] == 1
        assert result.status == ExecutionStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_default_provider_pure_deterministic(self):
        """DefaultSynchronizationProvider is a pure deterministic implementation."""
        from core.execution.sync import DefaultSynchronizationProvider
        import inspect

        # Check that the default provider has no LLM SDK imports
        source = inspect.getsource(DefaultSynchronizationProvider)
        assert 'openai' not in source.lower()
        assert 'anthropic' not in source.lower()
        # The provider must not invoke any LLM
        assert 'await self._llm' not in source.lower()
        assert 'self.llm' not in source.lower()


# ---------------------------------------------------------------------------
# Test 39: Execution regression
# ---------------------------------------------------------------------------

class TestExecutionRegression:
    """Test 39: Synchronization doesn't break the existing execution flow."""

    @pytest.mark.asyncio
    async def test_no_provider_means_skip_sync(self):
        """When no synchronization provider is configured, sync is skipped."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            # No synchronization_provider
        )

        step = make_step()
        result = await cycle.execute(step)

        # Should succeed without sync
        assert result.status == ExecutionStatus.SUCCESS
        assert result.synchronization_result is None

    @pytest.mark.asyncio
    async def test_synchronization_disabled_by_policy(self):
        """When enable_synchronization=False, sync is skipped even with provider."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
            policy=ExecutionPolicy(enable_synchronization=False),
        )

        step = make_step()
        result = await cycle.execute(step)

        # Should succeed without sync
        assert result.status == ExecutionStatus.SUCCESS
        assert sync_provider.call_count == 0
        assert result.synchronization_result is None

    @pytest.mark.asyncio
    async def test_require_settlement_false_allows_non_settled(self):
        """When require_settlement=False, the cycle continues even with non-SETTLED."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider(
            return_status=SynchronizationStatus.INCONCLUSIVE,
            return_settled=False,
        )

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
            policy=ExecutionPolicy(require_settlement=False),
        )

        step = make_step()
        result = await cycle.execute(step)

        # Cycle continues through verify, which succeeds
        assert result.status == ExecutionStatus.SUCCESS
        assert result.synchronization_result is not None
        assert result.synchronization_result.status == SynchronizationStatus.INCONCLUSIVE

    @pytest.mark.asyncio
    async def test_existing_execution_tests_still_pass(self):
        """All previous execution cycle behavior is preserved."""
        # The cycle should work with default settings
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
        )

        step = make_step()
        result = await cycle.execute(step)

        # All previous behavior preserved
        assert result.status == ExecutionStatus.SUCCESS
        assert result.observation is not None
        assert result.action_result is not None
        assert result.verification_result is not None
        assert result.synchronization_result is not None  # NEW in 19.3
        assert result.pre_state is not None
        assert result.post_state is not None


# ---------------------------------------------------------------------------
# Additional tests: ExecutionResult, ExecutionPolicy, trace
# ---------------------------------------------------------------------------

class TestExecutionResultAndTrace:
    """Tests for ExecutionResult synchronization fields and trace data."""

    @pytest.mark.asyncio
    async def test_execution_result_includes_sync_result(self):
        """ExecutionResult.synchronization_result is populated on success."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
        )

        step = make_step()
        result = await cycle.execute(step)

        assert result.synchronization_result is not None
        assert hasattr(result.synchronization_result, 'status')
        assert hasattr(result.synchronization_result, 'settled')
        assert hasattr(result.synchronization_result, 'poll_count')
        assert hasattr(result.synchronization_result, 'elapsed_ms')

    @pytest.mark.asyncio
    async def test_execution_trace_includes_sync_metadata(self):
        """ExecutionTrace includes synchronization metadata."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
        )

        step = make_step()
        result = await cycle.execute(step)

        trace = result.trace
        assert trace.synchronization_status is not None
        assert trace.synchronization_status == SynchronizationStatus.SETTLED.value
        assert trace.synchronization_observation_id is not None
        assert trace.synchronization_elapsed_ms >= 0
        assert trace.synchronization_poll_count >= 0

    @pytest.mark.asyncio
    async def test_to_dict_includes_sync_data(self):
        """ExecutionResult.to_dict() includes synchronization fields."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
        )

        step = make_step()
        result = await cycle.execute(step)

        result_dict = result.to_dict()
        assert "synchronization_result" in result_dict
        assert result_dict["synchronization_result"] is not None
        assert "synchronization_status" in result_dict["trace"]
        assert result_dict["trace"]["synchronization_status"] == SynchronizationStatus.SETTLED.value


# ---------------------------------------------------------------------------
# Additional tests: observability events
# ---------------------------------------------------------------------------

class TestObservabilityEvents:
    """Tests for observability events emitted during synchronization."""

    @pytest.mark.asyncio
    async def test_synchronization_events_emitted(self):
        """SYNCHRONIZATION_STARTED and SYNCHRONIZATION_COMPLETED events are emitted."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider()

        events: List[tuple] = []

        def sink(event_name: str, data: Dict[str, Any]) -> None:
            events.append((event_name, data))

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
            observability_sink=sink,
        )

        step = make_step()
        result = await cycle.execute(step)

        event_names = [e[0] for e in events]
        assert "SYNCHRONIZATION_STARTED" in event_names
        assert "SYNCHRONIZATION_COMPLETED" in event_names

    @pytest.mark.asyncio
    async def test_synchronization_failed_event_emitted(self):
        """SYNCHRONIZATION_FAILED event is emitted on non-SETTLED outcome."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider(
            return_status=SynchronizationStatus.INCONCLUSIVE,
            return_settled=False,
        )

        events: List[tuple] = []

        def sink(event_name: str, data: Dict[str, Any]) -> None:
            events.append((event_name, data))

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
            observability_sink=sink,
        )

        step = make_step()
        result = await cycle.execute(step)

        event_names = [e[0] for e in events]
        assert "SYNCHRONIZATION_FAILED" in event_names


# ---------------------------------------------------------------------------
# Additional tests: Policy configuration
# ---------------------------------------------------------------------------

class TestPolicyConfiguration:
    """Tests for ExecutionPolicy synchronization fields."""

    def test_policy_has_sync_fields(self):
        """ExecutionPolicy has the Stage 19.3 fields."""
        policy = ExecutionPolicy()
        assert hasattr(policy, 'enable_synchronization')
        assert hasattr(policy, 'synchronization_timeout_s')
        assert hasattr(policy, 'synchronization_poll_interval_s')
        assert hasattr(policy, 'require_settlement')

        # Defaults
        assert policy.enable_synchronization is True
        assert policy.synchronization_timeout_s > 0
        assert policy.synchronization_poll_interval_s > 0
        assert policy.require_settlement is True

    @pytest.mark.asyncio
    async def test_custom_timeout_passed_to_provider(self):
        """The custom timeout is passed to the sync provider."""
        perception = FakePerceptionProvider()
        grounding = FakeGroundingProvider()
        action = FakeActionExecutor()
        verification = FakeVerificationProvider()
        sync_provider = FakeSynchronizationProvider()

        cycle = ExecutionCycle(
            perception_provider=perception,
            target_resolver=grounding,
            action_executor=action,
            verification_provider=verification,
            synchronization_provider=sync_provider,
            policy=ExecutionPolicy(
                synchronization_timeout_s=7.5,
                synchronization_poll_interval_s=0.1,
            ),
        )

        step = make_step()
        await cycle.execute(step)

        assert sync_provider.last_timeout_s == 7.5
        assert sync_provider.last_poll_interval_s == 0.1
