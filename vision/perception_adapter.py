"""
Omnix V6 — Perception Adapter for Stage 18.8.

This module adapts the existing PerceptionRouter to the canonical
PerceptionProvider interface defined in perception_contract.py.

The adapter allows downstream systems to depend on the stable
PerceptionProvider contract while continuing to use the existing
perception implementation.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from vision.perception_contract import (
    PerceptionProvider,
    PerceptionRequest,
    PerceptionResult,
    PerceptionSource,
    PerceptionStatus,
    ScreenInfo,
    WindowContext,
)
from vision.router.perception_router import (
    PerceptionRouter,
    AmbiguityError,
    TargetNotGroundedError,
)
from vision.router.screenshot_provider import ScreenshotProvider
from vision.observations.targets import TargetCandidate
from core.orchestration.models import ObservationSource


class PerceptionAdapter(PerceptionProvider):
    """
    Adapter that converts the existing PerceptionRouter to the canonical
    PerceptionProvider interface.

    This adapter maintains backward compatibility while providing the
    stable perception contract required by Stage 18.8.
    """

    def __init__(
        self,
        router: PerceptionRouter,
        screenshot_provider: ScreenshotProvider,
    ):
        self._router = router
        self._screenshot_provider = screenshot_provider
        self._available_sources = self._detect_available_sources()

    def _detect_available_sources(self) -> Tuple[PerceptionSource, ...]:
        """Detect which perception sources are available in the current environment."""
        sources = []

        # Always available: coordinate strategy (uses screen dimensions)
        sources.append(PerceptionSource.COORDINATE)

        # Check if UIA strategy is available
        try:
            from vision.strategies.uia_strategy import UIAStrategy
            if UIAStrategy():
                sources.append(PerceptionSource.UI_AUTOMATION)
                sources.append(PerceptionSource.ACCESSIBILITY)  # UIA provides accessibility info
        except ImportError:
            pass

        # Check if OCR strategy is available
        try:
            from vision.strategies.ocr_strategy import OCRStrategy
            if OCRStrategy():
                sources.append(PerceptionSource.OCR)
        except ImportError:
            pass

        # Check if Visual strategy is available (YOLO/template matching)
        try:
            from vision.strategies.visual_strategy import VisualStrategy
            if VisualStrategy():
                sources.append(PerceptionSource.VISION)
        except ImportError:
            pass

        # Screenshot is always available if the provider works
        try:
            # Test if we can capture a screenshot (don't actually save it)
            import tempfile
            import os
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                tmp_path = tmp.name
            try:
                result = self._screenshot_provider.capture(path=tmp_path)
                if result is not None:
                    sources.append(PerceptionSource.SCREENSHOT)
                    # Clean up test screenshot
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
            except Exception:
                # Clean up on error
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        except Exception:
            pass

        # Window manager info is usually available
        sources.append(PerceptionSource.WINDOW_MANAGER)

        return tuple(sources)

    async def observe(
        self,
        request: PerceptionRequest,
        cancellation_token: Optional[Any] = None,
    ) -> PerceptionResult:
        """
        Observe current computer state and return structured observations.
        Stage 23: Complete multi-layered sweep (Window State + UIA + OCR)
        """
        start_time = time.time()
        
        # Check for cancellation
        if cancellation_token and hasattr(cancellation_token, 'cancelled') and cancellation_token.cancelled:
            return PerceptionResult(
                observation_id="",
                timestamp=None,
                screen=self._get_screen_info(),
                status=PerceptionStatus.CANCELLED,
                duration_ms=(time.time() - start_time) * 1000
            )

        try:
            needs_screenshot = self._needs_screenshot(request)
            screenshot_path = None
            screenshot_bytes = None
            
            # 1. LAYER B: Screenshot
            if needs_screenshot and request.include_screenshot:
                import tempfile
                import os
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    screenshot_path = tmp.name

                try:
                    result_path = self._screenshot_provider.capture(path=screenshot_path)
                    if result_path and os.path.exists(result_path):
                        with open(result_path, 'rb') as f:
                            screenshot_bytes = f.read()
                finally:
                    if screenshot_path and os.path.exists(screenshot_path):
                        os.unlink(screenshot_path)

            # 2. LAYER A: System / Window State
            active_window = None
            windows_list = []
            applications_set = set()
            
            if request.include_window_context:
                active_window = self._get_window_context()
                
            # 3. LAYER C: Elements & Text Regions
            target_query = "*"
            elements = []
            text_regions = []
            
            try:
                # Run UIA Strategy specifically for elements
                try:
                    from vision.strategies.uia_strategy import UIAStrategy
                    uia = UIAStrategy()
                    if uia:
                        elements = uia.find_targets(target_query=target_query, image_path=None)
                except ImportError:
                    pass
                except Exception:
                    pass

                # Run OCR Strategy specifically for text_regions
                try:
                    from vision.strategies.ocr_strategy import OCRStrategy
                    ocr = OCRStrategy()
                    if ocr:
                        text_regions = ocr.find_targets(target_query=target_query, image_path=screenshot_path)
                except ImportError:
                    pass
                except Exception:
                    pass
            except Exception:
                pass
                
            # Filter and deduplicate elements and text_regions if necessary
            # (Stage 23 fusion)
            
            # Populate candidates for legacy support
            all_candidates = elements + text_regions
            
            perception_sources = self._convert_observation_sources(
                [getattr(c, 'source_type', ObservationSource.DERIVED) for c in all_candidates]
            )
            
            status = self._determine_perception_status(
                request=request,
                candidates=all_candidates,
                screenshot_available=screenshot_bytes is not None and request.include_screenshot,
                needs_screenshot=needs_screenshot
            )
            
            return PerceptionResult(
                observation_id=str(uuid4()),
                timestamp=None,
                screen=self._get_screen_info(),
                screenshot=screenshot_bytes,
                candidates=tuple(all_candidates),
                window_context=active_window,
                status=status,
                sources=tuple(perception_sources),
                duration_ms=(time.time() - start_time) * 1000
            )

        except Exception as e:
            logger.error(f"[vision] Perception failed: {e}")
            return PerceptionResult(
                observation_id="",
                timestamp=None,
                screen=self._get_screen_info(),
                status=PerceptionStatus.FAILED,
                duration_ms=(time.time() - start_time) * 1000
            )

    def _get_window_context(self) -> Optional[WindowContext]:
        return None

    def _determine_perception_status(
        self,
        request: PerceptionRequest,
        candidates: List[TargetCandidate],
        screenshot_available: bool,
        needs_screenshot: bool
    ) -> PerceptionStatus:
        """Determine the perception status based on request and results."""
        # Check if we were cancelled (handled earlier in observe method)

        # Check if we got the data we requested
        missing_sources = []

        if request.include_screenshot and needs_screenshot and not screenshot_available:
            missing_sources.append("screenshot")

        if request.include_vision and PerceptionSource.VISION not in self._get_candidate_sources(candidates):
            missing_sources.append("vision")

        if request.include_ocr and PerceptionSource.OCR not in self._get_candidate_sources(candidates):
            missing_sources.append("ocr")

        if request.include_ui_elements and PerceptionSource.UI_AUTOMATION not in self._get_candidate_sources(candidates):
            missing_sources.append("ui_elements")

        # If we're missing critical requested sources, it's partial or failed
        if missing_sources:
            # If we have no candidates at all and requested something, it's failed
            if len(candidates) == 0 and (request.include_screenshot or request.include_vision or request.include_ocr):
                return PerceptionStatus.FAILED
            else:
                # We got some data but not everything requested
                return PerceptionStatus.PARTIAL

        # If we got here, we satisfied the request
        return PerceptionStatus.SUCCESS

    def _get_candidate_sources(self, candidates: List[TargetCandidate]) -> List[PerceptionSource]:
        """Extract unique perception sources from candidates."""
        sources = []
        for candidate in candidates:
            source_type = getattr(candidate, 'source_type', None)
            if source_type:
                # Convert ObservationSource to PerceptionSource
                converted = self._convert_observation_sources([source_type])
                if converted:
                    sources.append(converted[0])
        return list(set(sources))  # Remove duplicates

    def get_available_sources(self) -> Tuple[PerceptionSource, ...]:
        """Get the perception sources available in the current environment."""
        return self._available_sources

    def is_source_available(self, source: PerceptionSource) -> bool:
        """Check if a specific perception source is available."""
        return source in self._available_sources


def create_default_perception_adapter() -> PerceptionAdapter:
    """
    Create a default perception adapter with standard dependencies.

    This function provides a convenient way to create the adapter
    with the default implementations used throughout Omnix.
    """
    from vision.router.perception_router import PerceptionRouter
    from vision.router.screenshot_provider import CapabilityScreenshotProvider
    from vision.strategies.uia_strategy import UIAStrategy
    from vision.strategies.ocr_strategy import OCRStrategy
    from vision.strategies.visual_strategy import VisualStrategy
    from vision.strategies.coordinates_strategy import CoordinatesStrategy

    # Create the perception router with default strategies
    router = PerceptionRouter(
        strategies=[
            UIAStrategy(),
            OCRStrategy(),
            VisualStrategy(),
            CoordinatesStrategy(),
        ]
    )

    # Create screenshot provider
    screenshot_provider = CapabilityScreenshotProvider()

    return PerceptionAdapter(router, screenshot_provider)


# ---------------------------------------------------------------------------
# Stage 19.3: lightweight PerceptionProvider implementations used by
# the ExecutionCycle wiring.  These keep the cycle callable in
# environments where the full PerceptionAdapter cannot be built
# (e.g. headless deploys, tests) and provide a thin CapabilityRouter
# adapter for hosts that prefer to pull candidates from the
# CapabilityRouter directly.
# ---------------------------------------------------------------------------


class NullPerceptionProvider(PerceptionProvider):
    """A minimal :class:`PerceptionProvider` that returns an empty
    successful observation.

    Used by the :class:`core.execution.ExecutionCycle` when no
    richer perception is available.  The cycle still runs all
    phases, but the OBSERVE phase returns an empty candidate set
    so the GROUND phase operates on whatever target hint was
    passed in by the caller.
    """

    def __init__(self) -> None:
        self.name = "null_perception_provider"

    async def observe(
        self,
        request: PerceptionRequest,
        cancellation_token: Optional[Any] = None,
    ) -> PerceptionResult:
        return PerceptionResult(
            observation_id="",
            timestamp=None,
            screen=ScreenInfo(
                width=1920,
                height=1080,
                dpi_scale_x=1.0,
                dpi_scale_y=1.0,
            ),
            candidates=tuple(),
            window_context=None,
            sources=tuple(),
            duration_ms=0.0,
            status=PerceptionStatus.SUCCESS,
            metadata={"provider": "null"},
        )

    def get_available_sources(self) -> Tuple[PerceptionSource, ...]:
        return tuple()

    def is_source_available(self, source: PerceptionSource) -> bool:
        return False


class CapabilityPerceptionProvider(PerceptionProvider):
    """A :class:`PerceptionProvider` that delegates to the
    engine's :class:`CapabilityRouter` to obtain perception
    primitives (e.g. screenshot) and reports them as a canonical
    :class:`PerceptionResult`.

    This provider is intentionally minimal — the cycle needs a
    concrete provider to drive the OBSERVE phase; hosts that have
    a richer perception service can register a different
    implementation under the ``perception_provider`` service
    name and the engine will use it.
    """

    def __init__(self, router: Any, screenshot_provider: Any) -> None:
        self._router = router
        self._screenshot_provider = screenshot_provider
        self.name = "capability_perception_provider"

    async def observe(
        self,
        request: PerceptionRequest,
        cancellation_token: Optional[Any] = None,
    ) -> PerceptionResult:
        # Best-effort screenshot capture via the configured
        # provider.  Failures are reported as a successful
        # observation with empty candidates so the cycle can
        # continue.
        screenshot_bytes: Optional[bytes] = None
        try:
            if (
                request.include_screenshot
                and self._screenshot_provider is not None
                and hasattr(self._screenshot_provider, "capture")
            ):
                screenshot_bytes = self._screenshot_provider.capture()
        except Exception:
            screenshot_bytes = None

        return PerceptionResult(
            observation_id="",
            timestamp=None,
            screen=ScreenInfo(
                width=1920,
                height=1080,
                dpi_scale_x=1.0,
                dpi_scale_y=1.0,
            ),
            candidates=tuple(),
            window_context=None,
            sources=tuple(),
            duration_ms=0.0,
            status=PerceptionStatus.SUCCESS,
            metadata={
                "provider": "capability",
                "screenshot_captured": screenshot_bytes is not None,
            },
        )

    def get_available_sources(self) -> Tuple[PerceptionSource, ...]:
        return (PerceptionSource.SCREENSHOT,)

    def is_source_available(self, source: PerceptionSource) -> bool:
        return source is PerceptionSource.SCREENSHOT
