"""
Vision Service for Omnix V6 Phase 7.1.

This module is the *only* place that wires perception strategies
together with the closed capability set.  It is a *service* (R-14),
not a singleton, and depends only on the minimum abstraction
necessary to request a screenshot (:class:`ScreenshotProvider`).

R-14: vision is a service, not a singleton.
R-21: vision depends on the closed capability set *only* through
      the :class:`ScreenshotProvider` seam.  It does NOT import
      :class:`OmnixEngine` or call any other capability directly.
R-22: routing is adaptive but deterministic.
R-8:  ``observe_expected_effect`` is OBSERVATION, not VERIFICATION
      -- it never claims ``verified=True`` from a single screenshot.
      The Brain / Agent compares before/after observations; the
      verifier is what assigns the verdict.

Phase 7.1 hardening:
  * Lazy screenshot acquisition: the router does NOT acquire
    screenshots; the service does, and *only* when a strategy
    that requires a screenshot is actually being invoked.
  * ``observe_state`` returns a structured :class:`VisionResult`
    with explicit ``status`` and ``observation`` fields; the
    verifier decides if it matches the expected effect.
"""
from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from loguru import logger

from core.orchestration.models import (
    ExpectedEffect,
    Observation,
    ObservationSource,
    VerificationVerdict,
)
from vision.observations.targets import GroundedTarget
from vision.observations.screenshot_metadata import (
    ScreenshotMetadata,
    make_screenshot_metadata,
)
from vision.router.perception_router import (
    AmbiguityError,
    PerceptionRouter,
    TargetNotGroundedError,
)
from vision.router.screenshot_provider import (
    CapabilityScreenshotProvider,
    NullScreenshotProvider,
    ScreenshotProvider,
)
from vision.strategies.coordinates_strategy import CoordinatesStrategy
from vision.strategies.ocr_strategy import OCRStrategy
from vision.strategies.uia_strategy import UIAStrategy
from vision.strategies.visual_strategy import VisualStrategy


# Status values exposed to the Brain.  The Brain is the only thing
# that may interpret these; vision itself never claims "verified".
_STATUS_OBSERVED = "OBSERVED"
_STATUS_AMBIGUOUS = "AMBIGUOUS"
_STATUS_NOT_FOUND = "NOT_FOUND"
_STATUS_ERROR = "ERROR"


@dataclass(frozen=True)
class VisionResult:
    """The structured observation result returned by :class:`VisionService`.

    The result is *observational* (R-8): the Brain is the only
    thing that decides whether an observation matches an
    :class:`ExpectedEffect`.  This is why ``verified`` is a
    derived :class:`VerificationVerdict`, not a boolean the
    service claims.

    Phase 13: ``screenshot_metadata`` is the typed description
    of the screenshot that was used (or would have been used)
    to produce this observation.  It is ``None`` when no
    screenshot was required.  The Agent uses it for the
    coordinate-safety gate.
    """

    status: str  # one of OBSERVED / AMBIGUOUS / NOT_FOUND / ERROR
    target_query: str = ""
    observation: Optional[Dict[str, Any]] = None
    alternatives_discarded: int = 0
    resolution_method: str = ""
    error: Optional[str] = None
    screenshot_used: bool = False
    screenshot_metadata: Optional[ScreenshotMetadata] = None


class VisionService:
    """Service for visual perception and target grounding."""

    def __init__(
        self,
        screenshot_provider: ScreenshotProvider,
        *,
        strategies: Optional[List[Any]] = None,
    ) -> None:
        # R-14: not a singleton -- every consumer instantiates its
        # own service (or shares one explicitly).
        self._screenshot_provider = screenshot_provider
        self._router = PerceptionRouter(
            strategies=strategies
            if strategies is not None
            else [
                UIAStrategy(),
                OCRStrategy(),
                VisualStrategy(),
                CoordinatesStrategy(),
            ]
        )

    # ----------------------------------------------------------- grounding
    def ground_target(
        self,
        target_query: str,
        preferred_strategy: Optional[str] = None,
    ) -> VisionResult:
        """Ground ``target_query`` into a single target observation.

        Returns a :class:`VisionResult` with ``status=OBSERVED``
        on success.  The router raises :class:`AmbiguityError`
        on indistinguishable candidates; the service surfaces
        that as ``status=AMBIGUOUS`` and includes the candidates
        in ``observation`` for the Brain to choose between.

        Lazy screenshot acquisition: we *first* try to ground
        without a screenshot, so UIA-only / coordinates-only
        queries cost zero captures even when screenshot-based
        strategies are also registered.  We only acquire a
        screenshot if the first attempt raises
        :class:`TargetNotGroundedError` AND at least one
        registered strategy requires a screenshot.

        Phase 13: when a screenshot is taken, a
        :class:`ScreenshotMetadata` is built and attached to the
        returned :class:`VisionResult`.  When no screenshot is
        taken (the common UIA-only case) ``screenshot_metadata``
        is ``None``.
        """
        logger.info(f"[vision] ground_target: {target_query!r}")
        image_path: Optional[str] = None
        screenshot_used = False
        screenshot_meta: Optional[ScreenshotMetadata] = None

        # First attempt: no screenshot.
        try:
            grounded: GroundedTarget = self._router.ground_target(
                target_query,
                image_path=image_path,
                preferred_strategy=preferred_strategy,
            )
        except TargetNotGroundedError as first_exc:
            # Lazy retry: only if a screenshot-requiring strategy exists.
            if not any(s.requires_screenshot for s in self._router.strategies):
                return VisionResult(
                    status=_STATUS_NOT_FOUND,
                    target_query=target_query,
                    screenshot_used=False,
                    screenshot_metadata=None,
                    error=str(first_exc),
                )

            try:
                image_path = self._screenshot_provider.capture()
                screenshot_used = image_path is not None
            except Exception as cap_exc:  # noqa: BLE001
                logger.warning(f"[vision] screenshot capture failed: {cap_exc!r}")
                return VisionResult(
                    status=_STATUS_NOT_FOUND,
                    target_query=target_query,
                    screenshot_used=False,
                    screenshot_metadata=None,
                    error=f"screenshot capture failed: {cap_exc}",
                )

            screenshot_meta = _build_screenshot_meta(image_path)

            try:
                grounded = self._router.ground_target(
                    target_query,
                    image_path=image_path,
                    preferred_strategy=preferred_strategy,
                )
            except TargetNotGroundedError as second_exc:
                return VisionResult(
                    status=_STATUS_NOT_FOUND,
                    target_query=target_query,
                    screenshot_used=screenshot_used,
                    screenshot_metadata=screenshot_meta,
                    error=str(second_exc),
                )
            except AmbiguityError as amb_exc:
                return VisionResult(
                    status=_STATUS_AMBIGUOUS,
                    target_query=target_query,
                    screenshot_used=screenshot_used,
                    screenshot_metadata=screenshot_meta,
                    observation={
                        "candidates": [
                            {
                                "source": c.source_type.value,
                                "bbox": c.bbox,
                                "confidence": c.confidence,
                                "text": c.text,
                                "properties": dict(c.properties),
                            }
                            for c in amb_exc.candidates
                        ]
                    },
                    alternatives_discarded=len(amb_exc.candidates),
                    error="Multiple indistinguishable candidates; "
                    "the Brain must disambiguate.",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("[vision] ground_target failed after screenshot")
                return VisionResult(
                    status=_STATUS_ERROR,
                    target_query=target_query,
                    screenshot_used=screenshot_used,
                    screenshot_metadata=screenshot_meta,
                    error=str(exc),
                )
        except AmbiguityError as amb_exc:
            return VisionResult(
                status=_STATUS_AMBIGUOUS,
                target_query=target_query,
                screenshot_used=screenshot_used,
                screenshot_metadata=None,
                observation={
                    "candidates": [
                        {
                            "source": c.source_type.value,
                            "bbox": c.bbox,
                            "confidence": c.confidence,
                            "text": c.text,
                            "properties": dict(c.properties),
                        }
                        for c in amb_exc.candidates
                    ]
                },
                alternatives_discarded=len(amb_exc.candidates),
                error="Multiple indistinguishable candidates; "
                "the Brain must disambiguate.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[vision] ground_target failed")
            return VisionResult(
                status=_STATUS_ERROR,
                target_query=target_query,
                screenshot_used=screenshot_used,
                screenshot_metadata=None,
                error=str(exc),
            )

        return VisionResult(
            status=_STATUS_OBSERVED,
            target_query=target_query,
            screenshot_used=screenshot_used,
            screenshot_metadata=screenshot_meta,
            observation={
                "source": grounded.candidate.source_type.value,
                "bbox": grounded.candidate.bbox,
                "confidence": grounded.candidate.confidence,
                "text": grounded.candidate.text,
                "properties": dict(grounded.candidate.properties),
            },
            alternatives_discarded=grounded.alternatives,
            resolution_method=grounded.resolution_method,
        )

    # ------------------------------------------------------ post-action
    def observe_state(
        self,
        subject: str,
        *,
        expected: Optional[ExpectedEffect] = None,
    ) -> VisionResult:
        """Capture the current visual state of ``subject``.

        This is the *post-action* observation hook.  It returns
        ``status=OBSERVED`` when a matching target is visible,
        and ``status=NOT_FOUND`` otherwise.  The Brain compares
        this to the *before* observation and to the
        :class:`ExpectedEffect`; the service NEVER claims
        ``verified=True`` -- that is the Verifier's job.

        Phase 13: when a screenshot is captured, a
        :class:`ScreenshotMetadata` is attached to the returned
        :class:`VisionResult` so the verifier can check staleness.
        """
        logger.info(f"[vision] observe_state: subject={subject!r}")
        image_path = self._maybe_capture_screenshot()
        screenshot_meta = _build_screenshot_meta(image_path) if image_path else None

        try:
            grounded = self._router.ground_target(
                subject, image_path=image_path
            )
        except TargetNotGroundedError:
            return VisionResult(
                status=_STATUS_NOT_FOUND,
                target_query=subject,
                screenshot_used=image_path is not None,
                screenshot_metadata=screenshot_meta,
                observation=None,
                error=f"Target {subject!r} not visible.",
            )
        except AmbiguityError as exc:
            return VisionResult(
                status=_STATUS_AMBIGUOUS,
                target_query=subject,
                screenshot_used=image_path is not None,
                screenshot_metadata=screenshot_meta,
                observation={
                    "candidates": [
                        {
                            "source": c.source_type.value,
                            "bbox": c.bbox,
                            "confidence": c.confidence,
                            "text": c.text,
                        }
                        for c in exc.candidates
                    ]
                },
                alternatives_discarded=len(exc.candidates),
                error="Multiple indistinguishable candidates after "
                "the action; the Brain must disambiguate.",
            )
        except Exception as exc:  # noqa: BLE001
            return VisionResult(
                status=_STATUS_ERROR,
                target_query=subject,
                screenshot_used=image_path is not None,
                screenshot_metadata=screenshot_meta,
                error=str(exc),
            )

        return VisionResult(
            status=_STATUS_OBSERVED,
            target_query=subject,
            screenshot_used=image_path is not None,
            screenshot_metadata=screenshot_meta,
            observation={
                "source": grounded.candidate.source_type.value,
                "bbox": grounded.candidate.bbox,
                "confidence": grounded.candidate.confidence,
                "text": grounded.candidate.text,
                "properties": dict(grounded.candidate.properties),
            },
            alternatives_discarded=grounded.alternatives,
            resolution_method=grounded.resolution_method,
        )

    def diff_observations(
        self,
        before: Optional[VisionResult],
        after: Optional[VisionResult],
    ) -> Dict[str, Any]:
        """Return a structural diff between two observations.

        Used by the Agent to decide if a state change happened.
        A diff is a pure function over two observations -- it
        must never call LLM / mouse / keyboard.
        """
        if before is None or after is None:
            return {
                "changed": None,
                "reason": "missing observation",
                "before": None if before is None else before.status,
                "after": None if after is None else after.status,
            }

        if before.status == _STATUS_OBSERVED and after.status == _STATUS_NOT_FOUND:
            return {
                "changed": True,
                "reason": "target disappeared",
                "before": before.observation,
                "after": after.observation,
            }
        if before.status == _STATUS_NOT_FOUND and after.status == _STATUS_OBSERVED:
            return {
                "changed": True,
                "reason": "target appeared",
                "before": before.observation,
                "after": after.observation,
            }
        if (
            before.status == _STATUS_OBSERVED
            and after.status == _STATUS_OBSERVED
            and before.observation != after.observation
        ):
            return {
                "changed": True,
                "reason": "target observation changed",
                "before": before.observation,
                "after": after.observation,
            }
        return {
            "changed": False,
            "reason": "no change detected",
            "before": before.observation,
            "after": after.observation,
        }

    # ---------------------------------------------------------- internals
    def _maybe_capture_screenshot(
        self,
        *,
        preferred_strategy: Optional[str] = None,
    ) -> Optional[str]:
        """Capture a screenshot *only* if a strategy that needs it is in play.

        This implements Phase 7.1's "lazy screenshot acquisition":
        UIA and coordinates never need a screenshot, so we never
        acquire one when those strategies are the only ones in
        play.  When ``preferred_strategy`` is set, we honour it;
        otherwise we ask the router for its planned order.
        """
        needs = self._any_strategy_needs_screenshot(preferred_strategy)
        if not needs:
            return None
        try:
            return self._screenshot_provider.capture()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[vision] screenshot capture failed: {exc!r}")
            return None

    def _any_strategy_needs_screenshot(
        self, preferred_strategy: Optional[str]
    ) -> bool:
        strategies = self._router.strategies
        if preferred_strategy:
            for s in strategies:
                if s.name == preferred_strategy and s.requires_screenshot:
                    return True
        for s in strategies:
            if s.requires_screenshot:
                return True
        return False


def _build_screenshot_meta(image_path: Optional[str]) -> Optional[ScreenshotMetadata]:
    """Build a :class:`ScreenshotMetadata` from a captured image path.

    Phase 13 helper.  We do not have the image's pixel dimensions
    at this seam (the screenshot capability does not return them
    yet), so we stamp a safe default of 1x1.  The coordinate
    safety gate will then reject any grounded point, which is the
    desired safe failure when dimensions are unknown.  Hosts that
    upgrade the screenshot capability to return ``width`` /
    ``height`` in its result get the typed metadata for free via
    :func:`vision.observations.screenshot_metadata.from_capability_result`.
    """
    if not image_path:
        return None
    try:
        import os
        if not os.path.exists(image_path):
            return None
        st = os.stat(image_path)
    except Exception:  # noqa: BLE001
        return None
    return make_screenshot_metadata(
        image_width=1,
        image_height=1,
        path=image_path,
        metadata={"file_size": st.st_size},
    )


__all__ = [
    "VisionService",
    "VisionResult",
    "ScreenshotProvider",
    "CapabilityScreenshotProvider",
    "NullScreenshotProvider",
]
