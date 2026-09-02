"""
Omnix V6 — Phase 6C ObservationProvider tests.

These tests pin the contract of the :class:`ObservationProvider`
abstraction: a StepResult plus a PlanStep should produce a
DERIVED Observation with deterministic confidence, and the
provider must never crash on missing capability_result.
"""

from __future__ import annotations

import time
import pytest

from core.orchestration import (
    Agent,
    AgentPolicy,
    AgentState,
    CapabilityResultObservationProvider,
    ExecutionContext,
    ExecutionOutcome,
    ExpectedEffect,
    Goal,
    Intent,
    IntentKind,
    Observation,
    ObservationProvider,
    ObservationSource,
    Plan,
    PlanStep,
    StepResult,
    StepState,
    make_blank_execution_result,
)
from core.results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
    VerificationStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cap_verified(step_id: str = "s1") -> CapabilityResult:
    return CapabilityResult(
        capability_name="test.echo",
        status=CapabilityStatus.VERIFIED,
        attempted=True,
        executed=True,
        verified=True,
        failed=False,
        action=ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="test.echo",
            details={"echoed": "hello"},
        ),
        verification=VerificationResult(
            status=VerificationStatus.VERIFIED,
            check_name="echo_ok",
            expected="hello",
            actual="hello",
        ),
    )


def _cap_failed(step_id: str = "s1") -> CapabilityResult:
    return CapabilityResult(
        capability_name="test.echo",
        status=CapabilityStatus.FAILED,
        attempted=True,
        executed=True,
        verified=False,
        failed=True,
        action=ActionResult(
            status=ActionStatus.FAILED,
            action_name="test.echo",
            details={"reason": "x"},
        ),
        error=Exception("x"),
    )


def _cap_attempted_only() -> CapabilityResult:
    return CapabilityResult(
        capability_name="test.echo",
        status=CapabilityStatus.ATTEMPTED,
        attempted=True,
        executed=False,
        verified=False,
        failed=False,
    )


def _step(step_id: str, capability_name: str = "test.echo") -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description=f"step {step_id}",
        capability_name=capability_name,
        parameters={"text": "hello"},
        expected_effect=ExpectedEffect(check_name="echo_ok", expected="hello"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCapabilityResultObservationProvider:
    def test_provider_has_name(self):
        p = CapabilityResultObservationProvider()
        assert p.name

    def test_verified_yields_high_confidence(self):
        p = CapabilityResultObservationProvider()
        obs = p.observe(_step("s1"), StepResult(
            step_id="s1", capability_name="test.echo",
            status=StepState.SUCCEEDED, capability_result=_cap_verified("s1"),
        ))
        assert obs is not None
        assert obs.source is ObservationSource.DERIVED
        assert obs.confidence == 1.0

    def test_failed_yields_zero_confidence(self):
        p = CapabilityResultObservationProvider()
        obs = p.observe(_step("s1"), StepResult(
            step_id="s1", capability_name="test.echo",
            status=StepState.FAILED, capability_result=_cap_failed("s1"),
            error="x",
        ))
        assert obs is not None
        assert obs.source is ObservationSource.DERIVED
        assert obs.confidence == 0.0

    def test_executed_no_verification_uses_half_confidence(self):
        # EXECUTED ≠ VERIFIED invariant
        p = CapabilityResultObservationProvider()
        cap = _cap_attempted_only()  # executed=False, verified=False
        obs = p.observe(_step("s1"), StepResult(
            step_id="s1", capability_name="test.echo",
            status=StepState.SUCCEEDED, capability_result=cap,
        ))
        assert obs is not None
        assert 0.0 < obs.confidence < 1.0

    def test_no_capability_result_returns_observation_with_lower_confidence(self):
        p = CapabilityResultObservationProvider()
        obs = p.observe(_step("s1"), StepResult(
            step_id="s1", capability_name="test.echo",
            status=StepState.SUCCEEDED, capability_result=None,
        ))
        # Provider may return None or a low-confidence observation.
        # If returned, it should reflect that nothing was verified.
        if obs is not None:
            assert obs.confidence < 1.0

    def test_observation_includes_capability_status(self):
        p = CapabilityResultObservationProvider()
        obs = p.observe(_step("s1"), StepResult(
            step_id="s1", capability_name="test.echo",
            status=StepState.SUCCEEDED, capability_result=_cap_verified("s1"),
        ))
        assert obs is not None
        # The provider exposes the capability status somewhere in data/metadata.
        text = str(obs.data) + str(obs.metadata)
        assert "verified" in text.lower() or "succeeded" in text.lower()

    def test_provider_implements_protocol(self):
        p = CapabilityResultObservationProvider()
        assert isinstance(p, ObservationProvider)


class TestObservationProviderContract:
    def test_provider_does_not_raise_on_minimal_input(self):
        # Empty / minimal inputs should be tolerated.
        p = CapabilityResultObservationProvider()
        minimal_step = PlanStep(
            step_id="s1", description="x",
            capability_name="test.echo", parameters={},
        )
        minimal_sr = StepResult(
            step_id="s1", capability_name="test.echo",
            status=StepState.SUCCEEDED,
        )
        # Must not raise.
        obs = p.observe(minimal_step, minimal_sr)
        # May return None or a low-confidence observation.

    def test_provider_observation_has_subject(self):
        p = CapabilityResultObservationProvider()
        obs = p.observe(_step("s1"), StepResult(
            step_id="s1", capability_name="test.echo",
            status=StepState.SUCCEEDED, capability_result=_cap_verified("s1"),
        ))
        if obs is not None:
            # subject is the step_id typically
            assert obs.subject or obs.data
