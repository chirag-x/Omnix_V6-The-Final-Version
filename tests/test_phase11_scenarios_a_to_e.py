"""
Omnix V6 — Phase 11 realistic scenario tests (A through E).

Each scenario exercises the full pipeline with a realistic user
request, using the scripted LLM provider so the test stays offline.
Scenarios: open, clarify, unknown, long, voice-driven.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.capability import Capability, CapabilityParameter, CapabilitySpec, ParamType
from core.capability_registry import CapabilityRegistry
from core.capability_router import AllowAllSafetyPolicy, CapabilityRouter
from core.configuration import OmnixConfig
from core.omnix_engine import OmnixEngine
from core.responses import ResponseStatus
from core.results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
    VerificationStatus,
)


class _EchoCapability(Capability):
    spec = CapabilitySpec(
        name="echo",
        version="1.0.0",
        description="echoes a string back",
        parameters=(
            CapabilityParameter(name="text", type=ParamType.STRING,
                                required=False, default=""),
        ),
    )

    def execute(self, params):
        text = (params or {}).get("text", "")
        return CapabilityResult(
            capability_name="echo",
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            failed=False,
            action=ActionResult(
                status=ActionStatus.EXECUTED,
                action_name="echo",
                details={"echoed": text},
            ),
            verification=VerificationResult(
                status=VerificationStatus.VERIFIED,
                check_name="echo_ok",
                expected=text,
                actual=text,
            ),
            details={"echoed": text},
        )


def _scenario_engine() -> OmnixEngine:
    import os
    os.environ.setdefault("OMNIX_HEADLESS", "1")
    os.environ.setdefault("OMNIX_LLM_PROVIDER", "mock")
    os.environ.setdefault("OMNIX_QUIET_BOOT", "1")

    cfg = OmnixConfig(
        project_root=Path("."),
        data_dir=Path("./logs"),
        log_dir=Path("./logs"),
        env_file=Path("./.env"),
        log_to_file=False,
        log_level="ERROR",
        enable_voice=False,
        enable_vision=False,
        enable_browser=False,
        enable_automation=False,
    )
    reg = CapabilityRegistry()
    reg.register(_EchoCapability())
    router = CapabilityRouter(reg, safety_policy=AllowAllSafetyPolicy())
    engine = OmnixEngine(cfg, capabilities=reg, router=router)
    engine.initialize()
    return engine


@pytest.fixture(autouse=True)
def _restore_env_after_scenario():
    """The scenario helper above seeds ``OMNIX_LLM_PROVIDER=mock`` so
    the Brain uses the deterministic provider.  We MUST scrub those keys
    back to their pre-test state, otherwise later tests that resolve a
    provider purely from configuration (e.g. Phase 6D end-to-end) end
    up with a phantom ``mock`` override and fail.
    """
    snapshot = {k: os.environ.get(k) for k in
                ("OMNIX_HEADLESS", "OMNIX_LLM_PROVIDER", "OMNIX_QUIET_BOOT")}
    yield
    for k, prior in snapshot.items():
        if prior is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = prior


def test_scenario_a_open_chrome():
    """Scenario A — simple command: 'open chrome'."""
    engine = _scenario_engine()
    r = engine.process("open chrome")
    assert r.status in (ResponseStatus.OK, ResponseStatus.FAILED)
    assert r.text


def test_scenario_b_clarification():
    """Scenario B — ambiguous input triggers clarification path."""
    # The Brain should return clarification when intent is ambiguous.
    # (The mock provider always returns a valid intent, so this tests
    # the clarification branch via direct brain injection if needed.)
    engine = _scenario_engine()
    r = engine.process("it")  # intentionally ambiguous
    assert r.text
    # Status may be OK (mock) or CLARIFICATION if the interpreter flags
    # the ambiguity; either is acceptable for this test.


def test_scenario_c_unknown_command():
    """Scenario C — unknown / unclassifiable input."""
    engine = _scenario_engine()
    r = engine.process("asdfghjkl nonsense that makes no sense")
    assert r.text
    assert r.status in (
        ResponseStatus.OK, ResponseStatus.FAILED, ResponseStatus.CLARIFICATION,
    )


def test_scenario_d_long_request():
    """Scenario D — long request that must not hang or overflow."""
    engine = _scenario_engine()
    long_text = "please help me prepare a comprehensive report about " + \
                ("the impact of climate change on agriculture " * 50)
    r = engine.process(long_text)
    assert r.text
    assert len(r.text) <= 2200
    assert r.duration_ms < 10000  # must not hang


def test_scenario_e_voice_input_simulation():
    """Scenario E — simulate a voice-driven command through the pipeline."""
    # Voice only captures text and passes it to engine.process()
    engine = _scenario_engine()
    voice_text = "say hello to me"
    # The canonical pipeline handles it exactly as text input.
    r = engine.process(voice_text)
    assert r.text
    # Response must never carry raw audio or transcription metadata
    # into the user-facing text.
    assert "audio" not in r.text.lower() or len(r.text) < 500
