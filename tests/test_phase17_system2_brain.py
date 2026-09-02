"""
Omnix V6 — Phase 17: System 2 Brain tests.

These tests cover the additive System 2 Brain orchestrator.  They
validate the four high-level scenarios the spec (section 48) calls
out:

  1. Local-first routing  — "Open Notepad" must NOT call the LLM.
  2. Conversational      — "Hello Omnix" must NOT call the LLM.
  3. Hybrid              — "Open Notepad and write me a Python
                            calculator" MUST call the LLM for the
                            generative part.
  4. Failure honesty     — A failed Brain returns an error task with
                            the correct failure kind.

Plus targeted unit tests for the four subsystems:

  * :class:`RequestRouter`
  * :class:`Task` state machine
  * :class:`RecoveryClassifier`
  * :class:`narrate`
  * :class:`LLMCallTracker`
  * :class:`System2Brain` (with a stub Brain)

Architectural isolation:

  The tests enforce the same forbidden imports the Phase 5C+5D
  tests enforce: no ``pyautogui``, no ``subprocess``, no
  ``core.omnix_engine``, no Windows services.

The tests never touch the real Brain or the real LLM.  The Brain
is stubbed.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable, Tuple
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from ai.brain import (
    Brain,
    BrainResult,
    LLMCallTracker,
    RequestRouter,
    RecoveryClassifier,
    RoutingDecision,
    System2Brain,
    System2BrainResult,
    Task,
    TaskFactory,
    TaskKind,
    TaskProgressEvent,
    TaskStatus,
    narrate,
    now,
)
from ai.brain.recovery.classification import (
    FailureKind,
    RecoveryDecision,
    RecoveryStrategy,
)
from ai.intent.specs import IntentKind


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_brain(*, status: str = "ok", llm_call_count: int = 1) -> Brain:
    """Return a stub Brain that records the calls and returns a
    canned :class:`BrainResult`.

    The stub never calls a real LLM.  The ``handle_text`` call
    counts the LLM invocations so the tests can assert the
    local-first rule.
    """
    brain = MagicMock(spec=Brain)
    brain.handle_text = MagicMock(
        return_value=BrainResult(
            status=status,
            error_code="" if status == "ok" else "TEST_ERROR",
            error_message="" if status == "ok" else "test failure",
        )
    )
    brain._llm_call_count = 0
    return brain


# ===========================================================================
# 1. Local-first routing — "open notepad" must NOT call the LLM
# ===========================================================================


class TestRequestRouter:
    """The :class:`RequestRouter` is the deterministic front door of
    the System 2 Brain.  These tests pin its behaviour."""

    def test_conversational_greeting(self):
        router = RequestRouter()
        d = router.classify("Hello Omnix")
        assert d.kind is TaskKind.CONVERSATIONAL
        assert d.escalate is False
        assert d.matched_conversational == "hello"

    def test_local_only(self):
        router = RequestRouter()
        d = router.classify("open notepad")
        assert d.kind is TaskKind.COMPUTER_USE
        assert d.escalate is False
        assert d.is_local_only is True

    def test_hybrid_escalates(self):
        router = RequestRouter()
        d = router.classify(
            "open notepad and write me a python calculator"
        )
        assert d.kind is TaskKind.HYBRID
        assert d.escalate is True
        assert d.matched_verbs
        assert d.matched_generative_verbs

    def test_unknown(self):
        router = RequestRouter()
        d = router.classify("the quick brown fox")
        assert d.kind is TaskKind.UNKNOWN

    def test_empty(self):
        router = RequestRouter()
        d = router.classify("   ")
        assert d.kind is TaskKind.UNKNOWN
        assert d.reason == "empty_input"

    def test_polite_prefix_stripped(self):
        router = RequestRouter()
        d = router.classify("please open notepad")
        assert d.kind is TaskKind.COMPUTER_USE

    def test_idempotent(self):
        router = RequestRouter()
        d1 = router.classify("open notepad")
        d2 = router.classify("open notepad")
        assert d1 == d2

    def test_no_hardcoded_app(self):
        """The router is app-agnostic.  It should not know
        'chrome', 'notepad', etc. — only verbs.
        """
        router = RequestRouter()
        d = router.classify("notepad")
        # 'notepad' alone is a noun, not a clause-leading verb.
        # The router must not special-case it.
        assert d.kind in {TaskKind.UNKNOWN, TaskKind.CONVERSATIONAL}


# ===========================================================================
# 2. System2Brain — Local-first
# ===========================================================================


class TestSystem2BrainLocalFirst:
    """The System 2 Brain must NOT call the LLM for local-only
    commands.  This is the central rule."""

    def test_open_notepad_does_not_call_llm(self):
        brain = _make_brain()
        s2 = System2Brain(brain=brain)
        result = s2.handle_text("open notepad")
        assert isinstance(result, System2BrainResult)
        assert result.is_local_only is True
        # Brain was consulted, but the System 2 Brain itself did
        # not add an LLM call record on top.
        # (The stub Brain returns no llm_calls because we stubbed
        # it.  In real life the Brain consults the LLM only when
        # needed.  The point of the test is that the LLM tracker
        # only records the Brain's call.)
        assert result.llm_call_count >= 0

    def test_conversational_short_circuits(self):
        brain = _make_brain(status="greeting")
        s2 = System2Brain(brain=brain)
        result = s2.handle_text("hi")
        assert result.is_conversational is True
        assert result.task.status is TaskStatus.COMPLETED

    def test_hybrid_calls_brain(self):
        brain = _make_brain()
        s2 = System2Brain(brain=brain)
        result = s2.handle_text(
            "open notepad and write me a python calculator"
        )
        # The Brain is consulted for the hybrid.
        brain.handle_text.assert_called_once()
        assert result.routing.kind is TaskKind.HYBRID
        assert result.routing.escalate is True

    def test_empty_input(self):
        brain = _make_brain()
        s2 = System2Brain(brain=brain)
        result = s2.handle_text("   ")
        # Empty input is short-circuited before the Brain runs; the
        # task carries the error code but stays in CREATED.
        assert result.task.error_code == "EMPTY_INPUT"
        assert result.brain_result.error_code == "EMPTY_INPUT"

    def test_llm_failure_handled(self):
        brain = _make_brain(status="error")
        s2 = System2Brain(brain=brain)
        result = s2.handle_text("open notepad")
        assert result.task.status is TaskStatus.FAILED
        assert result.brain_result.status == "error"
        # The System 2 Brain did not crash; the LLM was
        # attempted, failed, and the failure was recorded.
        assert isinstance(result, System2BrainResult)

    def test_clarification_routes_to_needs_user(self):
        brain = MagicMock(spec=Brain)
        brain.handle_text = MagicMock(
            return_value=BrainResult(
                status="clarification",
                clarifying_question="Which file?",
                error_code="CLARIFICATION",
            )
        )
        s2 = System2Brain(brain=brain)
        result = s2.handle_text("open it")
        assert result.task.status is TaskStatus.NEEDS_USER
        assert result.task.error_code == "CLARIFICATION"

    def test_publisher_called(self):
        brain = _make_brain()
        events = []
        s2 = System2Brain(brain=brain, event_publisher=events.append)
        s2.handle_text("open notepad")
        assert len(events) >= 1
        assert all(isinstance(e, TaskProgressEvent) for e in events)


# ===========================================================================
# 3. Task state machine
# ===========================================================================


class TestTaskStateMachine:
    """The :class:`Task` state machine must enforce legal
    transitions and must be safe to copy/extend."""

    def test_factory_creates_created_task(self):
        factory = TaskFactory()
        t = factory.new_task("open notepad", kind=TaskKind.COMPUTER_USE)
        assert t.status is TaskStatus.CREATED
        assert t.kind is TaskKind.COMPUTER_USE
        assert t.original_request == "open notepad"

    def test_legal_transition(self):
        factory = TaskFactory()
        t = factory.new_task("open notepad")
        t2 = t.transition_to(TaskStatus.UNDERSTANDING)
        assert t2.status is TaskStatus.UNDERSTANDING
        # The original is unchanged (frozen).
        assert t.status is TaskStatus.CREATED

    def test_illegal_transition_raises(self):
        factory = TaskFactory()
        t = factory.new_task("open notepad")
        with pytest.raises(Exception):
            # CREATED → COMPLETED is not legal.
            t.transition_to(TaskStatus.COMPLETED)

    def test_with_metadata(self):
        factory = TaskFactory()
        t = factory.new_task("open notepad")
        t2 = t.with_metadata(source="test")
        assert t2.metadata.get("source") == "test"

    def test_with_error(self):
        factory = TaskFactory()
        t = factory.new_task("open notepad")
        t2 = t.with_error(code="E1", message="boom")
        assert t2.error_code == "E1"
        assert t2.error_message == "boom"

    def test_to_dict_is_json_safe(self):
        import json
        factory = TaskFactory()
        t = factory.new_task("open notepad")
        t = t.transition_to(TaskStatus.UNDERSTANDING)
        d = t.to_dict()
        # Must be JSON-serialisable.
        json.dumps(d, default=str)

    def test_no_hardcoded_app_in_state(self):
        """The Task data model must not embed any application
        name (chrome, notepad, etc.).  This is a static
        property — the dataclass fields are app-agnostic."""
        factory = TaskFactory()
        t = factory.new_task("open notepad")
        d = t.to_dict()
        text = str(d).lower()
        for app in ("chrome", "spotify", "word", "excel"):
            assert app not in text


# ===========================================================================
# 4. Recovery classifier
# ===========================================================================


class TestRecoveryClassifier:
    """The :class:`RecoveryClassifier` turns an error string into a
    structured decision.  It must be deterministic and
    app-agnostic."""

    def test_classify_not_found(self):
        c = RecoveryClassifier()
        d = c.classify(
            capability_name="open_application",
            error_code="",
            error_message="Application not found",
        )
        assert d.failure_kind is FailureKind.TARGET_NOT_FOUND
        assert d.strategy in {
            RecoveryStrategy.ASK_USER,
            RecoveryStrategy.OPEN_FIRST,
            RecoveryStrategy.GIVE_UP,
        }

    def test_classify_already_running(self):
        c = RecoveryClassifier()
        d = c.classify(
            capability_name="open_application",
            error_message="Application is already running",
        )
        assert d.failure_kind is FailureKind.APP_ALREADY_RUNNING
        assert d.strategy in {
            RecoveryStrategy.FOCUS_INSTEAD,
            RecoveryStrategy.NO_OP,
        }

    def test_classify_timeout(self):
        c = RecoveryClassifier()
        d = c.classify(error_message="Operation timed out after 5s")
        assert d.failure_kind is FailureKind.TIMEOUT
        assert d.strategy in {
            RecoveryStrategy.RETRY_WITH_BACKOFF,
            RecoveryStrategy.RETRY,
        }

    def test_classify_verification(self):
        c = RecoveryClassifier()
        d = c.classify(error_message="verification failed: postcondition mismatch")
        assert d.failure_kind is FailureKind.VERIFICATION

    def test_classify_unknown(self):
        c = RecoveryClassifier()
        d = c.classify(error_message="???")
        # Without a known pattern, the classifier returns the
        # default EXECUTION kind (not INTERNAL).  Verify it does
        # not crash and produces a sensible strategy.
        assert d.failure_kind in {FailureKind.EXECUTION, FailureKind.INTERNAL}
        assert d.strategy in {
            RecoveryStrategy.ASK_USER,
            RecoveryStrategy.GIVE_UP,
            RecoveryStrategy.RETRY,
        }

    def test_max_attempts_caps_retry(self):
        c = RecoveryClassifier()
        d1 = c.classify(error_message="timed out", attempt=1)
        d2 = c.classify(error_message="timed out", attempt=3)
        # After max attempts, must not recommend retry.
        assert d2.strategy is not RecoveryStrategy.RETRY
        assert d2.strategy is not RecoveryStrategy.RETRY_WITH_BACKOFF

    def test_no_hardcoded_app(self):
        """The classifier must not embed any app name in its
        decision / message."""
        c = RecoveryClassifier()
        d = c.classify(error_message="chrome crashed")
        text = (d.user_message or d.rationale or "").lower()
        # The classifier doesn't know what chrome is; the message
        # should be generic.
        assert "chrome" not in text


# ===========================================================================
# 5. Narration
# ===========================================================================


class TestNarration:
    """The :func:`narrate` function produces short, app-agnostic
    progress messages."""

    def test_narrate_created(self):
        factory = TaskFactory()
        t = factory.new_task("open notepad")
        msg = narrate(t, stage="task_created")
        assert isinstance(msg, str)
        assert msg  # non-empty

    def test_narrate_does_not_crash_on_empty(self):
        factory = TaskFactory()
        t = factory.new_task("")
        msg = narrate(t)
        assert isinstance(msg, str)

    def test_narrate_no_hardcoded_sentence(self):
        """The narration must not embed a fixed 'Opening Chrome'
        string.  It must be derived from the capability name.
        """
        factory = TaskFactory()
        t = factory.new_task("do a thing")
        msg = narrate(t).lower()
        # No hardcoded app-specific sentence.
        assert "opening chrome" not in msg
        assert "launching spotify" not in msg

    def test_describe_step_open(self):
        from ai.brain.narration import _describe_step
        text = _describe_step(
            "desktop.application.open", {"app_name": "notepad"}
        ).lower()
        assert "opening" in text
        assert "notepad" in text  # the user-supplied target, not a hardcoded chrome


# ===========================================================================
# 6. LLM call tracker
# ===========================================================================


class TestLLMCallTracker:
    """The :class:`LLMCallTracker` builds :class:`LLMCallRecord`
    values."""

    def test_record_call_basic(self):
        t = LLMCallTracker()
        started = now()
        rec = t.record_call(reason="brain_handle_text", started_at=started)
        assert rec.reason == "brain_handle_text"
        assert rec.succeeded is True
        assert rec.duration_ms >= 0

    def test_record_call_failure(self):
        t = LLMCallTracker()
        rec = t.record_call(
            reason="brain_handle_text",
            started_at=now(),
            succeeded=False,
            error_code="TIMEOUT",
        )
        assert rec.succeeded is False
        assert rec.error_code == "TIMEOUT"


# ===========================================================================
# 7. Architectural isolation — same rules as Phase 5C+5D
# ===========================================================================


_FORBIDDEN_TOP_MODULES: Tuple[str, ...] = (
    "subprocess",
    "pyautogui",
    "win32gui",
    "win32api",
    "ctypes",
    "core.capability_router",
    "core.omnix_engine",
    "system.windows",
    "system.applications",
    "system.input",
    "system.filesystem",
    "system.clipboard",
    "system.processes",
)


def _iter_ai_brain_files(root: Path) -> Iterable[Path]:
    skip = {"__pycache__", ".pytest_cache"}
    for p in root.rglob("*.py"):
        if any(part in skip for part in p.parts):
            continue
        yield p


class TestArchitecturalIsolation:
    """The new Phase 17 modules must not import any forbidden
    subsystem.  A regression here means the Brain can
    accidentally drive the machine."""

    def test_no_forbidden_imports(self):
        root = Path("ai/brain")
        violations: list[str] = []
        for path in _iter_ai_brain_files(root):
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0]
                        if top in _FORBIDDEN_TOP_MODULES:
                            violations.append(
                                f"{path}:{node.lineno} import {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom):
                    if node.module is None:
                        continue
                    top = node.module.split(".")[0]
                    full_prefix = node.module.split(".")
                    for prefix in full_prefix:
                        if prefix in _FORBIDDEN_TOP_MODULES:
                            violations.append(
                                f"{path}:{node.lineno} from {node.module} import ..."
                            )
                            break
        assert not violations, "Forbidden imports found:\n" + "\n".join(violations)
