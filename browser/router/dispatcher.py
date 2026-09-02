"""
Browser action router (Phase 8).

Maps each :class:`BrowserAction` to a single method on
:class:`browser.session.session.BrowserSession`.  The router:

* Validates the request against the per-action closed parameter
  set (:data:`browser.models.contracts.ACTION_PARAM_KEYS`).
* Calls the right method, wrapping infrastructure errors as
  :class:`BrowserResult` values.
* Performs the vision fallback when the primary resolution fails
  *and* a vision fallback adapter is configured.

The router does **not**:

* Mutate the session registry (the :class:`BrowserService` does).
* Spawn Playwright (the session does).
* Evaluate LLM output directly.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from loguru import logger

from browser.models.contracts import (
    ACTION_PARAM_KEYS,
    BrowserAction,
    BrowserElement,
    BrowserObservation,
    BrowserPageState,
    BrowserRequest,
    BrowserResult,
    BrowserResultStatus,
    BROWSER_OBSERVATION_SOURCES,
    LocatorKind,
    TargetResolutionMethod,
)
from browser.session.session import (
    BrowserSession,
    BrowserSessionError,
)
from browser.strategies.vision_fallback import (
    NullVisionFallback,
    VisionFallback,
)


class BrowserRouterError(Exception):
    """Raised when the router cannot accept a request at all."""


class BrowserRouter:
    """Closed-set dispatch from :class:`BrowserRequest` to a session."""

    def __init__(
        self,
        session: BrowserSession,
        *,
        vision_fallback: Optional[VisionFallback] = None,
    ) -> None:
        if not isinstance(session, BrowserSession):
            raise TypeError(
                f"BrowserRouter expected a BrowserSession, "
                f"got {type(session).__name__}"
            )
        self._session = session
        self._vision = vision_fallback or NullVisionFallback()

    # ----------------------------------------------------------- dispatch
    def dispatch(
        self, request: BrowserRequest
    ) -> BrowserResult:
        """Dispatch ``request`` and return a structured result."""

        started = time.time()
        # Per-action parameter key check.
        allowed = ACTION_PARAM_KEYS.get(request.action, ())
        for key in request.parameters.keys():
            if key not in allowed:
                return self._result(
                    request, started,
                    status=BrowserResultStatus.INVALID_REQUEST,
                    error=(
                        f"action {request.action.value!r}: parameter "
                        f"key {key!r} is not in the closed parameter "
                        f"set {list(allowed)}"
                    ),
                )

        # OPEN / CLOSE are special — they don't require a session
        # to be open before the call (OPEN opens it; CLOSE tolerates
        # already-closed).
        try:
            if request.action == BrowserAction.OPEN:
                return self._do_open(request, started)
            if request.action == BrowserAction.CLOSE:
                return self._do_close(request, started)
            # Everything else requires an open session.
            if not self._session.is_open:
                return self._result(
                    request, started,
                    status=BrowserResultStatus.SESSION_NOT_FOUND,
                    error=(
                        f"session {self._session.session_id!r} is not "
                        f"open"
                    ),
                )

            if request.action == BrowserAction.NAVIGATE:
                return self._do_navigate(request, started)
            if request.action == BrowserAction.BACK:
                return self._do_back(request, started)
            if request.action == BrowserAction.FORWARD:
                return self._do_forward(request, started)
            if request.action == BrowserAction.RELOAD:
                return self._do_reload(request, started)
            if request.action == BrowserAction.CLICK:
                return self._do_click(request, started)
            if request.action == BrowserAction.HOVER:
                return self._do_hover(request, started)
            if request.action == BrowserAction.TYPE:
                return self._do_type(request, started)
            if request.action == BrowserAction.PRESS:
                return self._do_press(request, started)
            if request.action == BrowserAction.SCROLL:
                return self._do_scroll(request, started)
            if request.action == BrowserAction.SELECT:
                return self._do_select(request, started)
            if request.action == BrowserAction.WAIT:
                return self._do_wait(request, started)
            if request.action == BrowserAction.EXTRACT_TEXT:
                return self._do_extract_text(request, started)
            if request.action == BrowserAction.EXTRACT_PAGE:
                return self._do_extract_page(request, started)
            if request.action == BrowserAction.DOWNLOAD:
                return self._do_download(request, started)
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=self._classify_session_error(exc),
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                f"[browser] dispatch {request.action.value} failed"
            )
            return self._result(
                request, started,
                status=BrowserResultStatus.ERROR,
                error=f"unexpected: {exc}",
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.ERROR,
            error=f"unhandled action {request.action.value!r}",
        )

    # ------------------------------------------------------------ handlers
    def _do_open(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        params = request.parameters
        headless = params.get("headless")
        if headless is not None and not isinstance(headless, bool):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="open: 'headless' must be a bool",
            )
        vw = params.get("viewport_width")
        vh = params.get("viewport_height")
        if (vw is None) != (vh is None):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error=(
                    "open: 'viewport_width' and 'viewport_height' "
                    "must be set together"
                ),
            )
        if vw is not None and (not isinstance(vw, int) or vw <= 0):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="open: 'viewport_width' must be a positive int",
            )
        if vh is not None and (not isinstance(vh, int) or vh <= 0):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="open: 'viewport_height' must be a positive int",
            )
        start_url = params.get("start_url")
        if start_url is not None and not isinstance(start_url, str):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="open: 'start_url' must be a string",
            )

        try:
            self._session.open(start_url=start_url)
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=BrowserResultStatus.ERROR,
                error=str(exc),
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="URL",
                state=self._session_extract_page(),
                resolution_method=TargetResolutionMethod.SKIPPED,
            ),
            metadata={"opened": True},
        )

    def _do_close(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        self._session.close()
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="URL",
                resolution_method=TargetResolutionMethod.SKIPPED,
            ),
            metadata={"closed": True},
        )

    def _do_navigate(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        params = request.parameters
        url = params.get("url")
        if not isinstance(url, str) or not url.strip():
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="navigate: 'url' must be a non-empty string",
            )
        state = self._session.navigate(
            url,
            wait_until=params.get("wait_until", "load"),
            timeout_ms=int(params.get("timeout_ms", 30_000)),
        )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="URL",
                state=state,
                resolution_method=TargetResolutionMethod.SKIPPED,
            ),
        )

    def _do_back(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        state = self._session.back(
            timeout_ms=int(request.parameters.get("timeout_ms", 30_000))
        )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="URL", state=state,
                resolution_method=TargetResolutionMethod.SKIPPED,
            ),
        )

    def _do_forward(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        state = self._session.forward(
            timeout_ms=int(request.parameters.get("timeout_ms", 30_000))
        )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="URL", state=state,
                resolution_method=TargetResolutionMethod.SKIPPED,
            ),
        )

    def _do_reload(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        state = self._session.reload(
            timeout_ms=int(request.parameters.get("timeout_ms", 30_000))
        )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="URL", state=state,
                resolution_method=TargetResolutionMethod.SKIPPED,
            ),
        )

    def _do_click(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        target = self._require_target(request)
        if target is None:
            return self._invalid_target_result(request, started)
        params = request.parameters
        button = params.get("button", "left")
        if button not in ("left", "right", "middle"):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error=(
                    f"click: 'button' must be left/right/middle, "
                    f"got {button!r}"
                ),
            )
        click_count = int(params.get("click_count", 1))
        delay_ms = int(params.get("delay_ms", 0))
        force = bool(params.get("force", False))
        timeout_ms = int(params.get("timeout_ms", 30_000))

        resolved, fallback_obs = self._resolve_with_fallback(
            request, target, started
        )
        if resolved is None:
            # Resolution failed (DOM and vision both fell through).  The
            # caller expects a BrowserResult, not a raw observation.
            return self._result(
                request, started,
                status=BrowserResultStatus.TARGET_NOT_FOUND,
                error=str(
                    (fallback_obs.error if fallback_obs else "") or
                    "target not resolved"
                ),
                observation=fallback_obs,
            )
        try:
            self._session.click(
                target,
                button=button,
                click_count=click_count,
                delay_ms=delay_ms,
                force=force,
                timeout_ms=timeout_ms,
            )
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=self._classify_session_error(exc),
                error=str(exc),
                observation=fallback_obs,
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="DOM",
                element=fallback_obs.element if fallback_obs else None,
                resolution_method=(
                    fallback_obs.resolution_method
                    if fallback_obs
                    else TargetResolutionMethod.DOM
                ),
                state=self._session_extract_page(),
            ),
        )

    def _do_hover(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        target = self._require_target(request)
        if target is None:
            return self._invalid_target_result(request, started)
        params = request.parameters
        timeout_ms = int(params.get("timeout_ms", 30_000))
        resolved, fallback_obs = self._resolve_with_fallback(
            request, target, started
        )
        if resolved is None:
            # Resolution failed (DOM and vision both fell through).  The
            # caller expects a BrowserResult, not a raw observation.
            return self._result(
                request, started,
                status=BrowserResultStatus.TARGET_NOT_FOUND,
                error=str(
                    (fallback_obs.error if fallback_obs else "") or
                    "target not resolved"
                ),
                observation=fallback_obs,
            )
        try:
            self._session.hover(target, timeout_ms=timeout_ms)
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=self._classify_session_error(exc),
                error=str(exc),
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="DOM",
                element=fallback_obs.element if fallback_obs else None,
                resolution_method=(
                    fallback_obs.resolution_method
                    if fallback_obs
                    else TargetResolutionMethod.DOM
                ),
            ),
        )

    def _do_type(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        target = self._require_target(request)
        if target is None:
            return self._invalid_target_result(request, started)
        params = request.parameters
        text = params.get("text")
        if not isinstance(text, str):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="type: 'text' must be a string",
            )
        delay_ms = int(params.get("delay_ms", 0))
        submit = bool(params.get("submit", False))
        resolved, fallback_obs = self._resolve_with_fallback(
            request, target, started
        )
        if resolved is None:
            # Resolution failed (DOM and vision both fell through).  The
            # caller expects a BrowserResult, not a raw observation.
            return self._result(
                request, started,
                status=BrowserResultStatus.TARGET_NOT_FOUND,
                error=str(
                    (fallback_obs.error if fallback_obs else "") or
                    "target not resolved"
                ),
                observation=fallback_obs,
            )
        try:
            self._session.type_text(
                target, text, delay_ms=delay_ms, submit=submit
            )
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=self._classify_session_error(exc),
                error=str(exc),
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="DOM",
                element=fallback_obs.element if fallback_obs else None,
                resolution_method=(
                    fallback_obs.resolution_method
                    if fallback_obs
                    else TargetResolutionMethod.DOM
                ),
                details={"typed_chars": len(text), "submit": submit},
            ),
        )

    def _do_press(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        key = request.parameters.get("key")
        if not isinstance(key, str) or not key.strip():
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="press: 'key' must be a non-empty string",
            )
        try:
            self._session.press(key)
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=self._classify_session_error(exc),
                error=str(exc),
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="DOM",
                resolution_method=TargetResolutionMethod.SKIPPED,
                details={"key": key},
            ),
        )

    def _do_scroll(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        params = request.parameters
        direction = params.get("direction")
        amount = params.get("amount")
        if not isinstance(direction, str):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="scroll: 'direction' must be a string",
            )
        if not isinstance(amount, int) or amount <= 0:
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="scroll: 'amount' must be a positive int",
            )
        target = request.target
        try:
            self._session.scroll(
                direction=direction, amount=amount, target=target
            )
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=self._classify_session_error(exc),
                error=str(exc),
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="DOM",
                resolution_method=TargetResolutionMethod.SKIPPED,
                details={"direction": direction, "amount": amount},
            ),
        )

    def _do_select(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        target = self._require_target(request)
        if target is None:
            return self._invalid_target_result(request, started)
        params = request.parameters
        value = params.get("value")
        label = params.get("label")
        if (value is None) == (label is None):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error=(
                    "select: exactly one of 'value' or 'label' must be set"
                ),
            )
        if value is not None and not isinstance(value, str):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="select: 'value' must be a string",
            )
        if label is not None and not isinstance(label, str):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="select: 'label' must be a string",
            )
        resolved, fallback_obs = self._resolve_with_fallback(
            request, target, started
        )
        if resolved is None:
            # Resolution failed (DOM and vision both fell through).  The
            # caller expects a BrowserResult, not a raw observation.
            return self._result(
                request, started,
                status=BrowserResultStatus.TARGET_NOT_FOUND,
                error=str(
                    (fallback_obs.error if fallback_obs else "") or
                    "target not resolved"
                ),
                observation=fallback_obs,
            )
        try:
            self._session.select(
                target, value=value, label=label,
                timeout_ms=int(params.get("timeout_ms", 30_000)),
            )
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=self._classify_session_error(exc),
                error=str(exc),
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="DOM",
                element=fallback_obs.element if fallback_obs else None,
                resolution_method=(
                    fallback_obs.resolution_method
                    if fallback_obs
                    else TargetResolutionMethod.DOM
                ),
                details={"value": value, "label": label},
            ),
        )

    def _do_wait(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        params = request.parameters
        until = params.get("until")
        if not isinstance(until, str):
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="wait: 'until' must be a string",
            )
        timeout_ms = int(params.get("timeout_ms", 30_000))
        target = request.target
        try:
            resolved = self._session.wait(
                until=until, target=target, timeout_ms=timeout_ms
            )
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=self._classify_session_error(exc),
                error=str(exc),
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="DOM",
                resolution_method=TargetResolutionMethod.SKIPPED,
                details={"until": until, "timeout_ms": timeout_ms,
                         "had_target": target is not None},
            ),
        )

    def _do_extract_text(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        target = self._require_target(request)
        if target is None:
            return self._invalid_target_result(request, started)
        params = request.parameters
        max_chars = int(params.get("max_chars", 4000))
        include_attributes = bool(params.get("include_attributes", False))
        try:
            element, text = self._session.extract_text(
                target,
                max_chars=max_chars,
                include_attributes=include_attributes,
            )
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=self._classify_session_error(exc),
                error=str(exc),
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="TEXT",
                element=element,
                extracted_text=text,
                resolution_method=TargetResolutionMethod.DOM,
                details={"max_chars": max_chars},
            ),
        )

    def _do_extract_page(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        max_chars = int(request.parameters.get("max_chars", 8000))
        state = self._session.extract_page(max_chars=max_chars)
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="DOM",
                state=state,
                resolution_method=TargetResolutionMethod.SKIPPED,
                details={"max_chars": max_chars},
            ),
        )

    def _do_download(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        target = self._require_target(request)
        if target is None:
            return self._invalid_target_result(request, started)
        save_to = request.parameters.get("save_to")
        if not isinstance(save_to, str) or not save_to.strip():
            return self._result(
                request, started,
                status=BrowserResultStatus.INVALID_REQUEST,
                error="download: 'save_to' must be a non-empty string",
            )
        try:
            path = self._session.download(target, save_to=save_to)
        except BrowserSessionError as exc:
            return self._result(
                request, started,
                status=BrowserResultStatus.DOWNLOAD_FAILED,
                error=str(exc),
            )
        return self._result(
            request, started,
            status=BrowserResultStatus.OK,
            observation=BrowserObservation(
                source="DOWNLOAD",
                resolution_method=TargetResolutionMethod.DOM,
                details={"saved_to": path},
            ),
        )

    # --------------------------------------------------------- resolution
    def _resolve_with_fallback(
        self,
        request: BrowserRequest,
        target: Any,
        started: float,
    ) -> tuple[Optional[Any], Optional[BrowserObservation]]:
        """Try DOM resolution; on failure, try the vision fallback.

        Returns ``(resolved_handle, observation_or_none)`` or
        ``(None, failure_result)`` if both failed.
        """
        try:
            resolved = self._session.resolve(target)
            obs = BrowserObservation(
                source="DOM",
                element=resolved._build_element_snapshot(),
                resolution_method=resolved.method,
            )
            return resolved, obs
        except BrowserSessionError as primary_exc:
            # Try vision fallback if configured.
            if not isinstance(
                self._vision, NullVisionFallback
            ):
                shot = self._session.screenshot_path()
                if shot:
                    fallback = self._vision.ground_via_vision(
                        target.label or target.value, shot
                    )
                    if fallback.resolved:
                        return (
                            None,  # caller treats this as "via vision"
                            BrowserObservation(
                                source="DOM",
                                resolution_method=(
                                    TargetResolutionMethod.VISION_FALLBACK
                                ),
                                details={
                                    "x": fallback.x,
                                    "y": fallback.y,
                                    "width": fallback.width,
                                    "height": fallback.height,
                                    "confidence": fallback.confidence,
                                    "screenshot_path": shot,
                                },
                            ),
                        )
            # Vision not configured or did not resolve; surface UNRESOLVED.
            obs = BrowserObservation(
                source="ERROR",
                error=f"target not resolved: {primary_exc}",
                resolution_method=TargetResolutionMethod.UNRESOLVED,
            )
            return None, obs

    # ----------------------------------------------------------- helpers
    def _require_target(
        self, request: BrowserRequest
    ) -> Optional[Any]:
        if request.target is None:
            return None
        return request.target

    def _invalid_target_result(
        self, request: BrowserRequest, started: float
    ) -> BrowserResult:
        return self._result(
            request, started,
            status=BrowserResultStatus.INVALID_REQUEST,
            error=(
                f"action {request.action.value!r}: a target is required"
            ),
        )

    def _session_extract_page(self) -> BrowserPageState:
        try:
            return self._session.extract_page()
        except BrowserSessionError:
            return BrowserPageState(url="")

    def _classify_session_error(
        self, exc: BrowserSessionError
    ) -> BrowserResultStatus:
        msg = str(exc).lower()
        if "timeout" in msg:
            return BrowserResultStatus.TIMEOUT
        if "not found" in msg or "no element" in msg:
            return BrowserResultStatus.TARGET_NOT_FOUND
        if "navigate" in msg:
            return BrowserResultStatus.NAVIGATION_FAILED
        if "download" in msg:
            return BrowserResultStatus.DOWNLOAD_FAILED
        return BrowserResultStatus.ERROR

    def _result(
        self,
        request: BrowserRequest,
        started: float,
        *,
        status: BrowserResultStatus,
        observation: Optional[BrowserObservation] = None,
        error: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> BrowserResult:
        finished = time.time()
        result = BrowserResult(
            status=status,
            action=request.action,
            request=request,
            observation=observation,
            error=error,
            started_at=started,
            finished_at=finished,
            metadata=metadata or {},
        )
        if status != BrowserResultStatus.OK:
            logger.warning(
                f"[browser] {request.action.value}: status={status.value} "
                f"error={error!r}"
            )
        return result
