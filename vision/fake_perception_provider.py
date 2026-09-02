"""
Omnix V6 — Fake Perception Provider for Stage 18.8 testing.

This module provides a fake/test implementation of PerceptionProvider
that returns deterministic PerceptionResult without accessing the real desktop.
This allows future Agent/grounding tests to run without Windows UI interaction.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple, Dict, Any, List

from vision.perception_contract import (
    PerceptionProvider,
    PerceptionRequest,
    PerceptionResult,
    PerceptionSource,
    PerceptionStatus,
    ScreenInfo,
    WindowContext,
)
from vision.observations.targets import TargetCandidate


@dataclass(frozen=True)
class FakePerceptionProvider(PerceptionProvider):
    """
    Fake perception provider that returns deterministic observations
    without accessing the real desktop.

    This is useful for testing the perception → grounding → action pipeline
    without requiring actual screen interaction.
    """

    # Configuration for what the fake provider should return
    return_screenshot: bool = False
    return_candidates: List[TargetCandidate] = field(default_factory=list)
    return_window_context: Optional[WindowContext] = None
    return_sources: Tuple[PerceptionSource, ...] = ()
    status: PerceptionStatus = PerceptionStatus.SUCCESS
    duration_ms: float = 10.0
    observation_id: Optional[str] = None
    timestamp: Optional[datetime] = None
    error_message: Optional[str] = None

    async def observe(
        self,
        request: PerceptionRequest,
        cancellation_token: Optional[Any] = None,
    ) -> PerceptionResult:
        """
        Return deterministic perception result without accessing the desktop.

        This implementation:
        - Does NOT call LLMs for perception
        - Does NOT decide what actions to perform
        - Does NOT perform any actions
        - Returns structured observations only
        """
        # Check for cancellation
        if cancellation_token and hasattr(cancellation_token, 'cancelled') and cancellation_token.cancelled:
            return PerceptionResult(
                observation_id=self.observation_id or f"fake-obs-{uuid.uuid4().hex[:8]}",
                timestamp=self.timestamp or datetime.now(),
                screen=self._get_fake_screen_info(),
                status=PerceptionStatus.CANCELLED,
                duration_ms=self.duration_ms
            )

        # Build metadata
        metadata = {
            "fake_provider": True,
            "configured_sources": [s.value for s in self.return_sources],
            "requested_screenshot": request.include_screenshot
        }
        if self.error_message:
            metadata["error"] = self.error_message

        # Return the configured fake result
        return PerceptionResult(
            observation_id=self.observation_id or f"fake-obs-{uuid.uuid4().hex[:8]}",
            timestamp=self.timestamp or datetime.now(),
            screen=self._get_fake_screen_info(),
            screenshot=b"fake-screenshot-data" if self.return_screenshot and request.include_screenshot else None,
            candidates=tuple(self.return_candidates),
            window_context=self.return_window_context,
            sources=self.return_sources,
            duration_ms=self.duration_ms,
            status=self.status,
            metadata=metadata
        )

    def get_available_sources(self) -> Tuple[PerceptionSource, ...]:
        """Return the fake available sources."""
        # Return commonly available sources for testing
        return (
            PerceptionSource.SCREENSHOT,
            PerceptionSource.VISION,
            PerceptionSource.UI_AUTOMATION,
            PerceptionSource.COORDINATE,
        )

    def is_source_available(self, source: PerceptionSource) -> bool:
        """Check if a specific perception source is available in the fake provider."""
        return source in self.get_available_sources()

    def _get_fake_screen_info(self) -> ScreenInfo:
        """Get fake screen information for testing."""
        return ScreenInfo(
            width=1920,
            height=1080,
            dpi_scale_x=1.0,
            dpi_scale_y=1.0,
            monitor_id="fake-monitor-001",
            coordinate_space="screen"
        )


def create_fake_perception_provider(
    return_screenshot: bool = False,
    return_candidates: Optional[List[TargetCandidate]] = None,
    return_window_context: Optional[WindowContext] = None,
    return_sources: Optional[Tuple[PerceptionSource, ...]] = None,
    status: PerceptionStatus = PerceptionStatus.SUCCESS,
    duration_ms: float = 10.0,
    observation_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    error_message: Optional[str] = None
) -> FakePerceptionProvider:
    """
    Factory function to create a configured fake perception provider.

    Args:
        return_screenshot: Whether to return fake screenshot data
        return_candidates: List of TargetCandidate objects to return
        return_window_context: WindowContext to return
        return_sources: Perception sources that contributed to the result
        status: PerceptionStatus to return
        duration_ms: Duration of the fake observation in milliseconds
        observation_id: Fixed observation ID (if None, generates one)
        timestamp: Fixed timestamp (if None, uses current time)
        error_message: Optional error message to include in metadata

    Returns:
        Configured FakePerceptionProvider instance
    """
    return FakePerceptionProvider(
        return_screenshot=return_screenshot,
        return_candidates=return_candidates or [],
        return_window_context=return_window_context,
        return_sources=return_sources or (),
        status=status,
        duration_ms=duration_ms,
        observation_id=observation_id,
        timestamp=timestamp,
        error_message=error_message
    )


# Pre-configured fake providers for common test scenarios
def create_empty_fake_provider() -> FakePerceptionProvider:
    """Create a fake provider that returns no candidates."""
    return create_fake_perception_provider(
        return_candidates=[],
        status=PerceptionStatus.SUCCESS
    )


def create_single_candidate_provider(text: str = "Fake Button", confidence: float = 0.95) -> FakePerceptionProvider:
    """Create a fake provider that returns a single candidate."""
    from vision.observations.targets import TargetCandidate
    from core.orchestration.models import ObservationSource

    candidate = TargetCandidate(
        source_type=ObservationSource.UIA,
        bbox=(100, 100, 200, 140),  # (left, top, right, bottom)
        confidence=confidence,
        text=text,
        properties={"automation_id": "fake_button"},
        timestamp=time.time()
    )

    return create_fake_perception_provider(
        return_candidates=[candidate],
        return_sources=(PerceptionSource.UI_AUTOMATION,),
        status=PerceptionStatus.SUCCESS
    )


def create_failed_provider(error_message: str = "Fake perception failure") -> FakePerceptionProvider:
    """Create a fake provider that returns a failed status."""
    return create_fake_perception_provider(
        status=PerceptionStatus.FAILED,
        duration_ms=5.0,
        error_message=error_message
    )


def create_timeout_provider() -> FakePerceptionProvider:
    """Create a fake provider that returns a timeout status."""
    return create_fake_perception_provider(
        status=PerceptionStatus.TIMEOUT,
        duration_ms=5000.0  # 5 seconds
    )


def create_cancelled_provider() -> FakePerceptionProvider:
    """Create a fake provider that returns a cancelled status."""
    return create_fake_perception_provider(
        status=PerceptionStatus.CANCELLED,
        duration_ms=2.0
    )