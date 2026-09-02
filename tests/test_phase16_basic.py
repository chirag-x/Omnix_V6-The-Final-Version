"""
Basic tests for phase16 multi-step computer-use engine.
These tests can be skipped in headless/CI environments.
"""
from __future__ import annotations

import os
import pytest


@pytest.mark.skipif(
    os.environ.get("OMNIX_HEADLESS") == "1",
    reason="Skipping GUI test in headless mode",
)
def test_smoke_script_imports():
    """Verify the phase16 smoke script can be imported as a module
    and that the test registry is populated.

    We don't instantiate OmnixEngine in unit tests because it requires
    a fully-built config and a real provider.  Real-Windows tests live
    in scripts/phase16_real_windows_smoke.py and are run manually.
    """
    import importlib

    smoke = importlib.import_module("scripts.phase16_real_windows_smoke")

    # Registry should be populated with the 3 success-criteria tasks
    assert hasattr(smoke, "TASKS")
    assert isinstance(smoke.TASKS, dict)
    expected = {"notepad_hello", "chrome_search", "chrome_second_result"}
    assert expected.issubset(set(smoke.TASKS.keys())), (
        f"Missing tests: {expected - set(smoke.TASKS.keys())}"
    )

    # Each test must return a TestRecord on invocation.
    # We allow the test to be skipped (because the engine may not be
    # bootable in a unit-test context without a real LLM provider), but
    # when invoked it must return a structurally-valid TestRecord.
    for name, fn in smoke.TASKS.items():
        rec = fn()
        assert rec.name == name, f"{name}: name mismatch"
        assert isinstance(getattr(rec, "ok", None), bool), (
            f"{name}: .ok must be bool"
        )
        assert isinstance(getattr(rec, "skipped", None), bool), (
            f"{name}: .skipped must be bool"
        )
        assert isinstance(getattr(rec, "error", None), str), (
            f"{name}: .error must be str"
        )

    # Note: actual GUI testing is done via the smoke script on real Windows
    # This unit test just verifies the script is well-formed


@pytest.mark.skipif(
    os.environ.get("OMNIX_HEADLESS") == "1",
    reason="Skipping GUI test in headless mode",
)
def test_agent_has_structured_trace_capability():
    """Verify AgentResult has step_trace field (Part 2 requirement)."""
    from core.orchestration.agent_result import (
        AgentResult,
        StepTraceEntry,
        make_blank_agent_result,
    )

    # Create a blank result
    result = make_blank_agent_result(agent_run_id="test", goal_id="test")
    assert hasattr(result, "step_trace")
    assert isinstance(result.step_trace, tuple)
    assert len(result.step_trace) == 0

    # Verify we can append a trace entry
    entry = StepTraceEntry(
        phase="test_phase",
        message="test message",
        step_id="test_step",
        attempt=1,
        plan_id="test_plan",
        timestamp=1234567890.0,
        details={"test": "value"},
    )
    new_result = result.with_appended_step_trace(entry)
    assert len(new_result.step_trace) == 1
    assert new_result.step_trace[0].message == "test message"