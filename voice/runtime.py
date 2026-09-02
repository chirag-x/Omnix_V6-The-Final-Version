"""
Omnix V6 — Voice runtime (Part 3).

The :class:`VoiceRuntime` owns the user-facing voice layer:

  * the **microphone** (single resource, shared by command STT and
    wake listener — only one is active at a time)
  * the **wake-word listener** — enabled while the system is
    sleeping, disabled while the system is ready/active
  * the **command STT loop** — enabled while the system is ready
    or executing, disabled while sleeping
  * a **callback** that the engine hooks into for "the user said
    something, please process it".  The runtime does NOT call the
    engine directly — it only emits text.  The engine (or main.py
    for the text-input loop) decides what to do with it.

The runtime is a thin orchestrator on top of
:class:`core.state.RuntimeStateController`.  Every state transition
that affects voice is mirrored into subsystem enable/disable
calls so that a stray subsystem cannot keep the microphone open
after sleep.

This module intentionally does NOT touch TTS — the
:class:`core.services.SpeechQueue` is the single TTS path.  The
runtime only *enqueues* an announcement when the system
transitions to sleep ("going to sleep") or wakes up ("I'm awake").
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from loguru import logger

from core.services.speech_queue import SpeechItem, SpeechQueue
from core.state.runtime_state import RuntimeState, RuntimeStateController

from .audio.microphone import MicrophoneInput
from .stt.faster_whisper_provider import FasterWhisperProvider
from .vad.detector import SimpleVAD
from .wake.listener import WakeEvent, WakeWordListener


# Default wake phrase.  Kept short so the model / text-match
# backend fires fast.
DEFAULT_WAKE_PHRASE = "omnix"

# Default announcement copy.  These are spoken on every sleep /
# wake cycle.  The System 9 spec wants them to feel like a natural
# assistant — friendly, informative, and a single short sentence
# that tells the user exactly what state the system is in and
# what to do next.
GOING_TO_SLEEP_TEXT = (
    "I'm going to sleep. Say my wake word when you need me."
)
AWAKE_TEXT = "I'm awake. How can I help?"


class VoiceRuntime:
    """Owns microphone, wake listener, and command STT.

    The runtime is constructed once and bound to a
    :class:`RuntimeStateController`.  It listens for state
    transitions and reconfigures its subsystems accordingly.

    Public surface
    --------------

    * :meth:`start()` — wires the runtime to the state controller
      and enables the appropriate subsystem for the current state.
    * :meth:`stop()` — disables all subsystems.
    * :meth:`set_on_command` — register the callback that receives
      transcribed user text.
    * :meth:`sleep()` — request a sleep transition.  Idempotent.
    * :meth:`wake()` — request a wake transition.  Idempotent.
    * :meth:`run_command_listen_once` — block for one command
      utterance and return the transcribed text (or ``None``).
      Used by ``main.py``'s voice loop.

    The class is thread-safe; all public methods can be called
    from any thread.
    """

    def __init__(
        self,
        *,
        controller: Optional[RuntimeStateController] = None,
        speech_queue: Optional[SpeechQueue] = None,
        microphone: Optional[Any] = None,
        wake_listener: Optional[WakeWordListener] = None,
        stt: Optional[Any] = None,
        vad: Optional[Any] = None,
        wake_phrase: str = DEFAULT_WAKE_PHRASE,
    ) -> None:
        self._controller = controller or RuntimeStateController()
        # Wire the speech queue's lifecycle gate so normal progress
        # narration is suppressed while we are asleep.
        if speech_queue is not None:
            speech_queue.attach_state_controller(self._controller)
        self._queue = speech_queue
        # Audio + STT providers.  Tests inject fakes here.
        self._mic = microphone if microphone is not None else MicrophoneInput()
        self._vad = vad if vad is not None else SimpleVAD()
        self._stt = stt if stt is not None else FasterWhisperProvider()
        # Wake listener — uses the same microphone.
        if wake_listener is None:
            self._wake = WakeWordListener(
                self._mic,
                phrase=wake_phrase,
                on_wake=self._on_wake_event,
            )
        else:
            self._wake = wake_listener
        self._wake.attach_state_controller(self._controller)
        # Callback fired when command STT produces text.  The
        # callback is invoked from the command STT thread.  The
        # engine / main loop is responsible for handing the text
        # to the pipeline.
        self._on_command: Optional[Callable[[str], None]] = None
        # Internal locks
        self._lock = threading.RLock()
        self._running = False
        # Subsystem state — derived from RuntimeStateController.
        self._command_stt_enabled = False
        self._wake_enabled = False
        # Audio buffer used by run_command_listen_once.
        self._audio_buffer = bytearray()
        # Continuous listen-loop.  When started, a single background
        # thread drives ``run_command_listen_once`` in a loop while
        # command STT is enabled.  The engine does NOT have to call
        # ``run_command_listen_once`` itself — the runtime owns the
        # mic whenever it is awake.  This is what makes "always
        # listening after startup" work without the user typing
        # ``/voice`` between every command.
        self._listen_thread: Optional[threading.Thread] = None
        self._listen_stop = threading.Event()
        # Per-turn pause between listens so a thread that just
        # finished transcribing one utterance gives the engine a
        # moment to actually process the result before opening the
        # mic again.  Without this, a fast engine would race the
        # STT and never pick up the user's next command.
        self._listen_idle_s: float = 0.25
        # Metrics
        self._command_turns = 0
        self._wake_events = 0
        self._listen_iterations = 0
        self._listen_failures = 0
        # Optional engine reference for the "engine is mid-turn"
        # gate.  Wired by ``set_engine``; ``None`` disables the
        # gate (tests inject fakes without an engine).
        self._engine: Optional[Any] = None

    # =============================================================== props
    @property
    def controller(self) -> RuntimeStateController:
        return self._controller

    @property
    def state(self) -> RuntimeState:
        return self._controller.state

    def set_on_command(self, callback: Callable[[str], None]) -> None:
        """Register the callback invoked with transcribed user text.

        The callback runs on the command-STT thread.  Keep it
        non-blocking; if it has to do real work, dispatch it to a
        worker.
        """
        with self._lock:
            self._on_command = callback

    def set_engine(self, engine: Any) -> None:
        """Attach the engine so the listen loop can check
        :meth:`is_processing`.

        System 9: the listen loop pauses the microphone while the
        engine is mid-turn so the user does not race the pipeline.
        ``engine`` may be ``None``; ``None`` disables the gate
        (the loop will run unconditionally, which is the right
        behaviour for tests with a fake engine).
        """
        with self._lock:
            self._engine = engine

    # ============================================================== lifecycle
    def start(self) -> None:
        """Start the runtime.  Mirrors current state into subsystems."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._controller.add_listener(self._on_state_change)
        # Apply the current state.
        self._apply_state(self._controller.state)

    def stop(self) -> None:
        """Stop all subsystems and release the microphone."""
        with self._lock:
            if not self._running:
                return
            self._running = False
        # Stop the listen loop FIRST so it does not try to open
        # the microphone while we are tearing it down.
        self.stop_listen_loop()
        self._disable_wake_listener()
        self._disable_command_stt()
        try:
            self._mic.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._stt.close()
        except Exception:  # noqa: BLE001
            pass

    # ============================================================== listen loop
    def start_listen_loop(self) -> None:
        """Start the background listen loop.

        The loop drives :meth:`run_command_listen_once` repeatedly
        while the runtime is in an "alive" state and command STT is
        enabled.  Each transcribed utterance is delivered to the
        callback registered with :meth:`set_on_command`.  The loop
        is stopped automatically when the runtime sleeps or shuts
        down; callers do not have to drive it manually.

        Calling this method more than once is a no-op; the existing
        thread is reused.
        """
        with self._lock:
            if self._listen_thread is not None and self._listen_thread.is_alive():
                return
            self._listen_stop.clear()
            self._listen_thread = threading.Thread(
                target=self._listen_loop,
                name="omnix-voice-listen",
                daemon=True,
            )
            self._listen_thread.start()
        logger.info("VoiceRuntime: listen loop STARTED")

    def stop_listen_loop(self) -> None:
        """Stop the background listen loop.

        Idempotent and safe to call from any thread.  The loop
        checks the stop flag between iterations, so a call in the
        middle of a transcription completes that iteration first.
        """
        with self._lock:
            self._listen_stop.set()
            t = self._listen_thread
        # Do not join under the lock; that could deadlock if the
        # loop is mid-transcription.
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
        with self._lock:
            self._listen_thread = None
        logger.info("VoiceRuntime: listen loop STOPPED")

    def _listen_loop(self) -> None:
        """Worker body.  One iteration = one listen cycle.

        Each iteration:
        1.  Skips immediately if the runtime is not alive (sleeping,
            shutting down, etc.).
        2.  Calls :meth:`run_command_listen_once` with a short
            timeout.  That method already buffers, transcribes, and
            invokes the on_command callback.
        3.  Pauses briefly so a fast engine has a chance to start
            processing the callback before the next listen begins.
        4.  Catches every exception — a STT backend failure must
            not kill the loop.
        """
        while not self._listen_stop.is_set():
            # Bounce quickly while the system is asleep or shutting
            # down.  The lifecycle listener will call us back when
            # command STT is re-enabled.
            try:
                if not self._controller.is_alive:
                    time.sleep(0.1)
                    continue
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
                continue
            # Pause the microphone while the engine is mid-turn so
            # the user does not race the pipeline.
            try:
                engine = self._engine
                if engine is not None and getattr(engine, "is_processing", False):
                    time.sleep(0.1)
                    continue
            except Exception:  # noqa: BLE001
                # A misbehaving engine attribute must not kill the loop.
                pass
            try:
                self.run_command_listen_once(timeout_s=8.0)
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._listen_failures += 1
                logger.warning(
                    f"VoiceRuntime listen loop iteration failed: {exc!r}"
                )
            with self._lock:
                self._listen_iterations += 1
            # Cooldown so we don't hammer the STT model back-to-back
            # when the user has not actually finished a turn.
            for _ in range(int(max(0.05, self._listen_idle_s) / 0.05)):
                if self._listen_stop.is_set():
                    break
                time.sleep(0.05)

    # =========================================================== public API
    def sleep(self) -> None:
        """Request a sleep transition.

        Idempotent.  The transition is atomic with respect to the
        state controller's lock.  Subsystem toggling happens in
        the listener.
        """
        st = self._controller.state
        if st.is_asleep() or st is RuntimeState.WAKING:
            return
        # Announce *before* transitioning so the speech is heard
        # by the user as the last line of the awake session.
        if self._queue is not None:
            # Cancel any pending progress that no longer matters.
            try:
                self._queue.cancel_pending(kind="progress")
            except Exception:  # noqa: BLE001
                pass
        # The "going to sleep" line is bypassed through the sleep
        # gate so it is heard even though the system is about to
        # transition to SLEEPING.
        self._enqueue_bypass(GOING_TO_SLEEP_TEXT)
        self._controller.transition(RuntimeState.SLEEPING)

    def wake(self) -> None:
        """Request a wake transition.  Idempotent."""
        st = self._controller.state
        if st in (RuntimeState.READY, RuntimeState.LISTENING, RuntimeState.EXECUTING):
            return
        if st is RuntimeState.WAKING:
            return
        # Two-step transition: WAKING -> READY.  WAKING re-enables
        # the wake listener to finish the bounce; the controller
        # then promotes us to READY (which flips subsystems).
        self._controller.transition(RuntimeState.WAKING)
        # Speak the "I'm awake" line.  bypass_sleep=True so it is
        # not gated by the controller while it is WAKING.
        self._enqueue_bypass(AWAKE_TEXT)
        # Promote to READY.  The listener will then disable the
        # wake listener and enable command STT.
        self._controller.transition(RuntimeState.READY)

    def enable_command_stt(self) -> None:
        """Force command STT on.  No-op if the lifecycle gate
        disagrees (e.g. while sleeping)."""
        with self._lock:
            if not self._running:
                return
            st = self._controller.state
            if not (st.is_alive() or st is RuntimeState.WAKING):
                return
        self._enable_command_stt_locked()

    def disable_command_stt(self) -> None:
        self._disable_command_stt()

    def enable_wake_listener(self) -> None:
        with self._lock:
            if not self._running:
                return
            st = self._controller.state
            if not (st.is_asleep() or st is RuntimeState.WAKING):
                return
        self._enable_wake_listener_locked()

    def disable_wake_listener(self) -> None:
        self._disable_wake_listener()

    # ============================================================== command STT
    def run_command_listen_once(self, *, timeout_s: float = 8.0) -> Optional[str]:
        """Block for one command utterance and return the text.

        The runtime must be in an "alive" state for this to do
        anything — otherwise the call returns ``None`` immediately.
        Used by the legacy ``VoiceService.listen_and_transcribe``
        path; new code should use :meth:`set_on_command` and
        :meth:`enable_command_stt` instead.
        """
        if not self._controller.is_alive:
            return None
        # Pause the wake listener briefly so we don't fight for the mic.
        # In production, only one of {command STT, wake listener} is
        # enabled at a time so this is normally a no-op.
        self._audio_buffer.clear()
        self._vad.reset()
        deadline = time.time() + max(0.1, float(timeout_s))
        try:
            self._mic.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"VoiceRuntime mic start failed: {exc!r}")
            return None
        try:
            while time.time() < deadline:
                for chunk in self._mic.read():
                    if self._controller.is_asleep:
                        return None
                    if not self._vad.process_chunk(chunk):
                        # Silence between words — keep buffering.
                        pass
                    if self._vad.is_speaking:
                        self._audio_buffer.extend(chunk.data)
                    if self._vad.is_speech_ended() and self._audio_buffer:
                        break
                if self._audio_buffer:
                    break
        finally:
            try:
                self._mic.stop()
            except Exception:  # noqa: BLE001
                pass
        if not self._audio_buffer:
            return None
        try:
            result = self._stt.transcribe(
                bytes(self._audio_buffer), sample_rate=16000
            )
            text = (getattr(result, "text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"VoiceRuntime STT error: {exc!r}")
            with self._lock:
                self._listen_failures += 1
            return None
        except BaseException as exc:  # noqa: BLE001
            # Some STT backends raise non-Exception errors (e.g. a
            # C-level abort).  Treat them as failures too.
            logger.warning(f"VoiceRuntime STT raised: {exc!r}")
            with self._lock:
                self._listen_failures += 1
            return None
        if not text:
            return None
        with self._lock:
            self._command_turns += 1
        cb = self._on_command
        if cb is not None:
            try:
                cb(text)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"on_command callback raised: {exc!r}")
        return text

    # ============================================================== state sync
    def _on_state_change(
        self, old: RuntimeState, new: RuntimeState
    ) -> None:
        self._apply_state(new)

    def _apply_state(self, new: RuntimeState) -> None:
        if not self._running:
            return
        if new in (RuntimeState.READY, RuntimeState.LISTENING, RuntimeState.EXECUTING):
            # Awake: command STT on, wake listener off.
            self._disable_wake_listener()
            self._enable_command_stt_locked()
        elif new in (RuntimeState.SLEEPING, RuntimeState.WAKING):
            # Asleep: wake listener on, command STT off.
            self._disable_command_stt()
            self._enable_wake_listener_locked()
        else:
            # STARTING / INITIALIZING / SHUTTING_DOWN / FAILED
            self._disable_command_stt()
            self._disable_wake_listener()

    def _enable_command_stt_locked(self) -> None:
        with self._lock:
            if self._command_stt_enabled:
                return
            self._command_stt_enabled = True
        # The "real" command STT is the legacy VoiceService
        # listen-and-transcribe loop.  VoiceRuntime is the
        # gatekeeper: the engine / main loop drives the actual
        # listen() call.  We simply flip the flag here so other
        # subsystems can read it without locking.
        logger.debug("VoiceRuntime: command STT ENABLED")
        # Start the continuous listen loop so the runtime owns the
        # microphone whenever command STT is enabled.  This is what
        # makes the system always listening after startup without the
        # user typing /voice between every command.
        self.start_listen_loop()

    def _disable_command_stt(self) -> None:
        with self._lock:
            if not self._command_stt_enabled:
                return
            self._command_stt_enabled = False
        # Stop the mic so the wake listener can pick it up cleanly.
        try:
            self._mic.stop()
        except Exception:  # noqa: BLE001
            pass
        # Stop the listen loop so it doesn't fight the wake listener
        # for the mic.
        self.stop_listen_loop()
        logger.debug("VoiceRuntime: command STT DISABLED")

    def _enable_wake_listener_locked(self) -> None:
        with self._lock:
            if self._wake_enabled:
                return
            self._wake_enabled = True
        self._wake.enable()
        logger.info("VoiceRuntime: wake listener ENABLED")

    def _disable_wake_listener(self) -> None:
        with self._lock:
            if not self._wake_enabled:
                return
            self._wake_enabled = False
        self._wake.disable()
        logger.info("VoiceRuntime: wake listener DISABLED")

    # =============================================================== wake cb
    def _on_wake_event(self, event: WakeEvent) -> None:
        """Wake-word callback.  We request a wake transition.  The
        controller will then promote us to READY which flips
        command STT back on."""
        with self._lock:
            self._wake_events += 1
        logger.info(f"VoiceRuntime: wake detected (backend={event.backend}, conf={event.confidence:.2f})")
        try:
            self.wake()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"VoiceRuntime.wake() raised: {exc!r}")

    # ============================================================== helpers
    def _enqueue_bypass(self, text: str) -> None:
        if self._queue is None or not text:
            return
        try:
            self._queue.enqueue(
                SpeechItem(
                    text=text,
                    kind="announcement",
                    source="announcement",
                    bypass_sleep=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"bypass enqueue failed: {exc!r}")

    def statistics(self) -> dict:
        with self._lock:
            listen_alive = (
                self._listen_thread is not None and self._listen_thread.is_alive()
            )
        return {
            "type": "VoiceRuntime",
            "state": self._controller.state.value,
            "running": self._running,
            "command_stt_enabled": self._command_stt_enabled,
            "wake_listener_enabled": self._wake_enabled,
            "listen_loop_alive": listen_alive,
            "command_turns": self._command_turns,
            "wake_events": self._wake_events,
            "listen_iterations": self._listen_iterations,
            "listen_failures": self._listen_failures,
            "wake_stats": self._wake.statistics() if self._wake else None,
        }


__all__ = ["VoiceRuntime", "DEFAULT_WAKE_PHRASE", "GOING_TO_SLEEP_TEXT", "AWAKE_TEXT"]
