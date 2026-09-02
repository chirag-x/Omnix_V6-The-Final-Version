"""
Omnix V6 — TargetContext service (Phase 15).

A small, generic *target-acquisition* service.  Input capabilities
("type text", "press key", "hotkey", "click") need to know which
window they are dispatching into.  Without a focused target, the
input goes to whatever window is currently on top of the desktop,
which is exactly the bug that caused "Open Notepad and type Hello
World" to type into the wrong window.

The TargetContext service is the *only* place that knows how to
turn a human-friendly app name (e.g. "notepad", "chrome") or a
window title (e.g. "Untitled - Notepad") into a *focused* OS
window.  Input capabilities call it before any side-effecting
input, and again after, to verify the focus is still on the
intended window.

It composes three existing V6 services:

  * :class:`ApplicationService` — resolves ``app_name`` via the
    catalog to a record / process.
  * :class:`WindowService`     — finds / focuses the actual hwnd.
  * :class:`TargetContextStore` — remembers the most recent
    successful target so subsequent steps ("type", "press")
    inherit the focus without an extra round-trip.

Design constraints (from the Phase 15 brief):

  * No hard-coded application names.  The TargetContext is
    generic.
  * No app-specific branches (``if app == "notepad": ...``).  The
    service uses the catalog + process metadata to find a window.
  * No screen coordinates.  Resolution is by app_name → process →
    window title (or vice versa), not by coordinates.
  * State-based waiting, not ``time.sleep``.  Focus is verified
    by polling ``GetForegroundWindow`` until it returns the
    expected hwnd or a timeout elapses.

Public surface
--------------

  * :class:`TargetContext`        — frozen DTO describing a target
  * :class:`TargetContextResolver` — generic target acquisition
  * :class:`TargetContextStore`     — remembers the most recent
                                      successful target
  * :class:`InMemoryTargetContextStore` — process-local default

The store is intentionally tiny: it holds at most one
:class:`TargetContext` per logical "session" (the most recent
focused window).  Capabilities that need a target call
:meth:`TargetContextResolver.acquire` and pass the returned DTO
to the input service.  The store is a process-local cache; the
canonical state is still the desktop, queried live.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from loguru import logger as _loguru

from core.execution.interfaces import ApplicationService, WindowService
from core.results import ActionResult, ActionStatus


# ---------------------------------------------------------------------------
# TargetContext DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TargetContext:
    """A description of a focused target window.

    The DTO is *frozen* (R-10) so it can be safely threaded through
    capability code and recorded in observation entries.  The
    ``hwnd`` and ``foreground_state`` are populated by the
    :class:`TargetContextResolver` after a successful focus call.

    Field semantics
    ---------------

    * ``application``      — the user-facing app name the caller
      asked for ("notepad", "chrome").  Optional when the target
      was resolved from a window title only.
    * ``process``          — the resolved process basename
      ("notepad.exe", "chrome.exe").  May be ``None`` when the
      target is identified by title only.
    * ``window_title``     — the title of the focused window
      ("Untitled - Notepad").
    * ``hwnd``             — the integer window handle.
    * ``foreground_state`` — ``"focused"`` if the resolver
      confirmed ``GetForegroundWindow() == hwnd`` at acquisition
      time, else ``"unfocused"``.
    * ``expected_ui_state`` — a free-form description of the
      expected post-state ("notepad has a blinking caret", "chrome
      has a search bar focused").  Optional; the input
      capabilities do not require it to be set.
    """

    application: Optional[str] = None
    process: Optional[str] = None
    window_title: Optional[str] = None
    hwnd: Optional[int] = None
    foreground_state: str = "unknown"
    expected_ui_state: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "application": self.application,
            "process": self.process,
            "window_title": self.window_title,
            "hwnd": self.hwnd,
            "foreground_state": self.foreground_state,
            "expected_ui_state": self.expected_ui_state,
        }


# ---------------------------------------------------------------------------
# Service protocols (typed injection points)
# ---------------------------------------------------------------------------

@runtime_checkable
class ForegroundWindowReader(Protocol):
    """Tiny protocol the resolver uses to ask Windows which window
    is currently in the foreground.  Production code passes a
    closure over ``win32gui.GetForegroundWindow``; tests pass a
    stub that returns a fixed hwnd.
    """

    def get_foreground_hwnd(self) -> int:
        ...


@runtime_checkable
class TargetContextStore(Protocol):
    """A typed store for the most recent focused :class:`TargetContext`.

    Capabilities that need a target consult this store first
    (``get_recent``) and, on a successful focus, call
    ``record`` so subsequent steps inherit the same target.
    """

    def get_recent(self) -> Optional[TargetContext]:
        ...

    def record(self, ctx: TargetContext) -> None:
        ...

    def clear(self) -> None:
        ...


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TargetContextResolver:
    """Generic, app-agnostic target acquisition.

    The resolver knows how to:

      1. Find a target window for an ``app_name`` (using the
         :class:`ApplicationService` to resolve the name → process
         basename, then the :class:`WindowService` to find a
         window whose process matches).
      2. Find a target window for a window title (using
         :class:`WindowService.find_window(title=...)`).
      3. Focus the candidate window via
         :class:`WindowService.focus_window`.
      4. Verify focus by polling the
         :class:`ForegroundWindowReader` until
         ``GetForegroundWindow() == hwnd`` or a timeout elapses.

    The resolver never branches on app name.  A new app works the
    same way as Notepad: ask the catalog → ask the window service
    → focus.  No ``if app == "notepad"`` special case.
    """

    DEFAULT_FOCUS_TIMEOUT_S: float = 5.0
    DEFAULT_FOCUS_POLL_S: float = 0.05

    def __init__(
        self,
        *,
        app_service: ApplicationService,
        window_service: WindowService,
        foreground_reader: Optional[ForegroundWindowReader] = None,
        store: Optional[TargetContextStore] = None,
        focus_timeout_s: float = DEFAULT_FOCUS_TIMEOUT_S,
        focus_poll_s: float = DEFAULT_FOCUS_POLL_S,
    ) -> None:
        if app_service is None:
            raise TypeError(
                "TargetContextResolver requires an ApplicationService."
            )
        if window_service is None:
            raise TypeError(
                "TargetContextResolver requires a WindowService."
            )
        self._app = app_service
        self._win = window_service
        self._fg = foreground_reader or _Win32ForegroundReader()
        self._store = store or InMemoryTargetContextStore()
        self._focus_timeout_s = float(focus_timeout_s)
        self._focus_poll_s = float(focus_poll_s)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def acquire(
        self,
        *,
        app_name: Optional[str] = None,
        window_title: Optional[str] = None,
        expected_ui_state: Optional[str] = None,
    ) -> Optional[TargetContext]:
        """Acquire and focus a target window.

        Returns the focused :class:`TargetContext` or ``None`` when
        no candidate window could be found or focused.  The store
        is updated on success so subsequent calls without
        arguments can inherit the focus.
        """
        candidate = self._find_candidate(
            app_name=app_name, window_title=window_title
        )
        if candidate is None:
            _loguru.debug(
                "TargetContext: no candidate for app_name={!r} title={!r}",
                app_name, window_title,
            )
            return None
        hwnd = int(candidate.get("hwnd") or 0)
        if hwnd <= 0:
            return None
        title = str(candidate.get("title") or "")
        process = str(candidate.get("process") or "")
        # Try to focus the window.
        focus_result = self._win.focus_window(hwnd)
        # ``ActionResult.status`` is one of EXECUTED / FAILED /
        # TIMED_OUT / CANCELLED / SKIPPED.  A successful focus call
        # returns EXECUTED.  (VERIFIED is a CapabilityStatus and
        # VerificationStatus value, never an ActionStatus — Phase
        # 15 earlier code accidentally accepted it and silently
        # rejected every real result, which made the resolver
        # always return ``None``.)
        if not isinstance(focus_result, ActionResult) or (
            focus_result.status is not ActionStatus.EXECUTED
        ):
            _loguru.debug(
                "TargetContext: focus_window returned {}", focus_result
            )
            return None
        # Verify focus by polling GetForegroundWindow.
        if not self._wait_for_focus(hwnd):
            return None
        ctx = TargetContext(
            application=app_name,
            process=process or None,
            window_title=title or None,
            hwnd=hwnd,
            foreground_state="focused",
            expected_ui_state=expected_ui_state,
        )
        try:
            self._store.record(ctx)
        except Exception:  # noqa: BLE001
            # Store failures must not poison the resolver.
            pass
        return ctx

    def get_recent(self) -> Optional[TargetContext]:
        try:
            return self._store.get_recent()
        except Exception:  # noqa: BLE001
            return None

    def clear_recent(self) -> None:
        try:
            self._store.clear()
        except Exception:  # noqa: BLE001
            pass

    def acquire_hwnd(self, hwnd: int) -> Optional[TargetContext]:
        """Acquire a target context for a *known* HWND.

        Used when a prior step (typically
        ``desktop.application.open``) has already obtained a
        verified HWND and the next step needs the same window
        focused.  We call ``focus_window`` and then poll the
        foreground reader; if the OS foreground lockout blocks
        the focus call, we still return a :class:`TargetContext`
        marked ``foreground_state="known"`` so the caller can
        proceed — the previous step already verified the window
        existed and was correct.

        Returns ``None`` only when the HWND no longer exists.
        """
        try:
            h = int(hwnd)
        except (TypeError, ValueError):
            return None
        if h <= 0:
            return None
        if not self._win.find_window(hwnd=h):
            return None
        focus_result = self._win.focus_window(h)
        focused = (
            isinstance(focus_result, ActionResult)
            and focus_result.status is ActionStatus.EXECUTED
        )
        is_fg = self.is_foreground(h)
        # Look up title/process for the context
        try:
            info = self._win.find_window(hwnd=h) or {}
        except Exception:  # noqa: BLE001
            info = {}
        ctx = TargetContext(
            application=None,
            process=str(info.get("process") or "") or None,
            window_title=str(info.get("title") or "") or None,
            hwnd=h,
            foreground_state=(
                "focused" if (focused and is_fg)
                else "known"
            ),
            expected_ui_state=None,
        )
        try:
            self._store.record(ctx)
        except Exception:  # noqa: BLE001
            pass
        return ctx

    def is_foreground(self, hwnd: int) -> bool:
        try:
            return int(self._fg.get_foreground_hwnd()) == int(hwnd)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _find_candidate(
        self,
        *,
        app_name: Optional[str],
        window_title: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        # Path 1: explicit window title wins.
        if window_title:
            try:
                win = self._win.find_window(title=window_title)
            except Exception:  # noqa: BLE001
                win = None
            if win:
                return win
        # Path 2: app_name resolution via the catalog.
        if app_name:
            # The application service is allowed to look up the
            # canonical process name for us.  We don't re-implement
            # the catalog here — that's the catalog's job.
            target_exe = self._resolve_exe_for(app_name)
            if target_exe:
                try:
                    win = self._win.find_window(process=target_exe)
                except Exception:  # noqa: BLE001
                    win = None
                if win:
                    return win
        # Path 3: look at recent store.
        recent = self.get_recent()
        if recent and recent.hwnd:
            try:
                listed = self._win.list_windows() or []
            except Exception:  # noqa: BLE001
                listed = []
            for w in listed:
                if int(w.get("hwnd") or 0) == int(recent.hwnd):
                    return w
        return None

    def _resolve_exe_for(self, app_name: str) -> Optional[str]:
        """Map ``app_name`` to a process basename via the catalog.

        This is the *only* place we touch the catalog for the
        resolver.  The ApplicationService is the canonical owner
        of catalog-driven lookup; we delegate to it.  We do not
        hard-code any app.

        UWP records publish their *real* process basename in
        ``record.metadata['process_names']`` while
        ``record.executable`` carries a synthetic ``<Name>.uwp``
        token.  Window service lookup uses the basename, so we
        prefer ``process_names`` when present.
        """
        try:
            res = self._app.resolve(app_name)
        except Exception:  # noqa: BLE001
            return None
        if not getattr(res, "is_found", False):
            return None
        rec = getattr(res, "record", None)
        if rec is None:
            return None
        meta = getattr(rec, "metadata", None) or {}
        process_names = meta.get("process_names") if isinstance(meta, dict) else None
        if isinstance(process_names, (list, tuple)):
            for name in process_names:
                if isinstance(name, str) and name.lower().endswith(".exe"):
                    return name.strip()
        exe = getattr(rec, "executable", None)
        if isinstance(exe, str) and exe.strip():
            return exe.strip()
        return None

    def _wait_for_focus(self, hwnd: int) -> bool:
        deadline = time.time() + self._focus_timeout_s
        while time.time() < deadline:
            if self.is_foreground(hwnd):
                return True
            time.sleep(self._focus_poll_s)
        return self.is_foreground(hwnd)


# ---------------------------------------------------------------------------
# Default foreground reader
# ---------------------------------------------------------------------------


class _Win32ForegroundReader:
    """Production :class:`ForegroundWindowReader` backed by pywin32."""

    def get_foreground_hwnd(self) -> int:
        try:
            import win32gui  # type: ignore
            return int(win32gui.GetForegroundWindow())
        except Exception:  # noqa: BLE001
            return 0


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


@dataclass
class InMemoryTargetContextStore:
    """A trivial :class:`TargetContextStore` holding the most
    recent successful target.

    The store is process-local; cross-execution dedup is out of
    scope.  Tests inject a stub store; production code uses this
    default.
    """

    _value: Optional[TargetContext] = field(default=None, init=False)

    def get_recent(self) -> Optional[TargetContext]:
        return self._value

    def record(self, ctx: TargetContext) -> None:
        self._value = ctx

    def clear(self) -> None:
        self._value = None


__all__ = [
    "TargetContext",
    "TargetContextResolver",
    "TargetContextStore",
    "ForegroundWindowReader",
    "InMemoryTargetContextStore",
]
