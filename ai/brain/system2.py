"""
Omnix V6 — System 2 Brain orchestrator (Phase 17).

The System 2 Brain is the *additive* upgrade layer on top of the
existing :class:`ai.brain.Brain`.  It is the canonical central
seam that:

  * routes every user utterance to a :class:`TaskKind` (conversational
    / computer_use / hybrid / unknown) using
    :class:`ai.brain.router.RequestRouter`,
  * tracks the structured :class:`Task` (with state, steps, traces,
    LLM call history, verification, timings),
  * delegates *all* intent interpretation to the existing Brain
    (no behaviour drift for the LLM call),
  * tracks every LLM call through :class:`LLMCallTracker`,
  * publishes :class:`TaskProgressEvent` to the event bus,
  * turns the task state into a short narration via
    :func:`ai.brain.narration.narrate`,
  * classifies failures with :class:`RecoveryClassifier`.

The orchestrator is **additive, backward compatible**.  It does
not modify the existing Brain, the existing Agent, the existing
Planner, or the existing Pipeline.  It is consumed by the
:class:`core.pipeline.RequestPipeline` through its
``process(text)`` method when the engine wires it in.

The orchestrator is also **strictly read-only with respect to
side effects** — it never imports a Windows service, a
capability, or an LLM provider.  The :class:`ai.brain.Brain` it
delegates to is already isolated; this module preserves that
isolation.

Public surface:

    * :class:`System2Brain`         — the orchestrator
    * :class:`System2BrainResult`   — the structured result

This module must never import:

    * :mod:`subprocess`
    * :mod:`pyautogui`
    * :mod:`win32gui` / :mod:`win32api`
    * :mod:`core.capability_router`
    * :mod:`core.omnix_engine`
    * :mod:`ai.provider.*`
    * any V6 *Windows service* (e.g. ``system.windows.*``)
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence

from .brain import Brain, BrainResult
from .llm_tracking import LLMCallTracker
from .narration import TaskProgressEvent, narrate
from .recovery.classification import (
    FailureKind,
    RecoveryClassifier,
    RecoveryDecision,
    RecoveryStrategy,
)
from .router import RequestRouter, RoutingDecision
from .task.models import (
    LLMCallRecord,
    StepStatus,
    StepTrace,
    Task,
    TaskFactory,
    TaskKind,
    TaskStatus,
    VerificationRecord,
    now,
)


# ---------------------------------------------------------------------------
# Result wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class System2BrainResult:
    """The structured outcome of :meth:`System2Brain.handle_text`.

    The result carries:

      * the routing decision,
      * the :class:`Task` itself (so the pipeline can see the full
        state machine + step traces + LLM history),
      * the :class:`ai.brain.BrainResult` (the legacy surface the
        pipeline already understands),
      * the :class:`RecoveryDecision` (when a failure was classified),
      * a narration message the TTS layer can speak.
    """

    task: Task
    brain_result: BrainResult
    routing: RoutingDecision
    recovery: Optional[RecoveryDecision] = None
    narration: str = ""
    llm_calls: tuple = ()
    duration_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_conversational(self) -> bool:
        return self.routing.kind is TaskKind.CONVERSATIONAL

    @property
    def is_local_only(self) -> bool:
        return self.routing.is_local_only

    @property
    def llm_call_count(self) -> int:
        return len(self.llm_calls)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "routing": {
                "kind": self.routing.kind.value,
                "escalate": self.routing.escalate,
                "reason": self.routing.reason,
                "matched_verbs": list(self.routing.matched_verbs),
                "matched_generative_verbs": list(self.routing.matched_generative_verbs),
            },
            "brain_result": self.brain_result.to_dict() if self.brain_result else None,
            "narration": self.narration,
            "llm_call_count": self.llm_call_count,
            "duration_ms": self.duration_ms,
            "metadata": dict(self.metadata),
        }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class System2Brain:
    """The System 2 Brain orchestrator.

    The orchestrator is constructed once at engine boot.  It is
    read-only: it never imports a side-effecting subsystem, never
    dispatches a capability, never calls a provider directly.
    The :class:`ai.brain.Brain` it delegates to owns the LLM call.
    """

    def __init__(
        self,
        *,
        brain: Brain,
        router: Optional[RequestRouter] = None,
        recovery_classifier: Optional[RecoveryClassifier] = None,
        llm_tracker: Optional[LLMCallTracker] = None,
        factory: Optional[TaskFactory] = None,
        event_publisher: Optional[Any] = None,
        max_task_steps: int = 64,
    ) -> None:
        if brain is None or not isinstance(brain, Brain):
            raise TypeError("System2Brain requires a Brain")
        self._brain = brain
        self._router = router or RequestRouter()
        self._classifier = recovery_classifier or RecoveryClassifier()
        self._tracker = llm_tracker or LLMCallTracker()
        self._factory = factory or TaskFactory()
        self._publisher = event_publisher  # optional callable
        self._max_task_steps = int(max_task_steps)

    # --------------------------------------------------------------- API
    def handle_text(
        self,
        text: str,
        *,
        context_snapshot: Optional[Mapping[str, Any]] = None,
        priority: str = "normal",
    ) -> System2BrainResult:
        """Run the System 2 pipeline on ``text``.

        The orchestrator:

          1. Routes the text via :class:`RequestRouter`.
          2. Builds a :class:`Task` with the resolved kind.
          3. Delegates to the existing :class:`ai.brain.Brain` for
             intent interpretation and planning.  This is the
             *only* place the LLM is consulted.
          4. Maps the Brain's :class:`BrainResult` into the Task.
          5. Publishes a :class:`TaskProgressEvent` for every state
             transition.
          6. Returns a :class:`System2BrainResult` the pipeline can
             consume.
        """
        t0 = time.time()
        if not isinstance(text, str) or not text.strip():
            return self._empty_result(text, t0, reason="empty_input")

        # 1. Route.
        routing = self._router.classify(text)
        task = self._factory.new_task(
            original_request=text,
            kind=routing.kind,
            priority=_parse_priority(priority),
            context=dict(context_snapshot) if context_snapshot else {},
            metadata={
                "routing_reason": routing.reason,
                "matched_verbs": list(routing.matched_verbs),
                "matched_generative_verbs": list(routing.matched_generative_verbs),
            },
        )
        task = task.with_metadata(routing_kind=routing.kind.value)
        self._publish(task, stage="task_created")

        # 2. Conversational short-circuit.  The Brain still runs
        # (to get the canonical greeting reply), but we mark the
        # task as conversational.
        if routing.kind is TaskKind.CONVERSATIONAL:
            task = task.transition_to(TaskStatus.UNDERSTANDING)
            self._publish(task, stage="task_understanding")
            # The Brain's greeting pre-classification is the
            # canonical detector; we still delegate.
            return self._run_brain(
                task=task,
                routing=routing,
                text=text,
                context_snapshot=context_snapshot,
                t0=t0,
            )

        # 3. Local-only — the Brain still runs (to get the plan),
        # but we mark the task as a local computer-use task.
        if routing.is_local_only:
            task = task.transition_to(TaskStatus.UNDERSTANDING)
            self._publish(task, stage="task_understanding")
            return self._run_brain(
                task=task,
                routing=routing,
                text=text,
                context_snapshot=context_snapshot,
                t0=t0,
            )

        # 4. Hybrid / unknown — full Brain path with LLM.
        task = task.transition_to(TaskStatus.UNDERSTANDING)
        self._publish(task, stage="task_understanding")
        return self._run_brain(
            task=task,
            routing=routing,
            text=text,
            context_snapshot=context_snapshot,
            t0=t0,
        )

    def classify_failure(
        self,
        *,
        capability_name: str = "",
        error_code: str = "",
        error_message: str = "",
        attempt: int = 1,
    ) -> RecoveryDecision:
        """Expose the recovery classifier for downstream use.

        The pipeline may call this when a step's
        :class:`core.results.CapabilityResult` shows a failure.
        """
        return self._classifier.classify(
            capability_name=capability_name,
            error_code=error_code,
            error_message=error_message,
            attempt=attempt,
        )

    # --------------------------------------------------------------- helpers
    def _run_brain(
        self,
        *,
        task: Task,
        routing: RoutingDecision,
        text: str,
        context_snapshot: Optional[Mapping[str, Any]],
        t0: float,
    ) -> System2BrainResult:
        # Planning transition.
        task = task.transition_to(TaskStatus.PLANNING)
        self._publish(task, stage="task_planning")

        # Brain call — this is the only place the LLM is invoked.
        llm_started = now()
        try:
            brain_result = self._brain.handle_text(
                text,
                context_snapshot=dict(context_snapshot) if context_snapshot else None,
            )
            llm_call = self._tracker.record_call(
                reason="brain_handle_text",
                step_id="",
                started_at=llm_started,
                ended_at=now(),
                succeeded=brain_result.status != "error",
                error_code=brain_result.error_code or "",
            )
        except Exception as exc:  # noqa: BLE001
            llm_call = self._tracker.record_call(
                reason="brain_handle_text",
                step_id="",
                started_at=llm_started,
                ended_at=now(),
                succeeded=False,
                error_code="BRAIN_RAISED",
                metadata={"exception": type(exc).__name__},
            )
            task = task.with_llm_call(llm_call)
            task = task.with_error(
                code="BRAIN_RAISED",
                message=str(exc),
            )
            return self._finalize(
                task=task,
                routing=routing,
                brain_result=None,
                t0=t0,
            )
        task = task.with_llm_call(llm_call)

        # Map BrainResult → Task.
        task = self._absorb_brain_result(task, brain_result)
        # Stage transition.
        if brain_result.status == "ok":
            task = task.transition_to(TaskStatus.READY)
            self._publish(task, stage="task_ready")
        elif brain_result.status == "clarification":
            task = task.with_error(
                code="CLARIFICATION",
                message=brain_result.clarifying_question or "Clarification needed",
            )
            task = task.transition_to(TaskStatus.NEEDS_USER)
            self._publish(task, stage="task_needs_user")
        elif brain_result.status == "unknown":
            task = task.with_error(
                code="INTENT_UNKNOWN",
                message=brain_result.error_message or "Intent not understood",
            )
            task = task.transition_to(TaskStatus.BLOCKED)
            self._publish(task, stage="task_blocked")
        elif brain_result.status == "greeting":
            # Conversational: mark completed and short-circuit.
            task = task.with_metadata(brain_status="greeting")
            task = task.transition_to(TaskStatus.COMPLETED)
            self._publish(task, stage="task_completed")
        else:  # error
            task = task.transition_to(TaskStatus.FAILED)
            self._publish(task, stage="task_failed")
        return self._finalize(
            task=task,
            routing=routing,
            brain_result=brain_result,
            t0=t0,
        )

    def _absorb_brain_result(self, task: Task, br: BrainResult) -> Task:
        task = task.with_metadata(
            brain_status=br.status,
            brain_stage=(br.metadata.get("stage") if br.metadata else "") or "",
        )
        if br.intent is not None:
            try:
                task = task.with_metadata(
                    intent_kind=str(br.intent.kind.value)
                )
                task = replace_attr(task, "intent_kind", str(br.intent.kind.value))
            except Exception:  # noqa: BLE001
                pass
            # ``task.intent`` lives on the dataclass; we use the
            # ``with_context`` channel to keep the orchestrator
            # pure.
            task = task.with_context(intent=br.intent)
        if br.goal is not None:
            task = task.with_context(goal=br.goal)
        if br.plan is not None:
            task = replace_attr(task, "plan_id", br.plan.plan_id)
            # Project plan.steps into TaskStep list.
            try:
                task = self._project_plan(task, br.plan)
            except Exception:  # noqa: BLE001
                pass
        if br.clarifying_question:
            task = replace_attr(
                task,
                "clarifying_question",
                br.clarifying_question,
            )
        if br.error_code:
            task = task.with_error(
                code=br.error_code,
                message=br.error_message or "",
            )
        return task

    def _project_plan(self, task: Task, plan: Any) -> Task:
        if plan is None or not getattr(plan, "steps", None):
            return task
        steps = []
        for s in plan.steps[: self._max_task_steps]:
            try:
                steps.append(self._factory.new_step(
                    description=getattr(s, "description", "") or "",
                    capability_name=getattr(s, "capability_name", "") or "",
                    parameters=dict(getattr(s, "parameters", {}) or {}),
                    depends_on=list(getattr(s, "depends_on", ()) or ()),
                    expected_effect=_effect_to_dict(
                        getattr(s, "expected_effect", None)
                    ),
                    timeout_s=float(getattr(s, "timeout_s", 30.0) or 30.0),
                    max_retries=int(getattr(s, "max_retries", 1) or 1),
                ))
            except Exception:  # noqa: BLE001
                continue
        task = task.with_steps(steps)
        # Initialise step traces.
        traces = tuple(
            self._factory.new_step_trace(step_id=st.step_id, status=StepStatus.PENDING)
            for st in steps
        )
        return task.with_step_traces(traces)

    def _empty_result(
        self,
        text: Any,
        t0: float,
        *,
        reason: str,
    ) -> System2BrainResult:
        task = self._factory.new_task(
            original_request=str(text) if isinstance(text, str) else "",
            kind=TaskKind.UNKNOWN,
        )
        task = task.with_error(code="EMPTY_INPUT", message=reason)
        return System2BrainResult(
            task=task,
            brain_result=BrainResult(status="error", error_code="EMPTY_INPUT", error_message=reason),
            routing=RoutingDecision(kind=TaskKind.UNKNOWN, reason=reason),
            narration="I didn't catch that. Could you say it again?",
            duration_ms=(time.time() - t0) * 1000.0,
        )

    def _finalize(
        self,
        *,
        task: Task,
        routing: RoutingDecision,
        brain_result: Optional[BrainResult],
        t0: float,
    ) -> System2BrainResult:
        narration = narrate(task)
        return System2BrainResult(
            task=task,
            brain_result=brain_result
            or BrainResult(
                status="error",
                error_code=task.error_code or "BRAIN_ERROR",
                error_message=task.error_message or "Brain returned no result",
            ),
            routing=routing,
            narration=narration,
            llm_calls=tuple(task.llm_calls),
            duration_ms=(time.time() - t0) * 1000.0,
        )

    def _publish(self, task: Task, *, stage: str, step_index: int = -1) -> None:
        if self._publisher is None:
            return
        try:
            evt = TaskProgressEvent(
                task_id=task.task_id,
                stage=stage,
                status=task.status.value,
                step_index=step_index if step_index >= 0 else task.current_step_index,
                total_steps=task.total_steps,
                message=narrate(task, stage=stage, step_index=step_index),
                timestamp=now(),
            )
            self._publisher(evt)
        except Exception:  # noqa: BLE001
            # Publishing must never break the Brain.
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_priority(raw: Any) -> Any:
    from .task.models import TaskPriority
    if isinstance(raw, TaskPriority):
        return raw
    s = str(raw or "").strip().lower()
    table = {
        "low": TaskPriority.LOW,
        "normal": TaskPriority.NORMAL,
        "high": TaskPriority.HIGH,
        "urgent": TaskPriority.URGENT,
    }
    return table.get(s, TaskPriority.NORMAL)


def _effect_to_dict(effect: Any) -> Dict[str, Any]:
    if effect is None:
        return {}
    try:
        if isinstance(effect, Mapping):
            return dict(effect)
        return {
            "check_name": getattr(effect, "check_name", ""),
            "expected": getattr(effect, "expected", None),
            "timeout_s": getattr(effect, "timeout_s", 0.0),
            "description": getattr(effect, "description", ""),
        }
    except Exception:  # noqa: BLE001
        return {}


def replace_attr(task: Task, field: str, value: Any) -> Task:
    """Return a copy of ``task`` with ``field`` set to ``value``.

    Used for the few Task fields that are not exposed via
    ``with_*`` helpers (intent_kind, plan_id, clarifying_question).
    Imported here to avoid leaking the dataclass ``replace`` into
    the public surface.
    """
    from dataclasses import replace
    try:
        return replace(task, **{field: value})
    except Exception:  # noqa: BLE001
        return task


__all__ = ["System2Brain", "System2BrainResult"]
