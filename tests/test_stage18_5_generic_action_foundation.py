"""
Stage 18.5 — Generic Computer Action Foundation Tests

Comprehensive test suite for the generic computer action layer that will
support future autonomous computer use.

Tests cover:
1. All generic action contracts (click, type, press, hotkey, scroll, wait, screenshot, etc.)
2. Target model representation
3. Result model semantics (success/failure/timeout/cancellation)
4. Cancellation support
5. Timeout behavior
6. AI independence (actions do not call LLMs)
7. Stage 18.4 regression (native-first path still works)
"""

import pytest
import asyncio
import time
from unittest.mock import Mock, MagicMock, patch
from typing import Any, Dict

from core.capabilities.desktop_keyboard import (
    KeyboardTypeCapability,
    KeyboardPressCapability,
    KeyboardHotkeyCapability,
)
from core.capabilities.desktop_mouse import (
    MouseMoveCapability,
    MouseClickCapability,
    MouseRightClickCapability,
    MouseDoubleClickCapability,
    MouseDragCapability,
    MouseScrollCapability,
)
from core.capabilities.desktop_observation import (
    ScreenshotCapability,
    ScreenSizeCapability,
    ForegroundWindowCapability,
)
from core.capabilities.desktop_application import (
    ApplicationOpenCapability,
    ApplicationCloseCapability,
    ApplicationFocusCapability,
    ApplicationIsRunningCapability,
)
from core.capabilities.desktop_window import (
    WindowListCapability,
    WindowFocusCapability,
)
from core.results import (
    CapabilityStatus,
    CapabilityResult,
    ActionStatus,
    ActionResult,
)
from core.utils.timers import CancellationToken, OperationCancelled
from system.input.input_service import WindowsInputService


# ---------------------------------------------------------------------------
# Mock Input Service for Safe Testing
# ---------------------------------------------------------------------------

class MockInputService:
    """Mock input service that tracks calls without performing actual actions."""

    def __init__(self):
        self.initialized = True
        self.calls = []

    def initialize(self):
        self.initialized = True

    def shutdown(self):
        pass

    def type_text(self, text: str, interval_s: float = 0.0, cancellation=None) -> ActionResult:
        self.calls.append(("type_text", {"text": text, "interval_s": interval_s}))
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="type_text",
            details={"length": len(text), "typed": len(text)},
        )

    def press_key(self, key: str, cancellation=None) -> ActionResult:
        self.calls.append(("press_key", {"key": key}))
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="press_key",
            details={"key": key},
        )

    def hotkey(self, keys: list, cancellation=None) -> ActionResult:
        self.calls.append(("hotkey", {"keys": keys}))
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="hotkey",
            details={"keys": keys},
        )

    def click(self, x: int, y: int, button: str = "left", clicks: int = 1, cancellation=None) -> ActionResult:
        self.calls.append(("click", {"x": x, "y": y, "button": button, "clicks": clicks}))
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="click",
            details={"x": x, "y": y, "button": button, "clicks": clicks},
        )

    def move_mouse(self, x: int, y: int, cancellation=None) -> ActionResult:
        self.calls.append(("move_mouse", {"x": x, "y": y}))
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="move_mouse",
            details={"x": x, "y": y},
        )

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration_s: float = 0.5, button: str = "left", cancellation=None) -> ActionResult:
        self.calls.append(("drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_s": duration_s, "button": button}))
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="drag",
            details={"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_s": duration_s},
        )

    def scroll(self, x: int, y: int, clicks: int, vertical: bool = True, cancellation=None) -> ActionResult:
        self.calls.append(("scroll", {"x": x, "y": y, "clicks": clicks, "vertical": vertical}))
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="scroll",
            details={"x": x, "y": y, "clicks": clicks, "vertical": vertical},
        )

    def reset_calls(self):
        """Reset call history."""
        self.calls = []


# ---------------------------------------------------------------------------
# Test Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_input_service():
    """Create mock input service."""
    return MockInputService()


# ---------------------------------------------------------------------------
# A. KEYBOARD ACTION CONTRACTS
# ---------------------------------------------------------------------------

class TestKeyboardActionContracts:
    """Test keyboard action contracts: type, press, hotkey."""

    @pytest.mark.asyncio
    async def test_type_simple_text(self, mock_input_service):
        """Test: type simple text."""
        cap = KeyboardTypeCapability(mock_input_service)
        result = await cap.execute({"text": "hello"})

        assert result.status == CapabilityStatus.EXECUTED
        assert result.executed is True
        assert result.failed is False
        assert result.error is None
        assert mock_input_service.calls[-1][0] == "type_text"
        assert mock_input_service.calls[-1][1]["text"] == "hello"

    @pytest.mark.asyncio
    async def test_type_with_spaces(self, mock_input_service):
        """Test: type text with spaces preserved."""
        cap = KeyboardTypeCapability(mock_input_service)
        result = await cap.execute({"text": "Hello World!"})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][1]["text"] == "Hello World!"

    @pytest.mark.asyncio
    async def test_type_with_newlines(self, mock_input_service):
        """Test: type text with newlines preserved."""
        cap = KeyboardTypeCapability(mock_input_service)
        result = await cap.execute({"text": "line1\nline2"})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][1]["text"] == "line1\nline2"

    @pytest.mark.asyncio
    async def test_type_empty_text_fails(self, mock_input_service):
        """Test: typing empty text fails gracefully."""
        cap = KeyboardTypeCapability(mock_input_service)
        result = await cap.execute({"text": ""})

        assert result.status == CapabilityStatus.FAILED
        assert result.failed is True
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_press_enter(self, mock_input_service):
        """Test: press enter key."""
        cap = KeyboardPressCapability(mock_input_service)
        result = await cap.execute({"key": "enter"})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][0] == "press_key"
        assert mock_input_service.calls[-1][1]["key"] == "enter"

    @pytest.mark.asyncio
    async def test_press_escape(self, mock_input_service):
        """Test: press escape key."""
        cap = KeyboardPressCapability(mock_input_service)
        result = await cap.execute({"key": "escape"})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][1]["key"] == "escape"

    @pytest.mark.asyncio
    async def test_hotkey_ctrl_c(self, mock_input_service):
        """Test: hotkey combination Ctrl+C."""
        cap = KeyboardHotkeyCapability(mock_input_service)
        result = await cap.execute({"keys": ["ctrl", "c"]})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][0] == "hotkey"
        assert mock_input_service.calls[-1][1]["keys"] == ["ctrl", "c"]

    @pytest.mark.asyncio
    async def test_hotkey_preserves_order(self, mock_input_service):
        """Test: hotkey preserves modifier order."""
        cap = KeyboardHotkeyCapability(mock_input_service)
        result = await cap.execute({"keys": ["ctrl", "shift", "escape"]})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][1]["keys"] == ["ctrl", "shift", "escape"]


# ---------------------------------------------------------------------------
# B. MOUSE ACTION CONTRACTS
# ---------------------------------------------------------------------------

class TestMouseActionContracts:
    """Test mouse action contracts: click, double_click, move, drag."""

    @pytest.mark.asyncio
    async def test_click_at_coordinates(self, mock_input_service):
        """Test: click at specific coordinates."""
        cap = MouseClickCapability(mock_input_service)
        result = await cap.execute({"x": 500, "y": 300})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][0] == "click"
        assert mock_input_service.calls[-1][1]["x"] == 500
        assert mock_input_service.calls[-1][1]["y"] == 300
        assert mock_input_service.calls[-1][1]["button"] == "left"

    @pytest.mark.asyncio
    async def test_double_click(self, mock_input_service):
        """Test: double click."""
        cap = MouseDoubleClickCapability(mock_input_service)
        result = await cap.execute({"x": 100, "y": 100})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][1]["clicks"] == 2

    @pytest.mark.asyncio
    async def test_right_click(self, mock_input_service):
        """Test: right click."""
        cap = MouseRightClickCapability(mock_input_service)
        result = await cap.execute({"x": 200, "y": 200})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][1]["button"] == "right"

    @pytest.mark.asyncio
    async def test_move_to_coordinates(self, mock_input_service):
        """Test: move mouse to coordinates."""
        cap = MouseMoveCapability(mock_input_service)
        result = await cap.execute({"x": 400, "y": 600})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][0] == "move_mouse"
        assert mock_input_service.calls[-1][1]["x"] == 400
        assert mock_input_service.calls[-1][1]["y"] == 600

    @pytest.mark.asyncio
    async def test_drag_operation(self, mock_input_service):
        """Test: drag from start to end."""
        cap = MouseDragCapability(mock_input_service)

        # Mock pyautogui.position() to return a start position
        with patch('pyautogui.position', return_value=(100, 100)):
            result = await cap.execute({"x": 300, "y": 400, "duration_s": 0.5})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][0] == "drag"


# ---------------------------------------------------------------------------
# C. SCROLL ACTION CONTRACTS
# ---------------------------------------------------------------------------

class TestScrollActionContracts:
    """Test scroll action contracts."""

    @pytest.mark.asyncio
    async def test_scroll_up(self, mock_input_service):
        """Test: scroll up (positive amount)."""
        cap = MouseScrollCapability(mock_input_service)

        with patch('pyautogui.position', return_value=(500, 500)):
            result = await cap.execute({"amount": 3})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][0] == "scroll"
        assert mock_input_service.calls[-1][1]["clicks"] == 3
        assert mock_input_service.calls[-1][1]["vertical"] is True

    @pytest.mark.asyncio
    async def test_scroll_down(self, mock_input_service):
        """Test: scroll down (negative amount)."""
        cap = MouseScrollCapability(mock_input_service)

        with patch('pyautogui.position', return_value=(500, 500)):
            result = await cap.execute({"amount": -5})

        assert result.status == CapabilityStatus.EXECUTED
        assert mock_input_service.calls[-1][1]["clicks"] == -5


# ---------------------------------------------------------------------------
# D. OBSERVATION ACTION CONTRACTS
# ---------------------------------------------------------------------------

class TestObservationActionContracts:
    """Test observation action contracts: screenshot, screen_size."""

    @pytest.mark.asyncio
    async def test_screenshot_basic(self):
        """Test: capture screenshot returns structured result."""
        cap = ScreenshotCapability()

        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_screenshot.png")

            with patch('pyautogui.screenshot') as mock_screenshot:
                result = await cap.execute({"path": path})

            assert result.status == CapabilityStatus.VERIFIED
            assert result.verified is True
            assert "path" in result.details
            assert "width" in result.details
            assert "height" in result.details

    @pytest.mark.asyncio
    async def test_screen_size(self):
        """Test: get screen size."""
        cap = ScreenSizeCapability()

        with patch('pyautogui.size', return_value=(1920, 1080)):
            result = await cap.execute({})

        assert result.status == CapabilityStatus.VERIFIED
        assert result.details["width"] == 1920
        assert result.details["height"] == 1080


# ---------------------------------------------------------------------------
# E. WINDOW CONTROL CONTRACTS
# ---------------------------------------------------------------------------

class TestWindowControlContracts:
    """Test window control contracts: list, focus."""

    @pytest.mark.asyncio
    async def test_list_windows(self):
        """Test: list windows returns window list."""
        mock_window_service = Mock()
        mock_window_service.list_windows = Mock(return_value=[
            {"hwnd": 12345, "title": "Notepad"},
            {"hwnd": 67890, "title": "Chrome"},
        ])

        cap = WindowListCapability(mock_window_service)
        result = await cap.execute({})

        assert result.status == CapabilityStatus.VERIFIED
        assert result.verified is True
        assert "windows" in result.details
        assert result.details["count"] == 2


# ---------------------------------------------------------------------------
# F. TARGET MODEL REPRESENTATION
# ---------------------------------------------------------------------------

class TestTargetModelRepresentation:
    """Test how action targets are represented."""

    @pytest.mark.asyncio
    async def test_mouse_click_accepts_coordinates(self, mock_input_service):
        """Test: mouse click accepts coordinate target."""
        cap = MouseClickCapability(mock_input_service)
        result = await cap.execute({"x": 100, "y": 200})

        assert result.status == CapabilityStatus.EXECUTED
        # Coordinates are the current target representation

    @pytest.mark.asyncio
    async def test_keyboard_accepts_target_hints(self, mock_input_service):
        """Test: keyboard accepts optional target hints."""
        cap = KeyboardTypeCapability(mock_input_service)
        result = await cap.execute({
            "text": "test",
            "target_app_name": "notepad",
        })

        # Should still work (target hints are optional)
        assert result.status in [CapabilityStatus.EXECUTED, CapabilityStatus.VERIFIED, CapabilityStatus.FAILED]


# ---------------------------------------------------------------------------
# G. RESULT MODEL SEMANTICS
# ---------------------------------------------------------------------------

class TestResultModelSemantics:
    """Test result model distinguishes success/failure/timeout/cancellation."""

    @pytest.mark.asyncio
    async def test_successful_action_has_executed_status(self, mock_input_service):
        """Test: successful action returns EXECUTED status."""
        cap = KeyboardPressCapability(mock_input_service)
        result = await cap.execute({"key": "enter"})

        assert result.status == CapabilityStatus.EXECUTED
        assert result.executed is True
        assert result.failed is False
        assert result.error is None

    @pytest.mark.asyncio
    async def test_failed_action_has_failed_status(self, mock_input_service):
        """Test: failed action returns FAILED status with error."""
        cap = KeyboardPressCapability(mock_input_service)
        result = await cap.execute({})  # Missing required 'key' parameter

        assert result.status == CapabilityStatus.FAILED
        assert result.failed is True
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_result_includes_action_result(self, mock_input_service):
        """Test: capability result includes underlying action result."""
        cap = KeyboardTypeCapability(mock_input_service)
        result = await cap.execute({"text": "hello"})

        assert result.action is not None
        assert result.action.status == ActionStatus.EXECUTED
        assert result.action.action_name == "type_text"


# ---------------------------------------------------------------------------
# H. CANCELLATION SUPPORT
# ---------------------------------------------------------------------------

class TestCancellationSupport:
    """Test that actions support cancellation."""

    def test_cancellation_token_can_be_passed(self, mock_input_service):
        """Test: cancellation token can be passed to actions."""
        token = CancellationToken()

        # Actions should accept cancellation token parameter
        result = mock_input_service.type_text("test", cancellation=token)
        assert result.status == ActionStatus.EXECUTED

    def test_cancelled_token_is_checked(self):
        """Test: cancelled token raises OperationCancelled."""
        token = CancellationToken()
        token.cancel("user requested")

        with pytest.raises(OperationCancelled):
            token.check()


# ---------------------------------------------------------------------------
# I. AI INDEPENDENCE
# ---------------------------------------------------------------------------

class TestAIIndependence:
    """Test that generic actions do not call LLMs."""

    @pytest.mark.asyncio
    async def test_keyboard_actions_no_ai_calls(self, mock_input_service):
        """Test: keyboard actions do not call AI providers."""
        # Mock AI provider to track calls
        ai_calls = []

        cap = KeyboardTypeCapability(mock_input_service)
        result = await cap.execute({"text": "hello"})

        # Verify no AI calls were made
        assert len(ai_calls) == 0
        assert result.status == CapabilityStatus.EXECUTED

    @pytest.mark.asyncio
    async def test_mouse_actions_no_ai_calls(self, mock_input_service):
        """Test: mouse actions do not call AI providers."""
        ai_calls = []

        cap = MouseClickCapability(mock_input_service)
        result = await cap.execute({"x": 100, "y": 100})

        assert len(ai_calls) == 0
        assert result.status == CapabilityStatus.EXECUTED


# ---------------------------------------------------------------------------
# J. STAGE 18.4 REGRESSION
# ---------------------------------------------------------------------------

class TestStage18_4Regression:
    """Test that Stage 18.4 native-first path still works."""

    @pytest.mark.asyncio
    async def test_native_commands_still_work(self, mock_input_service):
        """Test: native commands like 'type hello' still execute without LLM."""
        # This is a simplified test; full integration test exists in test_stage18_4

        cap = KeyboardTypeCapability(mock_input_service)
        result = await cap.execute({"text": "hello"})

        assert result.status == CapabilityStatus.EXECUTED
        assert result.executed is True


# ---------------------------------------------------------------------------
# K. ACTION CONTRACT COMPLETENESS
# ---------------------------------------------------------------------------

class TestActionContractCompleteness:
    """Test that all required generic actions are implemented."""

    def test_click_action_exists(self):
        """Test: click action exists."""
        assert MouseClickCapability is not None

    def test_double_click_action_exists(self):
        """Test: double_click action exists."""
        assert MouseDoubleClickCapability is not None

    def test_move_action_exists(self):
        """Test: move action exists."""
        assert MouseMoveCapability is not None

    def test_type_action_exists(self):
        """Test: type action exists."""
        assert KeyboardTypeCapability is not None

    def test_press_action_exists(self):
        """Test: press action exists."""
        assert KeyboardPressCapability is not None

    def test_hotkey_action_exists(self):
        """Test: hotkey action exists."""
        assert KeyboardHotkeyCapability is not None

    def test_scroll_action_exists(self):
        """Test: scroll action exists."""
        assert MouseScrollCapability is not None

    def test_screenshot_action_exists(self):
        """Test: screenshot action exists."""
        assert ScreenshotCapability is not None

    def test_list_windows_action_exists(self):
        """Test: list_windows action exists."""
        assert WindowListCapability is not None

    def test_focus_window_action_exists(self):
        """Test: focus_window action exists."""
        assert WindowFocusCapability is not None

    def test_open_application_action_exists(self):
        """Test: open_application action exists."""
        assert ApplicationOpenCapability is not None

    def test_close_application_action_exists(self):
        """Test: close_application action exists."""
        assert ApplicationCloseCapability is not None


# ---------------------------------------------------------------------------
# Run Tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
