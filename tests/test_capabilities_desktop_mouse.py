import pytest
from core.capabilities.desktop_mouse import (
    MouseMoveCapability, 
    MouseClickCapability, 
    MouseDoubleClickCapability,
    MouseRightClickCapability, 
    MouseScrollCapability,
    MouseDragCapability
)
from core.results import CapabilityStatus
from system.input.input_service import WindowsInputService

@pytest.fixture
def input_service():
    # Park the cursor away from the FAILSAFE corner (0,0) so any
    # subsequent test that calls moveTo / click can run safely.
    # We briefly disable FAILSAFE for this single move.
    import pyautogui
    was_failsafe = pyautogui.FAILSAFE
    try:
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(200, 200, duration=0.0)
    finally:
        pyautogui.FAILSAFE = was_failsafe
    service = WindowsInputService()
    if not getattr(service, 'initialized', False):
        service.initialize()
    yield service
    service.shutdown()

@pytest.mark.asyncio
async def test_mouse_move_capability(input_service):
    cap = MouseMoveCapability(input_service)
    result = await cap.execute({"x": 100, "y": 100})
    
    assert result.status == CapabilityStatus.EXECUTED
    assert result.error is None
    assert result.details["x"] == 100
    assert result.details["y"] == 100

@pytest.mark.asyncio
async def test_mouse_click_capability(input_service):
    cap = MouseClickCapability(input_service)
    
    # Test click without moving (current pos)
    result = await cap.execute({})
    assert result.status == CapabilityStatus.EXECUTED
    assert result.error is None
    
    # Test click with moving
    result2 = await cap.execute({"x": 100, "y": 100})
    assert result2.status == CapabilityStatus.EXECUTED
    
    # Test missing param error
    result3 = await cap.execute({"x": 100})
    assert result3.status == CapabilityStatus.FAILED
    assert result3.failed is True

@pytest.mark.asyncio
async def test_mouse_scroll_capability(input_service):
    cap = MouseScrollCapability(input_service)
    
    result = await cap.execute({"amount": 10})
    assert result.status == CapabilityStatus.EXECUTED
    assert result.error is None
