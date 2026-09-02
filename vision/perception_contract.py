"""
Omnix V6 — Canonical Perception API Contract for Stage 18.8.

This module defines the stable, explicit API boundary between perception and grounding.
Perception has one clear responsibility: observe the current computer state and return
structured observations. It does NOT decide what action to perform, perform the action,
or call the LLM merely to perceive the screen.

The contract consists of:
1. PerceptionProvider - the canonical provider interface
2. PerceptionRequest - the request model for observation
3. PerceptionResult - the result model containing observations
4. TargetCandidate - generic observation candidates (reused from existing)
5. PerceptionSource - enumeration of available perception sources
6. PerceptionStatus - status of perception operations
7. ScreenInfo - screen coordinate environment information
8. WindowContext - current window context information
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Protocol, runtime_checkable
from uuid import uuid4

# Reuse existing types where appropriate
from vision.observations.targets import TargetCandidate
from core.orchestration.models import ObservationSource


class PerceptionSource(Enum):
    """Canonical perception sources that Omnix actually implements."""
    SCREENSHOT = "screenshot"
    VISION = "vision"
    OCR = "ocr"
    UI_AUTOMATION = "ui_automation"
    ACCESSIBILITY = "accessibility"
    WINDOW_MANAGER = "window_manager"
    COORDINATE = "coordinate"


class PerceptionStatus(Enum):
    """Canonical perception status values."""
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True)
class ScreenInfo:
    """Screen coordinate environment information.

    Defines the coordinate system used by TargetCandidate.bounds.
    All perception coordinates are in canonical screen coordinates
    (physical pixels, top-left origin).
    """
    width: int
    height: int
    dpi_scale_x: float
    dpi_scale_y: float
    monitor_id: Optional[str] = None
    coordinate_space: str = "screen"  # Always "screen" for canonical coordinates


@dataclass(frozen=True)
class WindowContext:
    """Current window context information.

    Allows downstream grounding to know: "This target came from THIS window."
    """
    hwnd: Optional[int] = None
    title: Optional[str] = None
    application: Optional[str] = None
    bounds: Optional[Tuple[int, int, int, int]] = None  # (left, top, right, bottom)
    is_foreground: bool = False


@dataclass(frozen=True)
class PerceptionRequest:
    """Request model for perception observation.

    Conceptually supports:
    - include_screenshot
    - include_vision
    - include_ocr
    - include_ui_elements
    - include_window_context
    - region
    - max_age
    """
    include_screenshot: bool = True
    include_vision: bool = True
    include_ocr: bool = False
    include_ui_elements: bool = False
    include_window_context: bool = True
    region: Optional[Tuple[int, int, int, int]] = None  # (left, top, right, bottom)
    max_age_ms: Optional[int] = None

    # For future extensibility while maintaining immutability
    _extensions: Dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class PerceptionResult:
    """Canonical perception result containing structured observations.

    One canonical perception result must exist - reusing existing models where equivalent.
    """
    observation_id: str
    timestamp: datetime

    screen: ScreenInfo

    # Optional perception data
    screenshot: Optional[bytes] = None  # Raw screenshot bytes if requested

    # Observation candidates from perception sources
    candidates: Tuple[TargetCandidate, ...] = field(default_factory=tuple)

    # Window context for the observation
    window_context: Optional[WindowContext] = None

    # Which perception sources contributed to this result
    sources: Tuple[PerceptionSource, ...] = field(default_factory=tuple)

    # Performance and status information
    duration_ms: float = 0.0
    status: PerceptionStatus = PerceptionStatus.SUCCESS

    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=tuple)

    def __post_init__(self):
        # Ensure observation_id is set if not provided
        if not self.observation_id:
            object.__setattr__(self, 'observation_id', str(uuid4()))

        # Ensure timestamp is set if not provided
        if self.timestamp is None:
            object.__setattr__(self, 'timestamp', datetime.now())


@runtime_checkable
class PerceptionProvider(Protocol):
    """Canonical perception provider interface.

    This is the one stable entry point for perception operations.
    Perception observes current computer state and returns structured observations.
    It does NOT decide actions, perform actions, or call LLMs for perception.
    """

    async def observe(
        self,
        request: PerceptionRequest,
        cancellation_token: Optional[Any] = None,
    ) -> PerceptionResult:
        """Observe current computer state and return structured observations.

        Args:
            request: PerceptionRequest specifying what to observe
            cancellation_token: Optional token for cancelling the operation

        Returns:
            PerceptionResult containing the observation data

        The perception provider must:
        - Not call LLMs for perception
        - Not decide what actions to perform
        - Not perform any actions
        - Return structured observations only
        """
        ...

    def get_available_sources(self) -> Tuple[PerceptionSource, ...]:
        """Get the perception sources available in the current environment.

        Returns:
            Tuple of PerceptionSource values that are actually available
        """
        ...

    def is_source_available(self, source: PerceptionSource) -> bool:
        """Check if a specific perception source is available.

        Args:
            source: The perception source to check

        Returns:
            True if the source is available, False otherwise
        """
        ...