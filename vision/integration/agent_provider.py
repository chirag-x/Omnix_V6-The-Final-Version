"""
Omnix V6 — VisionTargetProvider (Phase 13).

The Agent consults a :class:`VisionTargetProvider` before any
pre-action step that requires grounding.  The Protocol is
narrow — a single ``ground_target()`` method that returns a
:class:`core.orchestration.grounding.TargetGroundingContract`.
This isolates the Agent from the concrete vision pipeline.

The :class:`DefaultVisionTargetProvider` adapts an existing
:class:`core.services.vision_service.VisionService` (Phase 7.2)
into the Protocol shape.  The adapter is responsible for:

  * accepting the *result* of ``VisionService.ground_target()``
    (a :class:`VisionResult` with the Phase 7.x status string)
    and translating it into a :class:`TargetGroundingContract`;
  * enforcing the screenshot-freshness gate via
    :func:`vision.safety.freshness.is_fresh` when the service
    returned a :class:`ScreenshotMetadata`;
  * raising :class:`vision.safety.coordinates.CoordinateSafetyError`
    when the grounded center is unsafe to act on.

The Provider does NOT itself call the Agent, the PlanExecutor,
the CapabilityRouter, or any computer-use surface.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

from core.orchestration.grounding import (
    GroundingStatus,
    TargetGroundingContract,
)
from vision.observations.screenshot_metadata import ScreenshotMetadata
from vision.safety.freshness import is_fresh, StaleScreenError


# Default stale-screen threshold.  The Agent may override it per
# call.  Phase 13 keeps this default small so a host that fails to
# refresh the screen (e.g. an X11 server dropped the capture) is
# rejected rather than acting on a 30-second-old picture.
DEFAULT_MAX_SCREENSHOT_AGE_S: float = 5.0


# Status string from :class:`core.services.vision_service.VisionResult`
# mapped onto :class:`GroundingStatus`.  Kept here (not at the
# import boundary) so the Provider is the *only* place that
# translates between the two contracts.
_VISION_RESULT_TO_GROUNDING: dict = {
    "OBSERVED": GroundingStatus.GROUNDED,
    "AMBIGUOUS": GroundingStatus.AMBIGUOUS,
    "NOT_FOUND": GroundingStatus.NOT_FOUND,
    "ERROR": GroundingStatus.ERROR,
}


@runtime_checkable
class VisionTargetProvider(Protocol):
    """The typed seam between the Agent and the vision pipeline.

    Implementations MUST be pure: ``ground_target`` returns a
    :class:`TargetGroundingContract`; it does not call any
    computer-use capability, does not import the Agent, and does
    not import the CapabilityRouter.
    """

    def ground_target(
        self,
        target_query: str,
        *,
        preferred_strategy: Optional[str] = None,
    ) -> TargetGroundingContract:
        ...


class DefaultVisionTargetProvider:
    """Default :class:`VisionTargetProvider` backed by a :class:`VisionService`.

    The provider is constructed with a *vision_service* (an
    object exposing ``ground_target(target_query,
    preferred_strategy=...)`` and returning a
    :class:`core.services.vision_service.VisionResult`-shaped
    object).  Tests can pass a mock service to drive the
    provider without touching the real vision stack.

    The constructor takes an optional ``max_screenshot_age_s``
    for the screenshot-freshness gate.  When the service's
    result carries no :class:`ScreenshotMetadata`, the freshness
    gate is *skipped* (the legacy Phase 7.x path); when it does,
    the gate is enforced.
    """

    def __init__(
        self,
        vision_service: Any,
        *,
        max_screenshot_age_s: float = DEFAULT_MAX_SCREENSHOT_AGE_S,
    ) -> None:
        if vision_service is None:
            raise ValueError(
                "DefaultVisionTargetProvider requires a vision_service"
            )
        self._vision_service = vision_service
        self._max_screenshot_age_s = float(max_screenshot_age_s)

    @property
    def max_screenshot_age_s(self) -> float:
        return self._max_screenshot_age_s

    def ground_target(
        self,
        target_query: str,
        *,
        preferred_strategy: Optional[str] = None,
    ) -> TargetGroundingContract:
        """Ground ``target_query`` and return a typed contract.

        The implementation:

        1. Calls the underlying :class:`VisionService.ground_target`.
        2. Translates the :class:`VisionResult` to a
           :class:`TargetGroundingContract`.
        3. Applies the screenshot-freshness gate.
        4. Returns the contract.
        """
        if not isinstance(target_query, str) or not target_query:
            raise ValueError(
                f"ground_target requires a non-empty target_query (got {target_query!r})"
            )

        try:
            vision_result = self._vision_service.ground_target(
                target_query,
                preferred_strategy=preferred_strategy,
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "vision_service.ground_target raised: %r", exc
            )
            return TargetGroundingContract(
                status=GroundingStatus.ERROR,
                target_query=target_query,
                error=f"vision service raised: {exc!r}",
            )

        contract = self._translate(target_query=target_query, vision_result=vision_result)

        # Screenshot-freshness gate.  Only runs when the result
        # carries a ScreenshotMetadata.  A stale screenshot blocks
        # the step with SAFETY semantics.
        screenshot_meta = getattr(vision_result, "screenshot_metadata", None)
        if screenshot_meta is not None and contract.is_grounded:
            if not is_fresh(
                screenshot_meta,
                max_age_s=self._max_screenshot_age_s,
            ):
                return TargetGroundingContract(
                    status=GroundingStatus.REJECTED,
                    target_query=target_query,
                    confidence=contract.confidence,
                    source=contract.source,
                    error=(
                        f"screenshot {screenshot_meta.screenshot_id!r} is "
                        f"stale; refusing to dispatch a grounded action."
                    ),
                )
        return contract

    # ------------------------------------------------------- internals
    def _translate(
        self,
        *,
        target_query: str,
        vision_result: Any,
    ) -> TargetGroundingContract:
        """Translate a :class:`VisionResult` to a :class:`TargetGroundingContract`."""
        status_str = getattr(vision_result, "status", "ERROR")
        status = _VISION_RESULT_TO_GROUNDING.get(status_str, GroundingStatus.ERROR)

        observation = getattr(vision_result, "observation", None) or {}
        bbox = observation.get("bbox") if isinstance(observation, dict) else None
        confidence = (
            float(observation.get("confidence", 0.0))
            if isinstance(observation, dict)
            else 0.0
        )
        source_str = (
            observation.get("source") if isinstance(observation, dict) else None
        )
        text = (
            observation.get("text", "") if isinstance(observation, dict) else ""
        )
        resolution_method = getattr(vision_result, "resolution_method", "") or ""
        error = getattr(vision_result, "error", None) or ""

        center = None
        if bbox is not None and len(bbox) == 4:
            try:
                l, t, r, b = (int(v) for v in bbox)
                center = ((l + r) // 2, (t + b) // 2)
                bbox = (l, t, r, b)
            except (TypeError, ValueError):
                bbox = None
                center = None

        candidates_payload = []
        if status is GroundingStatus.AMBIGUOUS and isinstance(observation, dict):
            for c in observation.get("candidates", ()) or ():
                if isinstance(c, dict):
                    candidates_payload.append(dict(c))

        from core.orchestration.models import ObservationSource

        source: Optional[ObservationSource] = None
        if source_str:
            try:
                source = ObservationSource(source_str)
            except (ValueError, TypeError):
                source = None

        return TargetGroundingContract(
            status=status,
            target_query=target_query,
            bbox=bbox,
            center=center,
            confidence=confidence,
            source=source,
            text=text if isinstance(text, str) else "",
            resolution_method=resolution_method,
            candidates=tuple(candidates_payload),
            error=error,
            metadata={},
        )


__all__ = [
    "VisionTargetProvider",
    "DefaultVisionTargetProvider",
    "DEFAULT_MAX_SCREENSHOT_AGE_S",
]
