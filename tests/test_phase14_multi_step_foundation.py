"""
Omnix V6 — Phase 14 multi-step foundation tests.

This file is the *single* deterministic test surface for Phase 14.
It exercises the new modules without booting the real Agent or
PlanExecutor: every component is driven through its Protocol,
every dependency is a stub.  The LLM and the real vision service
are never touched.

Test groupings:

    1. StepLifecycle state machine
    2. StepExecutionState transitions
    3. MultiStepContext threading
    4. IdempotencyLog + idempotency_key
    5. Pre/postcondition enums + helpers
    6. ScrollPlan + ScrollStep bounds
    7. MultiStepCoordinator: preconditions
    8. MultiStepCoordinator: postconditions
    9. MultiStepCoordinator: idempotency
    10. MultiStepCoordinator: re-grounding
    11. MultiStepCoordinator: scroll fallback
    12. ai.brain.cross_domain.compose_cross_domain_plan

Each test is independent; failures point at exactly the broken
unit.  The tests do NOT call any V6 service; they use stub
Protocol implementations.
"""
from __future__ import annotations

import os
import sys

# Ensure the project root is on sys.path when this file is run
# directly (e.g. ``python tests/test_phase14_multi_step_foundation.py``).
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest  # noqa: E402

from core.orchestration import (  # noqa: E402
    ExecutionContext,
    Goal,
    MultiStepContext,
    MultiStepCoordinator,
    IdempotencyLog,
    InMemoryIdempotencyStore,
    InMemoryMultiStepContextStore,
    PlanStep,
    PreconditionKind,
    PostconditionKind,
    PreconditionOutcome,
    PostconditionOutcome,
    ScrollDirection,
    ScrollFallbackOutcome,
    ScrollPlan,
    ScrollSurface,
    ScrollStep,
    StepExecutionState,
    StepLifecycle,
    StepPostcondition,
    StepPrecondition,
    build_default_scroll_plan,
    can_transition,
    idempotency_key,
    is_step_terminal,
    postconditions_from_metadata,
    preconditions_from_metadata,
)
from core.orchestration.grounding import (  # noqa: E402
    GroundingStatus,
    TargetGroundingContract,
)
from core.orchestration.models import (  # noqa: E402
    ActionRequest,
    Observation,
    ObservationSource,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_goal() -> Goal:
    return Goal(goal_id="g1", description="open chrome and navigate to example.com")


def _make_execution_context(plan_steps=None) -> ExecutionContext:
    from core.orchestration.models import Plan
    plan = Plan(
        plan_id="p1",
        goal_id="g1",
        steps=plan_steps or (),
    )
    return ExecutionContext(
        execution_id="e1",
        goal=_make_goal(),
        plan=plan,
    )


def _make_plan_step(
    step_id: str = "s1",
    cap: str = "desktop.mouse.click",
    metadata=None,
) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description="click something",
        capability_name=cap,
        parameters={"x": 1, "y": 2},
        metadata=metadata or {},
    )


def _make_observation(data: str = "subject=ok") -> Observation:
    return Observation(
        source=ObservationSource.VISION,
        data=data,
        subject="",
        confidence=0.9,
    )


def _make_grounded_contract(query: str = "Submit button") -> TargetGroundingContract:
    return TargetGroundingContract(
        status=GroundingStatus.GROUNDED,
        target_query=query,
        bbox=(100, 100, 200, 200),
        center=(150, 150),
        confidence=0.95,
        source=ObservationSource.VISION,
    )


def _make_not_found_contract(query: str = "Submit button") -> TargetGroundingContract:
    return TargetGroundingContract(
        status=GroundingStatus.NOT_FOUND,
        target_query=query,
        error="not on screen",
    )


# ---------------------------------------------------------------------------
# 1. StepLifecycle state machine
# ---------------------------------------------------------------------------


class TestStepLifecycleStateMachine:
    def test_terminal_states_are_closed(self):
        for state in (
            StepLifecycle.COMPLETED,
            StepLifecycle.FAILED,
            StepLifecycle.TIMED_OUT,
            StepLifecycle.CANCELLED,
            StepLifecycle.SKIPPED,
        ):
            assert is_step_terminal(state)
            for other in StepLifecycle:
                # Self-transition is always legal; any *other*
                # transition out of a terminal state must be
                # refused.
                if other is state:
                    continue
                assert not can_transition(state, other)

    def test_planned_only_to_ready(self):
        for other in StepLifecycle:
            if other is StepLifecycle.READY or other is StepLifecycle.PLANNED:
                assert can_transition(StepLifecycle.PLANNED, other)
            else:
                assert not can_transition(StepLifecycle.PLANNED, other)

    def test_executing_to_expected_states(self):
        allowed = {
            StepLifecycle.EXECUTING,
            StepLifecycle.EXECUTED,
            StepLifecycle.FAILED,
            StepLifecycle.TIMED_OUT,
            StepLifecycle.CANCELLED,
            StepLifecycle.RECOVERING,
            StepLifecycle.REPLANNING,
        }
        for other in StepLifecycle:
            if other in allowed:
                assert can_transition(StepLifecycle.EXECUTING, other)
            else:
                assert not can_transition(StepLifecycle.EXECUTING, other)

    def test_self_transition_is_always_legal(self):
        for state in StepLifecycle:
            assert can_transition(state, state)

    def test_observed_routes_to_recovery_or_verification(self):
        for other in (
            StepLifecycle.OBSERVED,  # self-transition is legal
            StepLifecycle.VERIFIED,
            StepLifecycle.UNCERTAIN,
            StepLifecycle.RECOVERING,
            StepLifecycle.REPLANNING,
        ):
            assert can_transition(StepLifecycle.OBSERVED, other)
        for other in (
            StepLifecycle.EXECUTING,
            StepLifecycle.PLANNED,
            StepLifecycle.READY,
        ):
            assert not can_transition(StepLifecycle.OBSERVED, other)


# ---------------------------------------------------------------------------
# 2. StepExecutionState transitions
# ---------------------------------------------------------------------------


class TestStepExecutionState:
    def test_initial_state_is_planned(self):
        st = StepExecutionState(step_id="s1")
        assert st.state is StepLifecycle.PLANNED
        assert st.attempt == 0
        assert not st.is_terminal()

    def test_transition_to_returns_new_instance(self):
        st = StepExecutionState(step_id="s1")
        new = st.transition_to(StepLifecycle.READY)
        assert new.state is StepLifecycle.READY
        assert st.state is StepLifecycle.PLANNED  # original untouched
        assert new is not st

    def test_illegal_transition_raises(self):
        from core.orchestration import IllegalStepTransition
        st = StepExecutionState(step_id="s1", state=StepLifecycle.PLANNED)
        with pytest.raises(IllegalStepTransition):
            st.transition_to(StepLifecycle.VERIFIED)

    def test_terminal_state_blocks_anything_new(self):
        st = StepExecutionState(step_id="s1", state=StepLifecycle.COMPLETED)
        with pytest.raises(Exception):
            st.transition_to(StepLifecycle.EXECUTING)

    def test_to_dict_is_serialisable(self):
        st = StepExecutionState(
            step_id="s1",
            state=StepLifecycle.EXECUTING,
            attempt=2,
            last_verdict="passed",
        )
        d = st.to_dict()
        assert d["step_id"] == "s1"
        assert d["state"] == "executing"
        assert d["attempt"] == 2
        assert d["last_verdict"] == "passed"


# ---------------------------------------------------------------------------
# 3. MultiStepContext threading
# ---------------------------------------------------------------------------


class TestMultiStepContext:
    def test_empty_context_has_no_states(self):
        ctx = MultiStepContext(base=_make_execution_context())
        assert ctx.all_states() == ()
        assert ctx.pending_steps() == ()

    def test_with_step_state_appends(self):
        ctx = MultiStepContext(base=_make_execution_context())
        st = StepExecutionState(step_id="s1", state=StepLifecycle.READY)
        new_ctx = ctx.with_step_state(st)
        assert new_ctx.state_of("s1") is st
        # Original is unchanged (frozen).
        assert ctx.state_of("s1") is None

    def test_with_grounded_target_keeps_history(self):
        ctx = MultiStepContext(base=_make_execution_context())
        contract = _make_grounded_contract()
        new_ctx = ctx.with_grounded_target("s1", contract)
        assert new_ctx.grounded_target_for("s1") is contract

    def test_with_previous_observation(self):
        ctx = MultiStepContext(base=_make_execution_context())
        obs = _make_observation()
        new_ctx = ctx.with_previous_observation("s1", obs)
        assert new_ctx.previous_observation_for("s1") is obs

    def test_mark_step_started_increments_attempt(self):
        ctx = MultiStepContext(base=_make_execution_context())
        # PLANNED -> READY -> EXECUTING (must walk the lifecycle).
        new_ctx = ctx.mark_step_started("s1", started_at=123.0)
        st = new_ctx.state_of("s1")
        assert st.state is StepLifecycle.EXECUTING
        assert st.attempt == 1
        assert st.started_at == 123.0

    def test_mark_step_finished_records_verdict(self):
        ctx = MultiStepContext(base=_make_execution_context())
        ctx = ctx.mark_step_started("s1")
        # EXECUTING -> EXECUTED -> OBSERVED (walk the lifecycle).
        ctx = ctx.mark_step_finished("s1", new_state=StepLifecycle.EXECUTED)
        ctx = ctx.mark_step_finished(
            "s1", new_state=StepLifecycle.OBSERVED, verdict="passed", finished_at=200.0
        )
        st = ctx.state_of("s1")
        assert st.state is StepLifecycle.OBSERVED
        assert st.last_verdict == "passed"
        assert st.finished_at == 200.0

    def test_full_lifecycle_walk(self):
        ctx = MultiStepContext(base=_make_execution_context())
        # PLANNED -> READY -> EXECUTING -> EXECUTED -> OBSERVED -> VERIFIED -> COMPLETED
        ctx = ctx.mark_step_started("s1")
        ctx = ctx.mark_step_finished("s1", new_state=StepLifecycle.EXECUTED)
        ctx = ctx.mark_step_finished("s1", new_state=StepLifecycle.OBSERVED)
        ctx = ctx.mark_step_finished("s1", new_state=StepLifecycle.VERIFIED, verdict="passed")
        ctx = ctx.mark_step_finished("s1", new_state=StepLifecycle.COMPLETED)
        st = ctx.state_of("s1")
        assert st.state is StepLifecycle.COMPLETED
        assert st.is_terminal()
        assert st.last_verdict == "passed"


# ---------------------------------------------------------------------------
# 4. IdempotencyLog + idempotency_key
# ---------------------------------------------------------------------------


class TestIdempotencyLog:
    def test_key_is_stable_for_same_inputs(self):
        k1 = idempotency_key("test.cap", {"a": 1, "b": 2})
        k2 = idempotency_key("test.cap", {"b": 2, "a": 1})
        assert k1 == k2

    def test_key_changes_with_capability(self):
        k1 = idempotency_key("a", {"x": 1})
        k2 = idempotency_key("b", {"x": 1})
        assert k1 != k2

    def test_key_changes_with_parameters(self):
        k1 = idempotency_key("a", {"x": 1})
        k2 = idempotency_key("a", {"x": 2})
        assert k1 != k2

    def test_log_records_unique_dispatches(self):
        log = IdempotencyLog()
        log.record(step_id="s1", capability_name="a", parameters={"x": 1})
        log.record(step_id="s2", capability_name="a", parameters={"x": 2})
        assert log.size() == 2

    def test_log_detects_duplicate(self):
        log = IdempotencyLog()
        log.record(step_id="s1", capability_name="a", parameters={"x": 1})
        assert log.is_duplicate("a", {"x": 1})
        assert not log.is_duplicate("a", {"x": 2})

    def test_duplicate_record_updates_attempt(self):
        log = IdempotencyLog()
        log.record(step_id="s1", capability_name="a", parameters={"x": 1}, attempt=1)
        log.record(step_id="s2", capability_name="a", parameters={"x": 1}, attempt=3)
        entry = log.entry_for("a", {"x": 1})
        assert entry.attempt == 3
        assert "s1" in entry.step_ids
        assert "s2" in entry.step_ids


# ---------------------------------------------------------------------------
# 5. Pre/postcondition enums + helpers
# ---------------------------------------------------------------------------


class TestPrePostconditions:
    def test_preconditions_from_metadata_empty(self):
        assert preconditions_from_metadata({}) == ()

    def test_preconditions_from_metadata_with_typed(self):
        pre = StepPrecondition(kind=PreconditionKind.STEP_COMPLETED, required_step_id="s1")
        out = preconditions_from_metadata(
            {"phase14_preconditions": [pre]}
        )
        assert out == (pre,)

    def test_preconditions_from_metadata_with_mapping(self):
        raw = {"phase14_preconditions": [
            {"kind": "step_completed", "required_step_id": "s1"},
            {"kind": "world_state_fact", "fact_key": "k", "fact_value": 1},
        ]}
        out = preconditions_from_metadata(raw)
        assert len(out) == 2
        assert out[0].kind is PreconditionKind.STEP_COMPLETED
        assert out[1].fact_key == "k"

    def test_postconditions_round_trip(self):
        post = StepPostcondition(
            kind=PostconditionKind.WORLD_STATE_FACT_SET,
            fact_key="x",
            fact_value=42,
        )
        out = postconditions_from_metadata(
            {"phase14_postconditions": [post]}
        )
        assert out[0] == post

    def test_unknown_kind_raises_value_error(self):
        with pytest.raises(ValueError):
            preconditions_from_metadata(
                {"phase14_preconditions": [{"kind": "nonsense"}]}
            )


# ---------------------------------------------------------------------------
# 6. ScrollPlan + ScrollStep bounds
# ---------------------------------------------------------------------------


class TestScrollPlan:
    def test_default_plan_has_five_steps(self):
        plan = build_default_scroll_plan(target_query="Settings")
        assert plan.max_steps == 5
        assert len(plan.steps) == 5
        assert plan.max_total_amount == 15

    def test_step_amount_must_be_positive(self):
        with pytest.raises(ValueError):
            ScrollStep(direction=ScrollDirection.DOWN, surface=ScrollSurface.DESKTOP, amount=0)

    def test_step_amount_capped_at_50(self):
        with pytest.raises(ValueError):
            ScrollStep(direction=ScrollDirection.DOWN, surface=ScrollSurface.DESKTOP, amount=51)

    def test_plan_total_must_be_greater_than_max_steps(self):
        with pytest.raises(ValueError):
            ScrollPlan(
                target_query="x",
                max_steps=10,
                max_total_amount=5,
            )

    def test_plan_target_query_must_be_non_empty(self):
        with pytest.raises(ValueError):
            ScrollPlan(target_query="")

    def test_is_within_bounds(self):
        plan = ScrollPlan(target_query="x", max_steps=5, max_total_amount=25)
        assert plan.is_within_bounds(0)
        assert plan.is_within_bounds(24)
        assert not plan.is_within_bounds(25)

    def test_remaining_steps(self):
        plan = ScrollPlan(target_query="x", max_steps=5, max_total_amount=25)
        assert plan.remaining_steps(0) == 5
        assert plan.remaining_steps(3) == 2
        assert plan.remaining_steps(5) == 0

    def test_step_projects_to_capability_params(self):
        step = ScrollStep(
            direction=ScrollDirection.UP,
            surface=ScrollSurface.BROWSER,
            amount=2,
            selector=".scrollable",
        )
        params = step.to_capability_parameters()
        assert params["direction"] == "up"
        assert params["amount"] == 2
        assert params["selector"] == ".scrollable"


# ---------------------------------------------------------------------------
# 7. MultiStepCoordinator: preconditions
# ---------------------------------------------------------------------------


class _StubWorld:
    def __init__(self):
        self.facts = {}

    def get_fact(self, key):
        return self.facts.get(key)

    def set_fact(self, key, value):
        self.facts[key] = value


class _StubGrounding:
    def __init__(self, contract):
        self._contract = contract
        self.calls = []

    def ground_target(self, target_query, *, preferred_strategy=None):
        self.calls.append((target_query, preferred_strategy))
        return self._contract


class _StubScrollExecutor:
    def __init__(self):
        self.scrolls = []

    def perform_scroll(self, step):
        self.scrolls.append(step)
        return True


def _build_coordinator(
    *,
    grounding: _StubGrounding = None,
    world: _StubWorld = None,
    scroll: _StubScrollExecutor = None,
    duplicate_policy: str = "refuse",
):
    ctx_store = InMemoryMultiStepContextStore()
    idem_store = InMemoryIdempotencyStore()
    return MultiStepCoordinator(
        context_store=ctx_store,
        idempotency_store=idem_store,
        world_state=world,
        grounding_provider=grounding,
        scroll_executor=scroll,
        duplicate_action_policy=duplicate_policy,
    )


class TestCoordinatorPreconditions:
    def test_no_preconditions_means_ok(self):
        coord = _build_coordinator()
        step = _make_plan_step(step_id="s1")
        out = coord.evaluate_preconditions(step)
        assert out.ok
        assert out.satisfied == ()
        assert out.failed == ()

    def test_step_completed_precondition_holds(self):
        coord = _build_coordinator()
        # Mark s1 COMPLETED — walk the full lifecycle.
        ctx = coord.context
        ctx = ctx.mark_step_started("s1")
        ctx = ctx.mark_step_finished("s1", new_state=StepLifecycle.EXECUTED)
        ctx = ctx.mark_step_finished("s1", new_state=StepLifecycle.OBSERVED)
        ctx = ctx.mark_step_finished("s1", new_state=StepLifecycle.VERIFIED, verdict="passed")
        ctx = ctx.mark_step_finished("s1", new_state=StepLifecycle.COMPLETED)
        coord.context_store.set(ctx)

        pre = StepPrecondition(kind=PreconditionKind.STEP_COMPLETED, required_step_id="s1")
        step = _make_plan_step(step_id="s2", metadata={"phase14_preconditions": [pre]})
        out = coord.evaluate_preconditions(step)
        assert out.ok
        assert ("step_completed",) == out.satisfied

    def test_step_completed_precondition_fails(self):
        coord = _build_coordinator()
        pre = StepPrecondition(kind=PreconditionKind.STEP_COMPLETED, required_step_id="s1")
        step = _make_plan_step(step_id="s2", metadata={"phase14_preconditions": [pre]})
        out = coord.evaluate_preconditions(step)
        assert not out.ok
        assert out.failed[0][0] == "step_completed"

    def test_world_state_fact_precondition(self):
        world = _StubWorld()
        world.facts["k"] = 1
        coord = _build_coordinator(world=world)
        pre = StepPrecondition(
            kind=PreconditionKind.WORLD_STATE_FACT,
            fact_key="k",
            fact_value=1,
        )
        step = _make_plan_step(step_id="s1", metadata={"phase14_preconditions": [pre]})
        out = coord.evaluate_preconditions(step)
        assert out.ok

    def test_world_state_fact_precondition_fails_on_mismatch(self):
        world = _StubWorld()
        world.facts["k"] = 0
        coord = _build_coordinator(world=world)
        pre = StepPrecondition(
            kind=PreconditionKind.WORLD_STATE_FACT,
            fact_key="k",
            fact_value=1,
        )
        step = _make_plan_step(step_id="s1", metadata={"phase14_preconditions": [pre]})
        out = coord.evaluate_preconditions(step)
        assert not out.ok

    def test_grounded_target_precondition(self):
        contract = _make_grounded_contract()
        coord = _build_coordinator()
        coord.context_store.set(coord.context.with_grounded_target("s1", contract))
        pre = StepPrecondition(
            kind=PreconditionKind.GROUNDED_TARGET_AVAILABLE,
            required_step_id="s1",
        )
        step = _make_plan_step(step_id="s2", metadata={"phase14_preconditions": [pre]})
        out = coord.evaluate_preconditions(step)
        assert out.ok


# ---------------------------------------------------------------------------
# 8. MultiStepCoordinator: postconditions
# ---------------------------------------------------------------------------


class TestCoordinatorPostconditions:
    def test_step_observed_postcondition_satisfied(self):
        coord = _build_coordinator()
        step = _make_plan_step(
            step_id="s1",
            metadata={
                "phase14_postconditions": [
                    StepPostcondition(kind=PostconditionKind.STEP_OBSERVED)
                ],
                "phase14_observation": _make_observation(),
            },
        )
        out = coord.evaluate_postconditions(step)
        assert out.ok

    def test_step_observed_postcondition_fails_without_observation(self):
        coord = _build_coordinator()
        step = _make_plan_step(
            step_id="s1",
            metadata={
                "phase14_postconditions": [
                    StepPostcondition(kind=PostconditionKind.STEP_OBSERVED)
                ],
            },
        )
        out = coord.evaluate_postconditions(step)
        assert not out.ok

    def test_grounded_target_recorded_postcondition(self):
        coord = _build_coordinator()
        contract = _make_grounded_contract()
        coord.context_store.set(coord.context.with_grounded_target("s1", contract))
        step = _make_plan_step(
            step_id="s1",
            metadata={
                "phase14_postconditions": [
                    StepPostcondition(kind=PostconditionKind.GROUNDED_TARGET_RECORDED)
                ],
            },
        )
        out = coord.evaluate_postconditions(step)
        assert out.ok

    def test_stamp_world_facts(self):
        world = _StubWorld()
        coord = _build_coordinator(world=world)
        step = _make_plan_step(
            step_id="s1",
            metadata={
                "phase14_postconditions": [
                    StepPostcondition(
                        kind=PostconditionKind.WORLD_STATE_FACT_SET,
                        fact_key="x",
                        fact_value=42,
                    )
                ],
            },
        )
        coord.stamp_world_facts(step)
        assert world.facts["x"] == 42


# ---------------------------------------------------------------------------
# 9. MultiStepCoordinator: idempotency
# ---------------------------------------------------------------------------


class TestCoordinatorIdempotency:
    def test_no_duplicate_first_time(self):
        coord = _build_coordinator()
        action = ActionRequest(capability_name="a", parameters={"x": 1})
        out = coord.check_idempotency(_make_plan_step(), action)
        assert not out.duplicate

    def test_refuse_policy_blocks_duplicate(self):
        coord = _build_coordinator(duplicate_policy="refuse")
        action = ActionRequest(capability_name="a", parameters={"x": 1})
        coord.record_dispatch(_make_plan_step(step_id="s1"), action)
        out = coord.check_idempotency(_make_plan_step(step_id="s2"), action)
        assert out.duplicate
        assert not out.short_circuited
        assert out.error is not None

    def test_skip_policy_short_circuits(self):
        coord = _build_coordinator(duplicate_policy="skip")
        action = ActionRequest(capability_name="a", parameters={"x": 1})
        coord.record_dispatch(_make_plan_step(), action)
        out = coord.check_idempotency(_make_plan_step(), action)
        assert out.duplicate
        assert out.short_circuited

    def test_rerun_policy_allows_duplicate(self):
        coord = _build_coordinator(duplicate_policy="re-run")
        action = ActionRequest(capability_name="a", parameters={"x": 1})
        coord.record_dispatch(_make_plan_step(), action)
        out = coord.check_idempotency(_make_plan_step(), action)
        assert out.duplicate
        assert not out.short_circuited

    def test_invalid_policy_rejected_at_construction(self):
        with pytest.raises(ValueError):
            _build_coordinator(duplicate_policy="nope")


# ---------------------------------------------------------------------------
# 10. MultiStepCoordinator: re-grounding
# ---------------------------------------------------------------------------


class TestCoordinatorRegrounding:
    def test_no_query_skips(self):
        coord = _build_coordinator(grounding=_StubGrounding(_make_grounded_contract()))
        step = _make_plan_step(step_id="s1", metadata={})
        assert coord.reground_for_step(step) is None

    def test_query_consults_provider(self):
        contract = _make_grounded_contract()
        provider = _StubGrounding(contract)
        coord = _build_coordinator(grounding=provider)
        step = _make_plan_step(
            step_id="s1",
            metadata={"vision_target_query": "Submit button"},
        )
        out = coord.reground_for_step(step)
        assert out is contract
        assert provider.calls == [("Submit button", None)]
        assert coord.context.grounded_target_for("s1") is contract

    def test_preferred_strategy_passed_through(self):
        provider = _StubGrounding(_make_grounded_contract())
        coord = _build_coordinator(grounding=provider)
        step = _make_plan_step(
            step_id="s1",
            metadata={
                "vision_target_query": "Submit",
                "vision_preferred_strategy": "uia",
            },
        )
        coord.reground_for_step(step)
        assert provider.calls == [("Submit", "uia")]


# ---------------------------------------------------------------------------
# 11. MultiStepCoordinator: scroll fallback
# ---------------------------------------------------------------------------


class TestCoordinatorScrollFallback:
    def test_no_plan_returns_early(self):
        coord = _build_coordinator()
        step = _make_plan_step()
        out = coord.attempt_scroll_fallback(step)
        assert not out.found
        assert out.scrolls_attempted == 0

    def test_scroll_found_after_one_step(self):
        contract = _make_grounded_contract()
        # Two scroll steps: after the first, ground returns NOT_FOUND;
        # after the second, ground returns GROUNDED.
        provider = _StubScrollGrounding(
            [_make_not_found_contract(), contract]
        )
        scroll = _StubScrollExecutor()
        coord = _build_coordinator(grounding=provider, scroll=scroll)
        step = _make_plan_step(
            step_id="s1",
            metadata={
                "phase14_scroll_plan": {
                    "target_query": "Submit",
                    "max_steps": 5,
                    "max_total_amount": 25,
                    "re_ground_after_each": True,
                    "surface": "desktop",
                    "steps": [
                        {"direction": "down", "surface": "desktop", "amount": 3},
                        {"direction": "down", "surface": "desktop", "amount": 3},
                    ],
                }
            },
        )
        out = coord.attempt_scroll_fallback(step)
        assert out.found
        assert out.scrolls_attempted == 2
        assert out.total_amount == 6
        assert len(scroll.scrolls) == 2

    def test_scroll_exhausts_budget(self):
        provider = _StubScrollGrounding([_make_not_found_contract()] * 10)
        scroll = _StubScrollExecutor()
        coord = _build_coordinator(grounding=provider, scroll=scroll)
        step = _make_plan_step(
            step_id="s1",
            metadata={
                "phase14_scroll_plan": {
                    "target_query": "Submit",
                    "max_steps": 2,
                    "max_total_amount": 25,
                    "re_ground_after_each": True,
                    "surface": "desktop",
                    "steps": [
                        {"direction": "down", "surface": "desktop", "amount": 3}
                    ] * 5,
                }
            },
        )
        out = coord.attempt_scroll_fallback(step)
        assert not out.found
        assert out.bounded
        assert out.scrolls_attempted == 2

    def test_scroll_skipped_when_no_provider(self):
        scroll = _StubScrollExecutor()
        coord = _build_coordinator(scroll=scroll)  # no grounding
        step = _make_plan_step(
            step_id="s1",
            metadata={
                "phase14_scroll_plan": {
                    "target_query": "Submit",
                    "max_steps": 5,
                    "max_total_amount": 25,
                    "re_ground_after_each": True,
                    "surface": "desktop",
                    "steps": [{"direction": "down", "surface": "desktop", "amount": 3}],
                }
            },
        )
        out = coord.attempt_scroll_fallback(step)
        assert not out.found
        assert "grounding provider" in out.reason


class _StubScrollGrounding:
    """A grounding stub that returns a sequence of contracts on each call."""

    def __init__(self, contracts):
        self._contracts = list(contracts)
        self.calls = 0

    def ground_target(self, target_query, *, preferred_strategy=None):
        if not self._contracts:
            return _make_not_found_contract(target_query)
        contract = self._contracts.pop(0)
        self.calls += 1
        return contract


# ---------------------------------------------------------------------------
# 12. ai.brain.cross_domain
# ---------------------------------------------------------------------------


class TestCrossDomainComposer:
    def test_compose_simple_plan(self):
        from ai.brain.cross_domain import compose_cross_domain_plan
        from ai.intent.interpreter import IntentKind

        plan = compose_cross_domain_plan(
            intent_kinds=[
                IntentKind.OPEN_APPLICATION,
                IntentKind.FILE_DELETE,
                IntentKind.BROWSER_NAVIGATE,
            ]
        )
        assert len(plan.steps) == 3
        assert [d.value for d in plan.domains_used] == [
            "desktop", "filesystem", "browser"
        ]

    def test_step_dependencies_chain(self):
        from ai.brain.cross_domain import compose_cross_domain_plan
        from ai.intent.interpreter import IntentKind

        plan = compose_cross_domain_plan(
            intent_kinds=[IntentKind.OPEN_APPLICATION, IntentKind.UI_CLICK_TARGET]
        )
        assert plan.steps[1].depends_on_domain_step_id == plan.steps[0].domain_step_id

    def test_safety_tags_applied(self):
        from ai.brain.cross_domain import compose_cross_domain_plan
        from ai.intent.interpreter import IntentKind

        plan = compose_cross_domain_plan(
            intent_kinds=[IntentKind.FILE_DELETE, IntentKind.BROWSER_NAVIGATE]
        )
        assert "dangerous" in plan.steps[0].safety_tags
        assert "network" in plan.steps[1].safety_tags

    def test_unknown_intent_kind_raises(self):
        from ai.brain.cross_domain import compose_cross_domain_plan

        with pytest.raises(ValueError):
            compose_cross_domain_plan(
                intent_kinds=["not_a_real_kind"]  # type: ignore[list-item]
            )

    def test_empty_intent_kinds_rejected(self):
        from ai.brain.cross_domain import compose_cross_domain_plan
        with pytest.raises(ValueError):
            compose_cross_domain_plan(intent_kinds=[])

    def test_mismatched_parameters_rejected(self):
        from ai.brain.cross_domain import compose_cross_domain_plan
        from ai.intent.interpreter import IntentKind

        with pytest.raises(ValueError):
            compose_cross_domain_plan(
                intent_kinds=[IntentKind.OPEN_APPLICATION],
                parameters=[{}, {}],  # length mismatch
            )


if __name__ == "__main__":
    # Run as a script for ad-hoc verification; CI uses pytest.
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
