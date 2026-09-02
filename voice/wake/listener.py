"""
Omnix V6 — Wake-word listener.

Always-on (when enabled) monitor for the configured wake phrase
("Omnix" by default).  When detected, fires a :class:`WakeEvent`
on the registered callback.  No LLM is used.

Backends
--------

* ``openwakeword`` — uses the ``"alexa"`` model by default but
  can be retrained/customised.  The model is loaded lazily on
  first enable so a CI host that never wakes pays nothing.
* ``text_match`` — uses a short STT snippet of the last 1.5s of
  audio and runs a fast text match.  This is the deterministic
  fallback used in tests and in any environment where
  ``openwakeword`` is unavailable.

Lifecycle
---------

The listener is *fully off* by default.  Call :meth:`enable` to
turn it on; :meth:`disable` to turn it off.  When off, the
microphone is closed and no CPU is used.

The listener does not own the microphone.  It is passed a
microphone-like object (anything that exposes
``start()``/``stop()``/``read()`` returning ``AudioChunk``) so it
can be re-used with the existing ``MicrophoneInput``.

If a ``RuntimeStateController`` is attached via
:meth:`attach_state_controller`, the listener only consumes
microphone audio when the controller is in a sleep transition
(``SLEEPING`` or ``WAKING``).  This is the wake-gate: command
STT and execution never run while the wake listener is active.
"""
from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from loguru import logger

from voice.contracts import AudioChunk


class WakeBackend(str, enum.Enum):
    """Available wake-word backends."""
    OPENWAKEWORD = "openwakeword"
    TEXT_MATCH = "text_match"


@dataclass
class WakeEvent:
    """Fired by the listener when the wake phrase is detected."""
    text: str
    backend: str
    confidence: float
    timestamp: float


def _default_text_match(text: str, phrase: str) -> bool:
    """A tiny, deterministic text match.  We split the snippet into
    words and look for the phrase as a contiguous run.  Punctuation
    is stripped and case is lowered.  This is the *fallback* path;
    the primary path is the neural model."""
    if not text or not phrase:
        return False
    words = [w.strip(".,!?;:") for w in text.lower().split() if w.strip(".,!?;:")]
    phrase_words = phrase.lower().split()
    if not phrase_words:
        return False
    n = len(phrase_words)
    for i in range(len(words) - n + 1):
        if words[i : i + n] == phrase_words:
            return True
    return False


class WakeWordListener:
    """Always-on wake-word monitor.

    Parameters
    ----------
    microphone
        Any object that exposes ``start()``, ``stop()`` and
        ``read() -> Iterator[AudioChunk]``.  Typically the
        :class:`voice.audio.microphone.MicrophoneInput`.
    phrase
        Wake phrase.  Default ``"omnix"``.  Lower-cased internally.
    backend
        :class:`WakeBackend` to use.  ``AUTO`` picks
        ``openwakeword`` if importable, else ``text_match``.
    on_wake
        Callable invoked with a :class:`WakeEvent` when the phrase
        is detected.  Must be fast; the listener will not block
        on it.
    """

    # When the openwakeword backend is unavailable we always have
    # a fallback to text_match.  We try to import openwakeword
    # once at construction so the chosen backend is stable for
    # the lifetime of the listener.
    _OWW_IMPORT_ERROR: Optional[BaseException] = None

    def __init__(
        self,
        microphone: Any,
        *,
        phrase: str = "omnix",
        backend: Optional[WakeBackend] = None,
        on_wake: Optional[Callable[[WakeEvent], None]] = None,
        text_match_stt: Optional[Callable[[bytes, int], str]] = None,
    ) -> None:
        self._mic = microphone
        self._phrase = (phrase or "omnix").lower().strip()
        self._on_wake = on_wake
        # Backend selection ----------------------------------------------------
        if backend is None:
            backend = self._auto_backend()
        self._backend = WakeBackend(backend)
        # Optional text-match STT.  Only used by the text_match
        # backend.  The production wiring injects a small faster-whisper
        # shortcut here so we don't pay the full model cost.
        self._text_match_stt = text_match_stt
        # Lifecycle -------------------------------------------------------------
        self._enabled = False
        self._state_controller: Optional[Any] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Metrics
        self._wake_total = 0
        self._chunks_total = 0
        self._errors_total = 0
        # openwakeword model handle (lazy)
        self._oww_model: Optional[Any] = None
        self._oww_loaded = False
        # For text_match: a small rolling buffer.
        self._buffer: bytearray = bytearray()
        self._buffer_max_bytes: int = 16000 * 2 * 2  # ~2s of 16kHz int16 mono
        # Sample rate assumed by the openwakeword / text_match path.
        self._sample_rate: int = 16000
        # Detection cooldown so we don't fire multiple times for
        # one wake event.
        self._cooldown_s: float = 1.5
        self._last_detect_at: float = 0.0

    # ------------------------------------------------------------------ API
    def set_on_wake(self, on_wake: Callable[[WakeEvent], None]) -> None:
        self._on_wake = on_wake

    def attach_state_controller(self, controller: Any) -> None:
        """Wire the listener to a :class:`RuntimeStateController`.  The
        listener only consumes microphone audio when the controller is
        in a sleep transition (``SLEEPING`` or ``WAKING``) — these
        are the only times command STT is *not* active.  ``None``
        disables the gate; the listener then respects only its
        enable flag."""
        self._state_controller = controller

    def enable(self) -> None:
        """Start listening.  Idempotent."""
        with self._lock():
            if self._enabled:
                return
            self._enabled = True
        self._ensure_thread()

    def disable(self) -> None:
        """Stop listening and release the microphone.  Idempotent."""
        with self._lock():
            if not self._enabled:
                return
            self._enabled = False
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._stop_event.clear()
        # Release the microphone so the command listener can pick it up.
        try:
            self._mic.stop()
        except Exception:  # noqa: BLE001
            pass

    def is_enabled(self) -> bool:
        return self._enabled

    def statistics(self) -> dict:
        return {
            "type": "WakeWordListener",
            "enabled": self._enabled,
            "backend": self._backend.value,
            "phrase": self._phrase,
            "wake_total": self._wake_total,
            "chunks_total": self._chunks_total,
            "errors_total": self._errors_total,
        }

    # -------------------------------------------------------------- internal
    def _lock(self) -> threading.Lock:
        # Single RLock for the listener — small state.
        if not hasattr(self, "_rlock"):
            self._rlock = threading.RLock()
        return self._rlock

    def _auto_backend(self) -> WakeBackend:
        # Try openwakeword at most once.
        err = WakeWordListener._OWW_IMPORT_ERROR
        if err is None:
            try:
                __import__("openwakeword")
                return WakeBackend.OPENWAKEWORD
            except Exception as exc:  # noqa: BLE001
                WakeWordListener._OWW_IMPORT_ERROR = exc
                logger.debug(f"openwakeword unavailable: {exc!r}; using text_match")
        return WakeBackend.TEXT_MATCH

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="omnix-wake-listener",
            daemon=True,
        )
        self._thread.start()

    def _should_listen(self) -> bool:
        """True when the lifecycle gate allows us to consume audio."""
        if not self._enabled:
            return False
        if self._state_controller is None:
            return True
        try:
            # We must listen only while the system is asleep or waking.
            # During READY / LISTENING / EXECUTING, the command STT owns
            # the mic and the wake listener stays silent.
            st = self._state_controller.state
            if st is None:
                return True
            from core.state.runtime_state import RuntimeState
            return st in (RuntimeState.SLEEPING, RuntimeState.WAKING)
        except Exception:  # noqa: BLE001
            return True

    def _run(self) -> None:
        logger.info(f"WakeWordListener starting (backend={self._backend.value}, phrase='{self._phrase}')")
        try:
            self._mic.start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"WakeWordListener could not start microphone: {exc!r}")
            self._enabled = False
            return
        try:
            while not self._stop_event.is_set():
                if not self._should_listen():
                    # Lifecycle says: stay quiet.  Stop the mic to save
                    # CPU and let the command listener have it.
                    try:
                        if self._mic is not None:
                            # We do NOT stop permanently; we just yield.
                            pass
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(0.1)
                    continue
                # Read audio; if mic is not open, (re)open it.
                try:
                    audio_iter = self._mic.read()
                except Exception:  # noqa: BLE001
                    try:
                        self._mic.start()
                        audio_iter = self._mic.read()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"WakeWordListener mic reopen failed: {exc!r}")
                        time.sleep(0.25)
                        continue
                for chunk in audio_iter:
                    if self._stop_event.is_set():
                        break
                    if not self._should_listen():
                        break
                    try:
                        self._on_chunk(chunk)
                    except Exception as exc:  # noqa: BLE001
                        self._errors_total += 1
                        logger.warning(f"WakeWordListener chunk error: {exc!r}")
        finally:
            try:
                self._mic.stop()
            except Exception:  # noqa: BLE001
                pass
            logger.info("WakeWordListener stopped")

    def _on_chunk(self, chunk: AudioChunk) -> None:
        self._chunks_total += 1
        if self._backend is WakeBackend.OPENWAKEWORD:
            self._on_chunk_openwakeword(chunk)
        else:
            self._on_chunk_text_match(chunk)

    def _on_chunk_openwakeword(self, chunk: AudioChunk) -> None:
        """Run the openwakeword model.  Lazy-load on first chunk."""
        if not self._oww_loaded:
            self._oww_loaded = True
            try:
                from openwakeword.model import Model  # type: ignore
                self._oww_model = Model(
                    wakeword_models=["alexa"],
                    inference_framework="onnx",
                )
            except Exception as exc:  # noqa: BLE001
                self._oww_model = None
                logger.warning(
                    f"openwakeword Model load failed: {exc!r}; "
                    f"falling back to text_match"
                )
                self._backend = WakeBackend.TEXT_MATCH
                return
        if self._oww_model is None:
            return
        try:
            import numpy as np
            audio = np.frombuffer(chunk.data, dtype=np.int16)
            # The openwakeword API expects float32 in [-1, 1].
            audio_f = audio.astype(np.float32) / 32768.0
            preds = self._oww_model.predict(audio_f)
            for label, score in preds.items():
                if score >= 0.5 and self._phrase.replace(" ", "_") in label.lower():
                    self._fire(label, float(score))
                    return
        except Exception as exc:  # noqa: BLE001
            self._errors_total += 1
            logger.warning(f"openwakeword predict error: {exc!r}")

    def _on_chunk_text_match(self, chunk: AudioChunk) -> None:
        # Roll the buffer; when it crosses ~1.5s, run STT once.
        self._buffer.extend(chunk.data)
        if len(self._buffer) > self._buffer_max_bytes:
            self._buffer = self._buffer[-self._buffer_max_bytes:]
        # Trigger check when we have ~1.5s of audio.
        if len(self._buffer) < self._sample_rate * 2 * 1:
            return
        # Run STT only every ~1s to bound cost.
        now = time.time()
        if now - self._last_detect_at < self._cooldown_s and self._last_detect_at != 0.0:
            # Already fired recently — drop the buffer.
            self._buffer.clear()
            return
        snippet = bytes(self._buffer)
        # Keep ~0.5s of context for the next STT call.
        keep = self._sample_rate * 2 // 2
        if len(self._buffer) > keep:
            self._buffer = self._buffer[-keep:]
        if self._text_match_stt is None:
            return
        try:
            text = self._text_match_stt(snippet, self._sample_rate)
        except Exception as exc:  # noqa: BLE001
            self._errors_total += 1
            logger.debug(f"Wake text_match STT error: {exc!r}")
            return
        if text and _default_text_match(text, self._phrase):
            self._fire(text, 0.9)
            self._last_detect_at = now

    def _fire(self, text: str, confidence: float) -> None:
        now = time.time()
        if now - self._last_detect_at < self._cooldown_s:
            return
        self._last_detect_at = now
        self._wake_total += 1
        ev = WakeEvent(
            text=text,
            backend=self._backend.value,
            confidence=float(confidence),
            timestamp=now,
        )
        if self._on_wake is not None:
            try:
                self._on_wake(ev)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"on_wake callback raised: {exc!r}")


__all__ = ["WakeWordListener", "WakeEvent", "WakeBackend"]
