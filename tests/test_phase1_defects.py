"""
Phase 1 — Defect regression tests (D1–D25).

Each test below targets a single defect flagged by the System 7
audit.  The tests are intentionally hermetic and run on plain
imports of the orchestration layer; they do not boot the full
Engine, do not require a Windows desktop, and do not call any
LLM.  They are the gating evidence for the Phase 1 close.

The mapping below is the canonical reference.  The re-audit
completed on 2026-09-01 confirmed which of the originally
flagged 25 defects are still present in the current code, and
which are already fixed (these tests cover the still-present
defects only — already-fixed ones are kept for documentation
but skip cleanly).

    D1   VerificationVerdict.confidence (still present)
    D2   multi_step_coordinator.reset() (still present)
    D3   AgentState.CONTINUE never assigned (still present)
    D4   _failure_from_step hardcodes attempt=1 (still present)
    D5   observability_sink not wired from Engine (still present)
    D6   PlanStep.max_retries is dead (still present — deprecate)
    D7   step.timeout_s honored (already fixed in plan_executor.py:434)
    D8   silent except in _emit_step_finished (still present)
    D9   _inflight dedupe (already fixed in plan_executor.py:865-870)
    D10  pipeline → agent.run(text) instead of run_goal (still present)
    D11  CancellationRequested raised but not checked (still present)
    D12  SKIP → replan (documented, not a defect)
    D13  RETRY → re-runs whole plan (documented, not a defect)
    D14  verifier context (already real)
    D15  _pending_next_plan typed (already fixed at agent.py:318)
    D16  fast-path cancellation (still absent)
    D17  EXECUTING without plan (already fixed at agent.py:533-534)
    D18  _branch in-scope result (already fixed)
    D19  4 timeout config fields (still present)
    D20  fast-path agent_state=None (still present)
    D21  pipeline timeout (already enforced)
    D22  cleanup
    D23  recovery kind map (still missing 6 UI failure kinds)
    D24  _state reset on run (already done)
    D25  AgentState.CONTINUE (same as D3)
"""
from __future__ import annotations

import time
import pytest
from dataclasses import FrozenInstanceError, replace

# ---------------------------------------------------------------------------
# Imports — the entire orchestration layer
# ---------------------------------------------------------------------------
from core.orchestration.models import (
    Plan,
    PlanStep,
    ActionRequest,
    VerificationVerdict,
    Failure,
    FailureKind,
    RecoveryAction,
    ExecutionContext,
    Intent,
    IntentKind,
    Goal,
    PlanStatus,
)
from core.orchestration.agent_result import (
    AgentState,
    AgentResult,
    _TERMINAL_AGENT_STATES,
    make_blank_agent_result,
    new_agent_run_id,
)
from core.orchestration.multi_step_coordinator import (
    MultiStepCoordinator,
    InMemoryMultiStepContextStore,
    InMemoryIdempotencyStore,
)
from core.orchestration.recovery import (
    DefaultRecoveryEngine,
    RecoveryPolicy,
)
from core.orchestration.agent import Agent, AgentPolicy


# ===========================================================================
# D1 — VerificationVerdict must carry a confidence field
# ===========================================================================

class TestD1VerificationVerdictConfidence:
    """A verdict without ``confidence`` is a plan-evaluator bug:
    ``_aggregate_observation`` reads ``v.confidence`` on every verdict
    and silently defaults to 1.0 if the attribute is missing.
    """

    def test_confidence_field_exists_with_default(self):
        v = VerificationVerdict(
            passed=True, failed=False, uncertain=False, check_name="t"
        )
        assert hasattr(v, "confidence"), (
            "VerificationVerdict must carry a confidence field (D1)"
        )
        # The default should be 1.0 so existing callers that don't
        # set it behave conservatively (high confidence).
        assert v.confidence == 1.0

    def test_confidence_can_be_set(self):
        v = VerificationVerdict(
            passed=False, failed=True, uncertain=False, check_name="t",
            confidence=0.3,
        )
        assert v.confidence == 0.3

    def test_confidence_immutable_via_replace(self):
        v = VerificationVerdict(
            passed=True, failed=False, uncertain=False, check_name="t",
        )
        v2 = v.with_confidence(0.7) if hasattr(v, "with_confidence") else \
             replace(v, confidence=0.7)
        assert v2.confidence == 0.7
        assert v.confidence == 1.0  # original unchanged

    def test_confidence_clamped_or_arbitrary(self):
        # We don't enforce a [0, 1] clamp at the type level —
        # tri-state logic is gated by the bool flags.  But the
        # field must be settable across the typical range.
        v = VerificationVerdict(
            passed=True, failed=False, uncertain=False, check_name="t",
            confidence=0.0,
        )
        assert v.confidence == 0.0
        v2 = replace(v, confidence=0.99)
        assert v2.confidence == 0.99


# ===========================================================================
# D2 — multi_step_coordinator.reset() must clear per-run state
# ===========================================================================

class TestD2MultiStepCoordinatorReset:
    def test_reset_clears_context(self):
        coord = MultiStepCoordinator(
            context_store=InMemoryMultiStepContextStore(),
            idempotency_store=InMemoryIdempotencyStore(),
        )
        # Add a grounded target to the context then reset
        ctx = coord.context
        from core.orchestration.grounding import (
            GroundingStatus, TargetGroundingContract,
        )
        contract = TargetGroundingContract(
            status=GroundingStatus.GROUNDED,
            target_query="a",
        )
        ctx2 = ctx.with_grounded_target("s1", contract)
        coord.context_store.set(ctx2)
        assert "s1" in coord.context.grounded_targets

        coord.reset()
        assert coord.context.grounded_targets == {}

    def test_reset_clears_idempotency_log(self):
        coord = MultiStepCoordinator(
            context_store=InMemoryMultiStepContextStore(),
            idempotency_store=InMemoryIdempotencyStore(),
        )
        # Make the log non-empty via the real API
        coord.idempotency_log.record(
            step_id="s1",
            capability_name="noop",
            parameters={"a": 1},
            attempt=1,
        )
        assert len(coord.idempotency_log.entries) >= 1

        coord.reset()
        assert len(coord.idempotency_log.entries) == 0


# ===========================================================================
# D3 / D25 — AgentState.CONTINUE is dead
# ===========================================================================

class TestD3AgentStateContinue:
    def test_continue_removed_or_used(self):
        # Either: CONTINUE is no longer in AgentState, OR
        # it is actually emitted by the closed loop.  The audit
        # found it was defined but never set.  We enforce one
        # of the two resolutions here.
        if hasattr(AgentState, "CONTINUE"):
            # If still defined, it MUST appear in the terminal set
            # OR be reachable from the closed loop.  A grep of
            # the agent source for `AgentState.CONTINUE` must
            # have at least one hit.  This test enforces that
            # the symbol is not dead.
            import subprocess
            import os
            agent_path = os.path.join(
                os.path.dirname(__file__),
                "..", "core", "orchestration", "agent.py",
            )
            src = open(agent_path, "r", encoding="utf-8").read()
            assert "AgentState.CONTINUE" in src, (
                "AgentState.CONTINUE is defined but never assigned. "
                "Either remove it from the enum (D3 fix) or wire it "
                "into the closed loop (D3 alternate fix)."
            )


# ===========================================================================
# D4 — Failure.attempt must come from the recovery engine, not be hardcoded
# ===========================================================================

class TestD4FailureAttempt:
    def test_failure_attempt_from_engine(self):
        # Construct a recovery engine that has recorded 3 attempts
        # for step "s1".  The Agent's _failure_from_step helper
        # should then produce a Failure with attempt=3, not
        # attempt=1 (the current hardcoded value).
        policy = RecoveryPolicy()
        engine = DefaultRecoveryEngine(policy=policy)
        # Record 3 attempts for "s1"
        for _ in range(3):
            engine.record_attempt("s1")
        # Engine must expose attempts_for
        assert hasattr(engine, "attempts_for"), (
            "DefaultRecoveryEngine must expose attempts_for(step_id) "
            "so the Agent can read the attempt counter (D4)."
        )
        expected = max(1, int(engine.attempts_for("s1")))
        # Build an agent with our engine.  The Agent requires
        # an interpreter, planner, and plan_executor; pass
        # minimal stub objects that satisfy the Protocols.
        class _StubInterpreter:
            name = "stub"
            def interpret(self, text, *, context_snapshot=None):
                return None
        class _StubPlanner:
            name = "stub"
            def plan(self, goal, *, intent=None, context_snapshot=None):
                return None
        class _StubExecutor:
            name = "stub"
            def execute(self, context, *, cancellation_token=None):
                return None
        agent = Agent(
            interpreter=_StubInterpreter(),
            planner=_StubPlanner(),
            plan_executor=_StubExecutor(),
            recovery_engine=engine,
            policy=AgentPolicy(),
        )
        # Build a StepResult for s1 (status EXECUTION failed)
        from core.orchestration.execution_result import StepResult, StepState
        sr = StepResult(
            step_id="s1",
            capability_name="noop",
            status=StepState.FAILED,
            started_at=0.0,
            completed_at=0.0,
            duration_ms=0.0,
            error="boom",
        )
        plan = Plan(plan_id="p1", goal_id="g1")
        f = agent._failure_from_step(sr, plan)
        assert f.attempt == expected, (
            f"_failure_from_step returned attempt={f.attempt}, "
            f"expected {expected} from the recovery engine"
        )


# ===========================================================================
# D6 — PlanStep.max_retries is dead (deprecate then remove)
# ===========================================================================

class TestD6MaxRetriesDeprecated:
    def test_max_retries_emits_deprecation_warning(self, recwarn):
        # PlanStep currently accepts max_retries but never reads
        # it.  We change it to issue a DeprecationWarning so the
        # field is dropped in a future release.
        with pytest.warns(DeprecationWarning):
            PlanStep(
                step_id="d6",
                capability_name="noop",
                description="d",
                max_retries=2,
            )


# ===========================================================================
# D8 — _emit_step_finished must not silently swallow exceptions
# ===========================================================================

class TestD8EmitStepFinishedLogsExceptions:
    def test_silent_except_replaced_with_log(self):
        import os
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "core", "orchestration", "plan_executor.py",
        )
        src = open(path, "r", encoding="utf-8").read()
        # Find the block immediately after the bus publish — there
        # must be NO bare `except Exception: pass` after
        # _publish_bus in _emit_step_finished.
        # Locate the method
        idx = src.find("def _emit_step_finished(")
        assert idx > 0
        # The method body to the next method
        next_def = src.find("\n    def ", idx + 1)
        body = src[idx:next_def if next_def > 0 else len(src)]
        # The forbidden pattern: a bare `except Exception:` followed
        # by `pass` within the next 8 lines (no logging).
        bad = "except Exception:  # noqa: BLE001\n            pass"
        assert bad not in body, (
            "_emit_step_finished still has a bare 'except Exception: pass'. "
            "Replace with logger.debug(...) so sink failures are visible."
        )


# ===========================================================================
# D10 — pipeline must call agent.run_goal(goal, intent), not agent.run(text)
# ===========================================================================

class TestD10PipelineBrainAgentSoo:
    def test_pipeline_does_not_invoke_agent_run_text(self):
        import os
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "core", "pipeline.py",
        )
        src = open(path, "r", encoding="utf-8").read()
        # After Phase 5, the pipeline must call run_goal, not run(text).
        # The legacy ``agent.run(text)`` wrapper is allowed ONLY
        # inside an explicit ``else`` branch (i.e. a fallback when
        # run_goal is unavailable).  We grep for the call signature
        # and check the surrounding context for either ``run_goal``
        # or the explicit fallback comment.
        forbidden = "self.agent.run("
        if forbidden in src:
            i = 0
            ok = True
            while True:
                i = src.find(forbidden, i)
                if i < 0:
                    break
                # Look at the surrounding 20 lines.
                window_start = max(0, i - 400)
                window_end = min(len(src), i + 400)
                window = src[window_start:window_end]
                # OK if the window contains a run_goal call OR
                # the legacy-fallback comment.
                if (
                    "run_goal" in window
                    and (
                        "legacy" in window.lower()
                        or "fallback" in window.lower()
                        or "else:" in window
                    )
                ):
                    i += len(forbidden)
                    continue
                ok = False
                break
            assert ok, (
                "pipeline.py still calls self.agent.run(...) outside "
                "the documented legacy/fallback wrapper.  Replace "
                "with run_goal(goal, intent)."
            )


# ===========================================================================
# D11 — CancellationRequested is raised but never checked
# ===========================================================================

class TestD11CancellationToken:
    def test_cancellation_module_exists(self):
        # Phase 4 introduces a CancellationToken.  After Phase 1
        # wiring, the Agent must consult the token at the top of
        # its loop.  We assert that the Agent's _run_goal method
        # references the token at least once.
        import inspect
        from core.orchestration.agent import Agent
        src = inspect.getsource(Agent._run_goal)
        assert "is_cancelled" in src or "cancel_token" in src, (
            "Agent._run_goal does not consult a cancellation token. "
            "Phase 4 must add a CancellationToken and check it at the "
            "top of every loop iteration."
        )


# ===========================================================================
# D15 — _pending_next_plan is typed
# ===========================================================================

class TestD15PendingPlanTyped:
    def test_pending_next_plan_is_typed_optional(self):
        import inspect
        from core.orchestration.agent import Agent
        # Check that the attribute annotation is Optional[Plan]
        src = inspect.getsource(Agent)
        # The annotation must use Optional and Plan
        assert "_pending_next_plan: Optional[Plan]" in src, (
            "Agent._pending_next_plan is not typed as Optional[Plan]"
        )


# ===========================================================================
# D16 / D20 — Fast-path must check cancellation and surface agent_state
# ===========================================================================

class TestD20FastPathAgentState:
    def test_fast_path_response_carries_agent_state(self):
        import os
        path = os.path.join(
            os.path.dirname(__file__),
            "..", "core", "pipeline.py",
        )
        src = open(path, "r", encoding="utf-8").read()
        # Find the fast-path block (after the comment "0. Fast path")
        idx = src.find("# 0. Fast path")
        assert idx > 0
        # The fast path is from idx to the next "# 1." comment
        end = src.find("# 1.", idx)
        block = src[idx:end if end > 0 else len(src)]
        # The response built in the fast path must carry
        # agent_state.  We assert that the response construction
        # is not bare "agent_state=None" for the success path.
        # Allow None ONLY for the failure path.  For success
        # the field should be AgentState.COMPLETE.
        # Heuristic: the success branch must reference a
        # non-None agent state value.
        assert "AgentState.COMPLETE" in block, (
            "Fast-path success response must carry agent_state="
            "AgentState.COMPLETE (or the equivalent for fast-path), "
            "not None (D20)."
        )


# ===========================================================================
# D19 — consolidate 4 timeout config fields into 1
# ===========================================================================

class TestD19SingleTimeoutConfig:
    def test_canonical_timeout_field_present(self):
        from core.configuration import OmnixConfig
        # Phase 1 / D19 introduced the canonical
        # ``default_step_timeout_s`` field.  The 4 legacy
        # timeout fields are kept for one release cycle so
        # existing .env files do not break.  In a future
        # release, only ``default_step_timeout_s`` will remain.
        assert hasattr(OmnixConfig, "default_step_timeout_s"), (
            "OmnixConfig must expose a single canonical "
            "default_step_timeout_s field (D19)."
        )
        # The canonical field should be a float.
        from pathlib import Path
        cfg = OmnixConfig(
            project_root=Path("."),
            data_dir=Path("."),
            log_dir=Path("."),
            env_file=Path(".env"),
        )
        assert isinstance(cfg.default_step_timeout_s, float)
        # It should be > 0 (a real timeout, not a sentinel).
        assert cfg.default_step_timeout_s > 0.0


# ===========================================================================
# D23 — recovery kind map must include 6 UI failure kinds
# ===========================================================================

class TestD23UIKindMap:
    def test_six_ui_kinds_in_recovery_map(self):
        from core.orchestration.recovery import (
            _DEFAULT_KIND_TO_ACTION,
        )
        required = {
            FailureKind.TARGET_NOT_FOUND,
            FailureKind.FOCUS_FAILED,
            FailureKind.WINDOW_NOT_READY,
            FailureKind.STALE_TARGET,
            FailureKind.PROVIDER_FAILURE,
            FailureKind.PERMISSION_FAILURE,
        }
        # First, the FailureKind enum itself must have these
        # values.
        for kind in required:
            assert hasattr(FailureKind, kind.name), (
                f"FailureKind.{kind.name} is not defined (D23)"
            )
        # Second, each must map to a default recovery action.
        for kind in required:
            assert kind in _DEFAULT_KIND_TO_ACTION, (
                f"FailureKind.{kind.name} is not in "
                f"_DEFAULT_KIND_TO_ACTION (D23)"
            )
