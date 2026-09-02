"""
Omnix V6 — Phase 14: idempotency log.

A multi-step plan that retries a failed step must not also
re-execute every *side-effecting* capability twice.  A click that
opens a menu, a file-write that creates a document, a
browser-navigate that loads a page — re-running any of them may
corrupt the world the user is operating in.

This module provides a small, deterministic, in-memory log:

    * Every dispatched :class:`core.orchestration.ActionRequest` is
      recorded with its *idempotency key* (a hash of the
      capability name + the canonicalised parameters).
    * Before a new dispatch, the executor asks
      :meth:`IdempotencyLog.is_duplicate`.  If the key has already
      been recorded, the executor may either:
        (a) short-circuit and treat the cached result as the new
            result, **or**
        (b) refuse to dispatch and surface a structured
            :class:`DuplicateActionError` to the recovery engine.

    The decision is policy-driven (Phase 14 §25: "the Agent may
    consult the policy to decide between skip and re-run"); the
    log itself is policy-agnostic.

The log is a *value type* with a mutable ``record`` method, but it
lives only inside the Agent's per-execution state — it is never
shared across executions and never persisted.  That is the right
scope: cross-execution dedup would require a stable store and a
much larger surface, which Phase 14 explicitly defers.

Architectural isolation:
    This module MUST NOT import:
        * :mod:`core.omnix_engine`
        * :mod:`core.pipeline`
        * :mod:`core.capability_router`
        * :mod:`core.services.*` (vision / browser / memory / voice)
        * any V6 *Windows service* (e.g. ``system.windows.*``)
        * any V6 *AI provider* (e.g. ``ai.provider.*``)

    The log is data; it never executes a capability, never calls a
    service, never reads the screen.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Tuple


def canonical_parameters(parameters: Mapping[str, Any]) -> str:
    """Return a deterministic JSON string for ``parameters``.

    ``dict`` ordering is implementation-defined in Python 3.6 but
    JSON-serialising a dict with ``sort_keys=True`` is part of the
    standard library, so the result is stable across processes.
    """
    return json.dumps(
        dict(parameters),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def idempotency_key(
    capability_name: str,
    parameters: Mapping[str, Any],
) -> str:
    """Return a stable idempotency key for a capability call.

    Two ``ActionRequest`` instances with the same
    ``capability_name`` and the same parameter set produce the
    same key; any difference in either produces a different key.
    """
    h = hashlib.sha256()
    h.update(capability_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(canonical_parameters(parameters).encode("utf-8"))
    return h.hexdigest()


@dataclass
class IdempotencyLog:
    """An append-only log of dispatched capability calls.

    A log is created per :class:`MultiStepContext`.  The Agent owns
    the log and threads it through every dispatch.  After every
    successful (or attempted) dispatch, the executor calls
    :meth:`record`.  Before dispatch, it calls
    :meth:`is_duplicate`.

    The log does not store the *result* of the call; it stores
    just enough to detect duplicates.  Caching the result is a
    separate concern (Phase 14 §25 keeps it policy-driven).
    """

    entries: Dict[str, "IdempotencyEntry"] = field(default_factory=dict)

    def record(
        self,
        *,
        step_id: str,
        capability_name: str,
        parameters: Mapping[str, Any],
        attempt: int = 0,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Record a dispatch and return the idempotency key.

        Calling ``record`` more than once with the same key is
        allowed; the log updates the existing entry rather than
        rejecting the call.  This matches the "the same step may
        re-dispatch after a retry" case.
        """
        key = idempotency_key(capability_name, parameters)
        existing = self.entries.get(key)
        if existing is not None:
            # IdempotencyEntry is frozen; we build a new one.
            self.entries[key] = replace(
                existing,
                attempt=max(existing.attempt, attempt),
                step_ids=tuple(dict.fromkeys([*existing.step_ids, step_id])),
            )
        else:
            self.entries[key] = IdempotencyEntry(
                key=key,
                capability_name=capability_name,
                parameters_canonical=canonical_parameters(parameters),
                step_ids=(step_id,),
                attempt=attempt,
                metadata=dict(metadata or {}),
            )
        return key

    def is_duplicate(
        self,
        capability_name: str,
        parameters: Mapping[str, Any],
    ) -> bool:
        key = idempotency_key(capability_name, parameters)
        return key in self.entries

    def entry_for(
        self,
        capability_name: str,
        parameters: Mapping[str, Any],
    ) -> Optional["IdempotencyEntry"]:
        key = idempotency_key(capability_name, parameters)
        return self.entries.get(key)

    def clear(self) -> None:
        self.entries.clear()

    def size(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class IdempotencyEntry:
    """A single record in an :class:`IdempotencyLog`."""

    key: str
    capability_name: str
    parameters_canonical: str
    step_ids: Tuple[str, ...] = ()
    attempt: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "IdempotencyEntry",
            "key": self.key,
            "capability_name": self.capability_name,
            "parameters_canonical": self.parameters_canonical,
            "step_ids": list(self.step_ids),
            "attempt": self.attempt,
            "metadata": dict(self.metadata),
        }


class DuplicateActionError(Exception):
    """Raised when a duplicate capability call is refused.

    The recovery engine catches this and routes it through
    :class:`core.orchestration.FailureKind` (``EXECUTION`` /
    ``PLAN_INFEASIBLE``) for the policy to act on.  The error
    carries the *idempotency key* so the audit log can show which
    prior dispatch the new request collided with.
    """

    def __init__(self, key: str, message: str = ""):
        self.key = key
        if not message:
            message = f"Duplicate action refused (idempotency key {key!r})."
        super().__init__(message)


__all__ = [
    "IdempotencyLog",
    "IdempotencyEntry",
    "idempotency_key",
    "canonical_parameters",
    "DuplicateActionError",
]
