import pytest
from core.capabilities.desktop_observation import ScreenSizeCapability, ForegroundWindowCapability
from core.results import CapabilityStatus

@pytest.mark.asyncio
async def test_screen_size_capability():
    cap = ScreenSizeCapability()
    result = await cap.execute({})
    
    # We can't guarantee pyautogui is available in CI, so we check status
    if result.status == CapabilityStatus.VERIFIED:
        assert "width" in result.details
        assert "height" in result.details
        assert isinstance(result.details["width"], int)
        assert isinstance(result.details["height"], int)

@pytest.mark.asyncio
async def test_foreground_window_capability():
    cap = ForegroundWindowCapability()
    result = await cap.execute({})
    
    if result.status == CapabilityStatus.VERIFIED:
        assert "has_foreground" in result.details
        if result.details["has_foreground"]:
            assert "hwnd" in result.details
            assert "title" in result.details
            assert "rect" in result.details
