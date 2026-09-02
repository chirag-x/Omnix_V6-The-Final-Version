"""
Omnix V6 — Target grounding contract (Phase 7.2).

The :class:`TargetGroundingContract` is the typed boundary between
:class:`core.services.vision_service.VisionService` and the
:class:`core.orchestration.agent.Agent`.  The Agent asks the Vision
Service to ground a human-meaningful target query (e.g. "the Save
button") into something the executor can act on; the contract
describes what the Agent is allowed to do with the result, and what
it must NOT do.

Why a separate module
---------------------
The vision service lives in :mod:`core.services.vision_service`; the
Agent in :mod:`core.orchestration.agent`.  Sharing a single
dataclass across both modules would couple them at the type level.
Instead, both modules import *this* module and reason about
:class:`TargetGroundingContract` -- a minimal, frozen data shape
that can be unit-tested without spinning up either side.

R-8: statuses are typed, not bare booleans.
R-22: the contract is the *only* path from vision into the
       orchestration layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from core.orchestration.models import ObservationSource


# Default confidence threshold below which the Agent must NOT
# dispatch a click.  This is a *safety* gate, not a quality-of-
# service gate; vision evidence below this level is treated as
# unreliable and routed to recovery.
DEFAULT_CONFIDENCE_THRESHOLD = 0.5


class GroundingStatus(str, Enum):
    """The status of a target-grounding attempt.

    Maps directly to :class:`core.services.vision_service.VisionResult.status`
    so the Agent does not need to translate values across modules.
    """

    GROUNDED = "GROUNDED"            # single, unambiguous target found
    AMBIGUOUS = "AMBIGUOUS"          # multiple indistinguishable candidates
    NOT_FOUND = "NOT_FOUND"          # no candidate matched
    ERROR = "ERROR"                  # vision pipeline failed
    REJECTED = "REJECTED"            # confidence below threshold
    SKIPPED = "SKIPPED"              # no visual target to ground (e.g. pure coordinate step)


@dataclass(frozen=True)
class TargetGroundingContract:
    """The typed contract between Vision and the Agent.

    The contract is intentionally small and *immutable*.  A
    grounded target carries:

      * ``status`` — one of :class:`GroundingStatus`.
      * ``target_query`` — the original user/planner query.
      * ``bbox`` — the bounding box (l, t, r, b) of the resolved
        target, when ``status == GROUNDED``.
      * ``center`` — the (x, y) center of the bbox, when
        ``status == GROUNDED``.  Pre-computed so the executor
        adapter does not have to recompute it on the hot path.
      * ``confidence`` — the routing confidence in ``[0, 1]``.
      * ``source`` — the sensor the candidate came from.
      * ``text`` — the OCR/UIA text that matched, if any.
      * ``resolution_method`` — a short label the router stamped.
      * ``candidates`` — when ``status == AMBIGUOUS``, the
        list of indistinguishable candidates.
      * ``error`` — human-readable reason when ``status`` is
        ``ERROR`` / ``REJECTED`` / ``NOT_FOUND``.
    """

    status: GroundingStatus
    target_query: str = ""
    bbox: Optional[Tuple[int, int, int, int]] = None
    center: Optional[Tuple[int, int]] = None
    confidence: float = 0.0
    source: Optional[ObservationSource] = None
    text: str = ""
    resolution_method: str = ""
    candidates: Tuple[Dict[str, Any], ...] = ()
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------- derived
    @property
    def is_grounded(self) -> bool:
        return self.status is GroundingStatus.GROUNDED

    @property
    def is_blocking(self) -> bool:
        """``True`` when the contract must block the Agent from acting.

        Blocking statuses are :data:`AMBIGUOUS`, :data:`NOT_FOUND`,
        :data:`ERROR`, and :data:`REJECTED`.  Only :data:`GROUNDED`
        (and the explicit :data:`SKIPPED`) let the Agent proceed.
        """
        return self.status in (
            GroundingStatus.AMBIGUOUS,
            GroundingStatus.NOT_FOUND,
            GroundingStatus.ERROR,
            GroundingStatus.REJECTED,
        )

    # ----------------------------------------------------- factories
    @classmethod
    def skipped(cls, target_query: str = "") -> "TargetGroundingContract":
        """Build a SKIPPED contract for steps that need no visual grounding."""
        return cls(
            status=GroundingStatus.SKIPPED,
            target_query=target_query,
        )

    @classmethod
    def rejected(
        cls,
        target_query: str,
        *,
        confidence: float,
        threshold: float,
        source: Optional[ObservationSource] = None,
    ) -> "TargetGroundingContract":
        """Build a REJECTED contract when confidence is below the safety gate."""
        return cls(
            status=GroundingStatus.REJECTED,
            target_query=target_query,
            confidence=confidence,
            source=source,
            error=(
                f"grounding confidence {confidence:.2f} is below the "
                f"required threshold {threshold:.2f}; refusing to dispatch."
            ),
        )


__all__ = [
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "GroundingStatus",
    "TargetGroundingContract",
]
