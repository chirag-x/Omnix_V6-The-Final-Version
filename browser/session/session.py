"""
Browser session / context / page lifecycle (Phase 8).

A thin wrapper around Playwright.  Only a small surface is exposed
to the rest of the system:

    * open/close
    * navigate, back, forward, reload
    * resolve a :class:`BrowserTarget` to a live Playwright locator
    * click/type/press/hover/scroll/select on a resolved locator
    * wait for a state
    * extract text, extract page state
    * download a file to a path

Safety
------

* No ``page.evaluate(...)`` is exposed.  The service never lets
  the caller supply raw JavaScript.  All Playwright JavaScript
  evaluation is server-internal and uses fixed, audited snippets.
* No ``os.system`` / ``subprocess`` / ``popen`` is used.  Playwright
  is launched through its official Python API.
* Page text and HTML are bounded; cookies are never exposed.
* Screenshots are acquired *only* on demand (e.g. for the vision
  fallback) and never persist outside the service.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger

from browser.models.contracts import (
    BrowserAction,
    BrowserElement,
    BrowserPageState,
    BrowserTarget,
    LocatorKind,
    TargetResolutionMethod,
)


# Maximum visible text we ever extract from a page (bytes).
_MAX_VISIBLE_TEXT = 8_000
# Maximum page HTML we ever hold in state.
_MAX_DOM_SOURCE = 16_000


class BrowserSessionError(Exception):
    """Raised by :class:`BrowserSession` on infrastructure-level errors.

    The :class:`BrowserService` translates these into structured
    :class:`BrowserResult` values; the rest of V6 never catches
    them.
    """


@dataclass
class _ResolvedElement:
    """An internal handle to a resolved element + how it was resolved."""

    locator: Any
    method: TargetResolutionMethod
    selector: str
    role: str = ""
    name: str = ""
    text: str = ""

    def _build_element_snapshot(self) -> BrowserElement:
        """Build a :class:`BrowserElement` from this resolved handle.

        Used by the router so the :class:`BrowserObservation` returned
        to the Brain can carry element-level info.
        """
        tag = ""
        if self.locator is not None:
            tag = safe_tag(self.locator)
        return BrowserElement(
            tag=tag,
            text=self.text,
            role=self.role,
            name=self.name,
            selector=self.selector,
            visible=True,
        )


@dataclass
class BrowserSessionState:
    """The minimum non-secret state the service exposes for one session."""

    session_id: str
    is_open: bool = False
    current_url: str = ""
    current_title: str = ""
    viewport: Tuple[int, int] = (0, 0)
    headless: bool = True
    opened_at: float = 0.0
    last_action_at: float = 0.0
    action_count: int = 0


class BrowserSession:
    """One browser session, wrapping Playwright.

    Not a singleton (R-14).  Construct one per session id; the
    :class:`BrowserService` keeps a registry of them.
    """

    def __init__(
        self,
        session_id: str,
        *,
        headless: bool = True,
        viewport: Tuple[int, int] = (1280, 720),
        engine: str = "chromium",
        # Optional hooks for tests — when ``playwright_factory`` is
        # provided, the session does NOT launch a real Playwright
        # process; it uses the factory to build a fake page.
        playwright_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._session_id = str(session_id)
        self._headless = bool(headless)
        self._viewport = tuple(viewport)
        self._engine = str(engine or "chromium").lower()
        self._playwright_factory = playwright_factory

        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._state = BrowserSessionState(
            session_id=session_id,
            is_open=False,
            viewport=self._viewport,
            headless=headless,
        )
        self._action_count = 0

    # ----------------------------------------------------------- properties
    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_open(self) -> bool:
        return self._state.is_open

    @property
    def state(self) -> BrowserSessionState:
        # Update volatile fields before returning.
        return replace(
            self._state,
            action_count=self._action_count,
            is_open=self._state.is_open and self._page is not None,
        )

    # ----------------------------------------------------------------- open
    def open(
        self,
        *,
        start_url: Optional[str] = None,
    ) -> None:
        """Open the session.  Idempotent: opening an open session is a no-op."""

        if self._state.is_open:
            return
        try:
            if self._playwright_factory is not None:
                # Test mode: a fake Playwright is provided.
                self._browser = self._playwright_factory()
                self._context = self._browser.new_context()
                self._page = self._context.new_page()
            else:
                # Real Playwright path.  The import is local so the
                # rest of V6 does not require Playwright at import
                # time (only the service boundary does).
                from playwright.sync_api import sync_playwright

                self._pw = sync_playwright().start()
                launcher = getattr(self._pw, self._engine, self._pw.chromium)
                self._browser = launcher.launch(headless=self._headless)
                self._context = self._browser.new_context(
                    viewport={"width": int(self._viewport[0]),
                              "height": int(self._viewport[1])}
                )
                self._page = self._context.new_page()
        except Exception as exc:  # noqa: BLE001
            self._cleanup()
            raise BrowserSessionError(
                f"failed to open browser session: {exc}"
            ) from exc

        self._state.is_open = True
        self._state.opened_at = time.time()
        self._state.last_action_at = self._state.opened_at
        logger.info(
            f"[browser] opened session={self._session_id} engine={self._engine}"
        )

        if start_url:
            try:
                self._goto(start_url)
            except Exception as exc:  # noqa: BLE001
                # We allow opening to succeed even if the start URL
                # fails — the service can still record the failure
                # in the result and recover.
                logger.warning(
                    f"[browser] start_url navigation failed: {exc!r}"
                )

    # ---------------------------------------------------------------- close
    def close(self) -> None:
        if not self._state.is_open:
            return
        self._cleanup()
        self._state.is_open = False
        logger.info(f"[browser] closed session={self._session_id}")

    # ----------------------------------------------------------------- URL
    def navigate(
        self,
        url: str,
        *,
        wait_until: str = "load",
        timeout_ms: int = 30_000,
    ) -> BrowserPageState:
        self._require_open()
        self._goto(url, wait_until=wait_until, timeout_ms=timeout_ms)
        self._bump_action()
        return self._snapshot()

    def back(self, *, timeout_ms: int = 30_000) -> BrowserPageState:
        self._require_open()
        try:
            self._page.go_back(timeout=timeout_ms, wait_until="load")
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"back: {exc}") from exc
        self._bump_action()
        return self._snapshot()

    def forward(self, *, timeout_ms: int = 30_000) -> BrowserPageState:
        self._require_open()
        try:
            self._page.go_forward(timeout=timeout_ms, wait_until="load")
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"forward: {exc}") from exc
        self._bump_action()
        return self._snapshot()

    def reload(self, *, timeout_ms: int = 30_000) -> BrowserPageState:
        self._require_open()
        try:
            self._page.reload(timeout=timeout_ms, wait_until="load")
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"reload: {exc}") from exc
        self._bump_action()
        return self._snapshot()

    # ----------------------------------------------------------- resolution
    def resolve(self, target: BrowserTarget) -> _ResolvedElement:
        """Resolve a :class:`BrowserTarget` to a live element.

        Resolution order:

            ACCESSIBILITY  →  CSS  →  TEST_ID  →  TEXT  →  XPATH

        Vision is *not* a LocatorKind — it is a separate fallback the
        service invokes *after* resolution has failed.
        """
        self._require_open()
        order = self._resolution_order(target.kind)
        last_exc: Optional[Exception] = None
        for kind in order:
            try:
                if kind == LocatorKind.CSS:
                    loc = self._page.locator(target.value)
                    if loc.count() == 0:
                        raise BrowserSessionError(
                            f"no element matches CSS selector {target.value!r}"
                        )
                    if target.nth is not None and target.nth < loc.count():
                        loc = loc.nth(target.nth)
                    text = self._safe_text(loc)
                    return _ResolvedElement(
                        locator=loc, method=TargetResolutionMethod.DOM,
                        selector=target.value, text=text,
                    )
                if kind == LocatorKind.TEST_ID:
                    selector = f'[data-testid="{_css_escape(target.value)}"]'
                    loc = self._page.locator(selector)
                    if loc.count() == 0:
                        raise BrowserSessionError(
                            f"no element has data-testid={target.value!r}"
                        )
                    if target.nth is not None and target.nth < loc.count():
                        loc = loc.nth(target.nth)
                    text = self._safe_text(loc)
                    return _ResolvedElement(
                        locator=loc, method=TargetResolutionMethod.DOM,
                        selector=selector, text=text,
                    )
                if kind == LocatorKind.TEXT:
                    text_value = target.value
                    if target.strict:
                        loc = self._page.get_by_text(
                            text_value, exact=True
                        )
                    else:
                        loc = self._page.get_by_text(text_value).first
                    if loc.count() == 0:
                        raise BrowserSessionError(
                            f"no element has visible text {text_value!r}"
                        )
                    sel = (
                        f"text={text_value!r}"
                        + (" (exact)" if target.strict else "")
                    )
                    return _ResolvedElement(
                        locator=loc, method=TargetResolutionMethod.DOM,
                        selector=sel, text=text_value,
                    )
                if kind == LocatorKind.ACCESSIBILITY:
                    spec = _parse_accessibility(target.value)
                    if spec is None:
                        # If the value isn't a valid accessibility
                        # spec, fall through to the next kind.
                        last_exc = BrowserSessionError(
                            f"invalid accessibility spec: {target.value!r}"
                        )
                        continue
                    role, name = spec
                    if not name:
                        loc = self._page.get_by_role(role)
                    else:
                        loc = self._page.get_by_role(role, name=name)
                    if loc.count() == 0:
                        raise BrowserSessionError(
                            f"no element has role={role!r} name={name!r}"
                        )
                    if target.nth is not None and target.nth < loc.count():
                        loc = loc.nth(target.nth)
                    sel = f'role={role} name={name!r}'
                    return _ResolvedElement(
                        locator=loc, method=TargetResolutionMethod.ACCESSIBILITY,
                        selector=sel, role=role, name=name,
                    )
                if kind == LocatorKind.XPATH:
                    loc = self._page.locator(
                        f"xpath={target.value}"
                    )
                    if loc.count() == 0:
                        raise BrowserSessionError(
                            f"no element matches xpath {target.value!r}"
                        )
                    if target.nth is not None and target.nth < loc.count():
                        loc = loc.nth(target.nth)
                    text = self._safe_text(loc)
                    return _ResolvedElement(
                        locator=loc, method=TargetResolutionMethod.DOM,
                        selector=f"xpath={target.value}", text=text,
                    )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
        # No strategy matched.
        raise BrowserSessionError(
            f"target not found: {target.to_dict()} "
            f"(last error: {last_exc})"
        )

    # -------------------------------------------------------------- actions
    def click(
        self,
        target: BrowserTarget,
        *,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int = 0,
        force: bool = False,
        timeout_ms: int = 30_000,
    ) -> _ResolvedElement:
        self._require_open()
        resolved = self.resolve(target)
        try:
            resolved.locator.click(
                button=button,
                click_count=click_count,
                delay=delay_ms / 1000.0 if delay_ms else None,
                force=force,
                timeout=timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"click: {exc}") from exc
        self._bump_action()
        return resolved

    def hover(
        self,
        target: BrowserTarget,
        *,
        timeout_ms: int = 30_000,
    ) -> _ResolvedElement:
        self._require_open()
        resolved = self.resolve(target)
        try:
            resolved.locator.hover(timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"hover: {exc}") from exc
        self._bump_action()
        return resolved

    def type_text(
        self,
        target: BrowserTarget,
        text: str,
        *,
        delay_ms: int = 0,
        submit: bool = False,
        timeout_ms: int = 30_000,
    ) -> _ResolvedElement:
        self._require_open()
        if not isinstance(text, str):
            raise BrowserSessionError("type: 'text' must be a string")
        resolved = self.resolve(target)
        try:
            resolved.locator.click(timeout=timeout_ms)
            if delay_ms:
                resolved.locator.type(
                    text, delay=delay_ms / 1000.0
                )
            else:
                resolved.locator.fill(text)
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"type: {exc}") from exc
        if submit:
            try:
                self._page.keyboard.press("Enter")
            except Exception as exc:  # noqa: BLE001
                raise BrowserSessionError(f"type:submit: {exc}") from exc
        self._bump_action()
        return resolved

    def press(self, key: str) -> None:
        self._require_open()
        if not isinstance(key, str) or not key.strip():
            raise BrowserSessionError("press: 'key' must be a non-empty string")
        # Restricted key set — anything that looks like a multi-key
        # chord with shell metacharacters is rejected.  Real chord
        # syntax in Playwright is ``Control+a`` (plus signs only).
        if not re.match(r"^[A-Za-z0-9+\-]+$", key):
            raise BrowserSessionError(
                f"press: key {key!r} contains illegal characters"
            )
        try:
            self._page.keyboard.press(key)
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"press: {exc}") from exc
        self._bump_action()

    def scroll(
        self,
        *,
        direction: str,
        amount: int,
        target: Optional[BrowserTarget] = None,
    ) -> None:
        self._require_open()
        if direction not in ("up", "down", "left", "right"):
            raise BrowserSessionError(
                f"scroll: direction must be up/down/left/right, "
                f"got {direction!r}"
            )
        if not isinstance(amount, int) or amount <= 0:
            raise BrowserSessionError(
                f"scroll: amount must be a positive int, got {amount!r}"
            )
        # Use mouse.wheel with bounded deltas; do NOT use page.evaluate
        # so the caller cannot smuggle JavaScript.
        dx, dy = 0, 0
        if direction == "up":
            dy = -amount
        elif direction == "down":
            dy = amount
        elif direction == "left":
            dx = -amount
        elif direction == "right":
            dx = amount
        if target is not None:
            # Scroll the target into view, then use the mouse wheel.
            try:
                resolved = self.resolve(target)
                resolved.locator.scroll_into_view_if_needed()
            except Exception as exc:  # noqa: BLE001
                raise BrowserSessionError(
                    f"scroll:target resolve: {exc}"
                ) from exc
        try:
            self._page.mouse.wheel(dx, dy)
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"scroll:wheel: {exc}") from exc
        self._bump_action()

    def select(
        self,
        target: BrowserTarget,
        *,
        value: Optional[str] = None,
        label: Optional[str] = None,
        timeout_ms: int = 30_000,
    ) -> _ResolvedElement:
        self._require_open()
        if (value is None) == (label is None):
            raise BrowserSessionError(
                "select: exactly one of 'value' or 'label' must be set"
            )
        resolved = self.resolve(target)
        try:
            if value is not None:
                resolved.locator.select_option(value=value, timeout=timeout_ms)
            else:
                resolved.locator.select_option(label=label, timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"select: {exc}") from exc
        self._bump_action()
        return resolved

    def wait(
        self,
        *,
        until: str,
        target: Optional[BrowserTarget] = None,
        timeout_ms: int = 30_000,
    ) -> Optional[_ResolvedElement]:
        self._require_open()
        if until not in ("visible", "hidden", "attached", "networkidle"):
            raise BrowserSessionError(
                f"wait: 'until' must be one of visible/hidden/"
                f"attached/networkidle, got {until!r}"
            )
        try:
            if until == "networkidle":
                self._page.wait_for_load_state(
                    "networkidle", timeout=timeout_ms
                )
            elif target is None:
                raise BrowserSessionError(
                    "wait: target is required for visible/hidden/attached"
                )
            else:
                resolved = self.resolve(target)
                if until == "visible":
                    resolved.locator.wait_for(
                        state="visible", timeout=timeout_ms
                    )
                elif until == "hidden":
                    resolved.locator.wait_for(
                        state="hidden", timeout=timeout_ms
                    )
                elif until == "attached":
                    resolved.locator.wait_for(
                        state="attached", timeout=timeout_ms
                    )
                self._bump_action()
                return resolved
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"wait: {exc}") from exc
        self._bump_action()
        return None

    # --------------------------------------------------------- observations
    def extract_text(
        self,
        target: Optional[BrowserTarget],
        *,
        max_chars: int = 4000,
        include_attributes: bool = False,
    ) -> Tuple[BrowserElement, str]:
        self._require_open()
        if target is None:
            raise BrowserSessionError(
                "extract_text: target is required (use extract_page for whole page)"
            )
        resolved = self.resolve(target)
        try:
            text = resolved.locator.inner_text(timeout=5000) or ""
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"extract_text: {exc}") from exc
        text = _truncate(text, max_chars)
        attrs: Dict[str, str] = {}
        if include_attributes:
            for name in ("href", "name", "role", "value", "type",
                         "aria-label", "placeholder"):
                try:
                    val = resolved.locator.get_attribute(name)
                except Exception:  # noqa: BLE001
                    val = None
                if val:
                    attrs[name] = val
        element = BrowserElement(
            tag=self._safe_tag(resolved.locator),
            text=text,
            role=resolved.role or attrs.get("role", ""),
            name=resolved.name or attrs.get("aria-label", ""),
            value=attrs.get("value", ""),
            href=attrs.get("href", ""),
            selector=resolved.selector,
            attributes=attrs,
            visible=True,
        )
        self._bump_action()
        return element, text

    def extract_page(self, *, max_chars: int = 8000) -> BrowserPageState:
        self._require_open()
        self._bump_action()
        return self._snapshot(max_chars=max_chars)

    def download(
        self,
        target: BrowserTarget,
        *,
        save_to: str,
    ) -> str:
        self._require_open()
        if not isinstance(save_to, str) or not save_to.strip():
            raise BrowserSessionError("download: 'save_to' is required")
        # Make sure the parent directory exists.  We deliberately
        # use ``os.makedirs`` (not ``os.system``); the safety
        # policy is what validates the resulting path.
        parent = os.path.dirname(os.path.abspath(save_to))
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(
                f"download: cannot create parent dir: {exc}"
            ) from exc
        try:
            with self._page.expect_download() as dl_info:
                resolved = self.resolve(target)
                resolved.locator.click()
            download = dl_info.value
            download.save_as(save_to)
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"download: {exc}") from exc
        self._bump_action()
        return save_to

    # --------------------------------------------------------- screenshot
    def screenshot_path(self) -> Optional[str]:
        """Take a screenshot of the current page to a temp file.

        Used *only* by the vision fallback path; the service
        cleans the file up.  Returns the path, or None on failure.
        Never raises — a missing screenshot is not fatal.
        """
        self._require_open()
        try:
            path = os.path.join(
                tempfile_gettempdir(), f"omnix_browser_{uuid.uuid4().hex}.png"
            )
            self._page.screenshot(path=path)
            return path
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[browser] screenshot failed: {exc!r}")
            return None

    # ------------------------------------------------------------- internals
    def _require_open(self) -> None:
        if not self._state.is_open or self._page is None:
            raise BrowserSessionError(
                f"session {self._session_id!r} is not open"
            )

    def _goto(
        self,
        url: str,
        *,
        wait_until: str = "load",
        timeout_ms: int = 30_000,
    ) -> None:
        try:
            self._page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise BrowserSessionError(f"navigate: {exc}") from exc

    def _snapshot(
        self,
        *,
        max_chars: int = _MAX_VISIBLE_TEXT,
    ) -> BrowserPageState:
        url = self._page.url
        try:
            title = self._page.title() or ""
        except Exception:  # noqa: BLE001
            title = ""
        try:
            visible = self._page.inner_text("body") or ""
        except Exception:  # noqa: BLE001
            visible = ""
        visible = _truncate(visible, max_chars)
        try:
            cookies = self._context.cookies()
        except Exception:  # noqa: BLE001
            cookies = []
        try:
            dom = self._page.content() or ""
        except Exception:  # noqa: BLE001
            dom = ""
        dom = _truncate(dom, _MAX_DOM_SOURCE)

        # A small set of representative element refs.
        refs: List[BrowserElement] = []
        try:
            anchors = self._page.locator("a, button, h1, h2, h3").all()
        except Exception:  # noqa: BLE001
            anchors = []
        for loc in anchors[:8]:
            try:
                tag = self._safe_tag(loc)
                text = _truncate(loc.inner_text() or "", 100)
                href = loc.get_attribute("href") or ""
                role = loc.get_attribute("role") or ""
                name = (
                    loc.get_attribute("aria-label") or ""
                )
                refs.append(BrowserElement(
                    tag=tag, text=text, href=href, role=role, name=name,
                ))
            except Exception:  # noqa: BLE001
                continue

        # Update volatile state fields.
        self._state.current_url = url
        self._state.current_title = title

        return BrowserPageState(
            url=url,
            title=title,
            element_refs=tuple(refs),
            dom_source=dom,
            visible_text=visible,
            cookies_count=len(cookies),
            is_secure_context=url.lower().startswith("https://"),
            viewport=self._viewport,
        )

    def _bump_action(self) -> None:
        self._action_count += 1
        self._state.last_action_at = time.time()
        self._state.action_count = self._action_count

    def _safe_text(self, locator: Any) -> str:
        try:
            return (locator.inner_text() or "")[:200]
        except Exception:  # noqa: BLE001
            return ""

    def _safe_tag(self, locator: Any) -> str:
        return safe_tag(locator)

    def _resolution_order(
        self, kind: LocatorKind
    ) -> Tuple[LocatorKind, ...]:
        """Return the resolution order for ``kind``.

        The declared ``kind`` is tried first.  If the declared kind
        doesn't match (e.g. invalid accessibility spec), we fall
        through the deterministic order:

            CSS  →  TEST_ID  →  TEXT  →  ACCESSIBILITY  →  XPATH
        """
        primary = (
            LocatorKind.CSS,
            LocatorKind.TEST_ID,
            LocatorKind.TEXT,
            LocatorKind.ACCESSIBILITY,
            LocatorKind.XPATH,
        )
        if kind in primary:
            rest = tuple(k for k in primary if k != kind)
            return (kind,) + rest
        return primary

    def _cleanup(self) -> None:
        for close in (self._close_page, self._close_context,
                      self._close_browser, self._stop_pw):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def _close_page(self) -> None:
        if self._page is not None:
            try:
                self._page.close()
            except Exception:  # noqa: BLE001
                pass
        self._page = None

    def _close_context(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:  # noqa: BLE001
                pass
        self._context = None

    def _close_browser(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        self._browser = None

    def _stop_pw(self) -> None:
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._pw = None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_chars: int) -> str:
    if not isinstance(text, str):
        return ""
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


# ``tempfile.gettempdir`` is wrapped so tests can monkeypatch it.
def tempfile_gettempdir() -> str:
    import tempfile
    return tempfile.gettempdir()


def safe_tag(locator: Any) -> str:
    """Return the lower-cased tag name of a Playwright locator.

    Returns ``""`` on any failure (the locator is detached, the
    page is closed, etc.).  Used by the router to build
    :class:`BrowserElement` snapshots.
    """
    if locator is None:
        return ""
    try:
        handle = locator.element_handle()
        if handle is None:
            return ""
        return (handle.evaluate("el => el.tagName.toLowerCase()") or "")
    except Exception:  # noqa: BLE001
        return ""


# Allowed roles for ACCESSIBILITY locator.  A small, fixed set is
# exposed so we never let an LLM invent a role name that
# Playwright does not understand.
_ALLOWED_ROLES = frozenset({
    "alert", "alertdialog", "application", "article", "banner",
    "blockquote", "button", "caption", "cell", "checkbox",
    "code", "columnheader", "combobox", "complementary",
    "contentinfo", "definition", "deletion", "dialog",
    "directory", "document", "emphasis", "feed", "figure",
    "form", "generic", "grid", "gridcell", "group", "heading",
    "img", "insertion", "link", "list", "listbox", "listitem",
    "log", "main", "marquee", "math", "menu", "menubar", "menuitem",
    "menuitemcheckbox", "menuitemradio", "navigation", "none",
    "note", "option", "paragraph", "presentation", "progressbar",
    "radio", "radiogroup", "region", "row", "rowgroup",
    "rowheader", "scrollbar", "search", "searchbox", "separator",
    "slider", "spinbutton", "status", "strong", "subscript",
    "superscript", "switch", "tab", "table", "tablist", "tabpanel",
    "term", "textbox", "time", "timer", "toolbar", "tooltip",
    "tree", "treegrid", "treeitem",
})


def _parse_accessibility(value: str) -> Optional[Tuple[str, str]]:
    """Parse a small, fixed accessibility spec.

    Accepts two shapes:

    * ``role`` or ``role:name`` (the colon separates the role from
      the accessible name) — kept for backward compatibility and for
      the simple planner outputs.
    * ``{"role": "link", "name": "Help center"}`` — JSON, so callers
      that already work with structured output can pass the spec as
      a stringified dict.

    Returns ``(role, name)`` or ``None`` if the value is malformed or
    references a role outside :data:`_ALLOWED_ROLES`.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    # JSON form.
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None
        role = str(obj.get("role", "")).strip().lower()
        if not role or role not in _ALLOWED_ROLES:
            return None
        name = str(obj.get("name", "")).strip()
        return role, name
    # role / role:name form.
    parts = raw.split(":", 1)
    role = parts[0].strip().lower()
    name = parts[1].strip() if len(parts) == 2 else ""
    if role not in _ALLOWED_ROLES:
        return None
    return role, name


# A minimal CSS attribute-value escape; quotes inside data-testid
# would otherwise break out of the selector.
def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
