"""
Omnix V6 — Synchronization & State-Settling Foundation (Stage 19.3).

This module defines the generic synchronization abstraction used by the
ExecutionCycle to wait for the environment to become "settled" after an
action, before requesting a fresh observation for verification.

Key design rules:

* The synchronization layer is **deterministic** — it does not call any LLM,
  it does not reason about applications, and it does not implement
  application-specific waiters.
* The mechanism is **bounded** — every synchronization is wrapped with
  both a timeout and a cancellation token. There is no
  ``while True: observe()`` pattern.
* The mechanism is **observation-based** — it relies on the existing
  perception provider to obtain fresh observations. It does not introduce
  new perception backends.
* The mechanism is **cache-aware** — it always invalidates the
  observation cache before obtaining the first post-action observation,
  so that stale data is never mistaken for fresh settlement evidence.
* The mechanism is **contextual** — when an expected state or a target
  region is provided, it prefers expectation-driven waiting over
  pixel-level global change detection.

Public surface:

* :class:`SynchronizationStatus` — closed set of synchronization outcomes.
* :class:`SynchronizationContext` — per-call execution context.
* :class:`SynchronizationResult` — structured synchronization result.
* :class:`SynchronizationProvider` — provider protocol.
* :class:`DefaultSynchronizationProvider` — default implementation.
* :func:`create_default_synchronization_provider` — factory function.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Protocol, runtime_checkable
from uuid import uuid4

# Reuse existing types where appropriate
from vision.perception_contract import (
    PerceptionProvider,
    PerceptionRequest,
    PerceptionResult,
    PerceptionStatus,
    ScreenInfo,
)
from core.orchestration.cancellation import CancellationToken

from .expectation import VerificationExpectation, ExpectationKind
from .state import ExecutionState


class SynchronizationStatus(str, Enum):
    """Closed set of synchronization outcomes.

    The set deliberately mirrors the conventions used by other
    execution-level enums (VerificationStatus, PreconditionStatus).
    """
    SETTLED = "settled"            # environment reached expected / stable state
    TIMEOUT = "timeout"            # bounded wait exhausted before settlement
    CANCELLED = "cancelled"        # cancellation token tripped
    INCONCLUSIVE = "inconclusive"  # could not produce a definitive verdict
    ERROR = "error"                # an exception was raised during synchronization


@dataclass(frozen=True)
class SynchronizationContext:
    """Per-call context for synchronization.

    Carries the data needed to evaluate settlement without re-deriving
    it from the action result.

    The context is intentionally lightweight and immutable. It is the
    minimal information the default provider needs to decide whether
    the environment is settled.
    """
    # The execution step id, used purely for diagnostics.
    step_id: str = ""

    # The pre-action observation id, used to detect that the cache
    # must not return this observation as "fresh" evidence.
    before_observation_id: Optional[str] = None

    # Optional expectation describing the desired post-action state.
    expectation: VerificationExpectation = field(
        default_factory=VerificationExpectation.none
    )

    # Optional pre-action execution state. The default provider uses
    # this only to compare relevant-state slots for contextual stability.
    pre_state: Optional[ExecutionState] = None

    # Free-form metadata for downstream consumers and tests.
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SynchronizationResult:
    """Structured result of a synchronization attempt.

    ``status`` is the authoritative outcome. ``settled`` is a
    convenience boolean mirroring ``status == SynchronizationStatus.SETTLED``.
    """
    status: SynchronizationStatus
    settled: bool
    observation_id: Optional[str] = None
    confidence: float = 0.0
    elapsed_ms: float = 0.0
    reason: str = ""
    poll_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Ensure `settled` agrees with `status`.
        if self.status == SynchronizationStatus.SETTLED and not self.settled:
            object.__setattr__(self, "settled", True)
        elif self.settled and self.status != SynchronizationStatus.SETTLED:
            # Never let `settled=True` lie about a non-SETTLED status.
            object.__setattr__(self, "settled", False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "settled": self.settled,
            "observation_id": self.observation_id,
            "confidence": self.confidence,
            "elapsed_ms": self.elapsed_ms,
            "reason": self.reason,
            "poll_count": self.poll_count,
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class SynchronizationProvider(Protocol):
    """Canonical synchronization provider interface.

    A SynchronizationProvider waits for the observable environment to
    reach a sufficiently stable state for the next phase of execution.

    It MUST:

    * Be bounded (timeout + cancellation).
    * Use a fresh perception observation as settlement evidence
      (never reuse a pre-action observation as proof of settlement).
    * Invalidate (or otherwise defeat) the perception cache for the
      pre-action observation so stale data cannot be mistaken for fresh.
    * Honor an optional expectation by treating its presence as
      evidence of settlement.
    * Return a structured :class:`SynchronizationResult`.
    * Not call any LLM.
    """
    name: str

    async def wait_until_settled(
        self,
        context: SynchronizationContext,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.05,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> SynchronizationResult:
        """Wait for the environment to become settled.

        Args:
            context: Per-call synchronization context.
            timeout_s: Hard upper bound on the wait.
            poll_interval_s: Minimum interval between polls.
            cancellation_token: Optional token for cancelling the wait.

        Returns:
            SynchronizationResult describing the outcome.
        """
        ...


# ---------------------------------------------------------------------------
# Default implementation
# ---------------------------------------------------------------------------

class DefaultSynchronizationProvider:
    """Default synchronization provider.

    Settlement strategy (in priority order):

    1. **Expectation-driven**: if ``context.expectation`` describes a
       concrete post-action state (e.g. target visible, window focused,
       text present), poll until a fresh observation shows that state,
       or the timeout expires.

    2. **Contextual stability**: if a pre-action state is provided,
       poll for two consecutive observations whose relevant-state slots
       (window context, foreground flag) are equivalent. Irrelevant
       desktop changes do not block settlement.

    3. **Single-observation fallback**: if neither an expectation nor a
       pre-state is available, one fresh observation is enough to
       consider the environment settled (because the previous
       observation was invalidated).

    The provider always invalidates the perception cache (if available)
    for the pre-action observation id before beginning, so that
    settlement evidence cannot be drawn from a stale cached entry.
    """

    def __init__(
        self,
        perception_provider: PerceptionProvider,
        perception_cache: Optional[Any] = None,
        name: str = "default_synchronization_provider",
    ) -> None:
        self._perception_provider = perception_provider
        # The cache is duck-typed: anything that exposes a sync or async
        # ``invalidate`` method is acceptable. The provider is best-effort
        # about cache invalidation — failures are logged but never raised.
        self._perception_cache = perception_cache
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    # ---------------------------------------------------------- public
    async def wait_until_settled(
        self,
        context: SynchronizationContext,
        *,
        timeout_s: float = 5.0,
        poll_interval_s: float = 0.05,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> SynchronizationResult:
        """Wait for the environment to become settled.

        The method is bounded by ``timeout_s`` and the cancellation
        token. It always returns a structured result, never raises
        for timeouts or cancellation.
        """
        start_time = time.time()
        poll_count = 0

        # 1. Invalidate any cached observation that could be mistaken
        #    for fresh settlement evidence.
        await self._invalidate_pre_action_cache(context)

        # 2. Choose a settlement strategy based on context.
        has_expectation = (
            context.expectation is not None
            and context.expectation.kind != ExpectationKind.NONE
        )
        has_pre_state = context.pre_state is not None

        # Cap poll_interval to a sensible minimum so tests can run fast.
        effective_poll_interval = max(0.001, float(poll_interval_s))

        deadline = start_time + max(0.0, float(timeout_s))

        last_observation: Optional[PerceptionResult] = None
        stable_streak = 0  # number of consecutive equivalent observations

        try:
            while True:
                poll_count += 1

                # Check cancellation first.
                if cancellation_token is not None and getattr(
                    cancellation_token, "is_cancelled", False
                ):
                    return SynchronizationResult(
                        status=SynchronizationStatus.CANCELLED,
                        settled=False,
                        observation_id=(
                            last_observation.observation_id
                            if last_observation is not None
                            else None
                        ),
                        elapsed_ms=(time.time() - start_time) * 1000,
                        reason="Cancelled during synchronization",
                        poll_count=poll_count,
                        metadata={"context_step_id": context.step_id},
                    )

                # Check timeout.
                now = time.time()
                if now >= deadline:
                    return SynchronizationResult(
                        status=SynchronizationStatus.TIMEOUT,
                        settled=False,
                        observation_id=(
                            last_observation.observation_id
                            if last_observation is not None
                            else None
                        ),
                        elapsed_ms=(now - start_time) * 1000,
                        reason="Synchronization timeout",
                        poll_count=poll_count,
                        metadata={"context_step_id": context.step_id},
                    )

                # Obtain a fresh observation. This is always direct from
                # the perception provider; the cache must not be allowed
                # to return a pre-action observation.
                try:
                    observation = await asyncio.wait_for(
                        self._perception_provider.observe(
                            self._build_request(),
                            cancellation_token,
                        ),
                        timeout=max(0.001, deadline - now),
                    )
                except asyncio.TimeoutError:
                    return SynchronizationResult(
                        status=SynchronizationStatus.TIMEOUT,
                        settled=False,
                        observation_id=(
                            last_observation.observation_id
                            if last_observation is not None
                            else None
                        ),
                        elapsed_ms=(time.time() - start_time) * 1000,
                        reason="Perception observe timed out during sync",
                        poll_count=poll_count,
                        metadata={"context_step_id": context.step_id},
                    )
                except Exception as exc:  # noqa: BLE001
                    return SynchronizationResult(
                        status=SynchronizationStatus.ERROR,
                        settled=False,
                        observation_id=(
                            last_observation.observation_id
                            if last_observation is not None
                            else None
                        ),
                        elapsed_ms=(time.time() - start_time) * 1000,
                        reason=f"Perception observe error: {exc}",
                        poll_count=poll_count,
                        metadata={"context_step_id": context.step_id},
                    )

                # Reject observations that are themselves failed.
                if observation.status not in (
                    PerceptionStatus.SUCCESS,
                    PerceptionStatus.PARTIAL,
                ):
                    # If perception failed and we're near deadline, return
                    # INCONCLUSIVE. Otherwise loop and try again.
                    if time.time() >= deadline:
                        return SynchronizationResult(
                            status=SynchronizationStatus.INCONCLUSIVE,
                            settled=False,
                            observation_id=observation.observation_id,
                            elapsed_ms=(time.time() - start_time) * 1000,
                            reason=f"Perception returned {observation.status.value}",
                            poll_count=poll_count,
                            metadata={"context_step_id": context.step_id},
                        )
                    await asyncio.sleep(effective_poll_interval)
                    continue

                # Reject observations that match the invalidated pre-action
                # observation. This guards against the cache somehow
                # returning the pre-action observation as fresh.
                if (
                    context.before_observation_id is not None
                    and observation.observation_id == context.before_observation_id
                ):
                    if time.time() >= deadline:
                        return SynchronizationResult(
                            status=SynchronizationStatus.TIMEOUT,
                            settled=False,
                            observation_id=observation.observation_id,
                            elapsed_ms=(time.time() - start_time) * 1000,
                            reason="Stale pre-action observation returned",
                            poll_count=poll_count,
                            metadata={"context_step_id": context.step_id},
                        )
                    await asyncio.sleep(effective_poll_interval)
                    continue

                # 3. Apply the chosen settlement strategy.
                if has_expectation:
                    satisfied = self._expectation_satisfied(
                        context.expectation, observation
                    )
                    if satisfied:
                        return SynchronizationResult(
                            status=SynchronizationStatus.SETTLED,
                            settled=True,
                            observation_id=observation.observation_id,
                            confidence=observation.metadata.get("confidence", 1.0)
                            if isinstance(observation.metadata, dict)
                            else 1.0,
                            elapsed_ms=(time.time() - start_time) * 1000,
                            reason=f"Expectation satisfied: {context.expectation.kind.value}",
                            poll_count=poll_count,
                            metadata={"context_step_id": context.step_id},
                        )
                elif has_pre_state:
                    # Contextual stability: require two consecutive
                    # observations whose relevant-state slots match.
                    if last_observation is not None and self._is_contextually_stable(
                        context.pre_state, last_observation, observation
                    ):
                        # We already have last_observation equivalent to
                        # the new one; that is a stable streak of 2.
                        return SynchronizationResult(
                            status=SynchronizationStatus.SETTLED,
                            settled=True,
                            observation_id=observation.observation_id,
                            confidence=1.0,
                            elapsed_ms=(time.time() - start_time) * 1000,
                            reason="Contextually stable (two equivalent observations)",
                            poll_count=poll_count,
                            metadata={"context_step_id": context.step_id},
                        )
                else:
                    # Fallback: a single fresh observation is enough.
                    return SynchronizationResult(
                        status=SynchronizationStatus.SETTLED,
                        settled=True,
                        observation_id=observation.observation_id,
                        confidence=1.0,
                        elapsed_ms=(time.time() - start_time) * 1000,
                        reason="Single fresh observation (no expectation, no pre_state)",
                        poll_count=poll_count,
                        metadata={"context_step_id": context.step_id},
                    )

                last_observation = observation

                # Sleep until next poll (or the deadline).
                sleep_for = min(effective_poll_interval, max(0.0, deadline - time.time()))
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                else:
                    # Deadline reached during processing.
                    return SynchronizationResult(
                        status=SynchronizationStatus.TIMEOUT,
                        settled=False,
                        observation_id=(
                            last_observation.observation_id
                            if last_observation is not None
                            else None
                        ),
                        elapsed_ms=(time.time() - start_time) * 1000,
                        reason="Deadline reached",
                        poll_count=poll_count,
                        metadata={"context_step_id": context.step_id},
                    )
        except asyncio.CancelledError:
            # Cooperative cancellation surfaced through asyncio.
            return SynchronizationResult(
                status=SynchronizationStatus.CANCELLED,
                settled=False,
                observation_id=(
                    last_observation.observation_id
                    if last_observation is not None
                    else None
                ),
                elapsed_ms=(time.time() - start_time) * 1000,
                reason="Cancelled (asyncio.CancelledError)",
                poll_count=poll_count,
                metadata={"context_step_id": context.step_id},
            )
        except Exception as exc:  # noqa: BLE001
            return SynchronizationResult(
                status=SynchronizationStatus.ERROR,
                settled=False,
                observation_id=(
                    last_observation.observation_id
                    if last_observation is not None
                    else None
                ),
                elapsed_ms=(time.time() - start_time) * 1000,
                reason=f"Synchronization error: {exc}",
                poll_count=poll_count,
                metadata={"context_step_id": context.step_id},
            )

    # ---------------------------------------------------------- helpers
    def _build_request(self) -> PerceptionRequest:
        """Build a perception request for the post-action observation.

        The request deliberately requests vision and window context so
        that the relevant-state slots used for contextual stability are
        populated.
        """
        return PerceptionRequest(
            include_screenshot=False,
            include_vision=True,
            include_ocr=False,
            include_ui_elements=False,
            include_window_context=True,
            region=None,
            max_age_ms=0,  # Never reuse cached observations during sync
        )

    async def _invalidate_pre_action_cache(
        self, context: SynchronizationContext
    ) -> None:
        """Best-effort cache invalidation for the pre-action observation.

        The cache is duck-typed: any object that exposes ``invalidate``
        (sync or async) and accepts a key argument is acceptable. We
        never raise on cache errors — they are logged but suppressed.
        """
        if self._perception_cache is None:
            return
        invalidate = getattr(self._perception_cache, "invalidate", None)
        if invalidate is None:
            return
        try:
            # Pass None to mean "invalidate everything". This is the
            # safest default because the pre-action observation_id
            # may not match the cache's PerceptionCacheKey shape.
            result = invalidate(key=None)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            # Best-effort: never let cache errors break synchronization.
            pass

    def _expectation_satisfied(
        self, expectation: VerificationExpectation, observation: PerceptionResult
    ) -> bool:
        """Return True iff the expectation is satisfied by the observation.

        Only the kinds that can be verified deterministically from a
        PerceptionResult without LLM calls are supported here. These
        match the set supported by DefaultVerificationProvider.
        """
        kind = expectation.kind
        if kind == ExpectationKind.NONE:
            return True

        if kind in (ExpectationKind.TARGET_VISIBLE, ExpectationKind.TARGET_PRESENT):
            query = (expectation.target_query or "").lower()
            if not query:
                # Without a query we cannot deterministically decide;
                # treat as inconclusive (i.e. not satisfied) and keep polling.
                return False
            for candidate in observation.candidates:
                if getattr(candidate, "text", None) and query in candidate.text.lower():
                    return True
                props = getattr(candidate, "properties", None)
                if isinstance(props, dict):
                    label = (
                        props.get("name")
                        or props.get("label")
                        or props.get("automation_id")
                    )
                    if label and query in label.lower():
                        return True
            return False

        if kind == ExpectationKind.TARGET_ABSENT:
            query = (expectation.target_query or "").lower()
            if not query:
                return True  # trivially absent
            for candidate in observation.candidates:
                if getattr(candidate, "text", None) and query in candidate.text.lower():
                    return False
                props = getattr(candidate, "properties", None)
                if isinstance(props, dict):
                    label = (
                        props.get("name")
                        or props.get("label")
                        or props.get("automation_id")
                    )
                    if label and query in label.lower():
                        return False
            return True

        if kind == ExpectationKind.WINDOW_EXISTS:
            ctx = observation.window_context
            if ctx is None:
                return False
            if expectation.expected_window_title:
                if not ctx.title:
                    return False
                if expectation.expected_window_title.lower() not in ctx.title.lower():
                    return False
            if expectation.expected_application:
                if not ctx.application:
                    return False
                if expectation.expected_application.lower() not in ctx.application.lower():
                    return False
            return True

        if kind == ExpectationKind.WINDOW_FOCUSED:
            ctx = observation.window_context
            if ctx is None or not getattr(ctx, "is_foreground", False):
                return False
            if expectation.expected_window_title and (
                not ctx.title
                or expectation.expected_window_title.lower() not in ctx.title.lower()
            ):
                return False
            if expectation.expected_application and (
                not ctx.application
                or expectation.expected_application.lower() not in ctx.application.lower()
            ):
                return False
            return True

        if kind in (
            ExpectationKind.TEXT_PRESENT,
            ExpectationKind.TEXT_CHANGED,
            ExpectationKind.SCREEN_CHANGED,
            ExpectationKind.FOCUS_CHANGED,
        ):
            # These require OCR or reference comparison not available
            # in this minimal default provider. Treat as not satisfied
            # so the cycle eventually times out (rather than falsely
            # reporting settled).
            return False

        return False

    def _is_contextually_stable(
        self,
        pre_state: ExecutionState,
        observation_a: PerceptionResult,
        observation_b: PerceptionResult,
    ) -> bool:
        """Return True iff the two observations are contextually equivalent.

        "Contextually equivalent" means the slots that matter for the
        execution target have not changed between two consecutive
        observations. We compare window context (title, application,
        foreground flag) and the screen dimensions. Screenshot bytes
        are deliberately not compared — irrelevant desktop changes
        (clock, cursor, notifications) must not block settlement.
        """
        a_ctx = observation_a.window_context
        b_ctx = observation_b.window_context
        if a_ctx is None and b_ctx is None:
            ctx_equal = True
        elif a_ctx is None or b_ctx is None:
            ctx_equal = False
        else:
            ctx_equal = (
                a_ctx.title == b_ctx.title
                and a_ctx.application == b_ctx.application
                and bool(a_ctx.is_foreground) == bool(b_ctx.is_foreground)
                and a_ctx.bounds == b_ctx.bounds
            )
        dims_equal = (
            observation_a.screen.width == observation_b.screen.width
            and observation_a.screen.height == observation_b.screen.height
        )
        return ctx_equal and dims_equal


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_default_synchronization_provider(
    perception_provider: PerceptionProvider,
    perception_cache: Optional[Any] = None,
) -> DefaultSynchronizationProvider:
    """Create a :class:`DefaultSynchronizationProvider`."""
    return DefaultSynchronizationProvider(
        perception_provider=perception_provider,
        perception_cache=perception_cache,
    )
