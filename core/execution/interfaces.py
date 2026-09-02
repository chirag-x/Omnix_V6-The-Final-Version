"""
Omnix V6 — System execution interface contracts (Phase 1 §29).

This module defines the *contracts* every later phase must satisfy.
It is **interfaces only** — no Windows API, no pyautogui, no
win32api.  The implementations land in Phase 2.

Why a dedicated module:
    - Every Windows-facing capability (open Chrome, type into
      Notepad, copy a file) goes through one of these protocols.
    - Tests can inject mock implementations without ever importing
      a real Windows API.
    - The capability router can declare ``requires_services``
      against these names; the registry uses them to gate
      availability.

The protocols use :class:`typing.Protocol` (structural) so concrete
implementations do not need to inherit from them; the runtime check
is informational only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from ..results import (
    ActionResult,
    CapabilityResult,
    ObservationResult,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# ApplicationService — discover / launch / focus / close applications
# ---------------------------------------------------------------------------

@runtime_checkable
class ApplicationService(Protocol):
    """Open, focus, close, and enumerate desktop applications."""

    def is_running(self, app_name: str) -> bool:
        """True iff the named application has a live process."""
        ...

    def launch(self, app_name: str, *, args: Optional[Tuple[str, ...]] = None) -> ActionResult:
        """Start the named application.  Returns a structured result."""
        ...

    def focus(self, app_name: str) -> ActionResult:
        """Bring the named application to the foreground."""
        ...

    def close(self, app_name: str, *, force: bool = False) -> ActionResult:
        """Close the named application.

        ``force=True`` corresponds to a hard kill (TerminateProcess);
        ``force=False`` corresponds to a graceful WM_CLOSE.
        """
        ...

    def list_running(self) -> List[str]:
        """Enumerate the names of every running application."""
        ...


# ---------------------------------------------------------------------------
# WindowService — focus / move / resize / list windows
# ---------------------------------------------------------------------------

@runtime_checkable
class WindowService(Protocol):
    """Operate on individual OS windows."""

    def list_windows(self) -> List[Dict[str, Any]]:
        """Return [{title, process, hwnd, pid, bounds}, ...]."""
        ...

    def find_window(self, *, title: Optional[str] = None, process: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Find a single window by title / process match.

        Returns ``None`` if no match is found; raises if multiple
        matches and no disambiguator.
        """
        ...

    def focus_window(self, hwnd: int) -> ActionResult:
        ...

    def move_window(self, hwnd: int, x: int, y: int, w: int, h: int) -> ActionResult:
        ...

    def resize_window(self, hwnd: int, w: int, h: int) -> ActionResult:
        ...


# ---------------------------------------------------------------------------
# ProcessService — process inspection / control
# ---------------------------------------------------------------------------

@runtime_checkable
class ProcessService(Protocol):
    """Query and control OS processes."""

    def is_process_running(self, name: str) -> bool: ...
    def pid_for(self, name: str) -> Optional[int]: ...
    def kill(self, pid: int, *, force: bool = True) -> ActionResult: ...
    def list_processes(self) -> List[Dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# InputService — mouse / keyboard
# ---------------------------------------------------------------------------

@runtime_checkable
class InputService(Protocol):
    """Low-level input (mouse + keyboard).  Phase 2 wires the real APIs."""

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> ActionResult: ...
    def double_click(self, x: int, y: int) -> ActionResult: ...
    def move_mouse(self, x: int, y: int) -> ActionResult: ...
    def type_text(self, text: str, *, interval_s: float = 0.0) -> ActionResult: ...
    def press_key(self, key: str) -> ActionResult: ...
    def hotkey(self, *keys: str) -> ActionResult: ...
    def drag(self, x1: int, y1: int, x2: int, y2: int, *, duration_s: float = 0.5) -> ActionResult: ...
    def scroll(self, x: int, y: int, *, clicks: int, vertical: bool = True) -> ActionResult: ...


# ---------------------------------------------------------------------------
# ClipboardService — read / write clipboard
# ---------------------------------------------------------------------------

@runtime_checkable
class ClipboardService(Protocol):
    def get_text(self) -> str: ...
    def set_text(self, text: str) -> ActionResult: ...
    def clear(self) -> ActionResult: ...


# ---------------------------------------------------------------------------
# FilesystemService — read / write / search files
# ---------------------------------------------------------------------------

@runtime_checkable
class FilesystemService(Protocol):
    def read_text(self, path: str, *, encoding: str = "utf-8") -> str: ...
    def write_text(self, path: str, content: str, *, encoding: str = "utf-8") -> ActionResult: ...
    def exists(self, path: str) -> bool: ...
    def list_dir(self, path: str) -> List[str]: ...
    def search(
        self,
        root: str,
        pattern: str,
        *,
        recursive: bool = True,
    ) -> List[str]: ...


# ---------------------------------------------------------------------------
# VerificationService — verify a post-condition (R-5 / R-8)
# ---------------------------------------------------------------------------

@runtime_checkable
class VerificationService(Protocol):
    """Verify that the world matches a post-condition after an action."""

    def verify(self, *, check: str, expected: Any, **context: Any) -> VerificationResult:
        """Run ``check`` and compare against ``expected``.

        ``check`` is a free-form string the implementation knows how
        to interpret (``"app_is_running"``, ``"window_has_focus"``,
        ``"clipboard_contains"``, …).  ``context`` carries extra
        parameters the check needs.
        """
        ...


# ---------------------------------------------------------------------------
# ObservationService — sense the world
# ---------------------------------------------------------------------------

@runtime_checkable
class ObservationService(Protocol):
    """Sense something about the world (screen, UIA tree, DOM, etc.)."""

    def observe(self, *, target: str, **context: Any) -> ObservationResult:
        """Run a sensor and return a structured observation.

        ``target`` is a free-form name the implementation knows
        (``"screen"``, ``"uia_tree"``, ``"active_window"``, …).
        """
        ...


# ---------------------------------------------------------------------------
# Catalog — single source of truth for the canonical service names
# ---------------------------------------------------------------------------

SERVICE_NAMES = {
    "application": "core.execution.interfaces.ApplicationService",
    "window": "core.execution.interfaces.WindowService",
    "process": "core.execution.interfaces.ProcessService",
    "input": "core.execution.interfaces.InputService",
    "clipboard": "core.execution.interfaces.ClipboardService",
    "filesystem": "core.execution.interfaces.FilesystemService",
    "verification": "core.execution.interfaces.VerificationService",
    "observation": "core.execution.interfaces.ObservationService",
}


def capability_requires_service_name(capability_name: str) -> str:
    """Map a capability name to the service that implements it.

    Example: ``capability_requires_service_name("open_application")``
    returns ``"application"``.

    Used by the capability registry to auto-fill
    ``requires_services`` when a skill is registered without one.
    """
    name = capability_name.lower()
    if name.startswith("open_") or name.startswith("close_") or name.startswith("focus_app") or name.startswith("launch_"):
        return "application"
    if name.startswith("focus_window") or name.startswith("move_window") or name.startswith("resize_window"):
        return "window"
    if name.startswith("kill_process") or name.startswith("is_process_running") or name.startswith("pid_for"):
        return "process"
    if (
        name.startswith("click") or name.startswith("type") or name.startswith("press")
        or name.startswith("hotkey") or name.startswith("drag") or name.startswith("scroll")
        or name.startswith("double_click") or name.startswith("move_mouse")
    ):
        return "input"
    if name.startswith("clipboard") or name.startswith("copy") or name.startswith("paste") or name.startswith("cut"):
        return "clipboard"
    if name.startswith("read_file") or name.startswith("write_file") or name.startswith("search_file") or name.startswith("list_dir") or name.startswith("file_exists"):
        return "filesystem"
    if name.startswith("verify_"):
        return "verification"
    if name.startswith("observe_") or name.startswith("sense_") or name.startswith("capture_"):
        return "observation"
    return ""
