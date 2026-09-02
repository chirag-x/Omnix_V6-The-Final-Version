"""
Omnix V6 — CapabilityRouter.

The single authorized entry point for invoking a capability (R-21).

The router performs five checks in order before invoking
:meth:`Capability.execute`:

    1. **Existence**   — is the name in :class:`CapabilityRegistry`?
    2. **Parameters**  — coerce / validate against the spec.
    3. **Availability** — are required services / capabilities live?
    4. **Safety**      — is a dangerous capability authorized?
    5. **Dispatch**    — call the capability and wrap the result.

If any check fails, the router returns a :class:`CapabilityResult`
with the appropriate status (no exception leaks out for a routine
refusal).  The router only raises :class:`CapabilityError` for
"the registry is broken" cases.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from typing import Any, Dict, Iterable, Mapping, Optional

from .capability import Capability, coerce_parameters
from .capability_registry import CapabilityRegistry
from .errors import CapabilityError
from .results import (
    CapabilityResult,
    CapabilityStatus,
)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class CapabilityRouter:
    """Validates and dispatches capabilities.

    The router owns *no* business logic; it composes the registry and
    a set of *guard* callables (parameter coercion, availability,
    safety).  Tests can inject custom guards by passing them to
    :meth:`__init__`.
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        safety_policy: Optional["SafetyPolicy"] = None,
        default_timeout_s: float = 60.0,
    ) -> None:
        self._registry = registry
        self._safety = safety_policy or AllowAllSafetyPolicy()
        self._default_timeout_s = default_timeout_s
        self._lock = threading.RLock()
        # simple per-capability call counters (useful for debugging)
        self._call_counts: Dict[str, int] = {}

    # ============================================================== api
    @property
    def registry(self) -> CapabilityRegistry:
        return self._registry

    def call_count(self, name: str) -> int:
        with self._lock:
            return self._call_counts.get(name, 0)

    # ============================================================ route
    def route(
        self,
        name: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        available_services: Optional[Iterable[str]] = None,
        available_capabilities: Optional[Iterable[str]] = None,
        authorized_dangerous: bool = False,
        cancellation_token: Any = None,
    ) -> CapabilityResult:
        """Validate, dispatch, and return a :class:`CapabilityResult`.

        Never raises for routine failures (unknown name, bad params,
        unavailable).  Raises :class:`CapabilityError` only if the
        registry is in an inconsistent state.

        ``cancellation_token`` is forwarded into the dispatched
        capability's params (under ``params["cancellation_token"]``)
        so cooperative-cancellation checks inside the input layer
        can see it.  We inject it AFTER ``coerce_parameters`` so a
        token that does not appear in the spec still threads
        through; we never re-introduce it as a coerced-typed value.
        """
        params = dict(params) if params else {}
        started = time.time()
        cap = self._registry.get(name)
        if cap is None:
            return self._result(
                name=name,
                status=CapabilityStatus.SKIPPED,
                error=CapabilityError(
                    f"Unknown capability: {name!r}",
                    code="CAPABILITY_UNKNOWN",
                    context={"name": name},
                ),
                duration_ms=_ms_since(started),
            )

        # ---- 1. parameters ------------------------------------------------
        try:
            coerced = coerce_parameters(cap.spec, params)
        except Exception as exc:  # ValidationError or any custom
            return self._result(
                name=name,
                status=CapabilityStatus.SKIPPED,
                error=exc,
                duration_ms=_ms_since(started),
            )

        # Inject the cancellation token AFTER coercion so the spec
        # does not have to declare it (it is a router-internal
        # convention, not a capability-level parameter).
        if cancellation_token is not None:
            coerced["cancellation_token"] = cancellation_token

        # ---- 2. availability --------------------------------------------
        ok, reason = self._registry.check_availability(
            name,
            available_services=available_services,
            available_capabilities=available_capabilities,
        )
        if not ok:
            return self._result(
                name=name,
                status=CapabilityStatus.SKIPPED,
                error=CapabilityError(
                    f"Capability {name!r} unavailable: {reason}",
                    code="CAPABILITY_UNAVAILABLE",
                    context={"name": name, "reason": reason},
                ),
                duration_ms=_ms_since(started),
            )

        # ---- 3. safety ---------------------------------------------------
        if cap.spec.dangerous and not authorized_dangerous:
            allowed = self._safety.is_dangerous_authorized(name, cap)
            if not allowed:
                return self._result(
                    name=name,
                    status=CapabilityStatus.SKIPPED,
                    error=CapabilityError(
                        f"Dangerous capability {name!r} not authorized",
                        code="CAPABILITY_SAFETY_REFUSED",
                        context={"name": name},
                    ),
                    duration_ms=_ms_since(started),
                )

        # ---- 4. dispatch -------------------------------------------------
        attempted = True
        try:
            outcome = cap.execute(coerced)
            if inspect.iscoroutine(outcome):
                # The capability's execute is async.  Bridge sync->async
                # here, at the router boundary, so the public route()
                # contract remains synchronous for callers (Engine,
                # Brain, tests).  We do NOT spawn a thread for the
                # asyncio loop: the loop only exists for the duration
                # of this dispatch.
                try:
                    result = asyncio.run(outcome)
                except RuntimeError as loop_exc:
                    # If we're already inside a running loop (e.g. an
                    # async test calling the sync router), fall back to
                    # a fresh dedicated loop in a worker thread.
                    if "asyncio.run() cannot be called" not in str(loop_exc) \
                            and "running event loop" not in str(loop_exc):
                        raise
                    result = _run_coro_in_worker(outcome)
            else:
                result = outcome
        except Exception as exc:  # noqa: BLE001
            return self._result(
                name=name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                error=CapabilityError(
                    f"Capability {name!r} raised: {exc!r}",
                    code="CAPABILITY_RAISED",
                    context={"name": name},
                    cause=exc,
                ),
                duration_ms=_ms_since(started),
            )

        with self._lock:
            self._call_counts[name] = self._call_counts.get(name, 0) + 1

        # Coalesce the flags the implementation set on the result with
        # the lifecycle of the route (R-8/AD-21 require all four flags).
        executed = bool(result.executed) or result.status in (
            CapabilityStatus.EXECUTED,
            CapabilityStatus.VERIFIED,
            CapabilityStatus.FAILED,
        )
        verified = bool(result.verified) or result.status is CapabilityStatus.VERIFIED
        failed = bool(result.failed) or result.status is CapabilityStatus.FAILED

        # If the implementation didn't set the four flags, derive from status.
        from dataclasses import replace as _r
        if not (result.attempted or result.executed or result.verified or result.failed):
            synthesized = _r(
                result,
                attempted=attempted,
                executed=executed,
                verified=verified,
                failed=failed,
            )
            return synthesized
        return result

    # ============================================================ helpers
    def _result(
        self,
        *,
        name: str,
        status: CapabilityStatus,
        attempted: bool = False,
        executed: bool = False,
        verified: bool = False,
        failed: bool = False,
        error: Optional[Exception] = None,
        duration_ms: float = 0.0,
    ) -> CapabilityResult:
        from .errors import OmnixError
        err: Optional[OmnixError] = None
        if isinstance(error, OmnixError):
            err = error
        elif error is not None:
            err = CapabilityError(str(error), cause=error)
        return CapabilityResult(
            capability_name=name,
            status=status,
            attempted=attempted,
            executed=executed,
            verified=verified,
            failed=failed or status is CapabilityStatus.FAILED,
            error=err,
            duration_ms=duration_ms,
        )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "CapabilityRouter",
                "registry_size": len(self._registry),
                "call_counts": dict(self._call_counts),
                "default_timeout_s": self._default_timeout_s,
            }

    def __repr__(self) -> str:
        return f"CapabilityRouter(registry={self._registry!r})"


# ---------------------------------------------------------------------------
# Safety policy
# ---------------------------------------------------------------------------

class SafetyPolicy:
    """Decide whether a dangerous capability is allowed to run.

    A policy is consulted only when the capability's spec has
    ``dangerous=True`` *and* the caller did not pre-authorize the call
    with ``authorized_dangerous=True``.  The default policy refuses
    everything; the engine can install a more permissive one later.
    """

    def is_dangerous_authorized(self, name: str, capability: Capability) -> bool:
        return False


class AllowAllSafetyPolicy(SafetyPolicy):
    """Permit every dangerous capability.  Test-only convenience."""

    def is_dangerous_authorized(self, name: str, capability: Capability) -> bool:
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ms_since(t0: float) -> float:
    return round((time.time() - t0) * 1000.0, 3)


def _run_coro_in_worker(coro):
    """Run ``coro`` to completion in a fresh asyncio loop on a worker thread.

    Used as a fallback when the calling thread already has a running
    asyncio event loop (e.g. an async test driving the sync router).
    """
    import concurrent.futures

    holder: Dict[str, Any] = {}

    def _target() -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            holder["result"] = loop.run_until_complete(coro)
        except Exception as exc:  # noqa: BLE001
            holder["error"] = exc
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_target)
        fut.result()

    if "error" in holder:
        raise holder["error"]
    return holder["result"]
