"""
Vision fallback adapter (Phase 8).

The browser subsystem prefers DOM / accessibility / text resolution
("Preferred order: DOM/accessibility/locator → structured browser
target → browser action").  Vision is a *fallback*, not a primary
targeting mechanism.  This module defines the closed set of hooks
the :class:`BrowserService` may invoke to escalate a target to
vision.

Hard prohibitions:

* The vision fallback NEVER receives cookies, passwords, or
  full HTML.  It only ever sees a screenshot path and the
  target query.
* The fallback NEVER calls the LLM provider directly.  If it
  needs a model, it goes through the V6 :class:`VisionService`.
* The fallback NEVER executes JavaScript.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class VisionFallbackResult:
    """The structured outcome of a vision-fallback attempt."""

    resolved: bool
    x: Optional[int] = None
    y: Optional[int] = None
    width: int = 0
    height: int = 0
    confidence: float = 0.0
    text: str = ""
    error: Optional[str] = None
    metadata: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        # ``metadata`` may be set to a mapping; never a free-form
        # string.  Guard against accidental misuse.
        if self.metadata is not None and not isinstance(
            self.metadata, Mapping
        ):
            raise TypeError(
                "VisionFallbackResult.metadata must be a Mapping or None"
            )


@runtime_checkable
class VisionFallback(Protocol):
    """The closed set of operations a vision fallback must implement.

    The browser service treats this as a duck-typed seam; concrete
    implementations are injected by the :class:`BrowserService`
    constructor.  ``None`` means "no vision fallback" — the
    service surfaces :class:`TargetResolutionMethod.UNRESOLVED`
    when DOM resolution fails.
    """

    def ground_via_vision(
        self,
        target_query: str,
        screenshot_path: str,
    ) -> VisionFallbackResult:
        ...


class NullVisionFallback:
    """The default vision fallback: never resolves anything.

    A service without a real vision fallback wired in uses this.
    The fallback always returns ``resolved=False``; the service
    then surfaces ``UNRESOLVED`` to the Brain.
    """

    def ground_via_vision(
        self,
        target_query: str,
        screenshot_path: str,
    ) -> VisionFallbackResult:
        return VisionFallbackResult(
            resolved=False,
            error="vision fallback not configured",
        )


class VisionFallbackAdapter:
    """A thin adapter that wraps an existing :class:`VisionService`.

    This is *advisory* — the :class:`BrowserService` never imports
    the vision layer directly.  The adapter is duck-typed: it
    only needs a ``ground_target(target_query, image_path=...)``
    method that returns something with ``status``, ``observation``
    fields.

    This keeps the browser subsystem testable without the vision
    layer, and keeps the vision subsystem testable without the
    browser.
    """

    def __init__(self, vision_service: Any) -> None:
        self._vs = vision_service

    def ground_via_vision(
        self,
        target_query: str,
        screenshot_path: str,
    ) -> VisionFallbackResult:
        if self._vs is None:
            return VisionFallbackResult(
                resolved=False,
                error="vision service is None",
            )
        try:
            result = self._vs.ground_target(
                target_query, image_path=screenshot_path
            )
        except Exception as exc:  # noqa: BLE001
            return VisionFallbackResult(
                resolved=False, error=f"vision raised: {exc}"
            )
        status = getattr(result, "status", "")
        if status != "OBSERVED":
            return VisionFallbackResult(
                resolved=False, error=f"vision status={status!r}"
            )
        obs = getattr(result, "observation", None) or {}
        bbox = obs.get("bbox") if isinstance(obs, dict) else None
        x: Optional[int] = None
        y: Optional[int] = None
        w = 0
        h = 0
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            x, y, w, h = (int(v) for v in bbox)
        confidence = (
            float(obs.get("confidence", 0.0))
            if isinstance(obs, dict) else 0.0
        )
        text = (
            str(obs.get("text", ""))
            if isinstance(obs, dict) else ""
        )
        return VisionFallbackResult(
            resolved=True,
            x=x,
            y=y,
            width=w,
            height=h,
            confidence=confidence,
            text=text,
            metadata={"resolution_method": "vision_fallback"},
        )
