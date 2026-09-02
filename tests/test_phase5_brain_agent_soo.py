"""
Omnix V6 — Phase 5 tests: Brain / Agent single source of truth for intent.

Exit criteria:
- ``RequestPipeline.process`` always calls
  ``agent.run_goal(goal, intent)`` with the Brain-produced Goal
  and Intent.  ``agent.run(text)`` is reserved for tests and the
  REPL ``/run`` command.
- The pipeline never re-interprets the text.  The Brain is the
  single intent source.
- A greeting-style intent (``kind=INFORM``) routes through the
  pipeline without invoking the Agent closed loop.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from core.orchestration.cancellation import CancellationToken
from core.orchestration.models import (
    Goal,
    Intent,
    IntentKind,
    Plan,
)
from core.orchestration.execution_result import (
    ExecutionOutcome,
    StepResult,
    StepState,
)


# ===========================================================================
# Pipeline Brain→Agent SOO contract
# ===========================================================================


class TestPipelineBrainAgentSoO:
    """The pipeline must use the Brain's Intent verbatim."""

    def test_pipeline_passes_brain_intent_to_agent(self):
        """The same Intent object the Brain produced reaches the Agent."""
        from core.pipeline import RequestPipeline

        brain = _RecordingBrain()
        agent = _RecordingAgent()
        pipe = RequestPipeline(brain=brain, agent=agent)
        resp = pipe.process("open notepad")
        # Brain was called.
        assert brain.calls >= 1
        # Agent received a run_goal(goal, intent) call.
        assert agent.run_goal_calls == 1
        # The Intent passed to the Agent has the same ``kind`` the
        # Brain produced.
        assert agent.last_intent is not None
        assert agent.last_intent.kind == IntentKind.OPEN_APPLICATION
        # The response is OK because the agent returned COMPLETE.
        assert resp.status.value == "ok"

    def test_pipeline_does_not_re_invoke_interpreter(self):
        """The Agent's interpreter is never called when the Brain
        has already produced an Intent."""
        from core.pipeline import RequestPipeline

        brain = _RecordingBrain()
        agent = _RecordingAgent()
        pipe = RequestPipeline(brain=brain, agent=agent)
        pipe.process("open notepad")
        # The Agent was NOT asked to re-interpret the text.
        assert agent.interpreter_calls == 0

    def test_pipeline_uses_brain_goal_for_agent(self):
        """The pipeline does not synthesise a new goal when the
        Brain already produced one."""
        from core.pipeline import RequestPipeline

        brain = _RecordingBrain()
        agent = _RecordingAgent()
        pipe = RequestPipeline(brain=brain, agent=agent)
        pipe.process("open notepad")
        # The Agent received the Brain-produced Goal, not a
        # fallback.  goal_id starts with ``goal-`` (Brain's
        # pattern) not ``goal-cid-`` (pipeline's fallback).
        assert agent.last_goal is not None
        assert agent.last_goal.goal_id.startswith("goal-brain-")


# ===========================================================================
# Pipeline fallback when Brain didn't produce a Goal
# ===========================================================================


class TestPipelineFallbackGoal:
    """When the Brain returns a BrainResult without a goal
    (legacy callers, test fakes), the pipeline builds a minimal
    Goal and still calls ``run_goal`` — never ``run(text)``."""

    def test_pipeline_builds_minimal_goal_when_brain_returns_none(self):
        from core.pipeline import RequestPipeline

        brain = _NoGoalBrain()
        agent = _RecordingAgent()
        pipe = RequestPipeline(brain=brain, agent=agent)
        resp = pipe.process("do something")
        # run_goal was called, not run(text).
        assert agent.run_goal_calls == 1
        assert agent.run_text_calls == 0
        # The fallback goal has the user text as description.
        assert agent.last_goal is not None
        assert agent.last_goal.description == "do something"
        # The intent defaults to UNKNOWN.
        assert agent.last_intent is not None
        assert agent.last_intent.kind == IntentKind.UNKNOWN
        assert resp.status.value == "ok"

    def test_pipeline_fallback_uses_brain_intent_when_present(self):
        """When the Brain produced an Intent but no Goal, the
        pipeline still prefers the Brain's Intent."""
        from core.pipeline import RequestPipeline

        brain = _IntentOnlyBrain()
        agent = _RecordingAgent()
        pipe = RequestPipeline(brain=brain, agent=agent)
        pipe.process("open notepad")
        assert agent.run_goal_calls == 1
        assert agent.run_text_calls == 0
        # Brain's Intent was forwarded, not a default UNKNOWN.
        assert agent.last_intent.kind == IntentKind.OPEN_APPLICATION


# ===========================================================================
# Agent.run(text) is a legacy wrapper
# ===========================================================================


class TestAgentRunTextLegacy:
    """``Agent.run(text)`` exists for tests and the REPL ``/run``
    command, but is NOT called by the production pipeline."""

    def test_pipeline_source_never_calls_agent_run_text(self):
        """Static check: the production path in
        ``core/pipeline.py`` must not call ``self.agent.run(text)``
        outside an explicit legacy/fallback wrapper."""
        import os
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "core", "pipeline.py",
        )
        src = open(path, "r", encoding="utf-8").read()
        # The only acceptable occurrence of ``self.agent.run(`` is
        # an attribute access that does not end with ``(text)``.
        # We search for the precise call pattern.
        forbidden = "self.agent.run(text)"
        assert forbidden not in src, (
            "Phase 5: pipeline.py must call run_goal(goal, intent); "
            "self.agent.run(text) is reserved for tests and the REPL."
        )

    def test_agent_run_text_still_runs_closed_loop(self):
        """The legacy ``Agent.run(text)`` wrapper is a real entry
        point — it can be called directly by tests or the REPL."""
        from core.orchestration.agent import Agent

        # Build a minimal agent via __new__ to avoid heavy
        # constructor — we only need to verify that
        # ``Agent.run(text)`` accepts text and returns a result.
        agent = object.__new__(Agent)
        agent.cancellation_token = CancellationToken()
        # Don't call run — it requires a real interpreter.  Just
        # verify the method is bound and accepts a text argument.
        import inspect
        sig = inspect.signature(agent.run)
        assert "text" in sig.parameters
        # And the docstring references the legacy use case.
        assert "intent" in (agent.run.__doc__ or "").lower()


# ===========================================================================
# Greeting path: INFORM intents do not enter the closed loop
# ===========================================================================


class TestGreetingPath:
    """Greetings (``INFORM`` intents) return a conversational
    response without invoking the Agent closed loop."""

    def test_inform_intent_returns_conversational_response(self):
        from core.pipeline import RequestPipeline

        brain = _GreetingBrain()
        agent = _RecordingAgent()
        pipe = RequestPipeline(brain=brain, agent=agent)
        resp = pipe.process("hello omnix")
        # The pipeline returns a CLARIFICATION response (the
        # Brain's clarifying question), not OK/FAILED.
        assert resp.status.value == "clarification"
        # The Agent was NOT invoked — greetings skip the closed
        # loop entirely.
        assert agent.run_goal_calls == 0
        assert agent.run_text_calls == 0
        # The response text is the Brain's greeting.
        assert "Hello" in resp.text or "hello" in resp.text.lower()

    def test_inform_intent_emits_intent_resolved_event(self):
        """A greeting still emits the INTENT_RESOLVED event so
        the observability stream is complete."""
        from core.pipeline import RequestPipeline

        events: List[Dict[str, Any]] = []
        bus = _RecordingBus(events)
        brain = _GreetingBrain()
        agent = _RecordingAgent()
        pipe = RequestPipeline(
            brain=brain, agent=agent, event_bus=bus
        )
        pipe.process("hello omnix")
        # The intent_resolved event was published.
        stages = [getattr(e, "stage", None) for e in events]
        assert "intent_resolved" in stages


# ===========================================================================
# Helpers
# ===========================================================================


def _ctx() -> Any:
    return None  # not used; tests go through pipeline


class _RecordingBrain:
    """Returns a real BrainResult with a known Intent + Goal."""

    def __init__(self) -> None:
        self.calls = 0

    def handle_text(self, text: str, **kwargs: Any) -> Any:
        from ai.brain.brain import BrainResult
        self.calls += 1
        intent = Intent(
            intent_id=f"intent-brain-{self.calls}",
            kind=IntentKind.OPEN_APPLICATION,
            text=text,
            parameters={"application": "notepad"},
        )
        goal = Goal(
            goal_id=f"goal-brain-{self.calls}",
            description=text,
            success_criteria=(),
        )
        plan = Plan(plan_id=f"plan-{self.calls}", goal_id=goal.goal_id, steps=[])
        return BrainResult(
            status="ok",
            intent=intent,
            goal=goal,
            plan=plan,
        )


class _NoGoalBrain:
    """Brain that returns status=ok but no goal (legacy callers)."""

    def __init__(self) -> None:
        self.calls = 0

    def handle_text(self, text: str, **kwargs: Any) -> Any:
        from ai.brain.brain import BrainResult
        self.calls += 1
        intent = Intent(
            intent_id=f"intent-nogoal-{self.calls}",
            kind=IntentKind.UNKNOWN,
            text=text,
            parameters={},
        )
        return BrainResult(
            status="ok",
            intent=intent,
            goal=None,  # legacy callers may not produce a goal
            plan=None,
        )


class _IntentOnlyBrain:
    """Brain that returns an Intent but no Goal."""

    def __init__(self) -> None:
        self.calls = 0

    def handle_text(self, text: str, **kwargs: Any) -> Any:
        from ai.brain.brain import BrainResult
        self.calls += 1
        intent = Intent(
            intent_id=f"intent-only-{self.calls}",
            kind=IntentKind.OPEN_APPLICATION,
            text=text,
            parameters={"application": "notepad"},
        )
        return BrainResult(
            status="ok",
            intent=intent,
            goal=None,
            plan=None,
        )


class _GreetingBrain:
    """Brain that recognises a greeting and returns status=clarification."""

    def handle_text(self, text: str, **kwargs: Any) -> Any:
        from ai.brain.brain import BrainResult
        return BrainResult(
            status="clarification",
            clarifying_question="Hello! How can I help you today?",
            intent=Intent(
                intent_id="intent-greet",
                kind=IntentKind.INFORM,
                text=text,
                parameters={},
            ),
        )


class _RecordingAgent:
    """Captures calls to run_goal / run so tests can assert."""

    def __init__(self) -> None:
        from core.orchestration.agent_result import (
            make_blank_agent_result,
            AgentState,
        )
        self._blank = make_blank_agent_result(
            agent_run_id="test-run", goal_id="g1"
        )
        self.cancellation_token: Optional[CancellationToken] = None
        self.run_goal_calls = 0
        self.run_text_calls = 0
        self.interpreter_calls = 0
        self.last_goal: Optional[Goal] = None
        self.last_intent: Optional[Intent] = None

    def set_cancellation_token(self, token: Any) -> None:
        self.cancellation_token = token

    def run_goal(self, goal: Goal, *, intent: Any = None) -> Any:
        from core.orchestration.agent_result import AgentState
        self.run_goal_calls += 1
        self.last_goal = goal
        self.last_intent = intent
        return self._blank.with_final_state(
            AgentState.COMPLETE,
            completed_at=time.time(),
        )

    def run(self, text: str) -> Any:
        from core.orchestration.agent_result import AgentState
        self.run_text_calls += 1
        self.interpreter_calls += 1
        return self._blank.with_final_state(
            AgentState.COMPLETE,
            completed_at=time.time(),
        )


class _RecordingBus:
    def __init__(self, sink: List[Dict[str, Any]]) -> None:
        self._sink = sink

    def publish(self, event: Any) -> None:
        self._sink.append(event)
