"""
ScreenshotProvider Protocol for V6 Phase 7.1.

This is the minimum seam between :class:`VisionService` and the rest
of the architecture.  Vision does not depend on :class:`OmnixEngine`
or on the closed capability set; it depends on a Protocol with a
single method, :meth:`capture`.

The default implementation (:class:`CapabilityScreenshotProvider`)
wraps the existing ``desktop.screenshot`` capability, so the closed
capability set is still the *only* allowed path to the screen --
Vision merely depends on the abstraction, not on the engine.

R-14: Vision is a service, not a singleton.  The provider is an
*injected* dependency of :class:`VisionService`.

R-21 boundary: this Protocol is the *only* contact point between
Vision and the capability layer.  The provider MUST NOT itself call
LLMs, must not call pyautogui directly (use the capability), and
must not execute other capabilities (the closed capability set
enforces that at execution time).
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Optional, Protocol, runtime_checkable

from loguru import logger


@runtime_checkable
class ScreenshotProvider(Protocol):
    """A plug-in for capturing a single screenshot for visual grounding.

    Implementations are responsible for:

      * Returning an absolute path to a PNG file (the screen image).
      * Returning ``None`` when the screen cannot be captured.
      * Not performing any I/O outside of writing the PNG file.

    The implementation MUST NOT call LLMs, pyautogui.mouse, or
    pyautogui.keyboard.  It is purely an image-acquisition seam.
    """

    name: str

    def capture(self, *, path: Optional[str] = None) -> Optional[str]:
        """Capture the current screen and write it to ``path``.

        :param path: Optional destination path.  When ``None``, the
            implementation picks a deterministic location (e.g. under
            the system temp directory).
        :return: The absolute path of the written file, or ``None`` if
            capture failed.
        """
        ...


class CapabilityScreenshotProvider:
    """Default :class:`ScreenshotProvider` backed by the closed capability set.

    The provider forwards to ``desktop.screenshot`` via whatever
    executor was injected (typically :class:`OmnixEngine`).
    """

    name: str = "capability-screenshot"

    def __init__(self, executor: Any) -> None:
        # We only depend on the executor's ``execute`` method; we do
        # NOT import :class:`OmnixEngine` here to keep this seam
        # narrow and testable.
        self._executor = executor

    def capture(self, *, path: Optional[str] = None) -> Optional[str]:
        target = path or os.path.join(
            tempfile.gettempdir(), "omnix_vision_screenshot.png"
        )
        try:
            result = self._executor.execute("desktop.screenshot", path=target)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[vision] screenshot capability raised: {exc!r}")
            return None

        # CapabilityResult.status is an enum with .name (or .value).
        # We accept VERIFIED/EXECUTED; everything else is a fail.
        status = getattr(result, "status", None)
        status_name = getattr(status, "name", str(status) or "")
        if status_name not in ("VERIFIED", "EXECUTED"):
            logger.debug(
                f"[vision] screenshot capability did not succeed: "
                f"status={status_name!r}"
            )
            return None

        if not os.path.exists(target):
            return None
        return target


class NullScreenshotProvider:
    """A no-op :class:`ScreenshotProvider` for tests and headless mode.

    Always returns ``None`` from :meth:`capture`; useful in CI and
    unit tests where a real screen is not available.
    """

    name: str = "null-screenshot"

    def capture(self, *, path: Optional[str] = None) -> Optional[str]:
        return None


__all__ = [
    "ScreenshotProvider",
    "CapabilityScreenshotProvider",
    "NullScreenshotProvider",
    "make_screenshot_provider",
]


# ---------------------------------------------------------------------------
# Canonical construction helper (Phase 11.5)
# ---------------------------------------------------------------------------

def make_screenshot_provider(engine: Any, *, headless: Optional[bool] = None) -> ScreenshotProvider:
    """Return the canonical :class:`ScreenshotProvider` for V6.

    This is the *only* place in V6 that decides which screenshot
    provider a host (the dev runtime, a future GUI, ...) should use
    to back :class:`core.services.vision_service.VisionService`.

    Resolution order:

      1. If ``headless`` is explicitly True OR the environment sets
         ``OMNIX_HEADLESS=1`` OR the engine's
         :class:`core.configuration.OmnixConfig.enable_vision` is
         False, return :class:`NullScreenshotProvider` (no screen).
      2. Otherwise, return :class:`CapabilityScreenshotProvider`
         wrapping the supplied engine, so the closed capability set
         is still the only path to the screen (R-21).

    The helper is intentionally narrow: it does NOT import
    :class:`OmnixEngine` itself, and it does NOT touch
    ``pyautogui`` / ``mss`` directly.  Screenshots always go through
    the canonical ``desktop.screenshot`` capability when running for
    real.
    """
    # 1. Resolve the headless flag.  The caller may pass it explicitly;
    #    otherwise we honour the canonical V6 env var and the
    #    engine's own config (when present).
    is_headless = False
    if headless is True:
        is_headless = True
    elif headless is None:
        try:
            if os.environ.get("OMNIX_HEADLESS", "0").strip() == "1":
                is_headless = True
        except Exception:  # noqa: BLE001
            pass
        if not is_headless:
            try:
                cfg = getattr(engine, "config", None)
                if cfg is not None and getattr(cfg, "enable_vision", True) is False:
                    is_headless = True
            except Exception:  # noqa: BLE001
                pass

    if is_headless or engine is None:
        return NullScreenshotProvider()

    return CapabilityScreenshotProvider(engine)
