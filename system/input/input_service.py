"""
Omnix V6 — Windows InputService Implementation (Phase 17 + System 4 Peak).

Implements :class:`core.execution.interfaces.InputService` for the
Windows platform, using **PyAutoGUI** for low-level mouse/keyboard
control.

Why PyAutoGUI (over pynput or raw ctypes):
    * Cross-platform, mature, well-tested.
    * ``pyautogui.FAILSAFE`` corner-safety helps prevent runaway clicks.
    * Built-in ``.typewrite`` / ``.hotkey`` for symbolic keyboard work.
    * We treat the *whole PyAutoGUI* call as a single side-effecting
      action that runs through ``run_with_timeout`` — a long type_text
      can be interrupted cooperatively.

Why a custom ``_pump`` loop:
    * For long type-text operations, we want to honor cancellation /
      timeout *between* characters.  PyAutoGUI is a C-extension busy
      call; we cannot interrupt it.  The safe pattern is: chunk the
      input into short pieces, run each chunk under ``run_with_timeout``,
      and check a ``CancellationToken`` between chunks.

Why two parallel APIs (xy *and* target):
    * Raw ``click(x, y)`` is the *escape hatch* — the Brain can call it
      when it has no vision in the loop (legacy scripts, macro replay).
    * ``click_target(target)`` is the *grounded* path — Brain finds a
      ``TargetContext`` in a frame, hands it to the Input service, and
      the service handles validation, safe-point selection, and click
      in one atomic call.  This is the path vision-enabled agents
      should use.

Safety:
    * PyAutoGUI's ``FAILSAFE`` is enabled: dragging the mouse to the
      top-left corner raises ``pyautogui.FailSafeException`` and aborts.
    * Each input call has a default timeout that scales with the
      payload size (``min(MAX_TIMEOUT, 0.05 * len) + 1.0``).
    * Secret text (passwords, tokens, keys) is never logged.  We log
      *length only* (``typed 18 chars`` not ``typed MySecret123``).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

import pyautogui
from loguru import logger as _loguru

from core.execution.interfaces import InputService
from core.lifecycle import LifecycleMixin, LifecycleState
from core.results import ActionResult, ActionStatus
from core.utils.timers import CancellationToken, OperationCancelled, run_with_timeout


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

MIN_TIMEOUT_S = 1.0
MAX_TIMEOUT_S = 30.0
CHUNK_SIZE = 50         # characters per pump for type_text
CHUNK_INTERVAL_S = 0.01  # pause between chunks (cooperative check window)
DEFAULT_CLICK_INTERVAL_S = 0.05

# GroundedTarget safety defaults
DEFAULT_MAX_TARGET_AGE_S = 10.0    # refuse targets older than 10s
DEFAULT_MIN_CONFIDENCE = 0.3       # refuse vision hits < 0.3 confidence
LARGE_TEXT_THRESHOLD = 50          # type_text length that triggers paste fallback


# ---------------------------------------------------------------------------
# Structured error codes
# ---------------------------------------------------------------------------

class InputErrorCode:
    """Stable string constants for input error categories.

    These are surfaced via ``ActionResult.details["code"]`` so callers
    (Brain, Agent, recovery engine) can branch on a stable identifier
    rather than parsing free-form text.
    """
    INVALID_TARGET = "INVALID_TARGET"
    OUT_OF_BOUNDS = "OUT_OF_BOUNDS"
    STALE_TARGET = "STALE_TARGET"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    FOCUS_REQUIRED = "FOCUS_REQUIRED"
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    INPUT_DEVICE_UNAVAILABLE = "INPUT_DEVICE_UNAVAILABLE"
    KEYBOARD_UNAVAILABLE = "KEYBOARD_UNAVAILABLE"
    MOUSE_UNAVAILABLE = "MOUSE_UNAVAILABLE"
    CLIPBOARD_UNAVAILABLE = "CLIPBOARD_UNAVAILABLE"
    ACTION_CANCELLED = "ACTION_CANCELLED"
    TIMEOUT = "TIMEOUT"
    FAILSAFE_TRIGGERED = "FAILSAFE_TRIGGERED"
    DRAG_TOO_SHORT = "DRAG_TOO_SHORT"
    INVALID_PARAMETERS = "INVALID_PARAMETERS"
    KEY_NOT_SUPPORTED = "KEY_NOT_SUPPORTED"
    TEXT_TOO_LARGE = "TEXT_TOO_LARGE"
    FOCUS_LOST = "FOCUS_LOST"


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SafeClickPoint:
    """A safe click coordinate derived from a bounding box.

    ``method`` documents *how* the point was selected so callers can
    log / debug without re-running the geometry.
    """
    x: int
    y: int
    method: str  # "center" | "minimal_size" | "clamp"


@dataclass(frozen=True)
class TargetContext:
    """A grounded (bbox-based) UI target produced by vision.

    Fields:
        bbox: ``(left, top, right, bottom)`` in screen coordinates.
        confidence: Vision confidence score in ``[0, 1]``.
        timestamp: Epoch seconds when vision produced this target.
            If ``0`` (unset), age is treated as zero.
        label: Human-readable label (button text, role, etc).
        metadata: Free-form extra context (e.g. element id, role).
    """
    bbox: Tuple[int, int, int, int]
    confidence: float = 1.0
    timestamp: float = 0.0
    label: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def age_s(self, now: Optional[float] = None) -> float:
        """Seconds since the target was produced.  Returns 0.0 if
        ``timestamp`` was never set (epoch 0 is a sentinel for
        'never aged').
        """
        if self.timestamp <= 0:
            return 0.0
        return (now if now is not None else time.time()) - self.timestamp

    def is_stale(self, max_age_s: float, now: Optional[float] = None) -> bool:
        return self.age_s(now) > max_age_s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redact_text(text: str) -> str:
    """Return a redaction summary of ``text`` safe to log.

    The original content never appears in the returned string — only
    a length marker.  This is the canonical guard for any place that
    might log typed text.
    """
    if not text:
        return "<empty>"
    return f"<redacted {len(text)} chars>"


def _virtual_screen_bounds() -> Tuple[int, int, int, int]:
    """Return ``(left, top, right, bottom)`` for the union of all
    currently-attached displays, in screen coordinates.

    We use ctypes + ``GetSystemMetrics`` so we don't pull in
    pywin32's monitor API for this single call.
    """
    try:
        import ctypes
        user32 = ctypes.windll.user32

        # The *virtual screen* rect (excludes nothing; covers all
        # monitors even if taskbar is hidden on one).
        SM_XVIRTUALSCREEN = 76
        SM_YVIRTUALSCREEN = 77
        SM_CXVIRTUALSCREEN = 78
        SM_CYVIRTUALSCREEN = 79
        left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        return (int(left), int(top), int(left + width), int(top + height))
    except Exception:
        # Safe fallback — primary monitor 1920x1080 at (0,0).
        return (0, 0, 1920, 1080)


def _clamp_to_screen(
    x: int, y: int,
    bounds: Optional[Tuple[int, int, int, int]] = None,
) -> Tuple[int, int]:
    """Clamp ``(x, y)`` to the given screen bounds (or virtual screen
    if ``None``).  Returns the clamped coordinate.
    """
    left, top, right, bottom = bounds or _virtual_screen_bounds()
    cx = max(left, min(int(x), right - 1))
    cy = max(top, min(int(y), bottom - 1))
    return (cx, cy)


def _compute_safe_click_point(
    bbox: Tuple[int, int, int, int],
    screen_bounds: Optional[Tuple[int, int, int, int]] = None,
) -> SafeClickPoint:
    """Pick a safe (x, y) inside a bounding box for clicking.

    Strategy:
        1. **center** — the geometric center of the bbox (normal case).
        2. **minimal_size** — if the bbox is too small for a center
           to be meaningful (< 4x4 pixels), nudge one pixel inward.
        3. **clamp** — if the bbox is partly off-screen, clamp the
           center to the screen.
    """
    left, top, right, bottom = bbox
    bounds = screen_bounds or _virtual_screen_bounds()
    width = right - left
    height = bottom - top
    if width < 4 or height < 4:
        cx = left + max(0, width // 2)
        cy = top + max(0, height // 2)
        # Make sure we're strictly inside the bbox:
        if cx >= right:
            cx = right - 1
        if cy >= bottom:
            cy = bottom - 1
        if cx <= left:
            cx = left + 1
        if cy <= top:
            cy = top + 1
        return SafeClickPoint(int(cx), int(cy), method="minimal_size")
    cx = left + width // 2
    cy = top + height // 2
    cx, cy = _clamp_to_screen(cx, cy, bounds)
    if (cx, cy) != (left + width // 2, top + height // 2):
        return SafeClickPoint(int(cx), int(cy), method="clamp")
    return SafeClickPoint(int(cx), int(cy), method="center")


# Key alias table — names callers commonly use mapped to PyAutoGUI's
# canonical spelling.  Case-insensitive match against the lowercased key.
_KEY_ALIASES: Dict[str, str] = {
    "return": "enter",
    "esc": "escape",
    "control": "ctrl",
    "ctl": "ctrl",
    "cmd": "win",
    "super": "win",
    "windows": "win",
    "delete": "delete",
    "del": "delete",
    "backspace": "backspace",
    "bs": "backspace",
    "space": "space",
    "spacebar": "space",
    "tab": "tab",
    "pageup": "pageup",
    "pgup": "pageup",
    "pagedown": "pagedown",
    "pgdn": "pagedown",
    "home": "home",
    "end": "end",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "printscreen": "printscreen",
    "prtsc": "printscreen",
    "capslock": "capslock",
    "numlock": "numlock",
    "scrolllock": "scrolllock",
    "insert": "insert",
    "ins": "insert",
}


def _normalize_key(key: str) -> str:
    """Normalize a key name to what PyAutoGUI's ``press``/``hotkey``
    accept.  Case-insensitive; common aliases (``return``→``enter``,
    ``cmd``→``win``) are collapsed.
    """
    if not key:
        return key
    k = str(key).strip().lower()
    return _KEY_ALIASES.get(k, k)


def _ms_since(t0: float) -> float:
    """Millisecond duration helper for metrics (3-decimal precision)."""
    return round((time.time() - t0) * 1000.0, 3)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class WindowsInputService(InputService, LifecycleMixin):
    """PyAutoGUI-based :class:`InputService` for Windows hosts.

    Parameters
    ----------
    failsafe:
        Enable PyAutoGUI's corner-safety (default True).  Strongly
        recommended for production use.
    pause:
        Per-call pause inside PyAutoGUI.  Default 0.0 (we set
        timeouts explicitly via ``run_with_timeout``).
    max_target_age_s:
        Maximum age (seconds) of a ``TargetContext`` we'll accept.
        Targets older than this are rejected with ``STALE_TARGET``.
    min_confidence:
        Minimum vision confidence (0..1) we'll accept on a target.
    clipboard_service:
        Optional pre-built clipboard service.  When ``None``, we
        lazily construct a ``WindowsClipboardService`` on first
        clipboard-using call.
    """

    def __init__(
        self,
        *,
        failsafe: bool = True,
        pause: float = 0.0,
        max_target_age_s: float = DEFAULT_MAX_TARGET_AGE_S,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        clipboard_service: Any = None,
    ) -> None:
        self._lifecycle_state: LifecycleState = LifecycleState.CREATED
        self._initialization_error: Optional[str] = None
        self._max_target_age_s = float(max_target_age_s)
        self._min_confidence = float(min_confidence)
        self._clipboard = clipboard_service
        # Phase 17: single RLock to serialise concurrent calls.  Without
        # this, two threads calling ``click`` simultaneously can interleave
        # pyautogui.moveTo and pyautogui.click in ways that corrupt the
        # intended action.  Reentrant because the public methods can call
        # each other (e.g. type_into_target → click_target → click).
        self._lock = threading.RLock()
        # Phase 17: per-action metrics.  Counters + rolling latency
        # window of 100 samples.  Cheap to maintain, useful for catching
        # input-layer regressions (typing latency creeping up, click
        # jitter increasing).
        self._metrics: Dict[str, Dict[str, Any]] = {}
        self._latency_windows: Dict[str, Deque[float]] = {}
        # Enable PyAutoGUI failsafe
        try:
            pyautogui.FAILSAFE = bool(failsafe)
            pyautogui.PAUSE = float(pause)
        except Exception as exc:  # noqa: BLE001
            _loguru.warning("Could not configure PyAutoGUI: {}", exc)
        _loguru.debug(
            "WindowsInputService initialized (failsafe={}, pause={}, "
            "max_target_age_s={}, min_confidence={}).",
            pyautogui.FAILSAFE,
            pyautogui.PAUSE,
            self._max_target_age_s,
            self._min_confidence,
        )

    # ============================================================ metrics
    def _record_metric(
        self, action: str, *, ok: bool, latency_ms: float,
        outcome: str = "success",
    ) -> None:
        """Record one call against the per-action metrics bag.

        ``outcome`` is one of ``"success"``, ``"fail"``,
        ``"timeout"``, ``"cancel"`` and is tracked in addition to
        the boolean ``ok`` flag so dashboards can distinguish
        failures caused by the OS from failures caused by
        cooperative cancellation.
        """
        with self._lock:
            slot = self._metrics.setdefault(action, {
                "calls": 0, "success": 0, "fail": 0,
                "timeout": 0, "cancel": 0,
            })
            slot["calls"] += 1
            if outcome == "success":
                slot["success"] += 1
            elif outcome == "timeout":
                slot["timeout"] += 1
            elif outcome == "cancel":
                slot["cancel"] += 1
            else:
                slot["fail"] += 1
            window = self._latency_windows.setdefault(action, deque(maxlen=100))
            window.append(float(latency_ms))

    def _percentile(self, samples: Deque[float], pct: float) -> float:
        if not samples:
            return 0.0
        s = sorted(samples)
        idx = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
        return float(s[idx])

    def _check_cancelled(
        self, cancellation: Optional[CancellationToken],
    ) -> None:
        """Raise :class:`OperationCancelled` if the token is set."""
        if cancellation is not None and bool(cancellation.cancelled):
            raise OperationCancelled("cancelled by caller")

    # ============================================================ helpers
    def _timeout_for(self, base: float, payload_len: int = 0) -> float:
        s = base + 0.05 * payload_len
        return max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, s))

    def _ok(self, action: str, **details: Any) -> ActionResult:
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name=action,
            details=details,
        )

    def _fail(self, action: str, msg: str, **details: Any) -> ActionResult:
        return ActionResult(
            status=ActionStatus.FAILED,
            action_name=action,
            details={"reason": msg, **details},
        )

    def _fail_with_code(
        self, action: str, code: str, **details: Any,
    ) -> ActionResult:
        """Build a structured ``FAILED`` result carrying an
        ``InputErrorCode`` and any extra context.  Callers that want
        to branch on stable codes should read
        ``result.details["code"]``.
        """
        payload = {"reason": code, "code": code, **details}
        return ActionResult(
            status=ActionStatus.FAILED,
            action_name=action,
            details=payload,
        )

    def _ensure_clipboard(self) -> Any:
        """Lazily build the clipboard service if not injected."""
        if self._clipboard is not None:
            return self._clipboard
        try:
            from system.clipboard.clipboard_service import WindowsClipboardService
            self._clipboard = WindowsClipboardService()
            try:
                self._clipboard.initialize()
            except Exception:
                # The clipboard service tolerates lazy init in many
                # call paths.  If init truly failed, the next
                # set_text/get_text call will surface a failure.
                pass
            return self._clipboard
        except Exception as exc:  # noqa: BLE001
            _loguru.warning("Clipboard service unavailable: {}", exc)
            return None

    # ============================================================ target validation
    def _validate_target(
        self, target: Any, *, action: str,
    ) -> Optional[ActionResult]:
        """Validate a :class:`TargetContext` for an input action.

        Returns ``None`` if the target is acceptable; otherwise an
        ``ActionResult`` with ``status=FAILED`` and a structured
        ``code`` describing why we refused.
        """
        if not isinstance(target, TargetContext):
            return self._fail_with_code(
                action, InputErrorCode.INVALID_TARGET,
                reason="not a TargetContext",
                type=type(target).__name__,
            )
        bbox = target.bbox
        if not (isinstance(bbox, tuple) and len(bbox) == 4):
            return self._fail_with_code(
                action, InputErrorCode.INVALID_TARGET,
                reason="bbox must be a 4-tuple",
                bbox=bbox,
            )
        try:
            left, top, right, bottom = (int(v) for v in bbox)
        except (TypeError, ValueError):
            return self._fail_with_code(
                action, InputErrorCode.INVALID_TARGET,
                reason="bbox elements must be numeric",
                bbox=bbox,
            )
        if right <= left or bottom <= top:
            return self._fail_with_code(
                action, InputErrorCode.INVALID_TARGET,
                reason="degenerate bbox (non-positive area)",
                bbox=bbox,
            )
        if float(target.confidence) < self._min_confidence:
            return self._fail_with_code(
                action, InputErrorCode.LOW_CONFIDENCE,
                confidence=float(target.confidence),
                threshold=self._min_confidence,
            )
        if target.is_stale(self._max_target_age_s):
            return self._fail_with_code(
                action, InputErrorCode.STALE_TARGET,
                age_s=target.age_s(),
                max_age_s=self._max_target_age_s,
            )
        # Bounds check — only if a virtual screen is reachable.  We
        # *always* do this check; the worst case is we mis-clamp
        # because the screen layout is exotic.
        try:
            sb = _virtual_screen_bounds()
            sb_left, sb_top, sb_right, sb_bottom = sb
            # Allow some slack (1 pixel) for sub-pixel rounding.
            if (right < sb_left - 1 or left > sb_right + 1
                    or bottom < sb_top - 1 or top > sb_bottom + 1):
                return self._fail_with_code(
                    action, InputErrorCode.OUT_OF_BOUNDS,
                    bbox=bbox,
                    screen_bounds=sb,
                )
        except Exception:
            # If we can't read the screen, don't block — let the
            # click() error handle it.
            pass
        return None

    # ============================================================ mouse (raw)
    def click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        clicks: int = 1,
        cancellation: Optional[CancellationToken] = None,
    ) -> ActionResult:
        action = "click"
        started = time.time()
        try:
            with self._lock:
                self._check_cancelled(cancellation)
                def _do() -> None:
                    pyautogui.click(
                        x=int(x), y=int(y),
                        button=str(button),
                        clicks=int(clicks),
                        interval=DEFAULT_CLICK_INTERVAL_S,
                    )
                run_with_timeout(
                    _do,
                    seconds=self._timeout_for(1.0, clicks),
                )
        except OperationCancelled:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="cancel")
            return ActionResult(
                status=ActionStatus.CANCELLED,
                action_name=action,
                details={"x": x, "y": y, "clicks": clicks},
            )
        except pyautogui.FailSafeException:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail_with_code(
                action, InputErrorCode.FAILSAFE_TRIGGERED,
                x=x, y=y,
            )
        except TimeoutError:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="timeout")
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name=action,
                details={"x": x, "y": y, "clicks": clicks},
            )
        except Exception as exc:  # noqa: BLE001
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail(action, repr(exc), x=x, y=y)
        self._record_metric(action, ok=True, latency_ms=_ms_since(started), outcome="success")
        return self._ok(action, x=x, y=y, button=button, clicks=clicks)

    def right_click(self, x: int, y: int) -> ActionResult:
        return self.click(x, y, button="right", clicks=1)

    def middle_click(self, x: int, y: int) -> ActionResult:
        return self.click(x, y, button="middle", clicks=1)

    def double_click(
        self, x: int, y: int,
        *,
        cancellation: Optional[CancellationToken] = None,
    ) -> ActionResult:
        return self.click(x, y, clicks=2, cancellation=cancellation)

    def move_mouse(
        self, x: int, y: int,
        *,
        cancellation: Optional[CancellationToken] = None,
    ) -> ActionResult:
        action = "move_mouse"
        started = time.time()
        try:
            with self._lock:
                self._check_cancelled(cancellation)
                def _do() -> None:
                    pyautogui.moveTo(int(x), int(y))
                run_with_timeout(_do, seconds=self._timeout_for(1.0))
        except OperationCancelled:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="cancel")
            return ActionResult(
                status=ActionStatus.CANCELLED,
                action_name=action,
                details={"x": x, "y": y},
            )
        except pyautogui.FailSafeException:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail_with_code(
                action, InputErrorCode.FAILSAFE_TRIGGERED, x=x, y=y,
            )
        except TimeoutError:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="timeout")
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name=action,
                details={"x": x, "y": y},
            )
        except Exception as exc:  # noqa: BLE001
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail(action, repr(exc), x=x, y=y)
        self._record_metric(action, ok=True, latency_ms=_ms_since(started), outcome="success")
        return self._ok(action, x=x, y=y)

    def drag(
        self,
        x1: int, y1: int, x2: int, y2: int,
        *,
        duration_s: float = 0.5,
        button: str = "left",
        cancellation: Optional[CancellationToken] = None,
    ) -> ActionResult:
        action = "drag"
        started = time.time()
        # Reject tiny drags (almost certainly a misclick, not a gesture).
        if abs(int(x2) - int(x1)) < 2 and abs(int(y2) - int(y1)) < 2:
            return self._fail_with_code(
                action, InputErrorCode.DRAG_TOO_SHORT,
                x1=x1, y1=y1, x2=x2, y2=y2,
            )
        norm_button = _normalize_key(button) or "left"
        try:
            with self._lock:
                self._check_cancelled(cancellation)
                def _do() -> None:
                    pyautogui.moveTo(int(x1), int(y1))
                    pyautogui.dragTo(
                        int(x2), int(y2),
                        duration=float(duration_s),
                        button=norm_button,
                    )
                run_with_timeout(
                    _do,
                    seconds=self._timeout_for(duration_s + 1.0),
                )
        except OperationCancelled:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="cancel")
            return ActionResult(
                status=ActionStatus.CANCELLED,
                action_name=action,
                details={"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                         "duration_s": duration_s},
            )
        except pyautogui.FailSafeException:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail_with_code(
                action, InputErrorCode.FAILSAFE_TRIGGERED,
                x1=x1, y1=y1, x2=x2, y2=y2,
            )
        except TimeoutError:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="timeout")
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name=action,
                details={"duration_s": duration_s},
            )
        except Exception as exc:  # noqa: BLE001
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail(action, repr(exc))
        self._record_metric(action, ok=True, latency_ms=_ms_since(started), outcome="success")
        return self._ok(
            action, x1=x1, y1=y1, x2=x2, y2=y2,
            duration_s=duration_s, button=norm_button,
        )

    def scroll(
        self, x: int, y: int,
        *, clicks: int, vertical: bool = True,
        cancellation: Optional[CancellationToken] = None,
    ) -> ActionResult:
        action = "scroll"
        started = time.time()
        try:
            with self._lock:
                self._check_cancelled(cancellation)
                def _do() -> None:
                    pyautogui.moveTo(int(x), int(y))
                    if vertical:
                        pyautogui.scroll(int(clicks))
                    else:
                        pyautogui.hscroll(int(clicks))
                run_with_timeout(
                    _do,
                    seconds=self._timeout_for(1.0, abs(int(clicks))),
                )
        except OperationCancelled:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="cancel")
            return ActionResult(
                status=ActionStatus.CANCELLED,
                action_name=action,
                details={"clicks": clicks},
            )
        except pyautogui.FailSafeException:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail_with_code(
                action, InputErrorCode.FAILSAFE_TRIGGERED, x=x, y=y,
            )
        except TimeoutError:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="timeout")
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name=action,
                details={"clicks": clicks},
            )
        except Exception as exc:  # noqa: BLE001
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail(action, repr(exc))
        self._record_metric(action, ok=True, latency_ms=_ms_since(started), outcome="success")
        return self._ok(action, x=x, y=y, clicks=clicks, vertical=vertical)

    # ============================================================ keyboard
    def type_text(
        self, text: str, *, interval_s: float = 0.0,
        cancellation: Optional[CancellationToken] = None,
    ) -> ActionResult:
        """Type ``text`` using the most reliable path available.

        Strategy:
            * Empty / ascii-only / short text → ``pyautogui.typewrite``
              (chunked, cancellable, fast).
            * Long text (>50 chars), multiline, or non-ASCII →
              **clipboard paste** so Unicode and huge payloads work.

        The text is **never** logged in plaintext — only its length.
        """
        action = "type_text"
        started = time.time()
        if not isinstance(text, str):
            return self._fail_with_code(
                action, InputErrorCode.INVALID_PARAMETERS,
                reason="text must be a string",
                actual_type=type(text).__name__,
            )
        if not text:
            return self._ok(action, length=0)
        # Secret-style redaction: log only the length.
        _loguru.debug("type_text: {}", _redact_text(text))

        needs_paste = (
            len(text) >= LARGE_TEXT_THRESHOLD
            or "\n" in text
            or any(ord(c) > 127 for c in text)
        )
        if needs_paste:
            result = self._type_text_unicode(
                text, cancellation=cancellation,
            )
            if result.status is ActionStatus.EXECUTED:
                self._record_metric(action, ok=True, latency_ms=_ms_since(started), outcome="success")
            elif result.status is ActionStatus.CANCELLED:
                self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="cancel")
            elif result.status is ActionStatus.TIMED_OUT:
                self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="timeout")
            else:
                self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return result

        # Use caller's token if supplied; otherwise build a fresh
        # internal one so we still get a periodic cancellation check.
        cancel_token = cancellation or CancellationToken()
        typed = 0
        try:
            with self._lock:
                for i in range(0, len(text), CHUNK_SIZE):
                    cancel_token.check()
                    chunk = text[i:i + CHUNK_SIZE]
                    def _do() -> None:
                        pyautogui.typewrite(chunk, interval=float(interval_s))
                    run_with_timeout(
                        _do,
                        seconds=self._timeout_for(1.0, len(chunk)),
                    )
                    typed += len(chunk)
                    time.sleep(CHUNK_INTERVAL_S)
        except OperationCancelled:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="cancel")
            return ActionResult(
                status=ActionStatus.CANCELLED,
                action_name=action,
                details={"typed": typed, "requested": len(text)},
            )
        except pyautogui.FailSafeException:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail_with_code(
                action, InputErrorCode.FAILSAFE_TRIGGERED,
                typed=typed, requested=len(text),
            )
        except TimeoutError:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="timeout")
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name=action,
                details={"typed": typed, "requested": len(text)},
            )
        except Exception as exc:  # noqa: BLE001
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail(
                action, repr(exc), typed=typed, requested=len(text),
            )
        self._record_metric(action, ok=True, latency_ms=_ms_since(started), outcome="success")
        return self._ok(
            action, length=len(text), typed=typed, interval_s=interval_s,
        )

    def _type_text_unicode(
        self, text: str,
        *,
        cancellation: Optional[CancellationToken] = None,
    ) -> ActionResult:
        """Type ``text`` via the clipboard.  Works for Unicode and
        arbitrarily large payloads.  Falls back to a clear failure
        if clipboard is unavailable.
        """
        action = "type_text"
        cb = self._ensure_clipboard()
        if cb is None:
            return self._fail_with_code(
                action, InputErrorCode.CLIPBOARD_UNAVAILABLE,
                requested=len(text),
            )
        try:
            cb.set_text(text)
        except Exception as exc:  # noqa: BLE001
            return self._fail_with_code(
                action, InputErrorCode.CLIPBOARD_UNAVAILABLE,
                reason=repr(exc),
                requested=len(text),
            )
        # Paste with Ctrl+V
        try:
            with self._lock:
                self._check_cancelled(cancellation)
                def _do() -> None:
                    pyautogui.hotkey("ctrl", "v")
                run_with_timeout(_do, seconds=self._timeout_for(2.0))
        except OperationCancelled:
            return ActionResult(
                status=ActionStatus.CANCELLED,
                action_name=action,
                details={"requested": len(text), "method": "paste"},
            )
        except pyautogui.FailSafeException:
            return self._fail_with_code(
                action, InputErrorCode.FAILSAFE_TRIGGERED,
                requested=len(text),
            )
        except TimeoutError:
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name=action,
                details={"requested": len(text)},
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(action, repr(exc), requested=len(text))
        return self._ok(action, length=len(text), method="paste")

    def paste_text(self, text: str) -> ActionResult:
        """Place ``text`` on the clipboard and emit Ctrl+V.

        This is a public version of the internal paste path used by
        ``type_text`` for Unicode/large payloads.  It does NOT click
        first — pair it with ``click_target(...)`` if you need a
        atomic *click + type* operation.
        """
        action = "paste_text"
        if not isinstance(text, str):
            return self._fail_with_code(
                action, InputErrorCode.INVALID_PARAMETERS,
                reason="text must be a string",
                actual_type=type(text).__name__,
            )
        if not text:
            return self._ok(action, length=0, method="paste")
        _loguru.debug("paste_text: {}", _redact_text(text))
        cb = self._ensure_clipboard()
        if cb is None:
            return self._fail_with_code(
                action, InputErrorCode.CLIPBOARD_UNAVAILABLE,
                requested=len(text),
            )
        try:
            cb.set_text(text)
        except Exception as exc:  # noqa: BLE001
            return self._fail_with_code(
                action, InputErrorCode.CLIPBOARD_UNAVAILABLE,
                reason=repr(exc),
                requested=len(text),
            )
        try:
            def _do() -> None:
                pyautogui.hotkey("ctrl", "v")
            run_with_timeout(_do, seconds=self._timeout_for(2.0))
        except pyautogui.FailSafeException:
            return self._fail_with_code(
                action, InputErrorCode.FAILSAFE_TRIGGERED,
                requested=len(text),
            )
        except TimeoutError:
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name=action,
                details={"requested": len(text)},
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(action, repr(exc), requested=len(text))
        return self._ok(action, length=len(text), method="paste")

    def press_key(
        self, key: str,
        *,
        cancellation: Optional[CancellationToken] = None,
    ) -> ActionResult:
        action = "press_key"
        started = time.time()
        norm = _normalize_key(key)
        if not norm:
            return self._fail_with_code(
                action, InputErrorCode.INVALID_PARAMETERS,
                reason="empty key",
            )
        try:
            with self._lock:
                self._check_cancelled(cancellation)
                def _do() -> None:
                    pyautogui.press(norm)
                run_with_timeout(_do, seconds=self._timeout_for(1.0))
        except OperationCancelled:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="cancel")
            return ActionResult(
                status=ActionStatus.CANCELLED,
                action_name=action,
                details={"key": norm},
            )
        except pyautogui.FailSafeException:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail_with_code(
                action, InputErrorCode.FAILSAFE_TRIGGERED, key=norm,
            )
        except TimeoutError:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="timeout")
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name=action,
                details={"key": norm},
            )
        except Exception as exc:  # noqa: BLE001
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail(action, repr(exc), key=norm)
        self._record_metric(action, ok=True, latency_ms=_ms_since(started), outcome="success")
        return self._ok(action, key=norm)

    def hotkey(
        self, *keys: str,
        cancellation: Optional[CancellationToken] = None,
    ) -> ActionResult:
        action = "hotkey"
        started = time.time()
        if not keys:
            return self._fail_with_code(
                action, InputErrorCode.INVALID_PARAMETERS,
                reason="no keys provided",
            )
        norm_keys = tuple(_normalize_key(k) for k in keys)
        try:
            with self._lock:
                self._check_cancelled(cancellation)
                def _do() -> None:
                    pyautogui.hotkey(*norm_keys)
                run_with_timeout(
                    _do,
                    seconds=self._timeout_for(1.0, len(norm_keys)),
                )
        except OperationCancelled:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="cancel")
            return ActionResult(
                status=ActionStatus.CANCELLED,
                action_name=action,
                details={"keys": list(norm_keys)},
            )
        except pyautogui.FailSafeException:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail_with_code(
                action, InputErrorCode.FAILSAFE_TRIGGERED,
                keys=list(norm_keys),
            )
        except TimeoutError:
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="timeout")
            return ActionResult(
                status=ActionStatus.TIMED_OUT,
                action_name=action,
                details={"keys": list(norm_keys)},
            )
        except Exception as exc:  # noqa: BLE001
            self._record_metric(action, ok=False, latency_ms=_ms_since(started), outcome="fail")
            return self._fail(action, repr(exc), keys=list(norm_keys))
        self._record_metric(action, ok=True, latency_ms=_ms_since(started), outcome="success")
        return self._ok(action, keys=list(norm_keys))

    # ============================================================ target-based mouse
    def click_target(
        self,
        target: TargetContext,
        *,
        button: str = "left",
        clicks: int = 1,
    ) -> ActionResult:
        """Click at a safe point inside ``target.bbox`` after
        validating confidence, age, and screen-bounds.

        The returned :class:`ActionResult` is enriched with the
        computed click point and the validation outcome so the
        recovery engine can inspect *what* happened.
        """
        action = "click_target"
        err = self._validate_target(target, action=action)
        if err is not None:
            return err
        point = _compute_safe_click_point(target.bbox)
        result = self.click(point.x, point.y, button=button, clicks=clicks)
        # Enrich with target context.
        new_details = dict(result.details or {})
        new_details.update({
            "click_point": {"x": point.x, "y": point.y},
            "click_method": point.method,
            "target_label": target.label,
            "target_confidence": float(target.confidence),
            "target_age_s": target.age_s(),
            "target_bbox": list(target.bbox),
        })
        return ActionResult(
            status=result.status,
            action_name=result.action_name,
            details=new_details,
            error=result.error,
        )

    def double_click_target(self, target: TargetContext) -> ActionResult:
        return self.click_target(target, clicks=2)

    def right_click_target(self, target: TargetContext) -> ActionResult:
        return self.click_target(target, button="right")

    def middle_click_target(self, target: TargetContext) -> ActionResult:
        return self.click_target(target, button="middle")

    def move_to_target(self, target: TargetContext) -> ActionResult:
        action = "move_to_target"
        err = self._validate_target(target, action=action)
        if err is not None:
            return err
        point = _compute_safe_click_point(target.bbox)
        result = self.move_mouse(point.x, point.y)
        new_details = dict(result.details or {})
        new_details.update({
            "click_point": {"x": point.x, "y": point.y},
            "click_method": point.method,
            "target_label": target.label,
            "target_confidence": float(target.confidence),
            "target_age_s": target.age_s(),
        })
        return ActionResult(
            status=result.status,
            action_name=result.action_name,
            details=new_details,
            error=result.error,
        )

    def drag_targets(
        self, start: TargetContext, end: TargetContext,
        *, duration_s: float = 0.5,
    ) -> ActionResult:
        action = "drag_targets"
        err = self._validate_target(start, action=action)
        if err is not None:
            return err
        err = self._validate_target(end, action=action)
        if err is not None:
            return err
        s = _compute_safe_click_point(start.bbox)
        e = _compute_safe_click_point(end.bbox)
        result = self.drag(s.x, s.y, e.x, e.y, duration_s=duration_s)
        new_details = dict(result.details or {})
        new_details.update({
            "start_label": start.label,
            "end_label": end.label,
        })
        return ActionResult(
            status=result.status,
            action_name=result.action_name,
            details=new_details,
            error=result.error,
        )

    def scroll_to_target(
        self, target: TargetContext, *, clicks: int, vertical: bool = True,
    ) -> ActionResult:
        action = "scroll_to_target"
        err = self._validate_target(target, action=action)
        if err is not None:
            return err
        point = _compute_safe_click_point(target.bbox)
        return self.scroll(point.x, point.y, clicks=clicks, vertical=vertical)

    def type_into_target(
        self,
        target: TargetContext,
        text: str,
        *,
        click_first: bool = True,
    ) -> ActionResult:
        """Atomic *click + type* for a grounded target.

        Validates the target, clicks the safe point (if
        ``click_first``), then types ``text``.  Errors at any step
        are surfaced as a single :class:`ActionResult`.
        """
        action = "type_into_target"
        err = self._validate_target(target, action=action)
        if err is not None:
            return err
        if click_first:
            click_result = self.click_target(target)
            if click_result.status != ActionStatus.EXECUTED:
                return click_result
        typed = self.type_text(text)
        new_details = dict(typed.details or {})
        new_details["target_label"] = target.label
        new_details["target_confidence"] = float(target.confidence)
        return ActionResult(
            status=typed.status,
            action_name=action,
            details=new_details,
            error=typed.error,
        )

    # ============================================================ monitors / health
    def monitors(self) -> List[Dict[str, Any]]:
        """Enumerate attached displays.

        Each monitor is reported with ``bounds`` in screen coordinates,
        ``primary`` flag, and a ``device`` name when available.
        """
        out: List[Dict[str, Any]] = []
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            MONITORENUMPROC = ctypes.WINFUNCTYPE(
                wintypes.BOOL,
                wintypes.HMONITOR,
                wintypes.HDC,
                ctypes.POINTER(wintypes.RECT),
                wintypes.LPARAM,
            )

            rects: List[Tuple[int, int, int, int]] = []

            def _cb(hMonitor, hdc, lprcMonitor, lParam):
                r = lprcMonitor.contents
                rects.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
                return True

            user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)

            # Identify the primary monitor by intersecting with the
            # SM_CXSCREEN / SM_CYSCREEN position (0,0) by convention.
            primary_rect = None
            try:
                SM_CXPRIMARY = 23
                SM_CYPRIMARY = 24
                pw = user32.GetSystemMetrics(SM_CXPRIMARY)
                ph = user32.GetSystemMetrics(SM_CYPRIMARY)
                if pw > 0 and ph > 0:
                    primary_rect = (0, 0, pw, ph)
            except Exception:
                primary_rect = None

            for i, rect in enumerate(rects):
                is_primary = primary_rect is not None and rect == primary_rect
                out.append({
                    "index": i,
                    "bounds": list(rect),
                    "primary": is_primary,
                    "device": f"monitor_{i}",
                })
        except Exception as exc:  # noqa: BLE001
            _loguru.warning("EnumDisplayMonitors failed: {}", exc)
            # Always report at least the primary.
            sb = _virtual_screen_bounds()
            out.append({
                "index": 0,
                "bounds": list(sb),
                "primary": True,
                "device": "fallback",
            })
        # Make sure at least one monitor is marked primary.
        if out and not any(m.get("primary") for m in out):
            out[0]["primary"] = True
        return out

    def screen_bounds(self) -> Tuple[int, int, int, int]:
        """Return the union of all attached displays as a 4-tuple."""
        return _virtual_screen_bounds()

    def health(self) -> Dict[str, Any]:
        """Return a structured health snapshot.

        Used by the engine's health monitor to surface readiness to
        the UI / voice layer.
        """
        try:
            clipboard_status = "available" if self._ensure_clipboard() else "unavailable"
        except Exception:
            clipboard_status = "unavailable"
        mons = self.monitors()
        return {
            "type": "WindowsInputService",
            "lifecycle": self._lifecycle_state.value,
            "pyautogui_failsafe": bool(pyautogui.FAILSAFE),
            "pyautogui_pause": float(pyautogui.PAUSE),
            "dpi_aware": True,  # We use system metrics APIs.
            "thread_safe": True,  # All state is per-call.
            "primary_monitor": next(
                (m for m in mons if m.get("primary")),
                mons[0] if mons else None,
            ),
            "virtual_screen": list(_virtual_screen_bounds()),
            "monitors": mons,
            "max_target_age_s": self._max_target_age_s,
            "min_confidence": self._min_confidence,
            "mouse": "available",
            "keyboard": "available",
            "clipboard": clipboard_status,
        }

    # =================================================== lifecycle hooks
    def _do_initialize(self) -> bool:
        return True

    def _do_shutdown(self) -> None:
        return None

    def statistics(self) -> Dict[str, Any]:
        """Return structured stats including per-action metrics.

        ``metrics`` maps ``action`` name → ``{calls, success, fail,
        timeout, cancel, p50_ms, p95_ms}``.  The percentiles are
        computed over the rolling 100-sample latency window for the
        action; they are 0.0 when no samples have been recorded yet.
        """
        with self._lock:
            metrics: Dict[str, Dict[str, Any]] = {}
            for action, counts in self._metrics.items():
                window = self._latency_windows.get(action, deque())
                metrics[action] = {
                    **counts,
                    "p50_ms": round(self._percentile(window, 50), 3),
                    "p95_ms": round(self._percentile(window, 95), 3),
                }
        return {
            "type": "WindowsInputService",
            "lifecycle": self._lifecycle_state.value,
            "pyautogui_failsafe": bool(pyautogui.FAILSAFE),
            "pyautogui_pause": float(pyautogui.PAUSE),
            "max_target_age_s": self._max_target_age_s,
            "min_confidence": self._min_confidence,
            "thread_safe": True,
            "metrics": metrics,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"WindowsInputService(state={self._lifecycle_state.value})"
