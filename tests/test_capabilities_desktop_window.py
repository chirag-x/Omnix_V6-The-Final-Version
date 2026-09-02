import pytest
from core.capabilities.desktop_window import (
    WindowListCapability, 
    WindowFocusCapability, 
    WindowMinimizeCapability,
    WindowMaximizeCapability,
    WindowRestoreCapability,
    WindowCloseCapability
)
from core.results import CapabilityStatus
from system.windows.window_service import WindowsWindowService

@pytest.fixture
def window_service():
    service = WindowsWindowService()
    if not getattr(service, 'initialized', False):
        service.initialize()
    yield service
    service.shutdown()

@pytest.mark.asyncio
async def test_window_list_capability(window_service):
    cap = WindowListCapability(window_service)
    result = await cap.execute({})
    
    assert result.status == CapabilityStatus.VERIFIED
    assert "windows" in result.details
    assert "count" in result.details
    assert isinstance(result.details["windows"], list)

@pytest.mark.asyncio
async def test_window_focus_capability(window_service):
    cap = WindowFocusCapability(window_service)
    
    # Get list and use first available window
    list_cap = WindowListCapability(window_service)
    list_result = await list_cap.execute({})
    if list_result.details["count"] > 0:
        first_hwnd = list_result.details["windows"][0]["hwnd"]
        result = await cap.execute({"hwnd": first_hwnd})
        # Some windows in test env may deny focus access, but the capability 
        # should still handle this gracefully
        assert result.status in (CapabilityStatus.EXECUTED, CapabilityStatus.FAILED)
        # If it failed, ensure error is reported
        if result.status == CapabilityStatus.FAILED:
            assert result.error is not None

@pytest.mark.asyncio
async def test_window_focus_missing_hwnd(window_service):
    cap = WindowFocusCapability(window_service)
    result = await cap.execute({})
    
    assert result.status == CapabilityStatus.FAILED
    assert result.failed is True
