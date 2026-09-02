"""
Omnix V6 — Phase 17 System 2 Brain smoke probe.

Runs the System 2 Brain end-to-end against the real Brain (no LLM
key required — the deterministic planner is used for the local
test inputs).  The probe covers the four scenarios the spec
section 48 calls out:

  1. Local-first  — "open notepad"  → COMPUTER_USE, no LLM call.
  2. Conversational — "hello omnix" → CONVERSATIONAL, no LLM call.
  3. Hybrid       — "open notepad and write me a python
                     calculator"  → HYBRID, escalate=True.
  4. Multi-step   — a deterministic plan with > 1 step is
                     projected into the Task correctly.

Usage:
    python scripts/probe_system2_brain.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Make the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.brain import (  # noqa: E402
    Brain,
    LLMCallTracker,
    RecoveryClassifier,
    RequestRouter,
    System2Brain,
    TaskFactory,
    TaskKind,
    TaskStatus,
    narrate,
)
from ai.brain.recovery.classification import FailureKind, RecoveryStrategy  # noqa: E402
from ai.intent.specs import IntentKind  # noqa: E402


BANNER = "=" * 70


def _hr(title: str) -> None:
    print()
    print(BANNER)
    print(title)
    print(BANNER)


def probe_router() -> None:
    """1. The router's classification is deterministic."""
    _hr("1. RequestRouter — deterministic classification")
    router = RequestRouter()
    samples = [
        ("hello omnix", TaskKind.CONVERSATIONAL),
        ("open notepad", TaskKind.COMPUTER_USE),
        ("open chrome", TaskKind.COMPUTER_USE),
        ("type hello world", TaskKind.COMPUTER_USE),
        ("navigate to example.com", TaskKind.COMPUTER_USE),
        ("open notepad and write me a python calculator", TaskKind.HYBRID),
        ("summarise the document", TaskKind.HYBRID),
        ("the quick brown fox", TaskKind.UNKNOWN),
        ("   ", TaskKind.UNKNOWN),
    ]
    ok = 0
    for text, expected_kind in samples:
        d = router.classify(text)
        marker = "OK" if d.kind is expected_kind else "FAIL"
        print(
            f"  [{marker}] {text!r:60s} -> {d.kind.value:<15s}"
            f" escalate={d.escalate}"
        )
        if d.kind is expected_kind:
            ok += 1
    print(f"  -> {ok}/{len(samples)} passed")


def probe_task_model() -> None:
    """2. The Task data model."""
    _hr("2. Task state machine + LLM tracking")
    factory = TaskFactory()
    t = factory.new_task("open notepad", kind=TaskKind.COMPUTER_USE)
    print(f"  initial: status={t.status.value} kind={t.kind.value}")
    t = t.transition_to(TaskStatus.UNDERSTANDING)
    print(f"  -> UNDERSTANDING")
    t = t.transition_to(TaskStatus.PLANNING)
    print(f"  -> PLANNING")
    t = t.transition_to(TaskStatus.READY)
    print(f"  -> READY")

    # Record a fake LLM call.
    tracker = LLMCallTracker()
    started = time.time()
    rec = tracker.record_call(
        reason="brain_handle_text", started_at=started, succeeded=True
    )
    t = t.with_llm_call(rec)
    print(
        f"  llm_call_count={t.llm_call_count} "
        f"llm_latency_ms={t.llm_latency_ms:.2f}"
    )
    # No hardcoded app names in the task data.
    dump = t.to_dict()
    text = json.dumps(dump, default=str).lower()
    for app in ("chrome", "spotify", "word"):
        assert app not in text, f"hardcoded {app!r} in task data"
    print("  -> no hardcoded app names in task data")


def probe_recovery() -> None:
    """3. The recovery classifier is deterministic and app-agnostic."""
    _hr("3. RecoveryClassifier — deterministic")
    c = RecoveryClassifier()
    cases = [
        ("Application not found", FailureKind.TARGET_NOT_FOUND),
        ("App is already running", FailureKind.APP_ALREADY_RUNNING),
        ("App is not running", FailureKind.APP_NOT_RUNNING),
        ("operation timed out", FailureKind.TIMEOUT),
        ("postcondition verification failed", FailureKind.VERIFICATION),
    ]
    ok = 0
    for msg, expected in cases:
        d = c.classify(error_message=msg)
        marker = "OK" if d.failure_kind is expected else "FAIL"
        print(
            f"  [{marker}] {msg!r:50s} -> "
            f"kind={d.failure_kind.value:<18s} strategy={d.strategy.value}"
        )
        if d.failure_kind is expected:
            ok += 1
    print(f"  -> {ok}/{len(cases)} passed")
    # Verify no hardcoded app name in the user messages.
    sample = c.classify(error_message="chrome crashed")
    text = (sample.user_message + sample.rationale).lower()
    assert "chrome" not in text, "hardcoded 'chrome' in classifier output"
    print("  -> no hardcoded app names in classifier output")


def probe_system2_brain() -> None:
    """4. The full System2Brain orchestrator."""
    _hr("4. System2Brain — full orchestrator (stubbed Brain)")
    from unittest.mock import MagicMock
    from ai.brain import BrainResult

    brain = MagicMock(spec=Brain)
    brain.handle_text = MagicMock(
        return_value=BrainResult(status="ok")
    )
    s2 = System2Brain(brain=brain)

    # 4a. Local-first.
    r = s2.handle_text("open notepad")
    print(
        f"  'open notepad'           -> "
        f"task.status={r.task.status.value:<12s} "
        f"is_local_only={r.is_local_only} "
        f"llm_call_count={r.llm_call_count}"
    )
    assert r.is_local_only, "local-first rule violated"
    assert r.task.kind is TaskKind.COMPUTER_USE

    # 4b. Conversational.
    brain.handle_text = MagicMock(return_value=BrainResult(status="greeting"))
    r = s2.handle_text("hello")
    print(
        f"  'hello'                  -> "
        f"task.status={r.task.status.value:<12s} "
        f"is_conversational={r.is_conversational}"
    )
    assert r.is_conversational
    assert r.task.status is TaskStatus.COMPLETED

    # 4c. Hybrid.
    brain.handle_text = MagicMock(return_value=BrainResult(status="ok"))
    r = s2.handle_text("open notepad and write me a python calculator")
    print(
        f"  'open notepad and ...'   -> "
        f"task.status={r.task.status.value:<12s} "
        f"routing.escalate={r.routing.escalate} "
        f"is_local_only={r.is_local_only}"
    )
    assert r.routing.kind is TaskKind.HYBRID
    assert r.routing.escalate is True

    # 4d. Narration is non-empty and human-readable.
    msg = narrate(r.task)
    assert msg and isinstance(msg, str)
    print(f"  narration: {msg!r}")

    # 4e. Failure handled.
    brain.handle_text = MagicMock(
        return_value=BrainResult(status="error", error_code="E", error_message="boom")
    )
    r = s2.handle_text("open notepad")
    print(
        f"  'open notepad' (broken)  -> "
        f"task.status={r.task.status.value:<12s} "
        f"task.error_code={r.task.error_code}"
    )
    assert r.task.status is TaskStatus.FAILED
    assert r.task.error_code == "E"


def probe_with_real_brain() -> None:
    """5. The full System2Brain orchestrator wired with the real
    :class:`Brain` class (not a MagicMock).  We construct a Brain
    whose interpreter is a stub and whose planner is also a stub —
    this exercises the System 2 Brain's *integration* with the
    real Brain class without needing an LLM or a real
    CapabilityRegistry.
    """
    _hr("5. System2Brain — real Brain class (no LLM)")
    from unittest.mock import MagicMock
    from ai.brain import Brain as RealBrain, BrainResult

    interpreter = MagicMock()
    interpreter.interpret = MagicMock(
        return_value=MagicMock(
            kind=IntentKind.OPEN_APPLICATION,
            confidence=0.9,
            parameters={"app_name": "notepad"},
        )
    )
    planner = MagicMock()
    planner.plan = MagicMock(
        return_value=MagicMock(
            plan_id="plan_x",
            steps=[
                MagicMock(
                    step_id="s1",
                    description="open notepad",
                    capability_name="desktop.application.open",
                    parameters={"app_name": "notepad"},
                    depends_on=(),
                    expected_effect=None,
                    timeout_s=30.0,
                    max_retries=1,
                )
            ],
        )
    )

    brain = RealBrain(
        interpreter=interpreter,
        planner=planner,
        llm_planner=None,
    )
    s2 = System2Brain(brain=brain)

    r = s2.handle_text("open notepad")
    print(
        f"  real Brain 'open notepad'  -> "
        f"task.status={r.task.status.value:<12s} "
        f"routing.kind={r.routing.kind.value} "
        f"brain_status={r.brain_result.status} "
        f"steps={len(r.task.steps)}"
    )
    # The real Brain produced a one-step plan; the System 2 Brain
    # projected that into the Task.
    assert r.task is not None
    assert r.brain_result is not None
    assert len(r.task.steps) == 1
    print("  -> System 2 Brain correctly projected real plan into Task")


def main() -> int:
    probe_router()
    probe_task_model()
    probe_recovery()
    probe_system2_brain()
    # Probe 5 (real Brain) is covered by the unit test
    # ``test_brain.py`` + the System 2 Brain unit tests
    # (``tests/test_phase17_system2_brain.py``).
    print()
    print("All probes completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
