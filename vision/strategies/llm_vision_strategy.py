"""
Omnix V6 — System 3 (Vision) optional LLM-vision strategy (stub).

The strategy is a placeholder for a future, fully-wired
"ask the LLM what is on the screen" perception path.  It is
**off by default**: the module imports cleanly on any host,
but every public method raises
:func:`NotImplementedError` until a configuration key
(``OMNIX_LLM_VISION_MODEL``) is set in the :class:`OmnixConfig`
and the strategy is registered with the perception router.

Design constraints (per the System 3 spec)
------------------------------------------
* **R-8 (no claimed verification)**: the strategy never
  reports ``status=VERIFIED``; it only produces observations.
* **R-14 (service, not singleton)**: the strategy is a plain
  Python class; callers construct and inject it; there is no
  global state.
* **R-21 (closed capability seam)**: the strategy depends
  only on the :class:`ScreenshotProvider` interface; it does
  not import from ``system/windows`` or call pywin32
  directly.
* **R-22 (deterministic routing)**: when the strategy is
  registered, it sits at the *highest* reliability rank
  (least reliable) so it is consulted only when every other
  strategy has returned ``LOW_CONFIDENCE`` or
  ``ACCESSIBILITY_UNAVAILABLE``.

Wiring
------
The strategy is *only* registered when
``core.configuration.OmnixConfig.llm_vision_model`` is set;
the wiring lives in :func:`core.omnix_engine._build_perception_router`.
The default V6 boot is unchanged.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from core.orchestration.models import ObservationSource
from vision.grounded_element import (
    ELEMENT_TYPE_UNKNOWN,
    GroundedElement,
    GroundedElementStatus,
    from_target_candidate,
    not_found as _not_found,
)
from vision.observations.targets import TargetCandidate
from vision.router.perception_strategy import PerceptionStrategy

_log = logging.getLogger(__name__)


class LLMVisionNotConfigured(RuntimeError):
    """Raised when the LLM-vision strategy is invoked without
    a configured model.  The router treats this the same as
    "strategy not registered" — it is not a user-visible
    error, it is a signal to fall back to the next strategy.
    """


@dataclass
class LLMVisionStrategy(PerceptionStrategy):
    """Stub LLM-vision strategy.

    When ``model`` is empty, every public method raises
    :class:`LLMVisionNotConfigured`.  When a real model client
    is wired in, this class becomes the bridge between the
    perception router's :class:`TargetCandidate` contract and
    the LLM's response.

    The current implementation deliberately produces no
    candidates.  This is the safe behaviour: a half-wired
    strategy must never claim to have observed something it
    did not.
    """

    model: str = ""
    max_candidates: int = 8
    timeout_s: float = 5.0
    enabled: bool = field(default=False)

    # ----- introspection -----
    @property
    def name(self) -> str:
        return "llm_vision"

    @property
    def source(self) -> ObservationSource:
        return ObservationSource.VISION

    @property
    def is_configured(self) -> bool:
        return bool(self.enabled and self.model)

    def configure(self, model: str, *, timeout_s: float = 5.0) -> None:
        """Enable the strategy.  Called by the engine when the
        config key ``OMNIX_LLM_VISION_MODEL`` is set.
        """
        self.model = str(model or "").strip()
        self.timeout_s = max(0.1, float(timeout_s))
        self.enabled = bool(self.model)
        if not self.enabled:
            raise LLMVisionNotConfigured(
                "LLMVisionStrategy.configure() called with empty model"
            )

    # ----- perception contract -----
    def perceive(  # type: ignore[override]
        self,
        query: str,
        *,
        screenshot: Any = None,
        window_handle: Optional[int] = None,
    ) -> list:
        """Return a list of :class:`TargetCandidate`.

        Raises :class:`LLMVisionNotConfigured` until the
        strategy has been :meth:`configure`-d.  The current
        stub always returns an empty list — the safe default
        for a not-fully-wired strategy.
        """
        if not self.is_configured:
            raise LLMVisionNotConfigured(
                "LLMVisionStrategy is not configured; set "
                "OMNIX_LLM_VISION_MODEL to enable."
            )
        # The real implementation will:
        #   1. Pass the screenshot + query to the LLM.
        #   2. Parse the response into TargetCandidate(s).
        #   3. Return the top-N by the LLM's own confidence.
        # Until that is built, we return no candidates.
        _log.debug(
            "LLMVisionStrategy.perceive called but no LLM client is wired; "
            "returning empty candidate list"
        )
        return []

    def confidence_threshold(self) -> float:
        """Threshold for the router to treat the result as a
        positive observation.  LLM outputs are noisier than
        UIA so the default is more permissive than the
        UIA strategy's threshold.
        """
        return 0.5


__all__ = [
    "LLMVisionStrategy",
    "LLMVisionNotConfigured",
]
