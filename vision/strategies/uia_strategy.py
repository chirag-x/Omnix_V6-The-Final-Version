"""
UIA Strategy for Omnix V6 Phase 7.

Observes the screen using Windows UIAutomation (pywinauto).

R-22 / Phase 7.1 hardening:
  * This strategy is the *most reliable* in the V6 design (Windows
    UIA walks the live accessibility tree, not a screenshot).
  * It does NOT require a screenshot.
  * It does NOT perform any side effects.
  * It does NOT call LLMs.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional

try:
    from pywinauto import Desktop
    from pywinauto.uia_element_info import UIAElementInfo  # noqa: F401  (presence check)
    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False

from core.orchestration.models import ObservationSource
from vision.router.perception_strategy import PerceptionStrategy
from vision.observations.targets import TargetCandidate


# UIA walks the live Windows accessibility tree -- this is more
# reliable than OCR or YOLO.  Used as the tie-breaker between
# strategies that returned candidates.
_UIA_RELIABILITY = 0.95
# UIA text matches are very confident when the live tree reports them.
_UIA_BASE_CONFIDENCE = 0.9


class UIAStrategy(PerceptionStrategy):
    """Windows UIA back-end strategy (pywinauto)."""

    @property
    def name(self) -> str:
        return "uia"

    @property
    def requires_screenshot(self) -> bool:
        # UIA does NOT need a screenshot -- it walks the live
        # accessibility tree.
        return False

    @property
    def source_reliability(self) -> float:
        return _UIA_RELIABILITY

    def find_targets(
        self,
        target_query: str,
        image_path: Optional[str] = None,
        **kwargs: Any,
    ) -> List[TargetCandidate]:
        if not PYWINAUTO_AVAILABLE:
            return []

        candidates: List[TargetCandidate] = []
        lower_query = target_query.lower().strip()
        if not lower_query:
            return []

        try:
            desktop = Desktop(backend="uia")
        except Exception:
            return []

        for win in desktop.windows(visible_only=True, enabled_only=True):
            try:
                elements = win.descendants()
                for el in elements:
                    if not el.is_visible() or not el.is_enabled():
                        continue

                    name = (el.window_text() or "").lower()
                    if not name:
                        continue
                    # Substring containment in either direction --
                    # UIA is deterministic and live.
                    if lower_query in name or name in lower_query:
                        rect = el.rectangle()
                        if rect.width() > 0 and rect.height() > 0:
                            candidates.append(
                                TargetCandidate(
                                    source_type=ObservationSource.UIA,
                                    bbox=(
                                        rect.left,
                                        rect.top,
                                        rect.right,
                                        rect.bottom,
                                    ),
                                    confidence=_UIA_BASE_CONFIDENCE,
                                    text=el.window_text(),
                                    properties={
                                        "control_type": el.friendly_class_name()
                                    },
                                )
                            )
            except Exception:
                # A single bad window must not poison the whole sweep.
                continue

        return candidates
