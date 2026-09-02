import pytest
import sys
import os
import subprocess
from core.capabilities.process import RunCommandCapability
from core.results import CapabilityStatus

@pytest.mark.asyncio
async def test_run_command_success():
    cap = RunCommandCapability()
    # Using echo which works on both Windows and Unix
    params = {"command": "echo Hello_Omnix"}
    result = await cap.execute(params)

    assert result.status == CapabilityStatus.VERIFIED
    assert result.error is None
    assert result.details["return_code"] == 0
    assert "Hello_Omnix" in result.details["stdout"]

@pytest.mark.asyncio
async def test_run_command_dangerous_chars_blocked():
    cap = RunCommandCapability()
    params = {"command": "rm -rf / | echo done"}
    result = await cap.execute(params)

    assert result.status == CapabilityStatus.FAILED
    assert result.failed is True
    assert "blocked" in str(result.error).lower() or "metachar" in str(result.error).lower()

@pytest.mark.asyncio
async def test_run_command_invalid_timeout():
    cap = RunCommandCapability()
    params = {"command": "echo", "timeout": -1}
    result = await cap.execute(params)

    assert result.status == CapabilityStatus.FAILED
    assert "positive integer" in str(result.error).lower() or "timeout" in str(result.error).lower()

@pytest.mark.asyncio
async def test_run_command_marked_dangerous():
    cap = RunCommandCapability()
    assert cap.spec.dangerous is True
