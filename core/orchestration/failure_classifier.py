"""
Omnix V6 — Phase 3: :class:`FailureClassifier` (R-12 contract).

This module is the **only** place in the orchestration layer that
reads :class:`core.errors.OmnixError` ``code`` strings.  The
classifier accepts a :class:`core.results.CapabilityResult` and
returns the :class:`FailureKind` the recovery engine should branch
on.

The mapping
-----------

The 6 desktop-automation failure kinds added in Phase 1 / D23 are
the failure modes a user actually encounters::

    TARGET_NOT_FOUND    → REPLAN   (re-ground via vision)
    FOCUS_FAILED        → RETRY    (one immediate retry)
    WINDOW_NOT_READY    → RETRY_WITH_BACKOFF  (1.0s)
    STALE_TARGET        → REPLAN   (re-ground)
    PROVIDER_FAILURE    → RETRY_WITH_BACKOFF  (2.0s)
    PERMISSION_FAILURE  → ASK_USER

The classifier is a *single* point of truth: a future capability
that wants to surface one of these failures only has to raise an
:class:`OmnixError` with the documented ``code``.  The recovery
engine, the agent, and the audit log never have to read raw
``code`` strings again.

Canonical error codes
---------------------

Capabilities raise :class:`OmnixError` with one of these codes::

    "TARGET_NOT_FOUND"        → FailureKind.TARGET_NOT_FOUND
    "FOCUS_FAILED"            → FailureKind.FOCUS_FAILED
    "WINDOW_NOT_READY"        → FailureKind.WINDOW_NOT_READY
    "STALE_TARGET"            → FailureKind.STALE_TARGET
    "PROVIDER_FAILURE"        → FailureKind.PROVIDER_FAILURE
    "PERMISSION_FAILURE"      → FailureKind.PERMISSION_FAILURE

Any other code (or no error at all) falls back to
:class:`FailureKind.EXECUTION` — the generic "the action failed"
case that the recovery engine maps to ``RETRY_WITH_BACKOFF``.

The classifier is a *pure function* of its inputs; it performs no
I/O and raises nothing.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.results import CapabilityResult
from .models import FailureKind


# Canonical mapping from OmnixError.code to FailureKind.
# Kept module-level so tests can introspect it without
# instantiating the classifier.
CODE_TO_KIND: Dict[str, FailureKind] = {
    "TARGET_NOT_FOUND": FailureKind.TARGET_NOT_FOUND,
    "FOCUS_FAILED": FailureKind.FOCUS_FAILED,
    "WINDOW_NOT_READY": FailureKind.WINDOW_NOT_READY,
    "STALE_TARGET": FailureKind.STALE_TARGET,
    "PROVIDER_FAILURE": FailureKind.PROVIDER_FAILURE,
    "PERMISSION_FAILURE": FailureKind.PERMISSION_FAILURE,
}


class FailureClassifier:
    """Map a :class:`CapabilityResult` to a :class:`FailureKind`.

    The classifier is intentionally a *function* packaged as a
    class.  It has no constructor arguments, no I/O, and no
    caches.  The Protocol is satisfied by a single :meth:`classify`
    method so it can be swapped for a custom implementation in
    tests.
    """

    name: str = "default-failure-classifier"

    # ---------------------------------------------------------- public API
    def classify(
        self,
        result: CapabilityResult,
        *,
        fallback: FailureKind = FailureKind.EXECUTION,
    ) -> FailureKind:
        """Return the :class:`FailureKind` for ``result``.

        ``fallback`` is the kind returned when ``result`` does
        not carry a recognisable error code.  The default is
        :class:`FailureKind.EXECUTION` (the generic "the action
        failed" case).
        """
        code = self._extract_code(result)
        if code is None:
            return fallback
        return CODE_TO_KIND.get(code, fallback)

    def classify_code(
        self,
        code: Optional[str],
        *,
        fallback: FailureKind = FailureKind.EXECUTION,
    ) -> FailureKind:
        """Classify a raw error ``code`` string.

        Used by code paths that have an error but not a full
        :class:`CapabilityResult` (e.g. the Agent's own error
        wrappers).
        """
        if not code:
            return fallback
        return CODE_TO_KIND.get(code, fallback)

    # ---------------------------------------------------------- helpers
    @staticmethod
    def _extract_code(result: CapabilityResult) -> Optional[str]:
        """Return the error code carried by ``result``, if any."""
        err = getattr(result, "error", None)
        if err is None:
            return None
        code = getattr(err, "code", None)
        if not code:
            return None
        return str(code)


__all__ = [
    "CODE_TO_KIND",
    "FailureClassifier",
]
