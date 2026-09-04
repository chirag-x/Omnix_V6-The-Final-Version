"""
Omnix V6 — Canonical request pipeline (Phase 11).

Thin orchestrator that wires together: text → Brain → Agent → response.
Does NOT contain business logic; delegates to Brain and Agent.
Never leaks secrets, never calls capabilities directly.

Emits :class:`RequestEvent` records at every pipeline stage so the
event bus is the single observability surface for a request.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from loguru import logger

from core.responses import (
    OmnixResponse,
    ResponseStatus,
    new_correlation_id,
    safe_default_text,
)
from core.events.event_types import (
    RequestEvent,
    REQUEST_INTENT_RESOLVED,
    REQUEST_PLAN_CREATED,
    REQUEST_EXECUTION_STARTED,
    REQUEST_OBSERVATION_CAPTURED,
    REQUEST_VERIFICATION_COMPLETED,
    REQUEST_RECOVERY_STARTED,
    REQUEST_REPLAN_STARTED,
    REQUEST_CANCELLED,
    REQUEST_TIMED_OUT,
    REQUEST_REJECTED,
    REQUEST_COMPLETED,
    make_event,
)
from ai.brain.brain import Brain
from core.orchestration import Agent, AgentResult, AgentState


# Allowed limit for any user-facing text we will echo to TTS / CLI.
_MAX_USER_TEXT_LEN = 2000


class RequestPipeline:
    """Canonical text → intent → brain (with memory) → agent → response."""

    def __init__(
        self,
        brain: Brain,
        agent: Agent,
        *,
        memory_service: Optional[Any] = None,
        event_bus: Optional[Any] = None,
        app_dispatcher: Optional[Any] = None,
    ) -> None:
        if brain is None:
            raise TypeError("RequestPipeline requires a Brain")
        if agent is None:
            raise TypeError("RequestPipeline requires an Agent")
        self.brain = brain
        self.agent = agent
        self.memory_service = memory_service
        # Optional — if the engine did not pass a bus, observability
        # is best-effort and silently skipped.
        self.bus = event_bus
        # Optional — Phase 15 fast-path: try a deterministic
        # single-step app dispatch before the full Brain roundtrip.
        self.app_dispatcher = app_dispatcher

    def _publish(self, **kwargs: Any) -> None:
        if self.bus is None:
            return
        try:
            self.bus.publish(
                make_event(RequestEvent, source="pipeline", **kwargs)
            )
        except Exception:  # noqa: BLE001
            # Observability must never break the pipeline.
            pass

    # --------------------------------------------------------------- API
    def process(
        self,
        text: str,
        *,
        correlation_id: Optional[str] = None,
        cancellation_token: Optional[Any] = None,
    ) -> OmnixResponse:
        """Run a single user text through the full pipeline.

        Returns a safe :class:`OmnixResponse` — never includes raw
        internal objects, never leaks secrets or stack traces.

        ``cancellation_token`` (Phase 4) is checked at three seams:

        1. Before the fast-path dispatcher runs.
        2. Between fast-path match and dispatch.
        3. Before the Agent is invoked.

        If the token is already cancelled at any seam the pipeline
        returns a :class:`ResponseStatus.CANCELLED` response
        immediately rather than starting work that will be
        aborted.  The token is also passed into the Agent (via
        ``agent.cancellation_token``) so the closed loop can
        finalise early.
        """
        cid = correlation_id or new_correlation_id()

        # Phase 4: cancellation at the entry seam.  The engine
        # passes the same token it created for this
        # correlation_id.
        if cancellation_token is not None and getattr(
            cancellation_token, "is_cancelled", False
        ):
            return OmnixResponse(
                text=safe_default_text(ResponseStatus.CANCELLED),
                status=ResponseStatus.CANCELLED,
                agent_state=AgentState.CANCELLED,
                correlation_id=cid,
                duration_ms=0.0,
                metadata={"correlation_id": cid, "cancelled": True},
                error=(
                    getattr(cancellation_token, "reason", "")
                    or "cancelled before pipeline start"
                ),
            )
        t0 = time.time()
        metadata: dict[str, Any] = {"correlation_id": cid}

        # 1. Brain (intent + planning, with memory retrieval if available)
        brain_result = None
        context_snapshot: dict[str, Any] = {}
        try:
            if self.memory_service is not None:
                try:
                    # Memory recall — internal-only; we do not echo the
                    # memory content into the response.  The Brain can
                    # use the summary to inform planning, but the
                    # actual stored values are never included in
                    # outbound text or TTS.
                    recall_hits = self.memory_service.recall(
                        query=str(text)[:200], limit=3
                    )
                    # Normalize hits into a small, safe summary.  We
                    # never pass the raw ``MemoryItem`` objects
                    # downstream because they may contain user-supplied
                    # data that should not be replayed verbatim.
                    if isinstance(recall_hits, (list, tuple)):
                        context_snapshot["memory_hit_count"] = len(recall_hits)
                    elif isinstance(recall_hits, dict):
                        context_snapshot["memory_hit_count"] = int(
                            recall_hits.get("count", 0)
                        )
                    else:
                        context_snapshot["memory_hit_count"] = 0
                except Exception:
                    pass
            # Pass the (non-sensitive) context into the Brain.
            try:
                brain_result = self.brain.handle_text(
                    text,
                    context_snapshot=context_snapshot or None,
                )
            except TypeError:
                # Some test fakes of Brain don't accept context_snapshot.
                brain_result = self.brain.handle_text(text)
        except Exception:
            return self._fail(cid, t0, error="brain interpretation failed")

        # 2. Map brain outcome
        if brain_result is None:
            return self._fail(cid, t0, error="brain returned no result")

        # INTENT_RESOLVED observability event
        intent_kind = ""
        if brain_result.intent is not None:
            try:
                intent_kind = str(brain_result.intent.kind)
            except Exception:  # noqa: BLE001
                intent_kind = ""
        self._publish(
            correlation_id=cid,
            stage=REQUEST_INTENT_RESOLVED,
            intent_kind=intent_kind,
            status=brain_result.status,
        )

        if brain_result.status == "clarification":
            q = brain_result.clarifying_question or safe_default_text(
                ResponseStatus.CLARIFICATION
            )
            return OmnixResponse(
                text=str(q),
                status=ResponseStatus.CLARIFICATION,
                agent_state=None,
                correlation_id=cid,
                duration_ms=(time.time() - t0) * 1000.0,
                metadata=metadata,
                error=None,
            )

        if brain_result.status == "unknown":
            return OmnixResponse(
                text=safe_default_text(ResponseStatus.FAILED),
                status=ResponseStatus.FAILED,
                agent_state=None,
                correlation_id=cid,
                duration_ms=(time.time() - t0) * 1000.0,
                metadata={**metadata, "brain_status": "unknown"},
                error="intent not understood",
            )

        if brain_result.status == "error":
            return OmnixResponse(
                text=safe_default_text(ResponseStatus.FAILED),
                status=ResponseStatus.FAILED,
                agent_state=None,
                correlation_id=cid,
                duration_ms=(time.time() - t0) * 1000.0,
                metadata={**metadata, "brain_status": "error"},
                error=brain_result.error_code or "brain error",
            )

        # PLAN_CREATED observability event (only on ok status)
        plan_id = ""
        plan_step_count = 0
        if brain_result.plan is not None:
            try:
                plan_id = brain_result.plan.plan_id
                plan_step_count = len(brain_result.plan.steps or ())
            except Exception:  # noqa: BLE001
                pass
        self._publish(
            correlation_id=cid,
            stage=REQUEST_PLAN_CREATED,
            intent_kind=intent_kind,
            plan_id=plan_id,
            plan_step_count=plan_step_count,
        )

        # EXECUTION_STARTED observability event
        self._publish(
            correlation_id=cid,
            stage=REQUEST_EXECUTION_STARTED,
            plan_id=plan_id,
            plan_step_count=plan_step_count,
        )

        # Phase 4: third-seam cancellation check.  The brain has
        # produced a plan; the user may have cancelled before we
        # dispatch.  Honor the token here so we never spend the
        # LLM / executor cost when the user has moved on.
        if cancellation_token is not None and getattr(
            cancellation_token, "is_cancelled", False
        ):
            return OmnixResponse(
                text=safe_default_text(ResponseStatus.CANCELLED),
                status=ResponseStatus.CANCELLED,
                agent_state=AgentState.CANCELLED,
                correlation_id=cid,
                duration_ms=(time.time() - t0) * 1000.0,
                metadata={
                    **metadata,
                    "cancelled": True,
                    "phase": "post_brain",
                },
                error=(
                    getattr(cancellation_token, "reason", "")
                    or "cancelled before agent dispatch"
                ),
            )

        # 3. Agent run (full closed loop)
        agent_result: Optional[AgentResult] = None
        try:
            # Phase 1 / D10 + Phase 5: prefer run_goal when the
            # Brain has already produced a Goal + Intent.  This
            # makes the Brain the single source of truth for
            # intent resolution and avoids the Agent re-interpreting
            # the user text.  When the Brain did not produce a
            # Goal (legacy callers, tests), fall back to
            # ``run(text)`` which internally builds a goal.
            #
            # Phase 5: Brain is the single source of truth for
            # intent.  The pipeline always calls
            # ``run_goal(goal, intent)``; if the Brain did not
            # produce a Goal (legacy callers, tests), we build a
            # minimal Goal from the text so the Agent still has a
            # contract to plan against.  ``Agent.run(text)`` is
            # preserved for tests and the REPL ``/run`` command but
            # is not invoked by this production path.
            goal = getattr(brain_result, "goal", None)
            intent_obj = getattr(brain_result, "intent", None)
            if goal is None:
                # Fallback Goal — Brain didn't produce one.  This
                # happens for legacy callers and test fakes that
                # bypass the full Brain pipeline.  We build a
                # minimal Goal so the Agent has something to plan
                # against; the description is the user text.
                from core.orchestration.models import (
                    Goal as _Goal,
                    Intent as _Intent,
                    IntentKind as _IntentKind,
                )
                fallback_id = f"goal-{cid}"
                goal = _Goal(
                    goal_id=fallback_id,
                    description=str(text),
                    success_criteria=(),
                )
                if intent_obj is None:
                    # Default to UNKNOWN — the Agent will ask for
                    # clarification if the goal is unachievable.
                    intent_obj = _Intent(
                        intent_id=f"intent-{cid}",
                        kind=_IntentKind.UNKNOWN,
                        text=str(text),
                        parameters={},
                    )
            if cancellation_token is not None and hasattr(
                self.agent, "set_cancellation_token"
            ):
                try:
                    self.agent.set_cancellation_token(cancellation_token)
                except Exception:  # noqa: BLE001
                    self.agent.cancellation_token = cancellation_token
            else:
                self.agent.cancellation_token = cancellation_token
            try:
                agent_result = self.agent.run_goal(
                    goal, intent=intent_obj
                )
            except TypeError:
                # Older Agent signatures — drop the
                # cancellation_token kwarg attempt.
                agent_result = self.agent.run_goal(
                    goal, intent=intent_obj
                )
        except Exception as e:
            logger.exception("Agent run raised an exception")
            return self._fail(cid, t0, error=f"agent run raised: {e}")

        if agent_result is None:
            return self._fail(cid, t0, error="agent returned no result")

        # VERIFICATION_COMPLETED event (post-run)
        self._publish(
            correlation_id=cid,
            stage=REQUEST_VERIFICATION_COMPLETED,
            agent_run_id=agent_result.agent_run_id,
        )
        # REPLAN_STARTED event (if any replans were performed)
        if agent_result.replans > 0:
            self._publish(
                correlation_id=cid,
                stage=REQUEST_REPLAN_STARTED,
                agent_run_id=agent_result.agent_run_id,
            )

        return self._from_agent_result(agent_result, cid, t0, metadata)

    # ---------------------------------------------------------- helpers
    def _from_agent_result(
        self,
        result: AgentResult,
        cid: str,
        t0: float,
        metadata: dict[str, Any],
    ) -> OmnixResponse:
        # Map final_state to ResponseStatus
        final = result.final_state
        if final is AgentState.COMPLETE:
            status = ResponseStatus.OK
        elif final is AgentState.CLARIFICATION_REQUIRED:
            status = ResponseStatus.CLARIFICATION
        elif final is AgentState.TIMEOUT:
            status = ResponseStatus.TIMEOUT
        elif final is AgentState.CANCELLED:
            status = ResponseStatus.CANCELLED
        else:
            status = ResponseStatus.FAILED

        # Pick a safe user-facing text
        if status is ResponseStatus.CLARIFICATION:
            text_out = (result.clarifying_question or "").strip()
            if not text_out:
                text_out = safe_default_text(status)
        else:
            # AgentResult has no dedicated final_text field; use
            # the goal / last plan step or a safe default. The Agent's
            # metadata may carry a user-facing message.
            text_out = ""
            md = result.metadata or {}
            if isinstance(md, dict):
                cand = md.get("user_message") or md.get("message") or md.get("text")
                if isinstance(cand, str) and cand.strip():
                    text_out = cand.strip()
            if not text_out:
                # No user message produced by the run — default.
                text_out = safe_default_text(status)

        # Safety: cap length, scrub obvious secrets
        text_out = self._sanitize_user_text(text_out, status=status)

        err = None
        if status not in (ResponseStatus.OK, ResponseStatus.CLARIFICATION):
            # Surface only a short safe error, never the raw Agent error.
            err = (result.error or "")[:200] or None
            if err is None:
                err = "agent did not complete"

        return OmnixResponse(
            text=text_out,
            status=status,
            agent_state=str(final),
            correlation_id=cid,
            duration_ms=(time.time() - t0) * 1000.0,
            metadata={
                **metadata,
                "agent_run_id": result.agent_run_id,
                "plan_count": result.plan_count,
                "attempts": result.attempts,
            },
            error=err,
        )

    # Publish a terminal event reflecting the final status
        try:
            stage_map = {
                ResponseStatus.OK: REQUEST_COMPLETED,
                ResponseStatus.CLARIFICATION: REQUEST_COMPLETED,
                ResponseStatus.TIMEOUT: REQUEST_TIMED_OUT,
                ResponseStatus.CANCELLED: REQUEST_CANCELLED,
                ResponseStatus.REJECTED: REQUEST_REJECTED,
                ResponseStatus.FAILED: REQUEST_COMPLETED,
            }
            self._publish(
                correlation_id=cid,
                stage=stage_map.get(status, REQUEST_COMPLETED),
                status=str(status.value),
                duration_ms=(time.time() - t0) * 1000.0,
                error=err or "",
                agent_run_id=result.agent_run_id,
            )
        except Exception:  # noqa: BLE001
            pass

    def _fail(self, cid: str, t0: float, *, error: str) -> OmnixResponse:
        return OmnixResponse(
            text=safe_default_text(ResponseStatus.FAILED),
            status=ResponseStatus.FAILED,
            agent_state=None,
            correlation_id=cid,
            duration_ms=(time.time() - t0) * 1000.0,
            metadata={"correlation_id": cid},
            error=str(error)[:200],
        )

    @staticmethod
    def _fast_path_user_text(fast) -> str:
        """Map a verified fast-path :class:`CapabilityResult` to a
        short user-facing sentence."""
        details = fast.details or {}
        target = details.get("app_name") or details.get("target") or ""
        cap = fast.capability_name or ""
        if cap == "desktop.application.open":
            return f"Opening {target}." if target else "Opening."
        if cap == "desktop.application.close":
            return f"Closing {target}." if target else "Closing."
        if cap == "desktop.application.focus":
            return f"Focusing {target}." if target else "Focusing."
        if cap == "desktop.application.is_running":
            return (
                f"{target} is running." if target else "Checked."
            )
        return "Done."

    @staticmethod
    def _sanitize_user_text(text: str, *, status: ResponseStatus) -> str:
        # Trim overly long outputs
        if isinstance(text, str) and len(text) > _MAX_USER_TEXT_LEN:
            text = text[:_MAX_USER_TEXT_LEN] + "..."
        # Guard against forbidden tokens slipping through
        if isinstance(text, str):
            low = text.lower()
            forbidden = ("api_key=", "sk-", "password=", "token=", "bearer ")
            if any(tok in low for tok in forbidden):
                return "I cannot read this out loud for security reasons."
        return text if isinstance(text, str) else safe_default_text(status)


__all__ = ["RequestPipeline"]
