import pytest
from core.capabilities.desktop_keyboard import (
    KeyboardTypeCapability, 
    KeyboardPressCapability, 
    KeyboardHotkeyCapability
)
from core.results import CapabilityStatus
from system.input.input_service import WindowsInputService

@pytest.fixture
def input_service():
    # Park cursor away from the FAILSAFE corner (0,0) so a stray
    # mouse move from a previous test does not block this one.
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
async def test_keyboard_press_capability(input_service):
    cap = KeyboardPressCapability(input_service)
    result = await cap.execute({"key": "escape"})
    
    assert result.status == CapabilityStatus.EXECUTED
    assert result.error is None
    assert result.details["key"] == "escape"

@pytest.mark.asyncio
async def test_keyboard_hotkey_capability(input_service):
    cap = KeyboardHotkeyCapability(input_service)
    result = await cap.execute({"keys": ["ctrl", "shift", "escape"]})
    
    assert result.status == CapabilityStatus.EXECUTED
    assert result.error is None
    assert result.details["keys"] == ["ctrl", "shift", "escape"]

@pytest.mark.asyncio
async def test_keyboard_press_missing_key(input_service):
    cap = KeyboardPressCapability(input_service)
    result = await cap.execute({})
    
    assert result.status == CapabilityStatus.FAILED
    assert result.failed is True
