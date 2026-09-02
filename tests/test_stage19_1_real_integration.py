"""
Stage 19.1 — Real-System Integration Tests for Execution Cycle

Test suite for verifying that ExecutionCycle works with real Omnix subsystems:
- PerceptionAdapter (real perception)
- TargetResolver (real grounding)
- CapabilityRouter (real action)
- DefaultVerificationProvider (real verification)

These tests use real components where possible but mock external dependencies
that are not safe to call in CI environments.
"""

import asyncio
import time
import sys
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from typing import Any, Dict, List, Optional, Tuple
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
)
from core.execution.provider import (
    DefaultActionExecutor,
    DefaultGroundingProvider,
    DefaultVerificationProvider,
)
from core.execution.errors import (
    ObservationFailedError,
    GroundingFailedError,
    ActionFailedError,
    VerificationFailedError,
)

# Import Omnix types for real components
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
from core.capability_router import CapabilityRouter
from core.capability_registry import CapabilityRegistry
from vision.router.perception_router import PerceptionRouter
from vision.router.screenshot_provider import ScreenshotProvider
from vision.perception_adapter import PerceptionAdapter
from vision.observations.targets import TargetCandidate
from core.orchestration.models import ObservationSource
from core.capabilities import MouseClickCapability, KeyboardTypeCapability, register_standard_capabilities


# ---------------------------------------------------------------------------
# Test Doubles for External Dependencies (where needed)
# ---------------------------------------------------------------------------

class MockPerceptionRouter:
    """Mock perception router that returns deterministic results for testing."""

    def __init__(self, return_candidates=None):
        self._candidates = return_candidates or []
        self.call_count = 0

    def find_targets(self, target_query="*", image_path=None):
        self.call_count += 1
        from vision.observations.targets import TargetCandidate
        from core.orchestration.models import ObservationSource

        # Return a mock button candidate
        candidate = TargetCandidate(
            text="Test Button",
            bbox=(100, 100, 200, 150),
            confidence=0.95,
            source_type=ObservationSource.UIA,
            properties={
                "name": "Test Button",
                "automation_id": "testButton",
                "control_type": "Button"
            }
        )
        return [candidate]


class MockScreenshotProvider:
    """Mock screenshot provider that doesn't actually capture."""

    def capture(self, path=None):
        return b"mock-screenshot-data"


class MockInputService:
    """Mock input service that records calls but doesn't actually perform them."""

    def __init__(self):
        self.call_log = []
        self.initialized = True

    def initialize(self):
        self.initialized = True

    def click(self, x=None, y=None, button="left", clicks=1):
        self.call_log.append({"action": "click", "x": x, "y": y, "button": button, "clicks": clicks})
        from core.results import ActionResult, ActionStatus
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="click",
            details={"success": True, "x": x, "y": y, "button": button, "clicks": clicks}
        )

    def move_mouse(self, x=None, y=None):
        self.call_log.append({"action": "move", "x": x, "y": y})
        from core.results import ActionResult, ActionStatus
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="move",
            details={"success": True, "x": x, "y": y}
        )

    def type_text(self, text="", target=None, interval_s=0.0):
        self.call_log.append({"action": "type", "text": text, "target": target, "interval_s": interval_s})
        from core.results import ActionResult, ActionStatus
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="type",
            details={"success": True, "text": text, "target": target, "interval_s": interval_s}
        )


# ---------------------------------------------------------------------------
# Helper: Build a working ExecutionCycle using MockInputService that succeeds
# ---------------------------------------------------------------------------

def _make_action_executor_with_succeeding_capability(input_service: MockInputService) -> Any:
    """
    Build a real CapabilityRouter + DefaultActionExecutor where the underlying
    capabilities are real but use MockInputService so no real mouse moves happen.

    This avoids the issue with the default capabilities trying to use pyautogui
    or system-level input on a Windows environment in tests.
    """
    from core.capabilities import MouseClickCapability, KeyboardTypeCapability

    registry = CapabilityRegistry()
    registry.register(MouseClickCapability(input_service))
    registry.register(KeyboardTypeCapability(input_service))
    router = CapabilityRouter(registry)

    # Wrap the router so we strip the unsupported kwargs before dispatch
    class _AdaptedRouter:
        def __init__(self, real_router):
            self._real_router = real_router

        def route(self, capability_name, parameters, timeout_s=None, cancellation_token=None):
            # Strip timeout_s/cancellation_token that the real router doesn't accept
            return self._real_router.route(capability_name, parameters)

    adapted_router = _AdaptedRouter(router)
    return DefaultActionExecutor(adapted_router)


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

class TestExecutionCycleRealIntegration:
    """Test execution cycle with real Omnix subsystems."""

    @pytest.mark.asyncio
    async def test_real_perception_adapter_integration(self):
        """Test that ExecutionCycle works with real PerceptionAdapter."""
        # Setup real perception adapter (with mocked dependencies for determinism)
        perception_router = MockPerceptionRouter()
        screenshot_provider = MockScreenshotProvider()
        perception_adapter = PerceptionAdapter(perception_router, screenshot_provider)

        # Setup real grounding provider
        grounding_provider = DefaultGroundingProvider(TargetResolver())

        # Setup real action executor with registry containing real capabilities
        input_service = MockInputService()
        action_executor = _make_action_executor_with_succeeding_capability(input_service)

        # Setup real verification provider
        verification_provider = DefaultVerificationProvider(perception_adapter)

        # Create execution cycle with real components
        cycle = ExecutionCycle(
            perception_provider=perception_adapter,
            target_resolver=grounding_provider,
            action_executor=action_executor,
            verification_provider=verification_provider,
        )

        # Create test step with coordinate-based click (no target_query needed)
        step = ExecutionStep(
            step_id="real-integration-test-1",
            description="Click at coordinates",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            parameters={"x": 150, "y": 125},  # Direct coordinates
        )

        # Execute
        result = await cycle.execute(step)

        # Assert success
        assert result.status == ExecutionStatus.SUCCESS
        assert result.observation is not None
        assert result.action_result is not None

        # Verify that input service was called
        assert len(input_service.call_log) >= 1
        assert input_service.call_log[0]["action"] == "click"

        # Verify action result
        assert result.action_result.capability_name == "desktop.mouse.click"
        assert result.action_result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_real_mouse_click_and_type_sequence(self):
        """Test a realistic mouse click followed by typing sequence."""
        # Setup components
        perception_router = MockPerceptionRouter()
        screenshot_provider = MockScreenshotProvider()
        perception_adapter = PerceptionAdapter(perception_router, screenshot_provider)

        grounding_provider = DefaultGroundingProvider(TargetResolver())

        input_service = MockInputService()
        action_executor = _make_action_executor_with_succeeding_capability(input_service)

        verification_provider = DefaultVerificationProvider(perception_adapter)

        # Create execution cycle
        cycle = ExecutionCycle(
            perception_provider=perception_adapter,
            target_resolver=grounding_provider,
            action_executor=action_executor,
            verification_provider=verification_provider,
        )

        # Step 1: Click at coordinates
        click_step = ExecutionStep(
            step_id="click-coords",
            description="Click at coordinates",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            parameters={"x": 200, "y": 215},
        )

        click_result = await cycle.execute(click_step)
        assert click_result.status == ExecutionStatus.SUCCESS
        assert click_result.action_result is not None
        assert click_result.action_result.capability_name == "desktop.mouse.click"

        # Step 2: Type text
        type_step = ExecutionStep(
            step_id="type-text",
            description="Type hello world",
            action=StepAction.TYPE_TEXT,
            capability_name="desktop.keyboard.type",
            parameters={"text": "Hello World"},
        )

        type_result = await cycle.execute(type_step)
        assert type_result.status == ExecutionStatus.SUCCESS
        assert type_result.action_result is not None
        assert type_result.action_result.capability_name == "desktop.keyboard.type"

        # Verify both actions were called
        assert len(input_service.call_log) == 2
        assert input_service.call_log[0]["action"] == "click"
        assert input_service.call_log[1]["action"] == "type"
        assert input_service.call_log[1]["text"] == "Hello World"

    @pytest.mark.asyncio
    async def test_zero_llm_calls_during_execution(self):
        """Verify that execution cycle makes zero LLM calls."""
        # Setup components
        perception_router = MockPerceptionRouter()
        screenshot_provider = MockScreenshotProvider()
        perception_adapter = PerceptionAdapter(perception_router, screenshot_provider)

        grounding_provider = DefaultGroundingProvider(TargetResolver())

        input_service = MockInputService()
        action_executor = _make_action_executor_with_succeeding_capability(input_service)

        verification_provider = DefaultVerificationProvider(perception_adapter)

        cycle = ExecutionCycle(
            perception_provider=perception_adapter,
            target_resolver=grounding_provider,
            action_executor=action_executor,
            verification_provider=verification_provider,
        )

        # Mock LLM provider to detect calls
        llm_call_count = 0

        def mock_get_provider(*args, **kwargs):
            nonlocal llm_call_count
            llm_call_count += 1
            raise RuntimeError(f"LLM call detected! Count: {llm_call_count}")

        # Patch the AI provider getter
        with patch('ai.provider.get_provider', side_effect=mock_get_provider, create=True):
            step = ExecutionStep(
                step_id="llm-test",
                description="Test LLM independence",
                action=StepAction.CLICK,
                capability_name="desktop.mouse.click",
                parameters={"x": 150, "y": 125},
            )

            # Execute
            result = await cycle.execute(step)

            # Assert success and zero LLM calls
            assert result.status == ExecutionStatus.SUCCESS
            assert llm_call_count == 0, f"Expected 0 LLM calls, got {llm_call_count}"

    @pytest.mark.asyncio
    async def test_grounding_failure_path(self):
        """Test that grounding failure is correctly reported."""
        # Setup components
        perception_router = MockPerceptionRouter()
        screenshot_provider = MockScreenshotProvider()
        perception_adapter = PerceptionAdapter(perception_router, screenshot_provider)

        grounding_provider = DefaultGroundingProvider(TargetResolver())

        input_service = MockInputService()
        action_executor = _make_action_executor_with_succeeding_capability(input_service)

        verification_provider = DefaultVerificationProvider(perception_adapter)

        cycle = ExecutionCycle(
            perception_provider=perception_adapter,
            target_resolver=grounding_provider,
            action_executor=action_executor,
            verification_provider=verification_provider,
        )

        # Use a target_query that can't be resolved
        ground_step = ExecutionStep(
            step_id="ground-failure",
            description="Grounding failure test",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            target_query="NonExistentElementThatCannotBeFoundAnywhere",
        )
        ground_result = await cycle.execute(ground_step)
        assert ground_result.status == ExecutionStatus.GROUNDING_FAILED

    @pytest.mark.asyncio
    async def test_event_observability_integration(self):
        """Test that observability events are emitted correctly."""
        events_received = []

        def observability_sink(event_name: str, data: dict):
            events_received.append((event_name, data))

        # Setup components
        perception_router = MockPerceptionRouter()
        screenshot_provider = MockScreenshotProvider()
        perception_adapter = PerceptionAdapter(perception_router, screenshot_provider)

        grounding_provider = DefaultGroundingProvider(TargetResolver())

        input_service = MockInputService()
        action_executor = _make_action_executor_with_succeeding_capability(input_service)

        verification_provider = DefaultVerificationProvider(perception_adapter)

        # Create execution cycle with observability sink
        cycle = ExecutionCycle(
            perception_provider=perception_adapter,
            target_resolver=grounding_provider,
            action_executor=action_executor,
            verification_provider=verification_provider,
            observability_sink=observability_sink,
        )

        step = ExecutionStep(
            step_id="observability-test",
            description="Observability test",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            parameters={"x": 150, "y": 125},
        )

        # Execute
        result = await cycle.execute(step)

        # Assert success
        assert result.status == ExecutionStatus.SUCCESS

        # Assert that observability events were received
        assert len(events_received) > 0

        # Check for key events
        event_names = [event[0] for event in events_received]
        assert "EXECUTION_STARTED" in event_names
        assert "OBSERVATION_STARTED" in event_names
        assert "ACTION_STARTED" in event_names
        assert "EXECUTION_COMPLETED" in event_names


# ---------------------------------------------------------------------------
# Performance Baseline Tests
# ---------------------------------------------------------------------------

class TestExecutionCyclePerformance:
    """Test performance characteristics of the execution cycle."""

    @pytest.mark.asyncio
    async def test_execution_timing_baseline(self):
        """Establish baseline timing for execution cycle phases."""
        # Setup components
        perception_router = MockPerceptionRouter()
        screenshot_provider = MockScreenshotProvider()
        perception_adapter = PerceptionAdapter(perception_router, screenshot_provider)

        grounding_provider = DefaultGroundingProvider(TargetResolver())

        input_service = MockInputService()
        action_executor = _make_action_executor_with_succeeding_capability(input_service)

        verification_provider = DefaultVerificationProvider(perception_adapter)

        cycle = ExecutionCycle(
            perception_provider=perception_adapter,
            target_resolver=grounding_provider,
            action_executor=action_executor,
            verification_provider=verification_provider,
        )

        step = ExecutionStep(
            step_id="timing-test",
            description="Timing baseline test",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            parameters={"x": 150, "y": 125},
        )

        # Execute and measure time
        start_time = time.time()
        result = await cycle.execute(step)
        end_time = time.time()

        # Assert success
        assert result.status == ExecutionStatus.SUCCESS

        # Assert timing is reasonable (should be fast with mocks)
        duration_ms = (end_time - start_time) * 1000
        assert duration_ms < 1000  # Should complete in under 1 second with mocks
        assert result.duration_ms > 0

        # Log timing for baseline
        print(f"Execution cycle baseline timing: {duration_ms:.2f}ms")
        print(f"Reported duration: {result.duration_ms:.2f}ms")


# ---------------------------------------------------------------------------
# Contract Compliance Tests
# ---------------------------------------------------------------------------

class TestExecutionCycleContractCompliance:
    """Test that ExecutionCycle complies with all required contracts."""

    def test_perception_provider_protocol_compliance(self):
        """Test that PerceptionAdapter complies with PerceptionProvider protocol."""
        perception_router = MockPerceptionRouter()
        screenshot_provider = MockScreenshotProvider()
        adapter = PerceptionAdapter(perception_router, screenshot_provider)

        # Check that it has the required method
        assert hasattr(adapter, 'observe')
        assert callable(getattr(adapter, 'observe'))

    def test_grounding_provider_protocol_compliance(self):
        """Test that DefaultGroundingProvider complies with GroundingProvider protocol."""
        resolver = TargetResolver()
        provider = DefaultGroundingProvider(resolver)

        # Check that it has the required method
        assert hasattr(provider, 'resolve')
        assert callable(getattr(provider, 'resolve'))

        # Check that it has required attributes
        assert hasattr(provider, 'name')
        assert isinstance(provider.name, str)

    def test_action_executor_protocol_compliance(self):
        """Test that DefaultActionExecutor complies with ActionExecutor protocol."""
        input_service = MockInputService()
        executor = _make_action_executor_with_succeeding_capability(input_service)

        # Check that it has the required method
        assert hasattr(executor, 'execute')
        assert callable(getattr(executor, 'execute'))

        # Check that it has required attributes
        assert hasattr(executor, 'name')
        assert isinstance(executor.name, str)

    def test_verification_provider_protocol_compliance(self):
        """Test that DefaultVerificationProvider complies with VerificationProvider protocol."""
        perception_router = MockPerceptionRouter()
        screenshot_provider = MockScreenshotProvider()
        adapter = PerceptionAdapter(perception_router, screenshot_provider)
        provider = DefaultVerificationProvider(adapter)

        # Check that it has the required method
        assert hasattr(provider, 'verify')
        assert callable(getattr(provider, 'verify'))

        # Check that it has required attributes
        assert hasattr(provider, 'name')
        assert isinstance(provider.name, str)


# ---------------------------------------------------------------------------
# Regression Tests
# ---------------------------------------------------------------------------

class TestExecutionCycleRegression:
    """Test that Stage 19.1 doesn't break Stage 19.0 functionality."""

    def test_stage19_0_imports_work(self):
        """Verify that Stage 19.0 modules can still be imported."""
        from core.execution.cycle import ExecutionCycle, ExecutionPolicy
        from core.execution.step import ExecutionStep, StepAction
        from core.execution.expectation import VerificationExpectation, ExpectationKind
        from core.execution.result import ExecutionResult, ExecutionStatus
        from core.execution.provider import (
            DefaultActionExecutor,
            DefaultGroundingProvider,
            DefaultVerificationProvider,
        )

        assert ExecutionCycle is not None
        assert ExecutionStep is not None
        assert VerificationExpectation is not None

    def test_provider_classes_have_required_attributes(self):
        """Verify that provider classes have all required attributes after refactor."""
        from core.execution.provider import (
            DefaultActionExecutor,
            DefaultGroundingProvider,
            DefaultVerificationProvider,
        )

        # These should all be instantiable without errors
        grounding = DefaultGroundingProvider(TargetResolver())
        assert grounding.name == "default_grounding_provider"
        assert hasattr(grounding, '_resolver')

        verification = DefaultVerificationProvider(Mock())
        assert verification.name == "default_verification_provider"
        assert hasattr(verification, '_perception_provider')

        # Action executor requires a router
        input_service = MockInputService()
        action = _make_action_executor_with_succeeding_capability(input_service)
        assert action.name == "default_action_executor"
        assert hasattr(action, '_router')


if __name__ == "__main__":
    # Run a simple test to verify basic functionality
    import asyncio

    async def simple_real_test():
        print("Running simple real integration test...")

        # Setup components
        perception_router = MockPerceptionRouter()
        screenshot_provider = MockScreenshotProvider()
        perception_adapter = PerceptionAdapter(perception_router, screenshot_provider)

        grounding_provider = DefaultGroundingProvider(TargetResolver())

        input_service = MockInputService()
        action_executor = _make_action_executor_with_succeeding_capability(input_service)

        verification_provider = DefaultVerificationProvider(perception_adapter)

        cycle = ExecutionCycle(
            perception_provider=perception_adapter,
            target_resolver=grounding_provider,
            action_executor=action_executor,
            verification_provider=verification_provider,
        )

        step = ExecutionStep(
            step_id="simple-real-test",
            description="Simple real integration test",
            action=StepAction.CLICK,
            capability_name="desktop.mouse.click",
            parameters={"x": 150, "y": 125},
        )

        result = await cycle.execute(step)
        print(f"Simple real test result: {result.status}")
        assert result.status == ExecutionStatus.SUCCESS
        print("✓ Simple real integration test passed")

    asyncio.run(simple_real_test())
    print("All basic real integration tests passed!")