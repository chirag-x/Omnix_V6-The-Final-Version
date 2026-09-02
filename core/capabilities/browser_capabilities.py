"""
Browser Capabilities (Phase 8).

These capabilities wrap the canonical
:class:`core.services.browser_service.BrowserService` actions and expose
them through the closed capability set (R-21).  The Brain / Agent can
only invoke a browser action by routing through one of these specs;
there is no direct path to Playwright.

The capability layer:

* is *thin*: it constructs a :class:`BrowserRequest` and forwards it
  to :class:`BrowserService`; it does no targeting, no policy, no
  Playwright calls.
* is *closed*: the parameter set is a fixed tuple on the spec; the
  router validates every parameter against the spec.
* is *observation-only*: the returned :class:`CapabilityResult` carries
  a :class:`BrowserObservation` (R-8) and never claims ``verified``.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Mapping, Optional

from core.capability import CapabilityParameter, CapabilitySpec, ParamType
from core.errors import OmnixError
from core.results import (
    CapabilityResult,
    CapabilityStatus,
    VerificationResult,
    VerificationStatus,
)
from core.services.browser_service import BrowserService
from browser.models.contracts import (
    BrowserRequest,
    BrowserTarget,
    LocatorKind,
)

from .base import BaseCapability


# ---------------------------------------------------------------------------
# Locator-kind enum values (closed set; mirrors LocatorKind)
# ---------------------------------------------------------------------------
_LOCATOR_VALUES: tuple = tuple(k.value for k in LocatorKind)


# ---------------------------------------------------------------------------
# Sync Playwright ↔ asyncio boundary
# ---------------------------------------------------------------------------
# :class:`BrowserService` is a synchronous facade over the Playwright
# *Sync* API.  When a brain / agent / test awaits an async capability
# (``browser.open`` etc.) the capability router runs that coroutine
# inside a live asyncio event loop.  Playwright's sync implementation
# detects the running loop and refuses to start::
#
#     It looks like you are using Playwright Sync API inside the
#     asyncio loop. Please use the Async API instead.
#
# It also ties ``sync_playwright().start()`` to the creating thread —
# page/context objects cannot be used from any other thread.  We satisfy
# both constraints by routing every browser-service call through a
# single **dedicated worker thread** owned by the :class:`BrowserService`
# instance.  That thread has no asyncio loop, and every browser action
# executes on the same thread, so Playwright is happy.
_WORKERS: "dict[int, _BrowserWorker]" = {}
_WORKERS_LOCK = threading.Lock()


class _BrowserWorker:
    """A long-lived thread that owns a single browser session's sync
    Playwright calls.

    Lives for the lifetime of the process; lazily created on first use.
    All calls submitted to this worker execute on the SAME thread,
    which is what the Playwright Sync API requires.
    """

    _SHUTDOWN = object()  # sentinel

    def __init__(self) -> None:
        self._in_q: "queue.Queue[tuple]" = queue.Queue()
        self._started = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def _ensure_started(self) -> None:
        with self._lock:
            if self._started:
                return
            t = threading.Thread(
                target=self._serve,
                args=(self._in_q,),
                name="browser-sync",
                daemon=True,
            )
            self._thread = t
            t.start()
            self._started = True

    @staticmethod
    def _serve(in_q: "queue.Queue[tuple]") -> None:
        while True:
            item = in_q.get()
            if item is _BrowserWorker._SHUTDOWN:
                return
            fn, args, kwargs, out_q = item
            try:
                out_q.put(("ok", fn(*args, **kwargs)))
            except BaseException as exc:  # noqa: BLE001
                out_q.put(("err", exc))

    def call(self, fn, /, *args: Any, **kwargs: Any) -> Any:
        self._ensure_started()
        out_q: "queue.Queue[tuple]" = queue.Queue(maxsize=1)
        self._in_q.put((fn, args, kwargs, out_q))
        kind, value = out_q.get()
        if kind == "err":
            raise value
        return value


def _get_worker_for(service: BrowserService) -> _BrowserWorker:
    """Return the long-lived worker thread for a given
    :class:`BrowserService` instance, creating it on first use."""
    key = id(service)
    with _WORKERS_LOCK:
        w = _WORKERS.get(key)
        if w is None:
            w = _BrowserWorker()
            _WORKERS[key] = w
        return w


def _run_sync_off_loop(
    service: BrowserService, fn, /, *args: Any, **kwargs: Any
) -> Any:
    """Call ``fn(*args, **kwargs)`` on the long-lived worker thread
    for the given :class:`BrowserService`.

    All browser calls go through the SAME thread, so the Playwright
    Sync API remains happy: it binds the page/context to the creating
    thread, and we never create them anywhere else.
    """
    return _get_worker_for(service).call(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _observation_to_details(res: Any) -> dict:
    """Convert a BrowserResult's observation to a JSON-safe details dict."""
    if res is None or getattr(res, "observation", None) is None:
        return {}
    obs = res.observation
    return obs.to_dict() if hasattr(obs, "to_dict") else dict(obs)


def _to_capability_result(
    cap_name: str, res: Any
) -> CapabilityResult:
    """Map a :class:`BrowserResult` to a :class:`CapabilityResult`.

    The mapping is R-8 honest: success is ``EXECUTED`` (the action ran),
    never ``VERIFIED`` (only the Brain / Verifier may assert that the
    post-state matches the expected effect).
    """
    if res is None:
        return CapabilityResult(
            capability_name=cap_name,
            status=CapabilityStatus.FAILED,
            attempted=True,
            failed=True,
            error=OmnixError("browser service returned no result"),
        )

    if res.ok:
        return CapabilityResult(
            capability_name=cap_name,
            status=CapabilityStatus.EXECUTED,
            attempted=True,
            executed=True,
            verified=False,
            details=_observation_to_details(res),
        )

    # Failure: surface a structured verification with status=FAILED.
    return CapabilityResult(
        capability_name=cap_name,
        status=CapabilityStatus.FAILED,
        attempted=True,
        failed=True,
        verification=VerificationResult(
            status=VerificationStatus.FAILED,
            check_name=f"{cap_name}.postcheck",
            expected=None,
            actual=None,
            details={
                "browser_status": (
                    res.status.value
                    if hasattr(res.status, "value")
                    else str(res.status)
                ),
            },
            error=(
                OmnixError(str(res.error) or "browser action failed")
                if res.error
                else None
            ),
        ),
        error=OmnixError(str(res.error) or "browser action failed"),
        details={
            "browser_status": (
                res.status.value
                if hasattr(res.status, "value")
                else str(res.status)
            ),
            **_observation_to_details(res),
        },
    )


def _build_target(params: Mapping[str, Any]) -> BrowserTarget:
    """Construct a :class:`BrowserTarget` from validated parameters."""
    kind_str = params.get("locator_kind")
    value = params.get("locator_value")
    if not kind_str or value is None:
        # Coercion / defaulting would have already failed at spec
        # validation; this is a defensive guard.
        raise OmnixError(
            "locator_kind and locator_value are required",
            code="BROWSER_TARGET_MISSING",
        )
    return BrowserTarget(
        kind=LocatorKind(kind_str),
        value=str(value),
    )


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class _BrowserCapabilityBase(BaseCapability):
    """Shared base — holds the injected :class:`BrowserService`."""

    def __init__(self, browser_service: BrowserService) -> None:
        if browser_service is None:
            raise ValueError(
                "_BrowserCapabilityBase requires a BrowserService instance"
            )
        self._browser = browser_service
        self._service = browser_service

    def _run_off_loop(self, fn, /, *args: Any, **kwargs: Any) -> Any:
        """Call a *synchronous* browser-service method safely from any context.

        When the capability is awaited from inside a running asyncio loop
        (the normal case for the brain/agent), the underlying Playwright
        sync API refuses to start.  This helper runs the call on the
        service's long-lived worker thread, which has no asyncio loop
        and so satisfies Playwright's contract.
        """
        return _run_sync_off_loop(self._service, fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Concrete capabilities
# ---------------------------------------------------------------------------

class BrowserNavigateCapability(_BrowserCapabilityBase):
    """Navigate the browser to a URL (creates the default session if
    none exists — the canonical browser boot path)."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.navigate",
            version="1.0.0",
            description=(
                "Navigate the browser to a URL.  If no session is open, "
                "one is created on the default session id."
            ),
            parameters=(
                CapabilityParameter(
                    name="url",
                    type=ParamType.STRING,
                    required=True,
                    description="Absolute URL (http/https only).",
                ),
                CapabilityParameter(
                    name="wait_until",
                    type=ParamType.ENUM,
                    required=False,
                    default="load",
                    description="When to consider navigation complete.",
                    allowed_values=("load", "domcontentloaded", "networkidle"),
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "navigate"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        url = params.get("url")
        if not url:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("url is required"),
            )
        try:
            # Open the session if needed, then navigate.  The navigate
            # call itself raises if the URL is invalid; the service
            # handles session creation through the OPEN action.
            existing = self._run_off_loop(self._browser.get_session_info)
            if existing is None or not existing.is_open:
                self._run_off_loop(self._browser.open)
            res = self._run_off_loop(
                self._browser.navigate,
                url=str(url),
                wait_until=str(params.get("wait_until", "load")),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.navigate failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserClickCapability(_BrowserCapabilityBase):
    """Click a targeted element in the current browser session."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.click",
            version="1.0.0",
            description=(
                "Click the element matching a closed-set locator."
            ),
            parameters=(
                CapabilityParameter(
                    name="locator_kind",
                    type=ParamType.ENUM,
                    required=True,
                    description="Locator kind (closed set).",
                    allowed_values=_LOCATOR_VALUES,
                ),
                CapabilityParameter(
                    name="locator_value",
                    type=ParamType.STRING,
                    required=True,
                    description="Locator value (selector, xpath, text, ...).",
                ),
                CapabilityParameter(
                    name="button",
                    type=ParamType.ENUM,
                    required=False,
                    default="left",
                    description="Mouse button.",
                    allowed_values=("left", "right", "middle"),
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "click"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            target = _build_target(params)
            res = self._run_off_loop(
                self._browser.click,
                target=target,
                button=str(params.get("button", "left")),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.click failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserTypeCapability(_BrowserCapabilityBase):
    """Type text into a targeted input element."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.type",
            version="1.0.0",
            description=(
                "Type text into the element matching a closed-set locator."
            ),
            parameters=(
                CapabilityParameter(
                    name="locator_kind",
                    type=ParamType.ENUM,
                    required=True,
                    allowed_values=_LOCATOR_VALUES,
                ),
                CapabilityParameter(
                    name="locator_value",
                    type=ParamType.STRING,
                    required=True,
                ),
                CapabilityParameter(
                    name="text",
                    type=ParamType.STRING,
                    required=True,
                    description="The text to type into the element.",
                ),
                CapabilityParameter(
                    name="submit",
                    type=ParamType.BOOLEAN,
                    required=False,
                    default=False,
                    description="Press Enter after typing.",
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "type"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        text = params.get("text")
        if text is None:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("text is required"),
            )
        try:
            target = _build_target(params)
            res = self._run_off_loop(
                self._browser.type_text,
                target=target,
                text=str(text),
                submit=bool(params.get("submit", False)),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.type failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserExtractTextCapability(_BrowserCapabilityBase):
    """Read the visible text of a targeted element."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.extract_text",
            version="1.0.0",
            description=(
                "Return the visible text of the element matching a "
                "closed-set locator.  Bounded to ``max_chars``."
            ),
            parameters=(
                CapabilityParameter(
                    name="locator_kind",
                    type=ParamType.ENUM,
                    required=True,
                    allowed_values=_LOCATOR_VALUES,
                ),
                CapabilityParameter(
                    name="locator_value",
                    type=ParamType.STRING,
                    required=True,
                ),
                CapabilityParameter(
                    name="max_chars",
                    type=ParamType.INTEGER,
                    required=False,
                    default=4000,
                    min_value=1,
                    max_value=100_000,
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "extract"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            target = _build_target(params)
            res = self._run_off_loop(
                self._browser.extract_text,
                target=target,
                max_chars=int(params.get("max_chars", 4000)),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.extract_text failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


# ---------------------------------------------------------------------------
# Additional browser capabilities (Phase 8)
# ---------------------------------------------------------------------------

class BrowserOpenCapability(_BrowserCapabilityBase):
    """Open a browser session (canonical boot path)."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.open",
            version="1.0.0",
            description=(
                "Open a browser session on the default session id. "
                "Idempotent: opening an already-open session is a no-op."
            ),
            parameters=(
                CapabilityParameter(
                    name="session_id",
                    type=ParamType.STRING,
                    required=False,
                    default="default",
                    description="Session id (default 'default').",
                ),
                CapabilityParameter(
                    name="headless",
                    type=ParamType.BOOLEAN,
                    required=False,
                    default=True,
                    description="Run the browser in headless mode.",
                ),
                CapabilityParameter(
                    name="start_url",
                    type=ParamType.STRING,
                    required=False,
                    default=None,
                    description="Optional initial URL to navigate to.",
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "open"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            res = self._run_off_loop(
                self._browser.open,
                headless=params.get("headless"),
                start_url=params.get("start_url"),
                session_id=str(params.get("session_id", "default")),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.open failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserCloseCapability(_BrowserCapabilityBase):
    """Close the default browser session (canonical teardown)."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.close",
            version="1.0.0",
            description=(
                "Close the default browser session. Idempotent: closing "
                "an already-closed session is a no-op."
            ),
            parameters=(
                CapabilityParameter(
                    name="session_id",
                    type=ParamType.STRING,
                    required=False,
                    default="default",
                    description="Session id (default 'default').",
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "close"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            res = self._run_off_loop(
                self._browser.close,
                session_id=str(params.get("session_id", "default")),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.close failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserBackCapability(_BrowserCapabilityBase):
    """Navigate back in the default browser session."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.back",
            version="1.0.0",
            description="Go back one page in the session's history.",
            parameters=(
                CapabilityParameter(
                    name="session_id",
                    type=ParamType.STRING,
                    required=False,
                    default="default",
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "navigate"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            res = self._run_off_loop(
                self._browser.back,
                session_id=str(params.get("session_id", "default")),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.back failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserForwardCapability(_BrowserCapabilityBase):
    """Navigate forward in the default browser session."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.forward",
            version="1.0.0",
            description="Go forward one page in the session's history.",
            parameters=(
                CapabilityParameter(
                    name="session_id",
                    type=ParamType.STRING,
                    required=False,
                    default="default",
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "navigate"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            res = self._run_off_loop(
                self._browser.forward,
                session_id=str(params.get("session_id", "default")),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.forward failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserReloadCapability(_BrowserCapabilityBase):
    """Reload the current page in the default browser session."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.reload",
            version="1.0.0",
            description="Reload the current page.",
            parameters=(
                CapabilityParameter(
                    name="session_id",
                    type=ParamType.STRING,
                    required=False,
                    default="default",
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "navigate"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            res = self._run_off_loop(
                self._browser.reload,
                session_id=str(params.get("session_id", "default")),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.reload failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserPressCapability(_BrowserCapabilityBase):
    """Press a single key on the current page (no target needed)."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.press",
            version="1.0.0",
            description=(
                "Press a single key (e.g. 'Enter', 'Escape', 'Tab') on the "
                "current page. Closed key set; no JavaScript / shell injection."
            ),
            parameters=(
                CapabilityParameter(
                    name="key",
                    type=ParamType.STRING,
                    required=True,
                    description="Key name (Playwright chord syntax).",
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "press"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        key = params.get("key")
        if not isinstance(key, str) or not key.strip():
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("key is required"),
            )
        try:
            res = self._run_off_loop(self._browser.press, key=str(key))
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.press failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserScrollCapability(_BrowserCapabilityBase):
    """Scroll the current page in a given direction."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.scroll",
            version="1.0.0",
            description=(
                "Scroll the current page (or a target element into view "
                "then scroll) by a bounded amount in a direction."
            ),
            parameters=(
                CapabilityParameter(
                    name="direction",
                    type=ParamType.ENUM,
                    required=True,
                    allowed_values=("up", "down", "left", "right"),
                ),
                CapabilityParameter(
                    name="amount",
                    type=ParamType.INTEGER,
                    required=True,
                    min_value=1,
                    max_value=10_000,
                    description="Scroll delta in pixels.",
                ),
                CapabilityParameter(
                    name="locator_kind",
                    type=ParamType.ENUM,
                    required=False,
                    allowed_values=_LOCATOR_VALUES,
                    description="Optional target to scroll into view first.",
                ),
                CapabilityParameter(
                    name="locator_value",
                    type=ParamType.STRING,
                    required=False,
                    description="Locator value (required if locator_kind set).",
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "scroll"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        direction = params.get("direction")
        amount = params.get("amount")
        if not isinstance(direction, str) or direction not in (
            "up", "down", "left", "right",
        ):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("direction must be up/down/left/right"),
            )
        if not isinstance(amount, int) or amount <= 0:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("amount must be a positive int"),
            )
        target = None
        kind = params.get("locator_kind")
        value = params.get("locator_value")
        if (kind is None) != (value is None):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(
                    "locator_kind and locator_value must be set together"
                ),
            )
        if kind and value is not None:
            target = BrowserTarget(kind=LocatorKind(kind), value=str(value))
        try:
            res = self._run_off_loop(
                self._browser.scroll,
                direction=str(direction),
                amount=int(amount),
                target=target,
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.scroll failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserHoverCapability(_BrowserCapabilityBase):
    """Hover a targeted element in the current browser session."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.hover",
            version="1.0.0",
            description="Hover the element matching a closed-set locator.",
            parameters=(
                CapabilityParameter(
                    name="locator_kind",
                    type=ParamType.ENUM,
                    required=True,
                    allowed_values=_LOCATOR_VALUES,
                ),
                CapabilityParameter(
                    name="locator_value",
                    type=ParamType.STRING,
                    required=True,
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "hover"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            target = _build_target(params)
            res = self._run_off_loop(self._browser.hover, target=target)
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.hover failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserSelectCapability(_BrowserCapabilityBase):
    """Select an option in a <select> element."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.select",
            version="1.0.0",
            description=(
                "Select an option in a <select> element by value or label."
            ),
            parameters=(
                CapabilityParameter(
                    name="locator_kind",
                    type=ParamType.ENUM,
                    required=True,
                    allowed_values=_LOCATOR_VALUES,
                ),
                CapabilityParameter(
                    name="locator_value",
                    type=ParamType.STRING,
                    required=True,
                ),
                CapabilityParameter(
                    name="value",
                    type=ParamType.STRING,
                    required=False,
                    default=None,
                    description="Option's value attribute.",
                ),
                CapabilityParameter(
                    name="label",
                    type=ParamType.STRING,
                    required=False,
                    default=None,
                    description="Option's visible label.",
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "select"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        value = params.get("value")
        label = params.get("label")
        if (value is None) == (label is None):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(
                    "exactly one of 'value' or 'label' must be set"
                ),
            )
        try:
            target = _build_target(params)
            res = self._run_off_loop(
                self._browser.select,
                target=target, value=value, label=label,
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.select failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserWaitCapability(_BrowserCapabilityBase):
    """Wait for a target state, a page state, or a network idle."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.wait",
            version="1.0.0",
            description=(
                "Wait for a target state, a page state, or a network-idle. "
                "Does not block indefinitely; bounded by ``timeout_ms``."
            ),
            parameters=(
                CapabilityParameter(
                    name="until",
                    type=ParamType.ENUM,
                    required=True,
                    allowed_values=(
                        "visible", "hidden", "attached", "networkidle",
                    ),
                ),
                CapabilityParameter(
                    name="timeout_ms",
                    type=ParamType.INTEGER,
                    required=False,
                    default=30_000,
                    min_value=1,
                    max_value=300_000,
                ),
                CapabilityParameter(
                    name="locator_kind",
                    type=ParamType.ENUM,
                    required=False,
                    allowed_values=_LOCATOR_VALUES,
                ),
                CapabilityParameter(
                    name="locator_value",
                    type=ParamType.STRING,
                    required=False,
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "wait"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        until = params.get("until")
        if until not in ("visible", "hidden", "attached", "networkidle"):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(
                    "until must be visible/hidden/attached/networkidle"
                ),
            )
        target = None
        kind = params.get("locator_kind")
        value = params.get("locator_value")
        if (kind is None) != (value is None):
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError(
                    "locator_kind and locator_value must be set together"
                ),
            )
        if kind and value is not None:
            target = BrowserTarget(kind=LocatorKind(kind), value=str(value))
        try:
            res = self._run_off_loop(
                self._browser.wait,
                until=str(until),
                target=target,
                timeout_ms=int(params.get("timeout_ms", 30_000)),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.wait failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserExtractPageCapability(_BrowserCapabilityBase):
    """Read a bounded snapshot of the current page."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.extract_page",
            version="1.0.0",
            description=(
                "Return a bounded snapshot of the current page: URL, title, "
                "visible text (capped at ``max_chars``), and a small set of "
                "representative element refs. Never full HTML / cookies."
            ),
            parameters=(
                CapabilityParameter(
                    name="max_chars",
                    type=ParamType.INTEGER,
                    required=False,
                    default=8000,
                    min_value=1,
                    max_value=100_000,
                ),
            ),
            requires_services=("browser_service",),
            tags=("browser", "extract"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        try:
            res = self._run_off_loop(
                self._browser.extract_page,
                max_chars=int(params.get("max_chars", 8000)),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.extract_page failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


class BrowserDownloadCapability(_BrowserCapabilityBase):
    """Download a file via clicking a targeted element."""

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            name="browser.download",
            version="1.0.0",
            description=(
                "Click a target to trigger a download and save the file "
                "to a path. The safety policy refuses executable extensions."
            ),
            parameters=(
                CapabilityParameter(
                    name="locator_kind",
                    type=ParamType.ENUM,
                    required=True,
                    allowed_values=_LOCATOR_VALUES,
                ),
                CapabilityParameter(
                    name="locator_value",
                    type=ParamType.STRING,
                    required=True,
                ),
                CapabilityParameter(
                    name="save_to",
                    type=ParamType.PATH,
                    required=True,
                    description="Absolute filesystem path to save the file.",
                ),
            ),
            requires_services=("browser_service",),
            dangerous=True,
            tags=("browser", "download"),
        )

    async def execute(self, params: Mapping[str, Any]) -> CapabilityResult:
        save_to = params.get("save_to")
        if not save_to:
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                failed=True,
                error=OmnixError("save_to is required"),
            )
        try:
            target = _build_target(params)
            res = self._run_off_loop(
                self._browser.download,
                target=target, save_to=str(save_to),
            )
        except Exception as exc:  # noqa: BLE001
            return CapabilityResult(
                capability_name=self.spec.name,
                status=CapabilityStatus.FAILED,
                attempted=True,
                failed=True,
                error=OmnixError(f"browser.download failed: {exc}"),
            )
        return _to_capability_result(self.spec.name, res)


__all__ = [
    "BrowserNavigateCapability",
    "BrowserClickCapability",
    "BrowserTypeCapability",
    "BrowserExtractTextCapability",
    "BrowserOpenCapability",
    "BrowserCloseCapability",
    "BrowserBackCapability",
    "BrowserForwardCapability",
    "BrowserReloadCapability",
    "BrowserPressCapability",
    "BrowserScrollCapability",
    "BrowserHoverCapability",
    "BrowserSelectCapability",
    "BrowserWaitCapability",
    "BrowserExtractPageCapability",
    "BrowserDownloadCapability",
]
