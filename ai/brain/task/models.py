"""
Omnix V6 — System 2 Brain task data model (Phase 17).

A :class:`Task` is the user-facing unit of work.  One user
utterance becomes one :class:`Task`.  A :class:`Task` carries:

    * the original user text,
    * the resolved intent (or None for pure-conversational tasks),
    * the goal (or None for pure-conversational tasks),
    * a structured ordered list of :class:`TaskStep` objects,
    * the priority,
    * a state machine status (CREATED, UNDERSTANDING, PLANNING, ...),
    * the LLM call history,
    * the per-step execution history,
    * the verification results,
    * timing for every stage.

This module is **pure data**.  It must never import:

    * :mod:`subprocess`
    * :mod:`pyautogui`
    * :mod:`win32gui` / :mod:`win32api`
    * :mod:`core.capability_router`
    * :mod:`core.omnix_engine`
    * :mod:`ai.provider.*`
    * any V6 *Windows service* (e.g. ``system.windows.*``)

Tests in ``tests/test_task_model.py`` enforce the isolation.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


def now() -> float:
    """Single source of wall-clock time.  Test seam."""
    return time.time()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskKind(str, Enum):
    """The high-level category of a user request.

    The Brain classifies every input as exactly one of these.  The
    routing decision flows from this classification:

      * CONVERSATIONAL  — pure social input ("Hello", "Thanks").
                          No subsystem is invoked.  No LLM call.
      * COMPUTER_USE    — local deterministic action ("open Notepad",
                          "type hello").  LLM called only on failure.
      * HYBRID          — a deterministic *and* a generative part
                          ("open Notepad and write me a Python
                          calculator").  LLM is used for the
                          generative part; local subsystems for
                          the rest.
      * UNKNOWN         — cannot classify.  The Brain asks the
                          interpreter (LLM) for help.
    """

    CONVERSATIONAL = "conversational"
    COMPUTER_USE = "computer_use"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class TaskStatus(str, Enum):
    """The Brain's authoritative task state machine.

    The state machine is *strict*; the only legal transitions are
    listed in :data:`_LEGAL_TRANSITIONS`.  Any other transition
    raises :class:`ValueError` from :meth:`Task.transition_to`.
    """

    CREATED = "created"
    UNDERSTANDING = "understanding"
    PLANNING = "planning"
    READY = "ready"
    EXECUTING = "executing"
    WAITING = "waiting"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    NEEDS_USER = "needs_user"

    def is_terminal(self) -> bool:
        return self in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        )


# Legal transitions.  ``None`` means "from this state we can stay
# here but cannot move on" (e.g. CREATED is the start; we move
# into UNDERSTANDING next).
_LEGAL_TRANSITIONS: Dict[TaskStatus, Tuple[TaskStatus, ...]] = {
    TaskStatus.CREATED: (
        TaskStatus.UNDERSTANDING,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    ),
    TaskStatus.UNDERSTANDING: (
        TaskStatus.PLANNING,
        TaskStatus.READY,
        TaskStatus.NEEDS_USER,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.PLANNING: (
        TaskStatus.READY,
        TaskStatus.COMPLETED,
        TaskStatus.NEEDS_USER,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.READY: (
        TaskStatus.EXECUTING,
        TaskStatus.BLOCKED,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    ),
    TaskStatus.EXECUTING: (
        TaskStatus.WAITING,
        TaskStatus.VERIFYING,
        TaskStatus.RECOVERING,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.WAITING: (
        TaskStatus.VERIFYING,
        TaskStatus.EXECUTING,
        TaskStatus.RECOVERING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.VERIFYING: (
        TaskStatus.EXECUTING,
        TaskStatus.RECOVERING,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.RECOVERING: (
        TaskStatus.EXECUTING,
        TaskStatus.READY,
        TaskStatus.NEEDS_USER,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.BLOCKED: (
        TaskStatus.READY,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    ),
    TaskStatus.NEEDS_USER: (
        TaskStatus.UNDERSTANDING,
        TaskStatus.PLANNING,
        TaskStatus.CANCELLED,
        TaskStatus.FAILED,
    ),
    TaskStatus.COMPLETED: (),
    TaskStatus.FAILED: (),
    TaskStatus.CANCELLED: (),
}


class StepStatus(str, Enum):
    """Per-step status.  Independent of the task-level state machine."""

    PENDING = "pending"
    READY = "ready"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class TaskPriority(str, Enum):
    """User-facing priority buckets.  Numeric ``rank`` is a
    sort key; higher rank = higher priority.
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"

    @property
    def rank(self) -> int:
        return {
            TaskPriority.LOW: 0,
            TaskPriority.NORMAL: 1,
            TaskPriority.HIGH: 2,
            TaskPriority.URGENT: 3,
        }[self]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMCallRecord:
    """One LLM invocation.

    Attributes
    ----------
    call_id:
        Stable id, used to correlate downstream observations.
    reason:
        Free-form structured reason (``"intent_interpretation"``,
        ``"plan_synthesis"``, ``"recovery_replan"``, ...).
    step_id:
        The :class:`TaskStep` step_id this call was made for, or
        an empty string when the call is task-level.
    started_at, ended_at:
        Wall-clock seconds.
    duration_ms:
        Computed at record time.
    succeeded:
        True when the LLM returned a parseable result.
    error_code:
        Stable error code on failure (``"PROVIDER_TIMEOUT"`` etc.).
    provider, model:
        Provider name and model id; ``""`` when unknown.
    input_tokens, output_tokens:
        Best-effort token counts; ``None`` when unknown.
    """

    call_id: str
    reason: str = ""
    step_id: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    duration_ms: float = 0.0
    succeeded: bool = True
    error_code: str = ""
    provider: str = ""
    model: str = ""
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_ok(self) -> bool:
        return self.succeeded and not self.error_code


@dataclass(frozen=True)
class VerificationRecord:
    """The outcome of a single verification attempt."""

    step_id: str = ""
    passed: bool = False
    failed: bool = False
    uncertain: bool = False
    reason: str = ""
    checked_at: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_passing(self) -> bool:
        return self.passed and not (self.failed or self.uncertain)


@dataclass(frozen=True)
class StepTrace:
    """A single step's full run history.

    The trace records:

      * the original step spec,
      * the execution attempts (one :class:`StepAttempt` per try),
      * the final status,
      * the final verification,
      * the final error message (if any).
    """

    step_id: str
    status: StepStatus = StepStatus.PENDING
    attempts: Tuple[Dict[str, Any], ...] = ()
    verification: Optional[VerificationRecord] = None
    error: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def duration_ms(self) -> float:
        if self.ended_at and self.started_at:
            return max(0.0, (self.ended_at - self.started_at) * 1000.0)
        return 0.0

    def with_attempt(self, attempt: Dict[str, Any]) -> "StepTrace":
        return replace(self, attempts=self.attempts + (dict(attempt),))

    def with_status(self, status: StepStatus) -> "StepTrace":
        return replace(self, status=status)

    def with_verification(self, v: VerificationRecord) -> "StepTrace":
        return replace(self, verification=v)

    def with_error(self, error: str) -> "StepTrace":
        return replace(self, error=error)

    def with_timing(self, *, started_at: float, ended_at: float) -> "StepTrace":
        return replace(self, started_at=started_at, ended_at=ended_at)


# ---------------------------------------------------------------------------
# TaskStep
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskStep:
    """A single step in a :class:`Task`.

    A :class:`TaskStep` is the *Brain's* view of a step.  It mirrors
    a :class:`core.orchestration.PlanStep` for execution purposes
    but is not coupled to the executor's internal model.  The Brain
    never reads or writes executor state — it only produces /
    consumes :class:`TaskStep` and :class:`StepTrace`.
    """

    step_id: str
    description: str
    capability_name: str
    parameters: Tuple[Tuple[str, Any], ...] = ()
    depends_on: Tuple[str, ...] = ()
    expected_effect: Dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 30.0
    max_retries: int = 1
    safety_tags: Tuple[str, ...] = ()
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ---- helpers -------------------------------------------------------
    def params_dict(self) -> Dict[str, Any]:
        return dict(self.parameters)

    def with_params(self, params: Dict[str, Any]) -> "TaskStep":
        return replace(self, parameters=tuple(sorted(params.items())))


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    """A user-facing unit of work.

    A :class:`Task` is a *value object*.  The Brain mutates it only
    via the ``with_*`` helpers, which always return a new
    :class:`Task` (frozen-immutable).  All state transitions go
    through :meth:`transition_to`, which enforces the legal
    transitions table.
    """

    task_id: str
    original_request: str
    kind: TaskKind = TaskKind.UNKNOWN
    priority: TaskPriority = TaskPriority.NORMAL
    status: TaskStatus = TaskStatus.CREATED

    # intent / plan data
    intent_kind: str = ""            # the IntentKind.value as a string
    intent: Optional[Any] = None     # the resolved Intent (frozen)
    goal: Optional[Any] = None       # the resolved Goal (frozen)
    plan_id: str = ""

    # ordered step list
    steps: Tuple[TaskStep, ...] = ()

    # observability
    llm_calls: Tuple[LLMCallRecord, ...] = ()
    step_traces: Tuple[StepTrace, ...] = ()
    failures: Tuple[Dict[str, Any], ...] = ()
    observations: Tuple[Dict[str, Any], ...] = ()
    verification_results: Tuple[VerificationRecord, ...] = ()

    # context
    context: Dict[str, Any] = field(default_factory=dict)
    constraints: Tuple[str, ...] = ()
    referenced_entities: Tuple[str, ...] = ()

    # timing
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    started_at: float = 0.0
    completed_at: float = 0.0

    # metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    clarifying_question: str = ""

    # ---- derived -------------------------------------------------------
    @property
    def task_run_id(self) -> str:
        """Alias for the integration with the Agent's run id."""
        return self.task_id

    @property
    def current_step_index(self) -> int:
        for idx, trace in enumerate(self.step_traces):
            if trace.status not in (
                StepStatus.SUCCEEDED,
                StepStatus.SKIPPED,
                StepStatus.CANCELLED,
            ):
                return idx
        return len(self.step_traces)

    @property
    def current_step(self) -> Optional[TaskStep]:
        idx = self.current_step_index
        if 0 <= idx < len(self.steps):
            return self.steps[idx]
        return None

    @property
    def total_steps(self) -> int:
        return len(self.steps)

    @property
    def succeeded_steps(self) -> int:
        return sum(
            1 for t in self.step_traces if t.status is StepStatus.SUCCEEDED
        )

    @property
    def failed_steps(self) -> int:
        return sum(
            1 for t in self.step_traces if t.status is StepStatus.FAILED
        )

    @property
    def llm_call_count(self) -> int:
        return len(self.llm_calls)

    @property
    def llm_latency_ms(self) -> float:
        return sum(c.duration_ms for c in self.llm_calls)

    @property
    def duration_ms(self) -> float:
        end = self.completed_at if self.completed_at else self.updated_at
        if end and self.created_at:
            return max(0.0, (end - self.created_at) * 1000.0)
        return 0.0

    # ---- state machine -------------------------------------------------
    def transition_to(self, target: TaskStatus) -> "Task":
        if not _is_legal_transition(self.status, target):
            raise ValueError(
                f"Task {self.task_id}: illegal transition "
                f"{self.status.value} -> {target.value}"
            )
        ts = now()
        new = replace(self, status=target, updated_at=ts)
        if target is TaskStatus.EXECUTING and not self.started_at:
            new = replace(new, started_at=ts)
        if target.is_terminal() and not self.completed_at:
            new = replace(new, completed_at=ts)
        return new

    # ---- mutators (immutable) -----------------------------------------
    def with_step_traces(self, traces: Sequence[StepTrace]) -> "Task":
        return replace(self, step_traces=tuple(traces), updated_at=now())

    def with_llm_call(self, record: LLMCallRecord) -> "Task":
        return replace(self, llm_calls=self.llm_calls + (record,), updated_at=now())

    def with_failure(self, failure: Dict[str, Any]) -> "Task":
        return replace(self, failures=self.failures + (dict(failure),), updated_at=now())

    def with_observation(self, observation: Dict[str, Any]) -> "Task":
        return replace(self, observations=self.observations + (dict(observation),), updated_at=now())

    def with_verification(self, record: VerificationRecord) -> "Task":
        return replace(
            self,
            verification_results=self.verification_results + (record,),
            updated_at=now(),
        )

    def with_error(self, *, code: str, message: str) -> "Task":
        return replace(
            self,
            error_code=code,
            error_message=message,
            updated_at=now(),
        )

    def with_metadata(self, **kv: Any) -> "Task":
        md = dict(self.metadata)
        md.update(kv)
        return replace(self, metadata=md, updated_at=now())

    def with_context(self, **kv: Any) -> "Task":
        ctx = dict(self.context)
        ctx.update(kv)
        return replace(self, context=ctx, updated_at=now())

    def with_steps(self, steps: Sequence[TaskStep]) -> "Task":
        return replace(self, steps=tuple(steps), updated_at=now())

    # ---- serialisation -------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "original_request": self.original_request,
            "kind": self.kind.value,
            "priority": self.priority.value,
            "status": self.status.value,
            "intent_kind": self.intent_kind,
            "plan_id": self.plan_id,
            "total_steps": self.total_steps,
            "current_step_index": self.current_step_index,
            "succeeded_steps": self.succeeded_steps,
            "failed_steps": self.failed_steps,
            "llm_call_count": self.llm_call_count,
            "llm_latency_ms": self.llm_latency_ms,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "clarifying_question": self.clarifying_question,
            "context": dict(self.context),
            "constraints": list(self.constraints),
            "referenced_entities": list(self.referenced_entities),
            "metadata": dict(self.metadata),
        }


def _is_legal_transition(
    current: TaskStatus, target: TaskStatus
) -> bool:
    if current is target:
        # Re-entering the same state is idempotent — allowed for the
        # convenience of "transition" being called on every event.
        return True
    return target in _LEGAL_TRANSITIONS.get(current, ())


# ---------------------------------------------------------------------------
# TaskFactory
# ---------------------------------------------------------------------------


class TaskFactory:
    """The single builder for :class:`Task` objects.

    Centralising construction here makes it impossible to build a
    :class:`Task` in a way that bypasses the canonical id scheme or
    the canonical timestamps.
    """

    def __init__(self, *, prefix: str = "task") -> None:
        self._prefix = prefix

    def new_task(
        self,
        original_request: str,
        *,
        kind: TaskKind = TaskKind.UNKNOWN,
        priority: TaskPriority = TaskPriority.NORMAL,
        context: Optional[Dict[str, Any]] = None,
        constraints: Sequence[str] = (),
        referenced_entities: Sequence[str] = (),
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Task:
        ts = now()
        return Task(
            task_id=f"{self._prefix}_{uuid.uuid4().hex[:12]}",
            original_request=original_request,
            kind=kind,
            priority=priority,
            status=TaskStatus.CREATED,
            created_at=ts,
            updated_at=ts,
            context=dict(context or {}),
            constraints=tuple(constraints),
            referenced_entities=tuple(referenced_entities),
            metadata=dict(metadata or {}),
        )

    def new_step(
        self,
        *,
        description: str,
        capability_name: str,
        parameters: Optional[Dict[str, Any]] = None,
        depends_on: Sequence[str] = (),
        expected_effect: Optional[Dict[str, Any]] = None,
        timeout_s: float = 30.0,
        max_retries: int = 1,
        safety_tags: Sequence[str] = (),
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TaskStep:
        return TaskStep(
            step_id=f"step_{uuid.uuid4().hex[:10]}",
            description=description,
            capability_name=capability_name,
            parameters=tuple(sorted((parameters or {}).items())),
            depends_on=tuple(depends_on),
            expected_effect=dict(expected_effect or {}),
            timeout_s=float(timeout_s),
            max_retries=int(max_retries),
            safety_tags=tuple(safety_tags),
            metadata=dict(metadata or {}),
        )

    def new_llm_call(
        self,
        *,
        reason: str,
        step_id: str = "",
        started_at: float,
        ended_at: float,
        succeeded: bool,
        error_code: str = "",
        provider: str = "",
        model: str = "",
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LLMCallRecord:
        duration_ms = max(0.0, (ended_at - started_at) * 1000.0) if started_at else 0.0
        return LLMCallRecord(
            call_id=f"llm_{uuid.uuid4().hex[:10]}",
            reason=reason,
            step_id=step_id,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            succeeded=bool(succeeded),
            error_code=error_code or "",
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            metadata=dict(metadata or {}),
        )

    def new_verification(
        self,
        *,
        step_id: str,
        passed: bool = False,
        failed: bool = False,
        uncertain: bool = False,
        reason: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> VerificationRecord:
        # XOR enforcement, mirroring the executor's Verifier protocol.
        if sum([bool(passed), bool(failed), bool(uncertain)]) != 1:
            raise ValueError(
                "VerificationRecord must have exactly one of "
                "passed/failed/uncertain set."
            )
        return VerificationRecord(
            step_id=step_id,
            passed=bool(passed),
            failed=bool(failed),
            uncertain=bool(uncertain),
            reason=reason,
            checked_at=now(),
            metadata=dict(metadata or {}),
        )

    def new_step_trace(
        self,
        *,
        step_id: str,
        status: StepStatus = StepStatus.PENDING,
    ) -> StepTrace:
        return StepTrace(step_id=step_id, status=status)


__all__ = [
    "LLMCallRecord",
    "StepStatus",
    "StepTrace",
    "Task",
    "TaskFactory",
    "TaskKind",
    "TaskPriority",
    "TaskStatus",
    "VerificationRecord",
    "now",
]
