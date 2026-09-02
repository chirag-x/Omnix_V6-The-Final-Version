"""
Visual / YOLO Strategy for Omnix V6 Phase 7.

Uses Ultralytics YOLO11n for object detection via lazy loading.

R-22 / Phase 7.1 hardening:
  * YOLO is *evidence only*, not a semantic understanding of
    Windows controls.  YOLO does NOT know that ``[556, 230]`` is
    a Save button -- it knows the box looks like a laptop / a cup /
    a person.  The Brain is the only thing that can do semantic
    reasoning.
  * REQUIRES a screenshot (``requires_screenshot = True``).
  * Returns only observations; no LLM calls, no mouse / keyboard.
  * The ``properties`` payload carries the YOLO class name and
    ID so callers can decide how to use the evidence; the
    strategy itself does NOT use that evidence to manufacture a
    semantic identity.
"""
from __future__ import annotations

import os
from typing import Any, List, Optional

from core.orchestration.models import ObservationSource
from vision.router.perception_strategy import PerceptionStrategy
from vision.observations.targets import TargetCandidate


# YOLO is the *least* reliable in the V6 design -- it is an
# evidence layer (object detection), not a UI grounding layer.
# Use it as a last-resort signal, not as a primary answer.
_YOLO_RELIABILITY = 0.4


class VisualStrategy(PerceptionStrategy):
    """Ultralytics YOLO11n object detector.

    The model is loaded on first use; subsequent calls reuse the
    instance.  The strategy exposes *bounding boxes* and *class
    names* -- it does NOT translate those into semantic UI
    understanding.
    """

    def __init__(self, model_path: Optional[str] = None) -> None:
        self._model: Any = None
        self._model_path = model_path or os.path.join(
            "vision", "models", "yolo11n.pt"
        )

    @property
    def name(self) -> str:
        return "yolo"

    @property
    def requires_screenshot(self) -> bool:
        return True

    @property
    def source_reliability(self) -> float:
        return _YOLO_RELIABILITY

    def _get_model(self) -> Any:
        if self._model is None:
            # Lazy load torch and ultralytics; the engine startup
            # is NOT blocked on this import.
            from ultralytics import YOLO  # type: ignore
            if os.path.exists(self._model_path):
                self._model = YOLO(self._model_path)
            else:
                self._model = YOLO("yolo11n.pt")
        return self._model

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
            model = self._get_model()
            results = model(image_path, verbose=False)
        except Exception:
            return []

        candidates: List[TargetCandidate] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            for box in boxes:
                try:
                    cls_id = int(box.cls[0].item())
                    cls_name = str(model.names[cls_id]).lower()
                    conf = float(box.conf[0].item())
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                except Exception:
                    continue

                # The match is purely lexical on the YOLO class
                # name.  We do NOT promote it to "this is a Save
                # button" -- the Brain decides what the box means.
                if lower_query in cls_name or cls_name in lower_query:
                    candidates.append(
                        TargetCandidate(
                            source_type=ObservationSource.VISION,
                            bbox=(int(x1), int(y1), int(x2), int(y2)),
                            confidence=conf,
                            text=cls_name,
                            properties={"class_id": cls_id},
                        )
                    )
        return candidates
