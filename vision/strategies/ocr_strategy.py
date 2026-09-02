"""
OCR Strategy for Omnix V6 Phase 7.

Uses EasyOCR for text detection via lazy loading.  This is a pure
*evidence* strategy -- OCR reports text it sees, not the semantic
identity of a UI control.

R-22 / Phase 7.1 hardening:
  * Lazy-loads the EasyOCR reader (first call only).
  * REQUIRES a screenshot (``requires_screenshot = True``).
  * Confidence reflects EasyOCR's own ``prob`` value, NOT a
    fabricated number.
  * Returns only observations; no side effects, no LLM calls.
"""
from __future__ import annotations

from typing import Any, List, Optional

from core.orchestration.models import ObservationSource
from vision.router.perception_strategy import PerceptionStrategy
from vision.observations.targets import TargetCandidate


# OCR is less reliable than UIA (it can be confused by font, color,
# and overlap) but is the fallback when UIA does not see the
# element.  We bias routing *toward* UIA first; OCR is the
# tie-breaker after UIA.
_OCR_RELIABILITY = 0.7


class OCRStrategy(PerceptionStrategy):
    """EasyOCR-based text detector.

    The reader is built on first use; subsequent calls reuse the
    instance.  Tests that do not have EasyOCR installed (or that
    pass a falsy ``image_path``) get a clean empty list.
    """

    def __init__(self) -> None:
        self._reader: Any = None

    @property
    def name(self) -> str:
        return "ocr"

    @property
    def requires_screenshot(self) -> bool:
        return True

    @property
    def source_reliability(self) -> float:
        return _OCR_RELIABILITY

    def _get_reader(self) -> Any:
        if self._reader is None:
            import easyocr  # type: ignore
            # GPU is preferable but not required; we honour whatever
            # the deployment actually has.  This call is lazy so the
            # import does not block engine startup.
            try:
                self._reader = easyocr.Reader(["en"], gpu=True)
            except Exception:
                self._reader = easyocr.Reader(["en"], gpu=False)
        return self._reader

    def find_targets(
        self,
        target_query: str,
        image_path: Optional[str] = None,
        **kwargs: Any,
    ) -> List[TargetCandidate]:
        if not image_path:
            return []

        lower_query = target_query.lower().strip()
        if not lower_query:
            return []

        try:
            reader = self._get_reader()
            results = reader.readtext(image_path)
        except Exception:
            return []

        candidates: List[TargetCandidate] = []
        for (bbox, text, prob) in results:
            try:
                tl_x, tl_y = bbox[0]
                br_x, br_y = bbox[2]
            except (IndexError, TypeError):
                continue

            text_str = (text or "").lower()
            if not text_str:
                continue
            if lower_query in text_str or text_str in lower_query:
                candidates.append(
                    TargetCandidate(
                        source_type=ObservationSource.OCR,
                        bbox=(int(tl_x), int(tl_y), int(br_x), int(br_y)),
                        confidence=float(prob),
                        text=text,
                        properties={},
                    )
                )
        return candidates
