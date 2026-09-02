"""
Omnix V6 — Inactivity timer (Part 3).

A small background timer that drives the sleep transition.  The
timer ticks once per second while the system is in an "alive" state
(READY / LISTENING / EXECUTING) and fires after
``timeout_s`` seconds of *no user input, no task activity, and no
active command*.

Pause-while-executing
---------------------

The spec requires that the timer does NOT fire while a task is
running.  The timer watches the runtime state and treats
``EXECUTING`` as a "paused" state — the wall clock keeps moving
but the inactivity accumulator is frozen.  When execution
finishes, the accumulator resumes from where it was.

Reset sources
-------------

* :meth:`reset_for_user_input` — called when the user types
  something or speaks to the system.
* :meth:`reset_for_task_event` — called when a task is *received*
  (i.e. the agent started running) so the timer is paused
  before the user has to think about it.
* :meth:`reset_for_response` — called when a response is
  delivered, even if the user has not produced new input.

The timer is intentionally simple.  There is exactly one
background thread; it sleeps in 1s slices and uses
``RuntimeStateController`` as its source of truth.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from loguru import logger

from core.state.runtime_state import RuntimeState, RuntimeStateController


# Default inactivity timeout.  30s per the Part 3 spec.
DEFAULT_INACTIVITY_S = 30.0
# How often the worker wakes up.  1s is the right balance
# between responsiveness and CPU.
_TICK_S = 1.0


class InactivityTimer:
    """Wall-clock timer that fires ``on_timeout`` after ``timeout_s``
    of inactivity.

    The timer is *not* a hard real-time scheduler.  It checks the
    accumulator once per second and is paused while the runtime
    state is :class:`RuntimeState.EXECUTING`.

    Parameters
    ----------
    controller
        The :class:`RuntimeStateController` whose state controls
        pausing.
    on_timeout
        Callback fired (from the timer thread) when the
        accumulator crosses the threshold.  The callback is
        responsible for transitioning the controller to
        :class:`RuntimeState.SLEEPING`; the timer only notifies.
    timeout_s
        Inactivity threshold in seconds.  Default 30s.
    """

    def __init__(
        self,
        controller: RuntimeStateController,
        *,
        on_timeout: Optional[Callable[[], None]] = None,
        timeout_s: float = DEFAULT_INACTIVITY_S,
    ) -> None:
        self._controller = controller
        self._on_timeout = on_timeout
        self._timeout_s = max(1.0, float(timeout_s))
        self._lock = threading.RLock()
        # Wall-clock seconds of inactivity, frozen while executing.
        self._accumulator_s: float = 0.0
        # True while a task is executing.  The accumulator does not
        # advance while this flag is set.
        self._paused: bool = False
        # Worker state.
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Metrics
        self._ticks_total = 0
        self._timeouts_total = 0
        self._resets_total = 0

    # =============================================================== props
    @property
    def accumulator_s(self) -> float:
        """Current inactivity in seconds.  Frozen while paused."""
        with self._lock:
            return self._accumulator_s

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def remaining_s(self) -> float:
        with self._lock:
            return max(0.0, self._timeout_s - self._accumulator_s)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # =============================================================== public
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="omnix-inactivity-timer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def reset(self) -> None:
        """Reset the accumulator to zero.  Does not change pause state."""
        with self._lock:
            self._accumulator_s = 0.0
            self._resets_total += 1

    def reset_for_user_input(self) -> None:
        """Reset for a fresh user utterance (voice or text)."""
        self.reset()

    def reset_for_response(self) -> None:
        """Reset for a delivered response.  Also clears the pause flag
        defensively in case the response was emitted without a
        follow-up state change."""
        with self._lock:
            self._accumulator_s = 0.0
            self._resets_total += 1

    def reset_for_task_event(self) -> None:
        """Mark a task as executing: pause the timer.  The accumulator
        is *not* reset to zero — the gap between the user input and
        the response should still count against the timer, but no
        new inactivity accrues while the agent is doing work."""
        with self._lock:
            self._paused = True
            self._resets_total += 1

    def mark_task_finished(self) -> None:
        """Mark the task as finished.  Resume the accumulator from
        its current value (it did not advance while paused)."""
        with self._lock:
            self._paused = False

    def set_timeout_s(self, timeout_s: float) -> None:
        with self._lock:
            self._timeout_s = max(1.0, float(timeout_s))
            if self._accumulator_s > self._timeout_s:
                self._accumulator_s = self._timeout_s

    def set_on_timeout(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._on_timeout = callback

    # ============================================================== internals
    def _run(self) -> None:
        logger.debug(
            f"InactivityTimer started (timeout={self._timeout_s}s, "
            f"tick={_TICK_S}s)"
        )
        try:
            while not self._stop.is_set():
                # Block in 1s slices so stop() is responsive.
                if self._stop.wait(timeout=_TICK_S):
                    return
                self._tick()
        finally:
            logger.debug("InactivityTimer stopped")

    def _tick(self) -> None:
        self._ticks_total += 1
        st = self._controller.state
        # Only count inactivity while the system is in an "alive"
        # state.  If we are asleep or in a terminal state the
        # timer is irrelevant.
        if not st.is_alive():
            with self._lock:
                self._accumulator_s = 0.0
            return
        # Pause-while-executing.
        paused = st is RuntimeState.EXECUTING
        with self._lock:
            if paused != self._paused:
                self._paused = paused
            if not self._paused:
                self._accumulator_s += _TICK_S
            acc = self._accumulator_s
            timeout = self._timeout_s
        if acc >= timeout:
            # Fire once.  Reset the accumulator so we do not fire
            # in a tight loop if the controller stays alive.
            with self._lock:
                self._accumulator_s = 0.0
                self._timeouts_total += 1
                cb = self._on_timeout
            logger.info(
                f"InactivityTimer firing (acc={acc:.1f}s >= "
                f"timeout={timeout:.1f}s)"
            )
            if cb is not None:
                try:
                    cb()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"InactivityTimer on_timeout raised: {exc!r}"
                    )

    def statistics(self) -> dict:
        with self._lock:
            return {
                "type": "InactivityTimer",
                "timeout_s": self._timeout_s,
                "accumulator_s": self._accumulator_s,
                "paused": self._paused,
                "ticks_total": self._ticks_total,
                "timeouts_total": self._timeouts_total,
                "resets_total": self._resets_total,
            }


__all__ = ["InactivityTimer", "DEFAULT_INACTIVITY_S"]
