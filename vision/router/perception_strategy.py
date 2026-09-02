"""
Perception Strategy Protocol for V6 Phase 7.

A :class:`PerceptionStrategy` is a pure observation module: it takes
a target query (and optionally a screenshot) and returns a list of
:meth:`TargetCandidate` observations.  The protocol is the *only*
allowed extension point for new perceptual back-ends (UIA, OCR,
YOLO, etc.).

R-22 boundary: strategies are pluggable, but they MUST NOT:
  * call LLMs
  * call pyautogui.mouse / pyautogui.keyboard
  * call other capabilities directly
  * mutate the screen or any world state
  * invent new candidates from non-observations (e.g. fabricate
    UI elements that UIA did not report)

Strategies that *require* a screenshot (OCR, YOLO) declare
``requires_screenshot = True``; the router uses that to drive
lazy screenshot acquisition (UIA does not need one).
"""
from __future__ import annotations

from typing import Any, List, Optional, Protocol

from vision.observations.targets import TargetCandidate


class PerceptionStrategy(Protocol):
    """Protocol for a strategy that parses visual/UI input."""

    @property
    def name(self) -> str:
        """Name of the strategy (e.g. ``'uia'``, ``'ocr'``, ``'yolo'``)."""
        ...

    @property
    def requires_screenshot(self) -> bool:
        """Whether this strategy needs a screenshot to operate.

        ``True`` for OCR / YOLO; ``False`` for UIA / coordinates,
        which observe the system directly.  The router uses this
        to drive lazy screenshot acquisition -- never call the
        capability eagerly if the chosen strategy does not need
        it.
        """
        ...

    @property
    def source_reliability(self) -> float:
        """How reliable the strategy is, in ``[0.0, 1.0]``.

        Used by the router when disambiguating.  Defaults to
        ``0.5``.  Strategies should NOT inflate this; UIA is the
        most reliable in the V6 design and is the default.
        """
        ...

    def find_targets(
        self,
        target_query: str,
        image_path: Optional[str] = None,
        **kwargs: Any,
    ) -> List[TargetCandidate]:
        """Return all candidates matching ``target_query``."""
        ...


__all__ = ["PerceptionStrategy"]
