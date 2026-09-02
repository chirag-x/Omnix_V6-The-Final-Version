"""
Omnix V6 — Phase 6C Recovery Engine / Policy tests.

These tests pin the contract of the :class:`DefaultRecoveryEngine`
and :class:`RecoveryPolicy`:
- bounded retry / replan policy
- FailureKind → RecoveryAction mapping
- downgrade to GIVE_UP / ASK_USER when budget is exhausted
- bounded runtime cap forces GIVE_UP
- reset() between runs works
"""

from __future__ import annotations

import time

import pytest

from core.orchestration import (
    DefaultRecoveryEngine,
    ExecutionContext,
    Failure,
    FailureKind,
    Goal,
    Plan,
    PlanStep,
    RecoveryAction,
    RecoveryDecision,
    RecoveryPolicy,
    make_blank_execution_result,
    make_failure,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx() -> ExecutionContext:
    """A minimal ExecutionContext for engine tests."""
    goal = Goal(goal_id="g1", description="d")
    plan = Plan(plan_id="p1", goal_id="g1", steps=(
        PlanStep(
            step_id="s1",
            description="d",
            capability_name="test.echo",
            parameters={},
        ),
    ))
    return ExecutionContext(
        goal=goal,
        plan=plan,
        execution_id="e1",
    )


# ---------------------------------------------------------------------------
# RecoveryPolicy
# ---------------------------------------------------------------------------

class TestRecoveryPolicy:
    def test_policy_defaults_are_bounded(self):
        p = RecoveryPolicy()
        assert p.max_attempts_per_step == 2
        assert p.max_replans == 2
        assert p.max_total_runtime_s > 0
        assert p.backoff_s >= 0

    def test_policy_with_overrides(self):
        p = RecoveryPolicy(max_replans=0)
        assert p.max_replans == 0
        # Other defaults preserved.
        assert p.max_attempts_per_step == 2

    def test_policy_is_frozen(self):
        p = RecoveryPolicy()
        with pytest.raises(Exception):
            p.max_replans = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DefaultRecoveryEngine — basic decisions
# ---------------------------------------------------------------------------

class TestDefaultRecoveryEngine:
    def test_engine_has_name(self):
        e = DefaultRecoveryEngine()
        assert e.name

    def test_default_engine_uses_default_policy(self):
        e = DefaultRecoveryEngine()
        assert isinstance(e.policy, RecoveryPolicy)

    def test_engine_accepts_custom_policy(self):
        p = RecoveryPolicy(max_replans=0, max_attempts_per_step=1)
        e = DefaultRecoveryEngine(policy=p)
        assert e.policy.max_replans == 0
        assert e.policy.max_attempts_per_step == 1

    def test_engine_resets_counters(self):
        e = DefaultRecoveryEngine()
        e.record_replan()
        e.record_attempt("s1")
        assert e.replans_remaining() == e.policy.max_replans - 1
        e.reset()
        assert e.replans_remaining() == e.policy.max_replans
        assert e.attempts_remaining("s1") == e.policy.max_attempts_per_step


# ---------------------------------------------------------------------------
# FailureKind → RecoveryAction mapping
# ---------------------------------------------------------------------------

class TestKindToActionMapping:
    def test_execution_default_is_retry_with_backoff(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action in (
            RecoveryAction.RETRY,
            RecoveryAction.RETRY_WITH_BACKOFF,
        )

    def test_verification_default_is_replan(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.VERIFICATION, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.REPLAN

    def test_timeout_default_is_replan(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.TIMEOUT, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.REPLAN

    def test_cancelled_default_is_give_up(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.CANCELLED, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.GIVE_UP

    def test_safety_default_is_give_up(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.SAFETY, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.GIVE_UP

    def test_unknown_capability_default_is_give_up(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.UNKNOWN_CAPABILITY, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.GIVE_UP

    def test_invalid_parameters_default_is_give_up(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.INVALID_PARAMETERS, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.GIVE_UP

    def test_plan_infeasible_default_is_replan(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.PLAN_INFEASIBLE, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.REPLAN

    def test_internal_default_is_give_up(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.INTERNAL, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.GIVE_UP


# ---------------------------------------------------------------------------
# Bounded budget
# ---------------------------------------------------------------------------

class TestBoundedBudget:
    def test_retry_downgrades_to_replan_when_attempts_exhausted(self):
        e = DefaultRecoveryEngine(
            policy=RecoveryPolicy(max_attempts_per_step=1, max_replans=2)
        )
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        # First record the failed attempt (the engine's counters track
        # attempts that the Agent has dispatched, not the ones decide
        # itself makes).
        e.record_attempt("s1")
        d = e.decide(f, _ctx())
        assert d.action in (RecoveryAction.REPLAN, RecoveryAction.GIVE_UP)

    def test_retry_downgrades_to_give_up_when_no_replans_left(self):
        e = DefaultRecoveryEngine(
            policy=RecoveryPolicy(max_attempts_per_step=1, max_replans=0)
        )
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        e.record_attempt("s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.GIVE_UP

    def test_replan_downgrades_to_give_up_when_no_replans_left(self):
        e = DefaultRecoveryEngine(
            policy=RecoveryPolicy(max_replans=0)
        )
        f = make_failure(kind=FailureKind.VERIFICATION, step_id="s1")
        d = e.decide(f, _ctx())
        # Verification replan-out-of-budget defaults to ASK_USER if policy
        # allows, else GIVE_UP.
        assert d.action in (RecoveryAction.ASK_USER, RecoveryAction.GIVE_UP)

    def test_verification_replan_out_of_budget_can_ask_user(self):
        e = DefaultRecoveryEngine(
            policy=RecoveryPolicy(max_replans=0, ask_user_on_uncertain=True)
        )
        f = make_failure(kind=FailureKind.VERIFICATION, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.ASK_USER
        assert d.ask_user_message

    def test_replans_remaining_decrements_on_record(self):
        e = DefaultRecoveryEngine(policy=RecoveryPolicy(max_replans=3))
        assert e.replans_remaining() == 3
        e.record_replan()
        assert e.replans_remaining() == 2
        e.record_replan()
        assert e.replans_remaining() == 1

    def test_attempts_remaining_decrements_on_record(self):
        e = DefaultRecoveryEngine(policy=RecoveryPolicy(max_attempts_per_step=3))
        assert e.attempts_remaining("s1") == 3
        e.record_attempt("s1")
        assert e.attempts_remaining("s1") == 2
        e.record_attempt("s1")
        assert e.attempts_remaining("s1") == 1

    def test_attempts_remaining_clamps_to_zero(self):
        e = DefaultRecoveryEngine(policy=RecoveryPolicy(max_attempts_per_step=1))
        e.record_attempt("s1")
        e.record_attempt("s1")
        e.record_attempt("s1")
        assert e.attempts_remaining("s1") == 0


# ---------------------------------------------------------------------------
# Bounded runtime
# ---------------------------------------------------------------------------

class TestBoundedRuntime:
    def test_runtime_cap_forces_give_up(self):
        p = RecoveryPolicy(max_total_runtime_s=0.001)
        e = DefaultRecoveryEngine(policy=p)
        # Wait long enough to exceed the cap.
        time.sleep(0.01)
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.GIVE_UP
        assert "runtime" in d.rationale.lower() or "budget" in d.rationale.lower()

    def test_zero_runtime_cap_disables_runtime_check(self):
        # max_total_runtime_s=0 means "no cap"; the runtime check is skipped.
        e = DefaultRecoveryEngine(
            policy=RecoveryPolicy(max_total_runtime_s=0)
        )
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action != RecoveryAction.GIVE_UP or "runtime" not in d.rationale.lower()


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------

class TestActionOverrides:
    def test_override_replaces_default(self):
        e = DefaultRecoveryEngine(
            action_overrides={FailureKind.EXECUTION: RecoveryAction.REPLAN}
        )
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.REPLAN

    def test_override_respects_bounds(self):
        # Override says REPLAN, but max_replans=0 → GIVE_UP (or ASK_USER
        # for verification).
        e = DefaultRecoveryEngine(
            policy=RecoveryPolicy(max_replans=0),
            action_overrides={FailureKind.EXECUTION: RecoveryAction.REPLAN},
        )
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.action is RecoveryAction.GIVE_UP


# ---------------------------------------------------------------------------
# Decision shape
# ---------------------------------------------------------------------------

class TestDecisionShape:
    def test_decision_has_id(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.decision_id
        assert d.failure_id == f.failure_id

    def test_decision_has_rationale(self):
        e = DefaultRecoveryEngine()
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        d = e.decide(f, _ctx())
        assert d.rationale

    def test_retry_with_backoff_carries_backoff(self):
        e = DefaultRecoveryEngine(
            policy=RecoveryPolicy(backoff_s=1.5)
        )
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s1")
        d = e.decide(f, _ctx())
        if d.action is RecoveryAction.RETRY_WITH_BACKOFF:
            assert d.backoff_s == 1.5
        else:
            # If attempts exhausted and downgraded, backoff should be 0.
            assert d.backoff_s == 0.0


# ---------------------------------------------------------------------------
# make_failure helper
# ---------------------------------------------------------------------------

class TestMakeFailure:
    def test_make_failure_assigns_id(self):
        f = make_failure(kind=FailureKind.EXECUTION)
        assert f.failure_id

    def test_make_failure_preserves_kind(self):
        f = make_failure(kind=FailureKind.VERIFICATION)
        assert f.kind is FailureKind.VERIFICATION

    def test_make_failure_preserves_step_id(self):
        f = make_failure(kind=FailureKind.EXECUTION, step_id="s9")
        assert f.step_id == "s9"

    def test_make_failure_preserves_message(self):
        f = make_failure(kind=FailureKind.EXECUTION, message="oops")
        assert f.message == "oops"
