import pytest
from core.capabilities.desktop_application import (
    ApplicationOpenCapability, 
    ApplicationCloseCapability, 
    ApplicationFocusCapability,
    ApplicationIsRunningCapability
)
from core.results import CapabilityStatus
from system.application.app_service import WindowsApplicationService

@pytest.fixture
def app_service():
    service = WindowsApplicationService()
    if not getattr(service, 'initialized', False):
        service.initialize()
    yield service
    service.shutdown()

@pytest.mark.asyncio
async def test_application_is_running_capability(app_service):
    cap = ApplicationIsRunningCapability(app_service)
    
    # Test with a system app that should exist
    result = await cap.execute({"app_name": "explorer.exe"})
    assert result.status == CapabilityStatus.VERIFIED
    assert "is_running" in result.details

@pytest.mark.asyncio
async def test_application_open_missing_app_name(app_service):
    cap = ApplicationOpenCapability(app_service)
    result = await cap.execute({})
    assert result.status == CapabilityStatus.FAILED
    assert result.failed is True
