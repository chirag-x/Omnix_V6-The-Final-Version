"""
Omnix V6 — System 8: bounded retry tracking for the Agent.

The recovery engine already enforces a bounded policy (per
:class:`RecoveryPolicy`): a hard cap on ``max_total_runtime_s``,
``max_step_retries``, ``max_replans``, etc.  This module adds a
small, *observable* counter — :class:`RetryTracker` — that the
Agent can update on every retry / replan / failure so the
:class:`ProgressBroadcaster` can show the user, in real time,
how many attempts remain.

Architectural rules honored here:

- R-8   — every status is a typed enum, never a bare bool.
- R-10  — counters are ``frozen=True``; mutation is by ``with_*``.
- R-23  — the tracker never mutates :class:`AgentResult`; it
          produces a new :class:`RetryCounters` value each time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Tuple


# ===========================================================================
# RetryCounters — one snapshot
# ===========================================================================

@dataclass(frozen=True)
class RetryCounters:
    """The Agent's per-run retry counters.

    The Agent owns one :class:`RetryCounters` per run; each
    recovery action that consumes budget increments the
    appropriate counter and produces a new value (R-10).

    Fields
    ------
    step_attempts:
        Map ``step_id → number of attempts so far``.
    replans:
        Number of replans performed.
    failures:
        Total number of :class:`Failure` records observed.
    decisions:
        Total number of :class:`RecoveryDecision` records observed.
    observations:
        Total number of :class:`Observation` records observed.
    step_retries:
        Map ``step_id → number of RETRY actions so far``.
    step_skips:
        Map ``step_id → number of SKIP actions so far``.
    started_at:
        Wall-clock seconds when the counters were first opened.
    """

    step_attempts: Dict[str, int] = field(default_factory=dict)
    replans: int = 0
    failures: int = 0
    decisions: int = 0
    observations: int = 0
    step_retries: Dict[str, int] = field(default_factory=dict)
    step_skips: Dict[str, int] = field(default_factory=dict)
    started_at: float = 0.0

    def with_step_attempt(
        self, step_id: str, delta: int = 1
    ) -> "RetryCounters":
        cur = int(self.step_attempts.get(step_id, 0)) + int(delta)
        new = dict(self.step_attempts)
        new[step_id] = cur
        return replace(self, step_attempts=new)

    def with_step_retry(
        self, step_id: str, delta: int = 1
    ) -> "RetryCounters":
        cur = int(self.step_retries.get(step_id, 0)) + int(delta)
        new = dict(self.step_retries)
        new[step_id] = cur
        return replace(self, step_retries=new)

    def with_step_skip(
        self, step_id: str, delta: int = 1
    ) -> "RetryCounters":
        cur = int(self.step_skips.get(step_id, 0)) + int(delta)
        new = dict(self.step_skips)
        new[step_id] = cur
        return replace(self, step_skips=new)

    def with_replan(self, delta: int = 1) -> "RetryCounters":
        return replace(self, replans=max(0, self.replans + delta))

    def with_failure(self, delta: int = 1) -> "RetryCounters":
        return replace(self, failures=max(0, self.failures + delta))

    def with_decision(self, delta: int = 1) -> "RetryCounters":
        return replace(self, decisions=max(0, self.decisions + delta))

    def with_observation(self, delta: int = 1) -> "RetryCounters":
        return replace(self, observations=max(0, self.observations + delta))

    def attempts_for(self, step_id: str) -> int:
        return int(self.step_attempts.get(step_id, 0))

    def retries_for(self, step_id: str) -> int:
        return int(self.step_retries.get(step_id, 0))

    def skips_for(self, step_id: str) -> int:
        return int(self.step_skips.get(step_id, 0))

    def elapsed_s(self, *, now: Optional[float] = None) -> float:
        if self.started_at <= 0:
            return 0.0
        return max(0.0, (now if now is not None else time.time()) - self.started_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "RetryCounters",
            "step_attempts": dict(self.step_attempts),
            "replans": self.replans,
            "failures": self.failures,
            "decisions": self.decisions,
            "observations": self.observations,
            "step_retries": dict(self.step_retries),
            "step_skips": dict(self.step_skips),
            "started_at": self.started_at,
            "elapsed_s": self.elapsed_s(),
        }


def make_blank_retry_counters() -> RetryCounters:
    """Return a fresh, empty :class:`RetryCounters` snapshot."""
    return RetryCounters(started_at=time.time())


# ===========================================================================
# RetryTracker — the seam
# ===========================================================================

class RetryTracker:
    """A small mutable wrapper around :class:`RetryCounters`.

    The Agent owns one :class:`RetryTracker` per run.  Each
    recovery action that consumes budget calls one of the
    ``record_*`` methods; the tracker updates its internal
    :class:`RetryCounters` snapshot and (optionally) emits a
    :class:`ProgressEvent` so the observability layer can show
    the user how many attempts remain.

    The tracker is *fail-soft*: if a step_id is missing the
    counter simply records ``1`` for it.  The Agent is the
    authoritative source of step ids, so this is expected to be
    rare in production.
    """

    def __init__(
        self,
        *,
        broadcaster: Optional[Any] = None,
        correlation_id: str = "",
    ) -> None:
        self._counters: RetryCounters = make_blank_retry_counters()
        self._broadcaster = broadcaster
        self._correlation_id = correlation_id

    # ---- read accessors
    @property
    def counters(self) -> RetryCounters:
        return self._counters

    def snapshot(self) -> RetryCounters:
        return self._counters

    def attempts_for(self, step_id: str) -> int:
        return self._counters.attempts_for(step_id)

    def retries_for(self, step_id: str) -> int:
        return self._counters.retries_for(step_id)

    def replans(self) -> int:
        return self._counters.replans

    # ---- record actions
    def record_step_attempt(self, step_id: str) -> RetryCounters:
        self._counters = self._counters.with_step_attempt(step_id)
        return self._counters

    def record_step_retry(self, step_id: str) -> RetryCounters:
        self._counters = self._counters.with_step_retry(step_id)
        return self._counters

    def record_step_skip(self, step_id: str) -> RetryCounters:
        self._counters = self._counters.with_step_skip(step_id)
        return self._counters

    def record_replan(self) -> RetryCounters:
        self._counters = self._counters.with_replan()
        return self._counters

    def record_failure(self) -> RetryCounters:
        self._counters = self._counters.with_failure()
        return self._counters

    def record_decision(self) -> RetryCounters:
        self._counters = self._counters.with_decision()
        return self._counters

    def record_observation(self) -> RetryCounters:
        self._counters = self._counters.with_observation()
        return self._counters

    # ---- reset
    def reset(self) -> None:
        self._counters = make_blank_retry_counters()


__all__ = [
    "RetryCounters",
    "make_blank_retry_counters",
    "RetryTracker",
]
