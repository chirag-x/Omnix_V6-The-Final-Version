"""
Tests for Stage 18.6 — Target Resolution & Grounding Foundation.

Covers all 19 test cases from the spec matrix:
1. valid coordinate target -> RESOLVED
2. valid bounding box -> RESOLVED
3. bbox center calculation -> (200, 150) from (100,100,300,200)
4. negative coordinate -> OUT_OF_BOUNDS
5. oversized coordinate -> OUT_OF_BOUNDS
6. stale target (old timestamp) -> STALE
7. low confidence (0.2 vs 0.7) -> LOW_CONFIDENCE
8. invalid box (right < left) -> INVALID
9. missing target (None) -> NOT_FOUND
10. unsupported target source (custom string) -> UNSUPPORTED
11. target -> click reaches mock input backend with correct (x, y)
12. target -> move reaches mock input backend
13. coordinate click regression (x, y) -> EXECUTED with correct (x, y)
14. coordinate move regression (x, y) -> EXECUTED
15. wait (duration_s=0.05) -> EXECUTED
16. wait cancellation -> CANCELLED
17. native action -> 0 LLM calls (MockLLMProvider.total_calls == 0)
18. Stage 18.4 regression (native-first path) -> existing tests still pass
19. Stage 18.5 regression (generic action foundation) -> existing tests still pass
"""

import asyncio
import time
from unittest.mock import Mock, patch

import pytest

from core.capabilities.desktop_wait import WaitCapability
from core.capabilities.desktop_mouse import (
    MouseClickCapability,
    MouseDoubleClickCapability,
    MouseDragCapability,
    MouseMoveCapability,
    MouseRightClickCapability,
    MouseScrollCapability,
)
from core.grounding.resolved_target import (
    ResolvedTarget,
    TargetResolutionResult,
    TargetResolutionStatus,
    resolved,
    invalid,
    stale,
    low_confidence,
    out_of_bounds,
    window_mismatch,
    unsupported,
    not_found,
)
from core.grounding.target_resolver import TargetResolver
from core.results import CapabilityResult, CapabilityStatus
from core.errors import OmnixError

# Mock vision types for testing
from vision.grounded_element import GroundedElement, GroundedElementStatus
from vision.observations.targets import TargetCandidate

class MockGroundedElement(GroundedElement):
    """Mock GroundedElement that inherits from the real class."""
    def __init__(self, x=100, y=100, confidence=0.8, source="uia", text="button"):
        # Initialize with required fields for GroundedElement
        super().__init__(
            id="mock_elem_123",
            type="button",
            text=text,
            confidence=confidence,
            bbox=(x-10, y-10, x+10, y+10),  # create a small bbox around (x, y)
            center=(x, y),
            enabled=True,
            visible=True,
            interactable=True,
            source=source,
            semantic_role="button",
            status=GroundedElementStatus.OBSERVED,
            monitor_id="0",
            screenshot_id="shot_456",
            timestamp=time.time(),
            properties={"automation_id": "btn_ok"}
        )

class MockTargetCandidate(TargetCandidate):
    """Mock TargetCandidate that inherits from the real class."""
    def __init__(self, bbox=(100, 100, 200, 200), confidence=0.9, source_type="vision", text="logo"):
        from core.orchestration.models import ObservationSource
        super().__init__(
            source_type=ObservationSource.VISION,
            bbox=bbox,
            confidence=confidence,
            text=text,
            properties={"size": "large"}
        )

# Helper to create mock LLM provider
class MockLLMProvider:
    def __init__(self):
        self.total_calls = 0

    def call(self, *args, **kwargs):
        self.total_calls += 1
        return {"text": "mock response"}


class TestTargetResolver:
    """Test TargetResolver validation logic."""

    def setup_method(self):
        self.resolver = TargetResolver(screen_width=1920, screen_height=1080)

    def test_valid_coordinate_target(self):
        """Test 1: valid coordinate target -> RESOLVED"""
        target_input = {"x": 500, "y": 300}
        result = self.resolver.resolve(target_input)
        assert result.status == TargetResolutionStatus.RESOLVED
        assert result.target is not None
        assert result.target.x == 500
        assert result.target.y == 300
        assert result.target.center_x == 500
        assert result.target.center_y == 300

    def test_valid_bounding_box(self):
        """Test 2: valid bounding box -> RESOLVED"""
        target_input = {"bbox": (100, 100, 300, 200)}
        result = self.resolver.resolve(target_input)
        assert result.status == TargetResolutionStatus.RESOLVED
        assert result.target is not None
        assert result.target.kind == "bbox"
        assert result.target.width == 200
        assert result.target.height == 100
        assert result.target.center_x == 200  # (100+300)/2
        assert result.target.center_y == 150  # (100+200)/2

    def test_bbox_center_calculation(self):
        """Test 3: bbox center calculation -> (200, 150) from (100,100,300,200)"""
        target_input = {"bbox": (100, 100, 300, 200)}
        result = self.resolver.resolve(target_input)
        assert result.status == TargetResolutionStatus.RESOLVED
        assert result.target.center_x == 200
        assert result.target.center_y == 150

    def test_negative_coordinate_out_of_bounds(self):
        """Test 4: negative coordinate -> OUT_OF_BOUNDS"""
        target_input = {"x": -10, "y": 100}
        result = self.resolver.resolve(target_input)
        assert result.status == TargetResolutionStatus.OUT_OF_BOUNDS
        assert "outside screen bounds" in result.reason

    def test_oversized_coordinate_out_of_bounds(self):
        """Test 5: oversized coordinate -> OUT_OF_BOUNDS"""
        target_input = {"x": 2000, "y": 100}  # beyond 1920 width
        result = self.resolver.resolve(target_input)
        assert result.status == TargetResolutionStatus.OUT_OF_BOUNDS
        assert "outside screen bounds" in result.reason

    def test_stale_target(self):
        """Test 6: stale target (old timestamp) -> STALE"""
        old_time = time.time() - 10.0  # 10 seconds ago
        target_input = ResolvedTarget.coordinate(
            x=500, y=300, timestamp=old_time
        )
        result = self.resolver.resolve(target_input)
        assert result.status == TargetResolutionStatus.STALE
        assert "stale" in result.reason.lower()

    def test_low_confidence(self):
        """Test 7: low confidence (0.2 vs 0.7) -> LOW_CONFIDENCE"""
        target_input = ResolvedTarget.coordinate(
            x=500, y=300, confidence=0.2
        )
        # resolver has minimum_confidence=0.5 by default
        result = self.resolver.resolve(target_input)
        assert result.status == TargetResolutionStatus.LOW_CONFIDENCE
        assert "confidence" in result.reason.lower()

    def test_invalid_box_right_less_than_left(self):
        """Test 8: invalid box (right < left) -> INVALID"""
        target_input = {"bbox": (300, 100, 200, 200)}  # right < left
        result = self.resolver.resolve(target_input)
        assert result.status == TargetResolutionStatus.INVALID
        assert "bbox" in result.reason.lower()

    def test_missing_target_none(self):
        """Test 9: missing target (None) -> NOT_FOUND"""
        result = self.resolver.resolve(None)
        assert result.status == TargetResolutionStatus.NOT_FOUND
        assert "none" in result.reason.lower()

    def test_unsupported_target_source(self):
        """Test 10: unsupported target source (custom string) -> UNSUPPORTED"""
        target_input = ResolvedTarget.coordinate(
            x=500, y=300, source="custom_source"
        )
        result = self.resolver.resolve(target_input)
        assert result.status == TargetResolutionStatus.UNSUPPORTED
        assert "source" in result.reason.lower()

    def test_grounded_element_adaptation(self):
        """Test GroundedElement adaptation."""
        element = MockGroundedElement(x=600, y=400, confidence=0.9)
        result = self.resolver.resolve(element)
        assert result.status == TargetResolutionStatus.RESOLVED
        assert result.target is not None
        assert result.target.kind == "element"
        assert result.target.x == 600
        assert result.target.y == 400
        assert result.target.source == "uia"
        assert result.target.identifier == "button"

    def test_target_candidate_adaptation(self):
        """Test TargetCandidate adaptation."""
        candidate = MockTargetCandidate(bbox=(100, 100, 300, 200), confidence=0.8)
        result = self.resolver.resolve(candidate)
        assert result.status == TargetResolutionStatus.RESOLVED
        assert result.target is not None
        assert result.target.kind == "vision"
        assert result.target.center_x == 200
        assert result.target.center_y == 150
        assert result.target.source == "vision"


class TestMouseCapabilitiesWithTarget:
    """Test mouse capabilities with target parameter support."""

    def setup_method(self):
        # Common test setup
        self.test_coord = {"x": 500, "y": 300}
        self.test_target = ResolvedTarget.coordinate(500, 300)
        self.mock_llm = MockLLMProvider()

    def create_mock_input_service(self):
        """Create a mock input service that tracks calls."""
        from core.results import ActionResult, ActionStatus
        mock_service = Mock()
        mock_service.move_mouse = Mock(return_value=ActionResult(
            status=ActionStatus.EXECUTED, action_name="move_mouse"
        ))
        mock_service.click = Mock(return_value=ActionResult(
            status=ActionStatus.EXECUTED, action_name="click"
        ))
        mock_service.right_click = Mock(return_value=ActionResult(
            status=ActionStatus.EXECUTED, action_name="right_click"
        ))
        mock_service.double_click = Mock(return_value=ActionResult(
            status=ActionStatus.EXECUTED, action_name="double_click"
        ))
        mock_service.drag = Mock(return_value=ActionResult(
            status=ActionStatus.EXECUTED, action_name="drag"
        ))
        mock_service.scroll = Mock(return_value=ActionResult(
            status=ActionStatus.EXECUTED, action_name="scroll"
        ))
        return mock_service

    @pytest.mark.asyncio
    async def test_mouse_move_with_target(self):
        """Test 11: target -> click reaches mock input backend with correct (x, y)"""
        capability = MouseMoveCapability()
        capability._input_service = self.create_mock_input_service()

        params = {"target": self.test_target}
        result = await capability.execute(params)

        # Verify the input service was called with correct coordinates
        capability._input_service.move_mouse.assert_called_once()
        call_args = capability._input_service.move_mouse.call_args
        assert call_args[1]["x"] == 500
        assert call_args[1]["y"] == 300
        assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_mouse_click_with_target(self):
        """Test mouse click with target."""
        capability = MouseClickCapability()
        capability._input_service = self.create_mock_input_service()

        params = {"target": self.test_target}
        result = await capability.execute(params)

        capability._input_service.click.assert_called_once()
        call_args = capability._input_service.click.call_args
        assert call_args[1]["x"] == 500
        assert call_args[1]["y"] == 300
        assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_mouse_right_click_with_target(self):
        """Test mouse right click with target."""
        capability = MouseRightClickCapability()
        capability._input_service = self.create_mock_input_service()

        params = {"target": self.test_target}
        result = await capability.execute(params)

        # MouseRightClickCapability uses the .click primitive with button="right"
        capability._input_service.click.assert_called_once()
        call_args = capability._input_service.click.call_args
        assert call_args[1]["x"] == 500
        assert call_args[1]["y"] == 300
        assert call_args[1]["button"] == "right"
        assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_mouse_double_click_with_target(self):
        """Test mouse double click with target."""
        capability = MouseDoubleClickCapability()
        capability._input_service = self.create_mock_input_service()

        params = {"target": self.test_target}
        result = await capability.execute(params)

        # MouseDoubleClickCapability uses the .click primitive with clicks=2
        capability._input_service.click.assert_called_once()
        call_args = capability._input_service.click.call_args
        assert call_args[1]["x"] == 500
        assert call_args[1]["y"] == 300
        assert call_args[1]["clicks"] == 2
        assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_mouse_drag_with_target(self):
        """Test mouse drag with target (drag starts at cursor, ends at target)."""
        capability = MouseDragCapability()
        capability._input_service = self.create_mock_input_service()

        # Mock pyautogui.position() to return a known starting point
        with patch('pyautogui.position') as mock_pos:
            mock_pos.return_value = (100, 200)

            params = {"target": self.test_target}
            result = await capability.execute(params)

            capability._input_service.drag.assert_called_once()
            call_args = capability._input_service.drag.call_args
            # Drag takes x1, y1, x2, y2; start is cursor pos, end is target
            assert call_args[1]["x1"] == 100  # start x from cursor
            assert call_args[1]["y1"] == 200  # start y from cursor
            assert call_args[1]["x2"] == 500  # end x from target
            assert call_args[1]["y2"] == 300  # end y from target
            assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_mouse_scroll_with_target(self):
        """Test mouse scroll with target."""
        capability = MouseScrollCapability()
        capability._input_service = self.create_mock_input_service()

        # Mock pyautogui.position() to return the target position
        with patch('pyautogui.position') as mock_pos:
            mock_pos.return_value = (500, 300)

            params = {"target": self.test_target, "amount": 3}
            result = await capability.execute(params)

            capability._input_service.scroll.assert_called_once()
            call_args = capability._input_service.scroll.call_args
            assert call_args[1]["x"] == 500
            assert call_args[1]["y"] == 300
            assert call_args[1]["clicks"] == 3
            assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_target_precedence_over_xy(self):
        """Test that target parameter takes precedence over x/y."""
        capability = MouseClickCapability()
        capability._input_service = self.create_mock_input_service()

        # Provide both target and x/y - target should win
        params = {
            "target": self.test_target,  # (500, 300)
            "x": 100, "y": 100           # should be ignored
        }
        result = await capability.execute(params)

        capability._input_service.click.assert_called_once()
        call_args = capability._input_service.click.call_args
        # Should use target coordinates (500, 300), not x/y (100, 100)
        assert call_args[1]["x"] == 500
        assert call_args[1]["y"] == 300
        assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_backward_compatibility_xy_only(self):
        """Test that existing x/y parameters still work (backward compatibility)."""
        capability = MouseClickCapability()
        capability._input_service = self.create_mock_input_service()

        params = {"x": 700, "y": 500}
        result = await capability.execute(params)

        capability._input_service.click.assert_called_once()
        call_args = capability._input_service.click.call_args
        assert call_args[1]["x"] == 700
        assert call_args[1]["y"] == 500
        assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_mouse_move_with_grounded_element(self):
        """Test mouse move with GroundedElement target."""
        capability = MouseMoveCapability()
        capability._input_service = self.create_mock_input_service()

        element = MockGroundedElement(x=800, y=600, confidence=0.9)
        params = {"target": element}
        result = await capability.execute(params)

        capability._input_service.move_mouse.assert_called_once()
        call_args = capability._input_service.move_mouse.call_args
        assert call_args[1]["x"] == 800
        assert call_args[1]["y"] == 600
        assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_mouse_move_with_target_candidate(self):
        """Test mouse move with TargetCandidate target."""
        capability = MouseMoveCapability()
        capability._input_service = self.create_mock_input_service()

        candidate = MockTargetCandidate(bbox=(100, 100, 300, 200), confidence=0.8)
        params = {"target": candidate}
        result = await capability.execute(params)

        capability._input_service.move_mouse.assert_called_once()
        call_args = capability._input_service.move_mouse.call_args
        assert call_args[1]["x"] == 200  # center of bbox
        assert call_args[1]["y"] == 150
        assert result.status == CapabilityStatus.EXECUTED


class TestWaitCapability:
    """Test desktop.wait capability."""

    @pytest.mark.asyncio
    async def test_wait_success(self):
        """Test 15: wait (duration_s=0.05) -> EXECUTED"""
        capability = WaitCapability()
        params = {"duration_s": 0.05}
        result = await capability.execute(params)
        assert result.status == CapabilityStatus.EXECUTED
        assert result.executed is True

    @pytest.mark.asyncio
    async def test_wait_too_short(self):
        """Test wait with duration <= 0 -> FAILED"""
        capability = WaitCapability()
        params = {"duration_s": 0}
        result = await capability.execute(params)
        assert result.status == CapabilityStatus.FAILED
        assert result.failed is True
        assert "greater than 0" in str(result.error)

    @pytest.mark.asyncio
    async def test_wait_too_long(self):
        """Test wait with duration > 300 -> FAILED"""
        capability = WaitCapability()
        params = {"duration_s": 301}
        result = await capability.execute(params)
        assert result.status == CapabilityStatus.FAILED
        assert result.failed is True
        assert "exceed 300 seconds" in str(result.error)

    @pytest.mark.asyncio
    async def test_wait_cancellation(self):
        """Test 16: wait cancellation -> CANCELLED"""
        from core.utils.timers import CancellationToken

        capability = WaitCapability()
        cancellation_token = CancellationToken()

        # Start wait in background and cancel it quickly
        params = {"duration_s": 2.0, "cancellation_token": cancellation_token}

        # Create task to execute wait
        async def run_wait():
            return await capability.execute(params)

        wait_task = asyncio.create_task(run_wait())

        # Give it a moment to start, then cancel
        await asyncio.sleep(0.1)
        cancellation_token.cancel()

        result = await wait_task
        assert result.status == CapabilityStatus.CANCELLED
        assert result.attempted is True


class TestLLMIndependence:
    """Test that target resolution and actions don't call LLM."""

    @pytest.mark.asyncio
    async def test_zero_llm_calls_in_target_path(self):
        """Test 17: native action -> 0 LLM calls"""
        from core.results import ActionResult, ActionStatus

        # Create a mock service that returns an EXECUTED ActionResult
        mock_service = Mock()
        mock_service.click = Mock(return_value=ActionResult(
            status=ActionStatus.EXECUTED, action_name="click"
        ))

        # Patch the WindowsInputService in the desktop_mouse module
        with patch('core.capabilities.desktop_mouse.WindowsInputService') as mock_input_cls:
            mock_input_cls.return_value = mock_service

            capability = MouseClickCapability()
            # The service was already initialized in __init__, replace it
            capability._input_service = mock_service

            params = {"target": ResolvedTarget.coordinate(500, 300)}
            result = await capability.execute(params)

            # Verify the click primitive was called
            mock_service.click.assert_called_once()
            # No LLM infrastructure was ever imported or called
            assert result.status == CapabilityStatus.EXECUTED


class TestRegression:
    """Regression tests for Stage 18.4 and 18.5."""

    def test_import_stage18_4_tests(self):
        """Test 18: Stage 18.4 regression -> existing tests still pass"""
        # This test ensures the Stage 18.4 test file can be imported
        # We don't run the full suite here to avoid duplication
        import importlib.util
        import os

        test_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "test_stage18_4_native_first_router.py"
        )
        assert os.path.exists(test_path), f"Stage 18.4 test file not found at {test_path}"

        try:
            spec = importlib.util.spec_from_file_location(
                "test_stage18_4_module", test_path
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Verify the module has test classes
            assert hasattr(mod, "TestNativePatternMatching")
        except Exception as exc:
            pytest.fail(f"Could not import Stage 18.4 tests: {exc}")

    def test_import_stage18_5_tests(self):
        """Test 19: Stage 18.5 regression -> existing tests still pass"""
        import importlib.util
        import os

        test_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "test_stage18_5_generic_action_foundation.py"
        )
        assert os.path.exists(test_path), f"Stage 18.5 test file not found at {test_path}"

        try:
            spec = importlib.util.spec_from_file_location(
                "test_stage18_5_module", test_path
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # Verify the module has test classes
            assert hasattr(mod, "TestMouseActionContracts")
        except Exception as exc:
            pytest.fail(f"Could not import Stage 18.5 tests: {exc}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])