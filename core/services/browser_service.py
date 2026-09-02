"""
Browser Service for Omnix V6 Phase 8.

This module is the *single canonical boundary* between V6 and the
browser subsystem.  It mirrors the structure of
:mod:`core.services.vision_service`:

* Not a singleton (R-14); every consumer instantiates its own
  service (or shares one explicitly).
* Closed-set input (:class:`BrowserRequest`) and output
  (:class:`BrowserResult`).
* A safety policy is applied *before* dispatch.
* A vision fallback is duck-typed (the service never assumes it
  is configured).
* The service does not import ``subprocess`` / ``os.system`` /
  ``os.popen``; it only invokes the closed action set through
  :class:`browser.router.dispatcher.BrowserRouter`.
* Cookies, passwords, full HTML, and full DOM are *never*
  exposed to callers.  The service only returns the bounded
  snapshots in :class:`BrowserPageState` and
  :class:`BrowserElement`.

Architecture rules honoured
----------------------------

* R-1  — single boot path; this service is constructed by the
  :class:`core.omnix_engine.OmnixEngine` like every other service.
* R-2  — service wrapper contract: returns
  :class:`BrowserResult` (a structured result dataclass with
  ``status``, ``observation``, ``error``).
* R-3  — result normalization through the closed ``status`` set.
* R-8  — observation ≠ verification: this service exposes
  ``observation`` but never claims ``verified``.
* R-10 — frozen dataclasses throughout.
* R-13 — closed set of action kinds; unknown kinds are rejected
  at the boundary.
* R-14 — service, not a singleton.
* R-21 — closed capability set is the only path to a real
  browser; this service is the path.
* R-22 — adaptive but deterministic routing; the service
  consults a vision fallback only after DOM resolution fails.
* R-24 — natural language is user-facing; this service accepts
  structured requests, not free text.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from loguru import logger

from browser.models.contracts import (
    BrowserAction,
    BrowserObservation,
    BrowserPageState,
    BrowserRequest,
    BrowserResult,
    BrowserResultStatus,
    BrowserSessionInfo,
    BrowserTarget,
    LocatorKind,
    TargetResolutionMethod,
)
from browser.router.dispatcher import BrowserRouter
from browser.safety.policy import (
    BrowserPolicyDecision,
    BrowserSafetyPolicy,
)
from browser.session.session import (
    BrowserSession,
    BrowserSessionError,
)
from browser.strategies.vision_fallback import (
    NullVisionFallback,
    VisionFallback,
)


# Default session id used when a request omits ``session_id``.
DEFAULT_SESSION_ID = "default"


class BrowserService:
    """The canonical V6 boundary to the browser subsystem.

    The service owns a small registry of sessions keyed by
    ``session_id``; the *only* public surface is a small set of
    structured operations and ``execute``.
    """

    def __init__(
        self,
        *,
        policy: Optional[BrowserSafetyPolicy] = None,
        vision_fallback: Optional[VisionFallback] = None,
        # Test/dev hook: provide a ``playwright_factory`` callable
        # that returns a Playwright BrowserContext-shaped object
        # to use instead of launching a real Playwright.
        # Production: leave ``playwright_factory=None``.
        playwright_factory: Optional[Any] = None,
        headless: bool = True,
        default_viewport: tuple = (1280, 720),
        engine: str = "chromium",
    ) -> None:
        self._policy = policy or BrowserSafetyPolicy()
        self._vision = vision_fallback or NullVisionFallback()
        self._playwright_factory = playwright_factory
        self._headless = headless
        self._default_viewport = tuple(default_viewport)
        self._engine = engine
        # Registry of open sessions.
        self._sessions: Dict[str, BrowserSession] = {}
        self._routers: Dict[str, BrowserRouter] = {}
        # Counter used to cap sessions (very loose; for tests).
        self._max_sessions = 16

    # ------------------------------------------------------- structured API
    def open(
        self,
        *,
        headless: Optional[bool] = None,
        viewport: Optional[tuple] = None,
        engine: Optional[str] = None,
        start_url: Optional[str] = None,
        session_id: str = DEFAULT_SESSION_ID,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        """Open a browser session and return a structured result."""

        params: Dict[str, Any] = {}
        if headless is not None:
            params["headless"] = bool(headless)
        if viewport is not None:
            params["viewport_width"] = int(viewport[0])
            params["viewport_height"] = int(viewport[1])
        if engine is not None:
            params["browser_engine"] = str(engine)
        if start_url is not None:
            params["start_url"] = str(start_url)

        request = BrowserRequest(
            action=BrowserAction.OPEN,
            session_id=session_id,
            parameters=params,
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        )
        return self.execute(request)

    def navigate(
        self,
        url: str,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        wait_until: str = "load",
        timeout_ms: int = 30_000,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.NAVIGATE,
            session_id=session_id,
            parameters={
                "url": str(url),
                "wait_until": str(wait_until),
                "timeout_ms": int(timeout_ms),
            },
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def back(
        self,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        timeout_ms: int = 30_000,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.BACK,
            session_id=session_id,
            parameters={"timeout_ms": int(timeout_ms)},
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def forward(
        self,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        timeout_ms: int = 30_000,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.FORWARD,
            session_id=session_id,
            parameters={"timeout_ms": int(timeout_ms)},
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def reload(
        self,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        timeout_ms: int = 30_000,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.RELOAD,
            session_id=session_id,
            parameters={"timeout_ms": int(timeout_ms)},
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def click(
        self,
        target: BrowserTarget,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
        force: bool = False,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.CLICK,
            session_id=session_id,
            target=target,
            parameters={
                "button": str(button),
                "click_count": int(click_count),
                "delay_ms": int(delay_ms),
                "force": bool(force),
            },
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def type_text(
        self,
        target: BrowserTarget,
        text: str,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        delay_ms: int = 0,
        submit: bool = False,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.TYPE,
            session_id=session_id,
            target=target,
            parameters={
                "text": str(text),
                "delay_ms": int(delay_ms),
                "submit": bool(submit),
            },
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def press(
        self,
        key: str,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.PRESS,
            session_id=session_id,
            parameters={"key": str(key)},
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def scroll(
        self,
        *,
        direction: str,
        amount: int,
        target: Optional[BrowserTarget] = None,
        session_id: str = DEFAULT_SESSION_ID,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.SCROLL,
            session_id=session_id,
            target=target,
            parameters={
                "direction": str(direction),
                "amount": int(amount),
            },
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def select(
        self,
        target: BrowserTarget,
        *,
        value: Optional[str] = None,
        label: Optional[str] = None,
        session_id: str = DEFAULT_SESSION_ID,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        params: Dict[str, Any] = {}
        if value is not None:
            params["value"] = str(value)
        if label is not None:
            params["label"] = str(label)
        return self.execute(BrowserRequest(
            action=BrowserAction.SELECT,
            session_id=session_id,
            target=target,
            parameters=params,
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def hover(
        self,
        target: BrowserTarget,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        timeout_ms: int = 30_000,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.HOVER,
            session_id=session_id,
            target=target,
            parameters={"timeout_ms": int(timeout_ms)},
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def wait(
        self,
        until: str,
        *,
        target: Optional[BrowserTarget] = None,
        session_id: str = DEFAULT_SESSION_ID,
        timeout_ms: int = 30_000,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.WAIT,
            session_id=session_id,
            target=target,
            parameters={"until": str(until), "timeout_ms": int(timeout_ms)},
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def extract_text(
        self,
        target: BrowserTarget,
        *,
        max_chars: int = 4000,
        include_attributes: bool = False,
        session_id: str = DEFAULT_SESSION_ID,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.EXTRACT_TEXT,
            session_id=session_id,
            target=target,
            parameters={
                "max_chars": int(max_chars),
                "include_attributes": bool(include_attributes),
            },
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def extract_page(
        self,
        *,
        max_chars: int = 8000,
        session_id: str = DEFAULT_SESSION_ID,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.EXTRACT_PAGE,
            session_id=session_id,
            parameters={"max_chars": int(max_chars)},
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def download(
        self,
        target: BrowserTarget,
        save_to: str,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.DOWNLOAD,
            session_id=session_id,
            target=target,
            parameters={"save_to": str(save_to)},
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    def close(
        self,
        *,
        session_id: str = DEFAULT_SESSION_ID,
        goal_id: str = "",
        plan_step_id: str = "",
    ) -> BrowserResult:
        return self.execute(BrowserRequest(
            action=BrowserAction.CLOSE,
            session_id=session_id,
            parameters={},
            goal_id=goal_id,
            plan_step_id=plan_step_id,
        ))

    # ------------------------------------------------------ canonical API
    def execute(self, request: BrowserRequest) -> BrowserResult:
        """Dispatch a :class:`BrowserRequest` to its handler.

        This is the canonical entry point.  The :class:`BrowserRouter`
        is invoked once per request; the service owns the session
        registry.
        """
        if not isinstance(request, BrowserRequest):
            raise TypeError(
                f"BrowserService.execute expected a BrowserRequest, "
                f"got {type(request).__name__}"
            )

        # 1. Policy check.
        session = self._sessions.get(request.session_id or DEFAULT_SESSION_ID)
        action_count = (
            session.state.action_count if session is not None else 0
        )
        decision: BrowserPolicyDecision = self._policy.check_request(
            request, session_action_count=action_count
        )
        if not decision.allowed:
            return self._policy_refusal(request, decision)

        # 2. OPEN is special — it creates the session.
        if request.action == BrowserAction.OPEN:
            if len(self._sessions) >= self._max_sessions:
                return self._result(
                    request,
                    status=BrowserResultStatus.ERROR,
                    error=(
                        f"too many open sessions "
                        f"(max={self._max_sessions})"
                    ),
                )
            session_id = request.session_id or DEFAULT_SESSION_ID
            if session_id in self._sessions:
                # Idempotent: re-open returns the existing session.
                return self._dispatch(request)
            new_session = self._create_session(request, session_id)
            self._sessions[session_id] = new_session
            self._routers[session_id] = BrowserRouter(
                new_session, vision_fallback=self._vision
            )
            return self._dispatch(request)

        # 3. CLOSE is special — it tears down the session.
        if request.action == BrowserAction.CLOSE:
            session_id = request.session_id or DEFAULT_SESSION_ID
            existing = self._sessions.pop(session_id, None)
            self._routers.pop(session_id, None)
            if existing is not None:
                try:
                    existing.close()
                except Exception:  # noqa: BLE001
                    pass
            return BrowserResult(
                status=BrowserResultStatus.OK,
                action=request.action,
                request=request,
                observation=BrowserObservation(
                    source="URL",
                    resolution_method=TargetResolutionMethod.SKIPPED,
                ),
                metadata={"closed": True, "session_id": session_id},
            )

        # 4. Other actions: the session must exist.
        if session is None:
            return self._result(
                request,
                status=BrowserResultStatus.SESSION_NOT_FOUND,
                error=(
                    f"session {request.session_id!r} is not open; "
                    f"call open() first"
                ),
            )

        return self._dispatch(request)

    # ----------------------------------------------------------- inspection
    def list_sessions(self) -> tuple:
        """Return a tuple of :class:`BrowserSessionInfo` (one per session)."""
        out = []
        for sid, sess in self._sessions.items():
            out.append(self._to_session_info(sess))
        return tuple(out)

    def get_session_info(
        self, session_id: str = DEFAULT_SESSION_ID
    ) -> Optional[BrowserSessionInfo]:
        sess = self._sessions.get(session_id)
        if sess is None:
            return None
        return self._to_session_info(sess)

    def is_healthy(self) -> bool:
        """The service is always available; readiness is per-session."""
        return True

    # ---------------------------------------------------- lifecycle surface
    # The :class:`core.service_registry.ServiceRegistry` requires every
    # registered service to expose ``initialize`` / ``shutdown`` /
    # ``statistics``.  ``BrowserService`` is a long-lived service
    # wrapper; ``initialize`` and ``shutdown`` are no-ops because
    # session creation is explicit (the caller invokes ``open()``).
    # ``statistics`` returns a small dict for the engine's health
    # monitor — never anything that includes cookies, passwords, or
    # full HTML.
    def initialize(self) -> bool:
        """Lifecycle hook — always succeeds.

        The service is ready to accept requests as soon as it is
        constructed; sessions are created on demand through
        :meth:`open`.
        """
        return True

    def shutdown(self) -> bool:
        """Lifecycle hook — close every open session cleanly.

        Returns True on clean shutdown, False if any session refused
        to close (the session is leaked but the engine still comes
        down).
        """
        leaked = 0
        for sid in list(self._sessions.keys()):
            sess = self._sessions.pop(sid, None)
            self._routers.pop(sid, None)
            if sess is None:
                continue
            try:
                if sess.is_open:
                    sess.close()
            except Exception:  # noqa: BLE001
                leaked += 1
        return leaked == 0

    def statistics(self) -> Dict[str, Any]:
        """A small, non-secret health snapshot for the engine."""
        open_sessions = sum(
            1 for s in self._sessions.values() if s.is_open
        )
        return {
            "service": "browser",
            "open_sessions": open_sessions,
            "registered_sessions": len(self._sessions),
            "vision_fallback_configured": (
                not isinstance(self._vision, NullVisionFallback)
            ),
            "engine": self._engine,
        }

    def describe(self) -> Dict[str, Any]:
        """Return a small, structured description of the service."""
        return {
            "service": "browser",
            "policy": {
                "host_allowlist": (
                    None
                    if self._policy.host_allowlist is None
                    else sorted(self._policy.host_allowlist)
                ),
                "allow_data_urls": self._policy.allow_data_urls,
                "allow_file_urls": self._policy.allow_file_urls,
                "max_actions_per_session": (
                    self._policy.max_actions_per_session
                ),
                "allow_executable_downloads": (
                    self._policy.allow_executable_downloads
                ),
            },
            "open_sessions": len(self._sessions),
            "default_viewport": list(self._default_viewport),
            "engine": self._engine,
            "vision_fallback": (
                "configured"
                if not isinstance(self._vision, NullVisionFallback)
                else "not configured"
            ),
        }

    # ----------------------------------------------------------- internals
    def _create_session(
        self, request: BrowserRequest, session_id: str
    ) -> BrowserSession:
        params = request.parameters
        headless = params.get("headless")
        if headless is None:
            headless = self._headless
        vw = params.get("viewport_width")
        vh = params.get("viewport_height")
        viewport = (
            (int(vw), int(vh))
            if vw is not None and vh is not None
            else self._default_viewport
        )
        engine = params.get("browser_engine", self._engine)
        return BrowserSession(
            session_id,
            headless=bool(headless),
            viewport=viewport,
            engine=str(engine),
            playwright_factory=self._playwright_factory,
        )

    def _dispatch(self, request: BrowserRequest) -> BrowserResult:
        router = self._routers.get(
            request.session_id or DEFAULT_SESSION_ID
        )
        if router is None:
            return self._result(
                request,
                status=BrowserResultStatus.SESSION_NOT_FOUND,
                error=(
                    f"no router for session "
                    f"{request.session_id!r}"
                ),
            )
        return router.dispatch(request)

    def _policy_refusal(
        self,
        request: BrowserRequest,
        decision: BrowserPolicyDecision,
    ) -> BrowserResult:
        return self._result(
            request,
            status=decision.status,
            error=decision.reason,
        )

    def _to_session_info(
        self, session: BrowserSession
    ) -> BrowserSessionInfo:
        s = session.state
        return BrowserSessionInfo(
            session_id=s.session_id,
            is_open=s.is_open,
            current_url=s.current_url,
            current_title=s.current_title,
            viewport=s.viewport,
            headless=s.headless,
            opened_at=s.opened_at,
            last_action_at=s.last_action_at,
            action_count=s.action_count,
        )

    def _result(
        self,
        request: BrowserRequest,
        *,
        status: BrowserResultStatus,
        error: Optional[str] = None,
    ) -> BrowserResult:
        started = time.time()
        return BrowserResult(
            status=status,
            action=request.action,
            request=request,
            error=error,
            started_at=started,
            finished_at=time.time(),
        )


__all__ = ["BrowserService", "DEFAULT_SESSION_ID"]
