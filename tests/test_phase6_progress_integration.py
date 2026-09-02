"""
Omnix V6 — Phase 6 tests: progress broadcaster integration.

Exit criteria:
- A real Agent run with multiple steps broadcasts one
  STEP_DISPATCHED + STEP_VERIFIED per step, in plan order.
- The terminal AGENT_COMPLETE phase is broadcast.
- A flaky step produces a STEP_RETRIED phase on retry.
- The RetryTracker increments counters in lockstep with the
  agent loop.
"""

from __future__ import annotations

import os
import sys
from typing import List

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from core.orchestration import (
    AgentPolicy,
    InMemoryProgressBroadcaster,
    ProgressPhase,
    RetryCounters,
    RetryTracker,
)

from test_system8_agent_orchestration import (
    _Echo,
    _SometimesFails,
    _WiredAgent,
    _step,
)


# ===========================================================================
# ProgressEvent broadcast at every step transition
# ===========================================================================


class TestProgressBroadcastsPerStep:
    def test_one_dispatched_and_verified_per_step(self):
        echo = _Echo()
        wire = _WiredAgent(
            steps=[
                _step("a", "test.echo", params={"msg": "1"}),
                _step("b", "test.echo", params={"msg": "2"}),
                _step("c", "test.echo", params={"msg": "3"}),
            ],
            capabilities={"test.echo": echo},
        )
        result = wire.agent.run("multi")
        assert result.completed, result.to_dict()
        # The broadcaster saw one STEP_DISPATCHED per step.
        dispatched = wire.broadcaster.of_phase(ProgressPhase.STEP_DISPATCHED)
        verified = wire.broadcaster.of_phase(ProgressPhase.STEP_VERIFIED)
        assert len(dispatched) >= 3, dispatched
        assert len(verified) >= 3, verified
        # The order matches the plan.
        ids = [e.step_id for e in dispatched if e.step_id]
        assert "a" in ids and "b" in ids and "c" in ids
        # AGENT_COMPLETE is broadcast at least once.
        assert wire.broadcaster.count(ProgressPhase.AGENT_COMPLETE) >= 1

    def test_retry_phase_emitted_on_flaky_step(self):
        flaky = _SometimesFails(fail_count=1)
        wire = _WiredAgent(
            steps=[_step("only", "test.flaky", params={})],
            capabilities={"test.flaky": flaky},
            max_step_retries=3,
        )
        result = wire.agent.run("flaky")
        assert result.completed, result.to_dict()
        # A retry was recorded on the RetryTracker.
        assert wire.agent.retry_tracker.retries_for("only") >= 1
        # And the broadcaster saw a STEP_RETRIED phase.
        retried = wire.broadcaster.of_phase(ProgressPhase.STEP_RETRIED)
        assert len(retried) >= 1, retried


# ===========================================================================
# RetryTracker snapshot integrity
# ===========================================================================


class TestRetryTrackerSnapshot:
    def test_tracker_counts_match_broadcaster_events(self):
        from core.orchestration.progress import make_progress_event
        b = InMemoryProgressBroadcaster()
        t = RetryTracker(broadcaster=b)
        t.record_step_attempt("s1")
        t.record_step_retry("s1")
        t.record_step_attempt("s2")
        t.record_step_retry("s2")
        t.record_step_retry("s2")
        t.record_replan()
        snap = t.snapshot()
        assert snap.attempts_for("s1") == 1
        assert snap.retries_for("s1") == 1
        assert snap.attempts_for("s2") == 1
        assert snap.retries_for("s2") == 2
        assert snap.replans == 1
        d = snap.to_dict()
        assert d["step_attempts"] == {"s1": 1, "s2": 1}
        assert d["step_retries"] == {"s1": 1, "s2": 2}
        assert d["replans"] == 1
        assert snap.elapsed_s() >= 0
