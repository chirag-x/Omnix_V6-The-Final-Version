"""
Omnix V6 — Agent observation provider (Phase 6C).

This module provides a small abstraction that turns an executor
:class:`StepResult` into an :class:`Observation` the Agent can
reason about.  Phase 6C ships a single, *derived* implementation
(:class:`CapabilityResultObservationProvider`); real sensor backends
(screen scrapers, UIA walkers, OCR pipelines, etc.) are Phase 7
work.

R-24 boundary
-------------
The Agent never invents observations out of thin air.  Every
observation the Agent uses is either:

  (a) a :class:`CapabilityResult` projection (DERIVED source), or
  (b) provided by a *caller-supplied* :class:`ObservationProvider`
      in :class:`Agent.run`.

This is the only place where DERIVED observations are produced, so
the audit log can show exactly which observations the Agent
consulted.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from .models import (
    ExpectedEffect,
    Observation,
    ObservationSource,
    PlanStep,
)
from .execution_result import StepResult, StepState


@runtime_checkable
class ObservationProvider(Protocol):
    """Plug-in point for real sensors (screen, UIA, OCR, vision).

    The Agent calls ``observe(step, step_result)`` after every
    :class:`StepResult` to obtain a post-action :class:`Observation`.
    The default implementation projects the
    :class:`core.results.CapabilityResult` into a DERIVED
    observation; richer providers can replace it.
    """

    name: str

    def observe(
        self,
        step: PlanStep,
        step_result: StepResult,
    ) -> Optional[Observation]:
        """Return an :class:`Observation` for ``step`` / ``step_result``.

        Returning ``None`` means "no observation available for this
        step" — the Agent treats that as ``UNKNOWN`` and may
        re-observe or ask the user, depending on policy.
        """
        ...


# ===========================================================================
# Default implementation: derive an Observation from the CapabilityResult
# ===========================================================================

class CapabilityResultObservationProvider:
    """The default :class:`ObservationProvider`.

    Projects the executor's :class:`StepResult` (and its embedded
    :class:`core.results.CapabilityResult`) into a single DERIVED
    :class:`Observation`.  This is the only safe default for Phase 6C:
    the Agent reasons about *what the capability said happened*, not
    about independent sensors it does not have.

    The observation is intentionally minimal:
      * ``source = ObservationSource.DERIVED``
      * ``subject = step.step_id``
      * ``data["status"]`` carries the :class:`StepState`.
      * ``data["capability_status"]`` carries the capability-side status.
      * ``data["verification"]`` carries the verification verdict (if any).
      * ``data["details"]`` carries the capability's details dict.
      * ``confidence`` is ``1.0`` when the capability reported VERIFIED,
        ``0.5`` when it reported EXECUTED, ``0.0`` otherwise.
    """

    name: str = "capability-derived"

    def observe(
        self,
        step: PlanStep,
        step_result: StepResult,
    ) -> Optional[Observation]:
        if step_result is None:
            return None
        if step_result.status is None:
            return None

        cap = step_result.capability_result
        cap_status = (
            getattr(cap, "status", None).value
            if getattr(cap, "status", None) is not None
            else ""
        )

        # Verification verdict is tri-state; preserve it on the observation
        # so the Verifier can reason about it without re-parsing the
        # capability result.
        verification = getattr(cap, "verification", None) if cap is not None else None
        verification_payload: Optional[Dict[str, Any]] = None
        if verification is not None:
            try:
                verification_payload = {
                    "check_name": getattr(verification, "check_name", ""),
                    "status": (
                        getattr(verification, "status", None).value
                        if getattr(verification, "status", None) is not None
                        else ""
                    ),
                    "expected": getattr(verification, "expected", None),
                    "actual": getattr(verification, "actual", None),
                    "confidence": getattr(verification, "confidence", 1.0),
                }
            except Exception:  # noqa: BLE001
                verification_payload = None

        data: Dict[str, Any] = {
            "status": step_result.status.value,
            "capability_status": cap_status,
            "capability_name": (
                getattr(cap, "capability_name", step_result.capability_name)
                or step_result.capability_name
            ),
            "verification": verification_payload,
            "details": dict(getattr(cap, "details", {}) or {}),
            "error": getattr(cap, "error", None) if cap is not None else None,
        }

        # Confidence reflects what the capability actually claimed.
        if cap_status == "verified":
            confidence = 1.0
        elif cap_status == "executed":
            confidence = 0.5
        elif cap_status == "attempted":
            confidence = 0.25
        else:
            confidence = 0.0

        return Observation(
            source=ObservationSource.DERIVED,
            data=data,
            timestamp=step_result.completed_at or 0.0,
            subject=step.step_id,
            confidence=confidence,
            metadata={
                "agent_observation_provider": self.name,
                "step_id": step.step_id,
            },
        )


__all__ = [
    "ObservationProvider",
    "CapabilityResultObservationProvider",
]
