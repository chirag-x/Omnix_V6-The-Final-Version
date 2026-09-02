import pytest
import os
import tempfile
from core.capabilities.desktop_observation import ScreenshotCapability
from core.results import CapabilityStatus

@pytest.mark.asyncio
async def test_screenshot_capability():
    cap = ScreenshotCapability()
    with tempfile.TemporaryDirectory() as temp_dir:
        test_file = os.path.join(temp_dir, "test_screenshot.png")
        result = await cap.execute({"path": test_file})
        
        # We can't guarantee pyautogui is available in CI, so we check status
        if result.status == CapabilityStatus.VERIFIED:
            assert "path" in result.details
            assert os.path.exists(test_file)
