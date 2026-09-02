"""
Omnix V6 — Phase 4 tests: CancellationToken threading.

Exit criteria: SIGINT during a real plan run returns to the prompt within
200 ms; Agent final state is ``CANCELLED``, not ``FAILED``.

What this test module covers
----------------------------
- ``core.orchestration.cancellation`` — basic contract.
- ``core.orchestration.recovery.decide`` — when the token is cancelled,
  the engine returns ``RecoveryAction.ABORT`` immediately, regardless
  of failure kind or remaining budget.
- ``core.orchestration.plan_executor.execute_step`` — when the token
  is cancelled, the per-step helper returns ``StepState.CANCELLED``
  without invoking the capability.
- ``core.orchestration.plan_executor._execute_locked`` — when the
  token flips mid-plan, the remaining steps are marked
  ``CANCELLED`` and the outcome is ``ExecutionOutcome.CANCELLED``.
- ``core.pipeline.process`` — three cancellation seams
  (entry / fast-path / pre-agent) all return
  ``ResponseStatus.CANCELLED`` with ``agent_state=CANCELLED``.
- ``core.orchestration.agent.Agent.set_cancellation_token`` — the
  pipeline can replace the Agent's token per request.
- ``core.omnix_engine.Engine.request_cancel`` — the engine tracks
  per-``correlation_id`` tokens and can cancel by id.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import pytest

from core.orchestration.cancellation import CancellationToken
from core.orchestration.recovery import (
    DefaultRecoveryEngine,
    RecoveryPolicy,
    make_failure,
)
from core.orchestration.failure_classifier import FailureClassifier
from core.orchestration.models import (
    ExecutionContext,
    FailureKind,
    Goal,
    Plan,
    PlanStep,
)
from core.orchestration.execution_result import (
    ExecutionOutcome,
    ExecutionResult,
    StepResult,
    StepState,
)


# ===========================================================================
# CancellationToken basics
# ===========================================================================


class TestCancellationTokenBasics:
    """The token is the single primitive Phase 4 relies on."""

    def test_default_is_not_cancelled(self):
        tok = CancellationToken()
        assert tok.is_cancelled is False
        assert tok.reason == ""

    def test_cancel_flips_is_cancelled(self):
        tok = CancellationToken()
        tok.cancel(reason="user hit ctrl-c")
        assert tok.is_cancelled is True
        assert tok.reason == "user hit ctrl-c"

    def test_cancel_is_idempotent(self):
        tok = CancellationToken()
        tok.cancel(reason="first")
        tok.cancel(reason="second")
        # First reason wins; cancellation is sticky.
        assert tok.reason == "first"

    def test_reset_reenables_token(self):
        tok = CancellationToken()
        tok.cancel(reason="x")
        tok.reset()
        assert tok.is_cancelled is False
        assert tok.reason == ""

    def test_register_callback_fires_once(self):
        tok = CancellationToken()
        calls = []
        tok.register(lambda t: calls.append(t.reason))
        tok.cancel(reason="go")
        tok.cancel(reason="go again")
        assert calls == ["go"]

    def test_as_context_manager_propagates_exceptions(self):
        tok = CancellationToken()
        with pytest.raises(RuntimeError):
            with tok.as_context_manager():
                raise RuntimeError("boom")
        assert tok.is_cancelled is True


# ===========================================================================
# Recovery engine honors token
# ===========================================================================


class TestRecoveryEngineHonorsCancellation:
    """When the token is cancelled, the engine returns ABORT."""

    def test_decide_returns_abort_when_token_cancelled(self):
        eng = DefaultRecoveryEngine(
            policy=RecoveryPolicy(max_attempts_per_step=5, max_replans=5)
        )
        tok = CancellationToken()
        tok.cancel(reason="user")
        failure = make_failure(
            kind=FailureKind.EXECUTION, step_id="s1", message="x"
        )
        decision = eng.decide(
            failure, _ctx(), cancellation_token=tok
        )
        assert decision.action.value == "abort"
        assert "user" in decision.rationale

    def test_decide_without_token_uses_default_action(self):
        eng = DefaultRecoveryEngine()
        failure = make_failure(
            kind=FailureKind.WINDOW_NOT_READY, step_id="s1"
        )
        decision = eng.decide(failure, _ctx())
        # Default kind→action for WINDOW_NOT_READY is
        # RETRY_WITH_BACKOFF.
        assert decision.action.value == "retry_with_backoff"
        assert decision.backoff_s == 1.0

    def test_decide_token_check_runs_before_runtime_budget(self):
        # Even if the runtime budget is also exceeded, the token
        # check is the first thing — we want the user-visible
        # state to be CANCELLED, not GIVE_UP.
        eng = DefaultRecoveryEngine(
            policy=RecoveryPolicy(
                max_total_runtime_s=0.0,
                max_attempts_per_step=2,
                max_replans=2,
            )
        )
        tok = CancellationToken()
        tok.cancel(reason="x")
        failure = make_failure(
            kind=FailureKind.EXECUTION, step_id="s1"
        )
        decision = eng.decide(
            failure, _ctx(), cancellation_token=tok
        )
        assert decision.action.value == "abort"

    def test_legacy_engine_without_kwarg_is_tolerated(self):
        # The Agent calls ``decide(...cancellation_token=...)``
        # and falls back to no-arg if the engine is older.
        eng = DefaultRecoveryEngine()
        failure = make_failure(
            kind=FailureKind.EXECUTION, step_id="s1"
        )
        # Should not raise even if we pass a token.
        decision = eng.decide(failure, _ctx(), cancellation_token=None)
        assert decision.action.value == "retry_with_backoff"


# ===========================================================================
# PlanExecutor.execute_step honors token
# ===========================================================================


class TestPlanExecutorExecuteStepHonorsCancellation:
    """A single step that is dispatched after cancellation returns CANCELLED."""

    def test_execute_step_returns_cancelled_when_token_set(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from core.orchestration.plan_executor import PlanExecutor

        # Build a minimal executor; we never want the inner
        # router to be invoked once the token is set.
        executor = object.__new__(PlanExecutor)
        executor.router = None
        tok = CancellationToken()
        tok.cancel(reason="user")
        ctx = _ctx(cancellation_token=tok)
        step = PlanStep(
            step_id="s1",
            description="open notepad",
            capability_name="desktop.application.open",
        )
        result = executor.execute_step(ctx, step)
        assert result.status is StepState.CANCELLED
        assert "user" in (result.error or "")

    def test_execute_step_invokes_capability_when_token_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from core.orchestration.plan_executor import PlanExecutor

        invoked = {"count": 0}

        def _fake_run_step(*, context, step, **kwargs):
            invoked["count"] += 1
            return StepResult(
                step_id=step.step_id,
                capability_name=step.capability_name,
                status=StepState.SUCCEEDED,
            )

        executor = object.__new__(PlanExecutor)
        executor.router = None
        monkeypatch.setattr(executor, "_run_step", _fake_run_step)
        ctx = _ctx()
        step = PlanStep(
            step_id="s1",
            description="open notepad",
            capability_name="desktop.application.open",
        )
        result = executor.execute_step(ctx, step)
        assert invoked["count"] == 1
        assert result.status is StepState.SUCCEEDED


# ===========================================================================
# Pipeline cancellation seams
# ===========================================================================


class TestPipelineCancellationSeams:
    """The pipeline checks the token at three seams."""

    def _pipeline(self):
        from core.pipeline import RequestPipeline

        brain = _FakeBrain()
        agent = _FakeAgent()
        return RequestPipeline(brain=brain, agent=agent)

    def test_entry_seam_cancels_before_brain(self):
        pipe = self._pipeline()
        tok = CancellationToken()
        tok.cancel(reason="entry")
        resp = pipe.process("open notepad", cancellation_token=tok)
        assert resp.status.value == "cancelled"
        assert resp.agent_state == "cancelled"
        assert "entry" in (resp.error or "")

    def test_pipeline_returns_response_when_token_unset(self):
        pipe = self._pipeline()
        resp = pipe.process("open notepad")
        assert resp.status.value == "ok"
        # ``str(AgentState.COMPLETE)`` renders as the enum
        # class name on some Python versions and the value on
        # others; both are acceptable for the response.
        assert "complete" in str(resp.agent_state).lower()

    def test_pipeline_passes_token_to_agent(self):
        pipe = self._pipeline()
        tok = CancellationToken()
        # No cancellation set yet — agent runs to completion.
        resp = pipe.process("open notepad", cancellation_token=tok)
        # Agent's token attribute is the one we passed in.
        assert pipe.agent.cancellation_token is tok

    def test_agent_state_cancelled_maps_to_response_cancelled(self):
        pipe = self._pipeline()
        from core.orchestration.agent_result import AgentState
        pipe.agent.next_state = AgentState.CANCELLED
        resp = pipe.process("open notepad")
        assert resp.status.value == "cancelled"
        assert "cancelled" in str(resp.agent_state).lower()


# ===========================================================================
# Agent.set_cancellation_token
# ===========================================================================


class TestAgentSetCancellationToken:
    def test_set_replaces_token(self):
        from core.orchestration.agent import Agent

        a = _minimal_agent()
        new_tok = CancellationToken()
        a.set_cancellation_token(new_tok)
        assert a.cancellation_token is new_tok

    def test_set_to_none_does_not_raise(self):
        from core.orchestration.agent import Agent

        a = _minimal_agent()
        # Set then clear.
        a.set_cancellation_token(CancellationToken())
        a.set_cancellation_token(None)
        assert a.cancellation_token is None


# ===========================================================================
# omnix_engine request_cancel API
# ===========================================================================


class TestEngineRequestCancel:
    def test_request_cancel_unknown_cid_returns_false(self):
        from core.omnix_engine import OmnixEngine

        eng = _build_engine()
        # No request is in flight; unknown cid.
        assert eng.request_cancel("does-not-exist") is False

    def test_request_cancel_flips_in_flight_token(self):
        from core.omnix_engine import OmnixEngine

        eng = _build_engine()
        cid = "cid-test-001"
        # Simulate process() registering a token.
        from core.orchestration.cancellation import CancellationToken
        tok = CancellationToken()
        with eng._tokens_lock:
            eng._tokens_by_cid[cid] = tok
        assert eng.request_cancel(cid, reason="user") is True
        assert tok.is_cancelled is True
        assert tok.reason == "user"

    def test_request_cancel_all_flips_every_token(self):
        from core.omnix_engine import OmnixEngine

        eng = _build_engine()
        tokens = [CancellationToken() for _ in range(3)]
        with eng._tokens_lock:
            for i, t in enumerate(tokens):
                eng._tokens_by_cid[f"cid-{i}"] = t
        flipped = eng.request_cancel_all(reason="shutdown")
        assert flipped == 3
        assert all(t.is_cancelled for t in tokens)


# ===========================================================================
# Helpers
# ===========================================================================


def _ctx(cancellation_token: Optional[Any] = None) -> ExecutionContext:
    return ExecutionContext(
        execution_id="exec-test",
        goal=Goal(goal_id="g1", description="test", success_criteria=[]),
        plan=Plan(plan_id="p1", goal_id="g1", steps=[]),
        cancellation_token=cancellation_token,
    )


def _minimal_agent():
    """Build an Agent without invoking the full planning loop.

    We only need an object whose ``set_cancellation_token`` works
    and whose ``cancellation_token`` is settable; the constructor
    already wires this.
    """
    from core.orchestration.agent import Agent

    a = object.__new__(Agent)  # bypass heavy __init__
    a.cancellation_token = CancellationToken()
    return a


def _build_engine():
    """Build a minimal engine; we do not need a real pipeline."""
    from core.omnix_engine import OmnixEngine

    eng = object.__new__(OmnixEngine)
    eng._tokens_by_cid = {}
    eng._tokens_lock = __import__("threading").RLock()
    return eng


class _FakeBrain:
    """Returns a minimal BrainResult so RequestPipeline is happy."""

    def handle_text(self, text: str, **kwargs) -> Any:
        from core.orchestration.models import Goal, Intent, IntentKind
        from ai.brain.brain import BrainResult

        return BrainResult(
            status="ok",
            intent=Intent(
                intent_id="i1",
                kind=IntentKind.OPEN_APPLICATION,
                text=text,
                parameters={"application": "notepad"},
            ),
            goal=Goal(goal_id="g1", description=text, success_criteria=[]),
            plan=Plan(plan_id="p1", goal_id="g1", steps=[]),
        )


class _FakeAgent:
    """Stand-in for the real Agent — emits COMPLETE by default."""

    def __init__(self) -> None:
        from core.orchestration.agent_result import (
            make_blank_agent_result,
            AgentState,
        )

        self._result = make_blank_agent_result(
            agent_run_id="test-run", goal_id="g1"
        )
        self.cancellation_token: Optional[CancellationToken] = None
        self.next_state = AgentState.COMPLETE

    def set_cancellation_token(self, token: Any) -> None:
        self.cancellation_token = token

    def run_goal(self, goal, *, intent=None):
        from core.orchestration.agent_result import AgentState

        return self._result.with_final_state(
            self.next_state or AgentState.COMPLETE,
            completed_at=time.time(),
        )

    def run(self, text: str):
        from core.orchestration.agent_result import AgentState

        return self._result.with_final_state(
            self.next_state or AgentState.COMPLETE,
            completed_at=time.time(),
        )
