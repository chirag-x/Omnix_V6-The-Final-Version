"""
Phase 2 — Per-Action Verifier Routing tests.

The Agent must route step verification to a per-capability
:class:`Verifier` (a :class:`VerifierRouter`) instead of the single
:class:`DefaultStepVerifier`.  Each capability gets the verifier
best suited to its observation shape; capabilities that have no
specialized verifier fall back to the default.

Acceptance criteria covered here:
  1. VerifierRouter maps capability_name -> Verifier.
  2. DefaultStepVerifier produces non-trivial confidence
     (1.0 for strict pass, 0.7 for partial, 0.3 for missing data).
  3. Agent.step_verifier is a VerifierRouter when one is provided.
  4. _aggregate_observation no longer wraps v.confidence in a
     defensive try/except (the field is now guaranteed).
"""
from __future__ import annotations

import os
import inspect
import pytest

from core.orchestration import (
    DefaultStepVerifier,
    DefaultGoalVerifier,
    ExpectedEffect,
    Observation,
    ObservationSource,
    VerificationVerdict,
    failed_verdict,
    passed_verdict,
    uncertain_verdict,
)
from core.orchestration.verifier_router import (
    VerifierRouter,
    build_default_router,
)
from core.orchestration.agent import Agent


# ---------------------------------------------------------------------------
# 1. VerifierRouter basic routing
# ---------------------------------------------------------------------------

class TestVerifierRouterBasic:
    def test_router_dispatches_to_registered_verifier(self):
        sentinel = DefaultStepVerifier()
        router = VerifierRouter(default=sentinel)
        # Build a per-capability verifier that we can identify
        class _MyVerifier:
            name = "my"
            def verify(self, **kwargs):
                return passed_verdict(
                    check_name=kwargs["effect"].check_name,
                    reason="custom router",
                )
        my = _MyVerifier()
        router.register("desktop.application.open", my)
        eff = ExpectedEffect(check_name="app_is_running", expected="notepad")
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={"status": "succeeded"},
        )
        v = router.verify(
            capability_name="desktop.application.open",
            effect=eff,
            observation=obs,
        )
        assert v.reason == "custom router"
        assert v.passed is True

    def test_router_falls_back_to_default(self):
        sentinel = DefaultStepVerifier()
        router = VerifierRouter(default=sentinel)
        eff = ExpectedEffect(check_name="app_is_running", expected="notepad")
        # No observation -> default verifier -> UNCERTAIN
        v = router.verify(
            capability_name="desktop.application.open",
            effect=eff,
            observation=None,
        )
        assert v.uncertain is True

    def test_router_unregistered_falls_back(self):
        sentinel = DefaultStepVerifier()
        router = VerifierRouter(default=sentinel)
        eff = ExpectedEffect(check_name="x")
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={"status": "succeeded", "capability_status": "verified",
                  "verification": {"status": "passed"}},
        )
        # Unknown capability uses default
        v = router.verify(
            capability_name="never.registered",
            effect=eff,
            observation=obs,
        )
        assert v.passed is True

    def test_router_register_overrides(self):
        router = VerifierRouter(default=DefaultStepVerifier())
        class _StubV:
            name = "stub"
            def verify(self, **kwargs):
                return failed_verdict(
                    check_name=kwargs["effect"].check_name,
                    reason="forced-fail override",
                )
        router.register("cap.x", _StubV())
        eff = ExpectedEffect(check_name="x")
        v = router.verify(capability_name="cap.x", effect=eff,
                          observation=None)
        assert v.failed is True
        assert v.reason == "forced-fail override"

    def test_router_name(self):
        router = VerifierRouter(default=DefaultStepVerifier())
        assert router.name == "verifier-router"

    def test_router_unregister(self):
        router = VerifierRouter(default=DefaultStepVerifier())
        class _StubV:
            name = "stub"
            def verify(self, **kwargs):
                return passed_verdict(check_name="x")
        router.register("cap.x", _StubV())
        assert "cap.x" in router
        router.unregister("cap.x")
        assert "cap.x" not in router
        # Now should fall back to default
        eff = ExpectedEffect(check_name="x")
        v = router.verify(capability_name="cap.x", effect=eff,
                          observation=None)
        assert v.uncertain is True

    def test_router_registered_count(self):
        router = VerifierRouter(default=DefaultStepVerifier())
        class _StubV:
            name = "stub"
            def verify(self, **kwargs):
                return passed_verdict(check_name="x")
        router.register("a", _StubV())
        router.register("b", _StubV())
        assert len(router) == 2


# ---------------------------------------------------------------------------
# 2. build_default_router covers the 12 desktop capabilities
# ---------------------------------------------------------------------------

class TestBuildDefaultRouter:
    def test_build_default_router_registers_desktop_capabilities(self):
        router = build_default_router(default=DefaultStepVerifier())
        # At minimum the 12 desktop.application.* / desktop.window.* /
        # desktop.input.* capabilities should be present.
        expected_caps = {
            "desktop.application.open",
            "desktop.application.close",
            "desktop.application.focus",
            "desktop.application.is_running",
            "desktop.window.list",
            "desktop.window.focus",
            "desktop.window.close",
            "desktop.input.click",
            "desktop.input.type_text",
            "desktop.input.key",
            "desktop.input.scroll",
            "desktop.screenshot",
        }
        registered = set(router.registered_capabilities())
        missing = expected_caps - registered
        assert not missing, f"router missing capabilities: {missing}"

    def test_build_default_router_default_fallback(self):
        router = build_default_router(default=DefaultStepVerifier())
        assert router.default is not None
        assert router.default.name == "default-step"


# ---------------------------------------------------------------------------
# 3. Per-action confidence: 1.0 strict / 0.7 partial / 0.3 missing
# ---------------------------------------------------------------------------

class TestPerActionConfidence:
    def test_strict_pass_confidence_is_one(self):
        v = DefaultStepVerifier()
        eff = ExpectedEffect(check_name="x")
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={
                "status": "succeeded",
                "capability_status": "verified",
                "verification": {"status": "passed", "actual": "x"},
            },
        )
        verdict = v.verify(effect=eff, observation=obs)
        assert verdict.passed is True
        assert verdict.confidence == 1.0

    def test_uncertain_confidence_below_one(self):
        v = DefaultStepVerifier()
        eff = ExpectedEffect(check_name="x")
        # succeeded but no verification block -> UNCERTAIN
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={"status": "succeeded"},
        )
        verdict = v.verify(effect=eff, observation=obs)
        assert verdict.uncertain is True
        assert 0.0 < verdict.confidence < 1.0

    def test_failed_confidence_high(self):
        # A hard step status="failed" is a *high-confidence*
        # failure — the step did not complete, and the verifier
        # is certain about that.
        v = DefaultStepVerifier()
        eff = ExpectedEffect(check_name="x")
        obs = Observation(
            source=ObservationSource.DERIVED,
            data={"status": "failed"},
        )
        verdict = v.verify(effect=eff, observation=obs)
        assert verdict.failed is True
        assert verdict.confidence >= 0.8

    def test_no_observation_confidence_low(self):
        v = DefaultStepVerifier()
        eff = ExpectedEffect(check_name="x")
        verdict = v.verify(effect=eff, observation=None)
        assert verdict.uncertain is True
        assert 0.0 < verdict.confidence <= 0.3

    def test_vision_diff_pass_high_confidence(self):
        v = DefaultStepVerifier()
        eff = ExpectedEffect(check_name="vision_observed")
        obs = Observation(
            source=ObservationSource.VISION,
            data={"changed": True, "reason": "target appeared"},
        )
        verdict = v.verify(effect=eff, observation=obs)
        assert verdict.passed is True
        assert verdict.confidence >= 0.7

    def test_vision_diff_failed_high_confidence(self):
        # A vision diff that says "no change" is a *high-confidence*
        # failure — the diff is unambiguous, so the verifier is
        # certain that the expected effect did not happen.
        v = DefaultStepVerifier()
        eff = ExpectedEffect(check_name="vision_observed")
        obs = Observation(
            source=ObservationSource.VISION,
            data={"changed": False, "reason": "no change"},
        )
        verdict = v.verify(effect=eff, observation=obs)
        assert verdict.failed is True
        assert verdict.confidence >= 0.8


# ---------------------------------------------------------------------------
# 4. Agent integrates the VerifierRouter
# ---------------------------------------------------------------------------

class TestAgentUsesVerifierRouter:
    def test_agent_default_step_verifier_is_router(self):
        # When no step_verifier is supplied, the Agent should now
        # default to a VerifierRouter (so per-capability routing
        # is always available, not just when the caller passes one).
        class _StubI:
            name = "stub"
            def interpret(self, text, *, context_snapshot=None):
                return None
        class _StubP:
            name = "stub"
            def plan(self, goal, *, intent=None, context_snapshot=None):
                return None
        class _StubE:
            name = "stub"
            def execute(self, context, *, cancellation_token=None):
                return None
        a = Agent(
            interpreter=_StubI(),
            planner=_StubP(),
            plan_executor=_StubE(),
        )
        from core.orchestration.verifier_router import VerifierRouter
        assert isinstance(a.step_verifier, VerifierRouter), (
            "Agent.step_verifier must default to a VerifierRouter "
            "(Phase 2 acceptance criterion)"
        )

    def test_agent_step_verifier_supports_capability_routing(self):
        # When the caller passes a VerifierRouter, the Agent uses it.
        class _StubI:
            name = "stub"
            def interpret(self, text, *, context_snapshot=None):
                return None
        class _StubP:
            name = "stub"
            def plan(self, goal, *, intent=None, context_snapshot=None):
                return None
        class _StubE:
            name = "stub"
            def execute(self, context, *, cancellation_token=None):
                return None
        router = VerifierRouter(default=DefaultStepVerifier())
        a = Agent(
            interpreter=_StubI(),
            planner=_StubP(),
            plan_executor=_StubE(),
            step_verifier=router,
        )
        assert a.step_verifier is router


# ---------------------------------------------------------------------------
# 5. _aggregate_observation no longer uses defensive try/except
# ---------------------------------------------------------------------------

class TestAggregateObservationHonorsConfidence:
    def test_no_defensive_try_except_in_aggregate(self):
        from core.orchestration.agent import Agent
        src = inspect.getsource(Agent._aggregate_observation)
        # The Phase 1 fix added v.confidence; the defensive fallback
        # to confidence=1.0 must be gone.
        assert "except Exception" not in src, (
            "_aggregate_observation still wraps v.confidence in a "
            "try/except.  Remove now that Phase 1 guarantees the field."
        )
        # And the call must reference v.confidence directly
        assert "v.confidence" in src


# ---------------------------------------------------------------------------
# 6. Step-verifier call site passes before_observation
# ---------------------------------------------------------------------------

class TestStepVerifierReceivesBeforeObservation:
    def test_step_verifier_call_passes_before_observation(self):
        from core.orchestration.agent import Agent
        # The new Phase 2 call site must pass before_observation
        # to the per-capability verifier (the keyword is what
        # distinguishes the new from the old).
        assert hasattr(Agent, "_evaluate"), (
            "Agent._evaluate must exist (the step-verifier dispatcher)"
        )
        src = inspect.getsource(Agent._evaluate)
        assert "before_observation" in src, (
            "Agent._evaluate must pass before_observation to the "
            "step verifier (Phase 2 acceptance criterion)"
        )
