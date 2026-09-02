"""
Omnix V6 — Windows WindowService Implementation (Phase 2).

Implements :class:`core.execution.interfaces.WindowService` for the
Windows platform.

Approach:
    * Uses ``win32gui`` (pywin32) as the primary window API.
        - ``win32gui.EnumWindows`` for discovery
        - ``win32gui.GetWindowText`` / ``GetWindowRect`` for metadata
        - ``SetForegroundWindow`` / ``ShowWindow`` for focus / restore
    * Returns *structured* :class:`ActionResult` for every state-changing
      call, never raises on recoverable failures.

Failure-mode contract:
    * No-window-matched, window destroyed mid-operation, etc. all map to
      ``ActionStatus.FAILED`` with descriptive notes, never exceptions.
    * Timeouts are honored via :class:`core.utils.timers.run_with_timeout`.

Safety classification:
    * MUTATING — focus changes the active window, move/resize change
      layout.  The capability router will ask before invoking these.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger as _loguru

from core.execution.interfaces import WindowService
from core.lifecycle import LifecycleMixin, LifecycleState
from core.results import ActionResult, ActionStatus
from core.utils.timers import CancellationToken, run_with_timeout


# ---------------------------------------------------------------------------
# Optional win32 imports — module must still *import* on non-Windows
# (test environment) without error, but the runtime calls will fail
# with a clear error if used.
# ---------------------------------------------------------------------------

try:
    import win32api  # type: ignore
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32process  # type: ignore

    _WIN32_AVAILABLE = True
except Exception:  # noqa: BLE001
    _WIN32_AVAILABLE = False
    win32gui = None  # type: ignore
    win32con = None  # type: ignore
    win32api = None  # type: ignore
    win32process = None  # type: ignore


# ---------------------------------------------------------------------------
# Window info DTO
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowInfo:
    """Snapshot of a single OS window."""

    hwnd: int
    title: str
    process: str
    pid: int
    bounds: Dict[str, int]  # {"x", "y", "w", "h"}
    visible: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "process": self.process,
            "pid": self.pid,
            "bounds": dict(self.bounds),
            "visible": self.visible,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_win32_available() -> None:
    if not _WIN32_AVAILABLE:
        raise RuntimeError(
            "WindowService requires Windows (pywin32). "
            "This host is not Windows or pywin32 is not installed."
        )


def _is_window(hwnd: int) -> bool:
    """True iff ``hwnd`` is still a live window."""
    if not _WIN32_AVAILABLE:
        return False
    try:
        return bool(win32gui.IsWindow(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _get_window_text(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:  # noqa: BLE001
        return ""


def _get_window_rect(hwnd: int) -> Tuple[int, int, int, int]:
    try:
        rect = win32gui.GetWindowRect(hwnd)
        x1, y1, x2, y2 = rect
        return (int(x1), int(y1), int(x2 - x1), int(y2 - y1))
    except Exception:  # noqa: BLE001
        return (0, 0, 0, 0)


def _get_window_pid(hwnd: int) -> int:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return int(pid)
    except Exception:  # noqa: BLE001
        return 0


def _get_process_name_from_pid(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return ""


def _is_window_visible(hwnd: int) -> bool:
    try:
        return bool(win32gui.IsWindowVisible(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _collect_window(hwnd: int) -> Optional[WindowInfo]:
    """Build a WindowInfo from a hwnd.  Skips non-app windows."""
    if not _is_window(hwnd):
        return None
    title = _get_window_text(hwnd)
    # Filter out invisible/zero-size windows that aren't really windows
    if not title.strip():
        # Some real windows have empty titles, but we filter most noise
        return None
    pid = _get_window_pid(hwnd)
    if pid <= 0:
        return None
    x, y, w, h = _get_window_rect(hwnd)
    if w == 0 or h == 0:
        return None
    return WindowInfo(
        hwnd=int(hwnd),
        title=title,
        process=_get_process_name_from_pid(pid),
        pid=pid,
        bounds={"x": x, "y": y, "w": w, "h": h},
        visible=_is_window_visible(hwnd),
    )


def _find_hwnd_by_title(needle: str) -> List[int]:
    """Return all hwnds whose title contains ``needle`` (case-insensitive)."""
    if not _WIN32_AVAILABLE:
        return []
    needle_l = needle.lower()
    matches: List[int] = []

    def _cb(hwnd: int, _ctx: Any) -> None:
        if not _is_window(hwnd):
            return
        title = _get_window_text(hwnd)
        if title and needle_l in title.lower():
            matches.append(int(hwnd))

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        pass
    return matches


def _find_hwnd_by_process(process_name: str) -> List[int]:
    """Return all hwnds whose owning process matches ``process_name``."""
    if not _WIN32_AVAILABLE:
        return []
    target = process_name.lower().rstrip(".exe")
    matches: List[int] = []

    def _cb(hwnd: int, _ctx: Any) -> None:
        if not _is_window(hwnd):
            return
        pid = _get_window_pid(hwnd)
        if pid <= 0:
            return
        proc = _get_process_name_from_pid(pid).lower().rstrip(".exe")
        if proc == target:
            matches.append(int(hwnd))

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        pass
    return matches


def _failed_result(message: str, *, action: str, details: Dict[str, Any]) -> ActionResult:
    return ActionResult(
        status=ActionStatus.FAILED,
        action_name=action,
        details=details,
        error=None,
    )


def _executed_result(action: str, details: Dict[str, Any]) -> ActionResult:
    return ActionResult(
        status=ActionStatus.EXECUTED,
        action_name=action,
        details=details,
        error=None,
    )


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------

class WindowsWindowService(WindowService, LifecycleMixin):
    """win32-based :class:`WindowService` for Windows hosts."""

    DEFAULT_TIMEOUT_S = 5.0

    def __init__(self) -> None:
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None
        self._win32_ok: bool = _WIN32_AVAILABLE
        if _WIN32_AVAILABLE:
            _loguru.debug("WindowsWindowService initialized (win32 ready).")
        else:
            _loguru.warning(
                "WindowsWindowService initialized without win32; "
                "all operations will return FAILED results."
            )

    # ============================================================ queries
    def list_windows(self) -> List[Dict[str, Any]]:
        """Return [{title, process, hwnd, pid, bounds, visible}, ...]."""
        if not self._win32_ok:
            return []
        out: List[Dict[str, Any]] = []

        def _cb(hwnd: int, _ctx: Any) -> None:
            info = _collect_window(hwnd)
            if info is not None:
                out.append(info.to_dict())

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:  # noqa: BLE001
            _loguru.exception("EnumWindows failed.")
        return out

    def find_window(
        self,
        *,
        title: Optional[str] = None,
        process: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find a single window.  Returns dict, or None on no match.

        Raises :class:`RuntimeError` if both filters are given and zero
        matches found? No — returns None.  Multiple matches → returns the
        first visible one, else the first.
        """
        if not self._win32_ok:
            return None
        if not title and not process:
            return None
        candidates: List[int] = []
        if title:
            candidates.extend(_find_hwnd_by_title(title))
        if process:
            candidates.extend(_find_hwnd_by_process(process))
        # de-dup preserving order
        seen: set = set()
        unique: List[int] = []
        for h in candidates:
            if h not in seen:
                seen.add(h)
                unique.append(h)
        if not unique:
            return None
        # Prefer visible windows
        for h in unique:
            if _is_window_visible(h):
                info = _collect_window(h)
                if info is not None:
                    return info.to_dict()
        # Fall back to first one (even if invisible)
        info = _collect_window(unique[0])
        return info.to_dict() if info is not None else None

    # ============================================================ mutating
    def focus_window(self, hwnd: int) -> ActionResult:
        if not self._win32_ok:
            return _failed_result(
                "WindowService is not available (no win32).",
                action="focus_window",
                details={"hwnd": hwnd},
            )
        if not _is_window(hwnd):
            return _failed_result(
                f"Window hwnd={hwnd} no longer exists.",
                action="focus_window",
                details={"hwnd": hwnd},
            )

        def _do() -> bool:
            # Restore if minimized, then bring to foreground.
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                # AttachThreadInput to bypass focus-stealing prevention.
                attached = False
                fg_thread = 0
                try:
                    fg_hwnd = win32gui.GetForegroundWindow()
                    fg_thread, _ = win32process.GetWindowThreadProcessId(fg_hwnd)
                    cur_thread = win32api.GetCurrentThreadId()
                    if fg_thread and fg_thread != cur_thread:
                        ctypes.windll.user32.AttachThreadInput(fg_thread, cur_thread, True)
                        attached = True
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                        win32gui.SetFocus(hwnd)
                    finally:
                        if attached:
                            ctypes.windll.user32.AttachThreadInput(fg_thread, cur_thread, False)
                except Exception:  # noqa: BLE001
                    # Fall back to plain call
                    win32gui.SetForegroundWindow(hwnd)
            except Exception as exc:  # noqa: BLE001
                _loguru.warning("focus_window raised: {}", exc)
                return False
            return win32gui.GetForegroundWindow() == hwnd

        try:
            ok = run_with_timeout(
                _do,
                seconds=self.DEFAULT_TIMEOUT_S,
            )
        except TimeoutError:
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name="focus_window",
                details={"hwnd": hwnd, "timeout_s": self.DEFAULT_TIMEOUT_S},
            )
        if not ok:
            return _failed_result(
                "Failed to bring window to foreground.",
                action="focus_window",
                details={"hwnd": hwnd},
            )
        return _executed_result("focus_window", {"hwnd": hwnd, "focused": True})

    def move_window(self, hwnd: int, x: int, y: int, w: int, h: int) -> ActionResult:
        if not self._win32_ok:
            return _failed_result(
                "WindowService not available.",
                action="move_window",
                details={"hwnd": hwnd},
            )
        if not _is_window(hwnd):
            return _failed_result(
                f"Window hwnd={hwnd} no longer exists.",
                action="move_window",
                details={"hwnd": hwnd},
            )
        if w <= 0 or h <= 0:
            return _failed_result(
                "Window size must be positive.",
                action="move_window",
                details={"w": w, "h": h},
            )
        try:
            win32gui.MoveWindow(hwnd, int(x), int(y), int(w), int(h), True)
        except Exception as exc:  # noqa: BLE001
            return _failed_result(
                f"MoveWindow failed: {exc!r}",
                action="move_window",
                details={"hwnd": hwnd, "x": x, "y": y, "w": w, "h": h},
            )
        return _executed_result(
            "move_window",
            {"hwnd": hwnd, "x": x, "y": y, "w": w, "h": h},
        )

    def resize_window(self, hwnd: int, w: int, h: int) -> ActionResult:
        if not self._win32_ok:
            return _failed_result(
                "WindowService not available.",
                action="resize_window",
                details={"hwnd": hwnd},
            )
        if not _is_window(hwnd):
            return _failed_result(
                f"Window hwnd={hwnd} no longer exists.",
                action="resize_window",
                details={"hwnd": hwnd},
            )
        if w <= 0 or h <= 0:
            return _failed_result(
                "Window size must be positive.",
                action="resize_window",
                details={"w": w, "h": h},
            )
        cur_x, cur_y, _, _ = _get_window_rect(hwnd)
        return self.move_window(hwnd, cur_x, cur_y, w, h)

    # =================================================== lifecycle hooks
    def _do_initialize(self) -> bool:
        return True

    def _do_shutdown(self) -> None:
        return None

    def statistics(self) -> Dict[str, Any]:
        return {
            "type": "WindowsWindowService",
            "lifecycle": self._lifecycle_state.value,
            "win32_available": self._win32_ok,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"WindowsWindowService(state={self._lifecycle_state.value}, win32={self._win32_ok})"
