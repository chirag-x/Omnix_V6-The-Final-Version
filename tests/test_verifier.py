"""
Omnix V6 — Phase 6C Verifier tests.

These tests pin the contract of the :class:`DefaultStepVerifier`
and :class:`DefaultGoalVerifier`:
- the EXECUTED ≠ VERIFIED invariant
- the tri-state verdict (exactly one of passed/failed/uncertain true)
- the goal verifier aggregates step verdicts correctly
"""

from __future__ import annotations

import pytest

from core.orchestration import (
    DefaultGoalVerifier,
    DefaultStepVerifier,
    ExpectedEffect,
    Observation,
    ObservationSource,
    VerificationVerdict,
    failed_verdict,
    passed_verdict,
    uncertain_verdict,
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
# Verdict construction
# ---------------------------------------------------------------------------

class TestVerdictHelpers:
    def test_passed_verdict_sets_passed(self):
        v = passed_verdict(check_name="x", expected="e", actual="a")
        assert v.passed is True
        assert v.failed is False
        assert v.uncertain is False
        assert v.check_name == "x"
        assert v.expected == "e"
        assert v.actual == "a"

    def test_failed_verdict_sets_failed(self):
        v = failed_verdict(check_name="x", expected="e", actual="a", reason="r")
        assert v.passed is False
        assert v.failed is True
        assert v.uncertain is False
        assert v.reason == "r"

    def test_uncertain_verdict_sets_uncertain(self):
        v = uncertain_verdict(check_name="x", expected="e", actual=None, reason="r")
        assert v.passed is False
        assert v.failed is False
        assert v.uncertain is True
        assert v.reason == "r"

    def test_verdict_is_exactly_one_of_three(self):
        for v in (
            passed_verdict(check_name="x", expected="e", actual="a"),
            failed_verdict(check_name="x", expected="e", actual="a"),
            uncertain_verdict(check_name="x", expected="e", actual="a"),
        ):
            n = sum(1 for x in (v.passed, v.failed, v.uncertain) if x)
            assert n == 1, f"verdict {v!r} is not exactly one-of-three"


# ---------------------------------------------------------------------------
# DefaultStepVerifier
# ---------------------------------------------------------------------------

class TestDefaultStepVerifier:
    def _cap_verified(self, expected="hello", actual="hello") -> CapabilityResult:
        return CapabilityResult(
            capability_name="test.echo",
            status=CapabilityStatus.VERIFIED,
            attempted=True, executed=True, verified=True, failed=False,
            action=ActionResult(status=ActionStatus.EXECUTED,
                                action_name="test.echo", details={}),
            verification=VerificationResult(
                status=VerificationStatus.VERIFIED,
                check_name="echo_ok", expected=expected, actual=actual,
            ),
        )

    def _cap_failed(self) -> CapabilityResult:
        return CapabilityResult(
            capability_name="test.echo",
            status=CapabilityStatus.FAILED,
            attempted=True, executed=True, verified=False, failed=True,
            action=ActionResult(status=ActionStatus.FAILED,
                                action_name="test.echo", details={}),
            error=Exception("x"),
        )

    def _cap_attempted_only(self) -> CapabilityResult:
        return CapabilityResult(
            capability_name="test.echo",
            status=CapabilityStatus.ATTEMPTED,
            attempted=True, executed=False, verified=False, failed=False,
        )

    def test_verifier_has_name(self):
        v = DefaultStepVerifier()
        assert v.name

    def test_verified_capability_passes(self):
        v = DefaultStepVerifier()
        cap = self._cap_verified("hello", "hello")
        # The verifier reads the data shape the CapabilityResultObservationProvider
        # emits (status/capability_status/verification), so build the observation
        # in that shape.
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={
                "status": "succeeded",
                "capability_status": "verified",
                "verification": {
                    "check_name": "echo_ok",
                    "status": "verified",
                    "expected": "hello",
                    "actual": "hello",
                },
            },
            confidence=1.0,
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="echo_ok", expected="hello"),
            observation=obs,
        )
        assert verdict.passed is True
        assert verdict.failed is False

    def test_failed_capability_fails(self):
        v = DefaultStepVerifier()
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={
                "status": "failed",
                "capability_status": "failed",
                "details": {"reason": "x"},
            },
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="echo_ok", expected="hello"),
            observation=obs,
        )
        assert verdict.failed is True
        assert verdict.passed is False

    def test_clean_succeeded_without_verification_is_uncertain(self):
        # EXECUTED ≠ VERIFIED invariant
        v = DefaultStepVerifier()
        # Clean succeeded step, capability status "executed" (not "verified"),
        # and no explicit verification block.
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={
                "status": "succeeded",
                "capability_status": "executed",
                "verification": None,
            },
            confidence=0.5,
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="echo_ok", expected="hello"),
            observation=obs,
        )
        # Conservative default: no explicit verification → UNCERTAIN
        assert verdict.uncertain is True
        assert verdict.passed is False
        assert verdict.failed is False

    def test_no_observation_is_uncertain(self):
        v = DefaultStepVerifier()
        verdict = v.verify(
            effect=ExpectedEffect(check_name="echo_ok", expected="hello"),
            observation=None,
        )
        assert verdict.uncertain is True
        assert verdict.passed is False
        assert verdict.failed is False

    def test_verifier_does_not_raise_on_malformed_observation(self):
        v = DefaultStepVerifier()
        # A bare dict (not a CapabilityResult) inside observation data
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={"some": "data"},
        )
        # Must not raise.
        verdict = v.verify(
            effect=ExpectedEffect(check_name="echo_ok", expected="hello"),
            observation=obs,
        )
        assert verdict is not None


# ---------------------------------------------------------------------------
# DefaultGoalVerifier
# ---------------------------------------------------------------------------

class TestDefaultGoalVerifier:
    def test_verifier_has_name(self):
        v = DefaultGoalVerifier()
        assert v.name

    def test_no_step_verdicts_yields_uncertain(self):
        v = DefaultGoalVerifier()
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={"step_verdicts": []},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="goal", expected=[]),
            observation=obs,
        )
        # No step evidence → UNCERTAIN
        assert verdict.uncertain is True

    def test_all_passed_yields_passed(self):
        v = DefaultGoalVerifier()
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={"step_verdicts": [
                {"step_id": "s1", "status": "passed"},
                {"step_id": "s2", "status": "passed"},
            ]},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="goal", expected=[]),
            observation=obs,
        )
        assert verdict.passed is True
        assert verdict.failed is False

    def test_any_failed_yields_failed(self):
        v = DefaultGoalVerifier()
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={"step_verdicts": [
                {"step_id": "s1", "status": "passed"},
                {"step_id": "s2", "status": "failed", "reason": "x"},
            ]},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="goal", expected=[]),
            observation=obs,
        )
        assert verdict.failed is True
        assert verdict.passed is False

    def test_uncertain_steps_yield_uncertain(self):
        v = DefaultGoalVerifier()
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={"step_verdicts": [
                {"step_id": "s1", "status": "passed"},
                {"step_id": "s2", "status": "uncertain", "reason": "?"},
            ]},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="goal", expected=[]),
            observation=obs,
        )
        assert verdict.uncertain is True


# ---------------------------------------------------------------------------
# Phase 7.1: vision-style before/after verification
# ---------------------------------------------------------------------------

class TestVisionStyleVerification:
    """The EXECUTED ≠ VERIFIED invariant for vision effects.

    Vision never claims ``verified=True`` from a single
    observation.  The verifier takes a *before* observation and
    classifies the structural diff.
    """

    def test_vision_observed_with_change_passes(self):
        v = DefaultStepVerifier()
        after = Observation(
            source=ObservationSource.VISION,
            data={"changed": True, "reason": "target appeared"},
            confidence=0.9,
        )
        before = Observation(
            source=ObservationSource.VISION,
            data={"changed": None, "reason": "missing observation"},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="vision_observed", expected="x"),
            observation=after,
            before_observation=before,
        )
        assert verdict.passed is True

    def test_vision_observed_without_change_fails(self):
        v = DefaultStepVerifier()
        after = Observation(
            source=ObservationSource.VISION,
            data={"changed": False, "reason": "no change detected"},
        )
        before = Observation(
            source=ObservationSource.VISION,
            data={"changed": None, "reason": "missing observation"},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="vision_observed", expected="x"),
            observation=after,
            before_observation=before,
        )
        assert verdict.failed is True

    def test_vision_disappeared_passes_on_target_disappeared(self):
        v = DefaultStepVerifier()
        after = Observation(
            source=ObservationSource.VISION,
            data={"changed": True, "reason": "target disappeared"},
        )
        before = Observation(
            source=ObservationSource.VISION,
            data={"changed": None, "reason": "missing observation"},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="vision_disappeared", expected="x"),
            observation=after,
            before_observation=before,
        )
        assert verdict.passed is True

    def test_vision_disappeared_fails_on_other_change(self):
        """If the diff says 'appeared' but we expected 'disappeared', FAILED."""
        v = DefaultStepVerifier()
        after = Observation(
            source=ObservationSource.VISION,
            data={"changed": True, "reason": "target appeared"},
        )
        before = Observation(
            source=ObservationSource.VISION,
            data={"changed": None, "reason": "missing observation"},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="vision_disappeared", expected="x"),
            observation=after,
            before_observation=before,
        )
        assert verdict.failed is True

    def test_vision_with_missing_after_is_uncertain(self):
        v = DefaultStepVerifier()
        verdict = v.verify(
            effect=ExpectedEffect(check_name="vision_observed", expected="x"),
            observation=None,
            before_observation=None,
        )
        assert verdict.uncertain is True

    def test_vision_with_unchanged_diff_fails(self):
        v = DefaultStepVerifier()
        after = Observation(
            source=ObservationSource.VISION,
            data={"changed": False, "reason": "no change detected"},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="vision_observed", expected="x"),
            observation=after,
            before_observation=None,
        )
        assert verdict.failed is True

    def test_vision_with_uncertain_diff_is_uncertain(self):
        v = DefaultStepVerifier()
        after = Observation(
            source=ObservationSource.VISION,
            data={"changed": None, "reason": "missing observation"},
        )
        verdict = v.verify(
            effect=ExpectedEffect(check_name="vision_observed", expected="x"),
            observation=after,
            before_observation=None,
        )
        assert verdict.uncertain is True
