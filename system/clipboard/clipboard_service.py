"""
Omnix V6 — Windows ClipboardService Implementation (Phase 2).

Implements :class:`core.execution.interfaces.ClipboardService` using
``pyperclip`` as the abstraction over the Windows clipboard.

Failure mode:
    * On Windows, ``pyperclip`` uses the built-in ``tkinter`` and
      occasionally fails with ``OSError`` when another process is
      holding the clipboard lock (e.g. Remote Desktop clipboard sync).
    * We wrap every call in a small retry loop and ultimately return
      a structured :class:`ActionResult` instead of raising.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from loguru import logger as _loguru

from core.execution.interfaces import ClipboardService
from core.lifecycle import LifecycleMixin, LifecycleState
from core.results import ActionResult, ActionStatus


# ---------------------------------------------------------------------------
# Optional pyperclip
# ---------------------------------------------------------------------------

try:
    import pyperclip  # type: ignore
    _HAS_PYPERCLIP = True
except Exception:  # noqa: BLE001
    _HAS_PYPERCLIP = False
    pyperclip = None  # type: ignore


MAX_RETRIES = 3
RETRY_DELAY_S = 0.05


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _call_with_retry(fn, *args, **kwargs):
    """Run ``fn`` with a small retry loop on transient clipboard errors."""
    last_exc: Optional[BaseException] = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs), None
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY_S)
    return None, last_exc


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class WindowsClipboardService(ClipboardService, LifecycleMixin):
    """pyperclip-based :class:`ClipboardService` for Windows hosts."""

    def __init__(self) -> None:
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None
        self._available: bool = _HAS_PYPERCLIP
        if not self._available:
            _loguru.warning(
                "WindowsClipboardService initialized without pyperclip; "
                "all operations will return FAILED."
            )

    # ============================================================ API
    def get_text(self) -> str:
        """Return the current clipboard text, or "" on failure."""
        if not self._available:
            return ""
        result, exc = _call_with_retry(pyperclip.paste)
        if exc is not None:
            _loguru.warning("Clipboard paste failed: {}", exc)
            return ""
        return result or ""

    def set_text(self, text: str) -> ActionResult:
        """Set the clipboard to ``text``."""
        if not self._available:
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="clipboard_set",
                details={"reason": "pyperclip_unavailable"},
            )
        if not isinstance(text, str):
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="clipboard_set",
                details={"reason": "text must be a string"},
            )
        _, exc = _call_with_retry(pyperclip.copy, text)
        if exc is not None:
            return ActionResult(
                status=ActionStatus.FAILED,
                action_name="clipboard_set",
                details={"reason": repr(exc)},
            )
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="clipboard_set",
            details={"length": len(text)},
        )

    def clear(self) -> ActionResult:
        return self.set_text("")

    # =================================================== lifecycle hooks
    def _do_initialize(self) -> bool:
        return True

    def _do_shutdown(self) -> None:
        return None

    def statistics(self) -> Dict[str, Any]:
        return {
            "type": "WindowsClipboardService",
            "lifecycle": self._lifecycle_state.value,
            "pyperclip_available": self._available,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"WindowsClipboardService(state={self._lifecycle_state.value}, available={self._available})"
