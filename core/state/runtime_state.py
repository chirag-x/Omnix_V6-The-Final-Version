"""
Omnix V6 — Runtime state model (Part 3).

One authoritative state machine for the user-facing runtime.  All
voice / sleep / wake / command-input decisions consult this state;
no subsystem invents its own lifecycle vocabulary.

The state set is intentionally narrow:

    STARTING
        │   (booting services)
        ▼
    INITIALIZING
        │   (services ready, before the wake-up announcement)
        ▼
    READY
        │   (announcement spoken; voice command STT ON; wake listener OFF)
        ▼
    LISTENING  ◀──┐
        │        │   (transient — actively capturing user speech)
        ▼        │
    EXECUTING ───┘
        │   (running a task)
        ▼
    READY

Sleep path:

    READY ──30s inactivity──▶ SLEEPING
        ▲                         │
        │                  (wake word detected)
        │                         ▼
        └─── wake transitions ── WAKING ──▶ READY

The lifecycle semantics are *suspension*, not destruction.  When
Omnix goes to SLEEPING:

  * the wake-word listener remains active
  * command STT is disabled
  * text input is disabled
  * TTS remains available (so the "going to sleep" announcement plays)
  * Brain / Agent / Vision / pipeline remain *constructed*; they are
    not destroyed and not re-built on wake-up.

When Omnix wakes, the same constructed subsystems are re-enabled
and command STT is turned back on.
"""

from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Callable, List, Optional


class RuntimeState(str, Enum):
    """The single, authoritative user-runtime state.

    Values are lowercase strings so they are stable in logs and
    JSON serialisation.  The state name does NOT collide with
    :class:`core.lifecycle.LifecycleState` — that enum tracks the
    engine's internal lifecycle, this one tracks the *user-facing*
    runtime (initialised vs active vs sleeping).
    """

    STARTING = "starting"
    INITIALIZING = "initializing"
    READY = "ready"
    LISTENING = "listening"
    EXECUTING = "executing"
    SLEEPING = "sleeping"
    WAKING = "waking"
    SHUTTING_DOWN = "shutting_down"
    FAILED = "failed"

    # ---- helpers -------------------------------------------------------
    def is_alive(self) -> bool:
        """True when the user can interact with the system."""
        return self in (
            RuntimeState.READY,
            RuntimeState.LISTENING,
            RuntimeState.EXECUTING,
        )

    def is_asleep(self) -> bool:
        return self is RuntimeState.SLEEPING

    def is_sleep_transition(self) -> bool:
        return self in (RuntimeState.SLEEPING, RuntimeState.WAKING)

    def is_terminal(self) -> bool:
        return self in (RuntimeState.SHUTTING_DOWN, RuntimeState.FAILED)


# Subsystem flags surface — what every other layer queries.
# They are derived from RuntimeState but stored explicitly so
# concurrent code can read them without holding the state lock.


class SubsystemFlags:
    """A snapshot of "which subsystem is currently ON?".

    The flags are *derived* from the authoritative
    :class:`RuntimeState` but stored so that hot-path readers do
    not need to take the runtime lock.  Mutation happens only
    inside :class:`RuntimeStateController`.
    """

    __slots__ = (
        "command_input_enabled",
        "wake_listener_enabled",
        "tts_enabled",
        "brain_active",
        "agent_active",
        "vision_active",
        "execution_pipeline_active",
    )

    def __init__(self) -> None:
        self.command_input_enabled: bool = False
        self.wake_listener_enabled: bool = False
        self.tts_enabled: bool = False
        self.brain_active: bool = False
        self.agent_active: bool = False
        self.vision_active: bool = False
        self.execution_pipeline_active: bool = False

    def as_dict(self) -> dict:
        return {
            "command_input": self.command_input_enabled,
            "wake_listener": self.wake_listener_enabled,
            "tts": self.tts_enabled,
            "brain": self.brain_active,
            "agent": self.agent_active,
            "vision": self.vision_active,
            "execution_pipeline": self.execution_pipeline_active,
        }


class RuntimeStateController:
    """The one place that owns :class:`RuntimeState`.

    Subsystems register as listeners so they can reconfigure
    themselves whenever the state changes.  The controller is
    thread-safe; transitions are atomic.
    """

    def __init__(self) -> None:
        self._state: RuntimeState = RuntimeState.STARTING
        self._lock = threading.RLock()
        self._listeners: List[Callable[[RuntimeState, RuntimeState], None]] = []
        self.flags = SubsystemFlags()
        self._last_transition_at: float = time.time()
        self._transition_history: List[tuple] = []

    # --------------------------------------------------------------- state
    @property
    def state(self) -> RuntimeState:
        with self._lock:
            return self._state

    @property
    def is_alive(self) -> bool:
        return self.state.is_alive()

    @property
    def is_asleep(self) -> bool:
        return self.state.is_asleep()

    @property
    def last_transition_at(self) -> float:
        with self._lock:
            return self._last_transition_at

    def add_listener(
        self,
        fn: Callable[[RuntimeState, RuntimeState], None],
    ) -> None:
        """Register a callback invoked on every transition.

        ``fn(old_state, new_state)`` is called outside the lock.
        Listeners that raise are caught and logged; they never
        prevent a transition.
        """
        with self._lock:
            self._listeners.append(fn)

    def transition(self, target: RuntimeState) -> bool:
        """Move to ``target``.  Returns True if the state changed.

        Idempotent: re-entering the current state is a no-op.
        """
        with self._lock:
            if self._state is target:
                return False
            old = self._state
            self._state = target
            self._last_transition_at = time.time()
            self._transition_history.append((old, target, self._last_transition_at))
            # Bound the history.
            if len(self._transition_history) > 64:
                self._transition_history = self._transition_history[-64:]
            # Recompute the derived subsystem flags.
            self._recompute_flags_locked(target)
        # Notify outside the lock.
        self._notify(old, target)
        return True

    # ----------------------------------------------------------- internals
    def _recompute_flags_locked(self, state: RuntimeState) -> None:
        """Update :attr:`flags` to match the new state.

        Rules (per Part 3 spec):

          * READY  / LISTENING / EXECUTING: command input ON, wake
            listener OFF, TTS ON, brain / agent / vision / pipeline
            all active.
          * SLEEPING: command input OFF, wake listener ON, TTS
            available, brain / agent / vision / pipeline suspended.
          * WAKING: same as SLEEPING until the transition completes,
            then the caller promotes us to READY.
          * STARTING / INITIALIZING: nothing is user-facing yet.
          * SHUTTING_DOWN / FAILED: everything is off.
        """
        f = self.flags
        if state in (RuntimeState.READY, RuntimeState.LISTENING, RuntimeState.EXECUTING):
            f.command_input_enabled = True
            f.wake_listener_enabled = False
            f.tts_enabled = True
            f.brain_active = True
            f.agent_active = True
            f.vision_active = True
            f.execution_pipeline_active = True
        elif state is RuntimeState.SLEEPING:
            f.command_input_enabled = False
            f.wake_listener_enabled = True
            f.tts_enabled = True   # so the "going to sleep" line can play
            f.brain_active = False
            f.agent_active = False
            f.vision_active = False
            f.execution_pipeline_active = False
        elif state is RuntimeState.WAKING:
            f.command_input_enabled = False
            f.wake_listener_enabled = True
            f.tts_enabled = True
            f.brain_active = False
            f.agent_active = False
            f.vision_active = False
            f.execution_pipeline_active = False
        else:
            # STARTING / INITIALIZING / SHUTTING_DOWN / FAILED
            f.command_input_enabled = False
            f.wake_listener_enabled = False
            f.tts_enabled = True
            f.brain_active = False
            f.agent_active = False
            f.vision_active = False
            f.execution_pipeline_active = False

    def _notify(self, old: RuntimeState, new: RuntimeState) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(old, new)
            except Exception:
                # Never let a bad listener take the system down.
                try:
                    from loguru import logger as _loguru
                    _loguru.warning(
                        f"RuntimeState listener {fn!r} raised on "
                        f"{old.value}->{new.value}"
                    )
                except Exception:
                    pass

    # -------------------------------------------------------- diagnostics
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self._state.value,
                "is_alive": self._state.is_alive(),
                "is_asleep": self._state.is_asleep(),
                "last_transition_at": self._last_transition_at,
                "flags": self.flags.as_dict(),
                "history": [
                    {"from": o.value, "to": n.value, "at": t}
                    for o, n, t in self._transition_history[-12:]
                ],
            }


__all__ = ["RuntimeState", "SubsystemFlags", "RuntimeStateController"]
