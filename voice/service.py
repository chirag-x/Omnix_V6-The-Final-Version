"""
Canonical Voice Service for V6.
Glues microphone, VAD, STT, Intent, and TTS together safely.
"""

import threading
import time
import queue
from typing import Optional, Callable
from loguru import logger

from .contracts import VoiceState, VoiceError, TTSRequest
from .session.voice_session import VoiceSession
from .audio.microphone import MicrophoneInput
from .vad.detector import SimpleVAD
from .stt.faster_whisper_provider import FasterWhisperProvider
from .tts.sapi_provider import SAPITTSProvider
from .policy import sanitize_for_tts, condense_response


class VoiceService:
    def __init__(self, engine=None, *, speech_queue=None):
        self._engine = engine
        self._session = VoiceSession()

        # Audio / VAD properties
        self._mic = MicrophoneInput()
        self._vad = SimpleVAD()

        # Providers
        self._stt = FasterWhisperProvider()
        self._tts = SAPITTSProvider()

        # Optional engine-owned SpeechQueue.  When present, the
        # service feeds spoken text to the queue instead of calling
        # TTS directly.  This is the real-time path: the
        # ProgressReporter writes short narration items to the queue
        # while the agent is still running, and the queue's worker
        # serialises them so the user hears one thing at a time.
        self._speech_queue = speech_queue

        self._shutdown_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._audio_buffer = bytearray()
        
    def initialize(self):
        """Prepare VoiceService for normal operation."""
        logger.info("Initializing VoiceService (Phase 10)")
        self._session.transition(VoiceState.IDLE)
        
    def listen_and_transcribe(self) -> Optional[str]:
        """A bounded block that listens until silence and transcribes."""
        self._session.transition(VoiceState.LISTENING)
        self._audio_buffer.clear()
        self._vad.reset()
        
        try:
            self._mic.start()
            logger.info("Listening (speak into the microphone)...")
            
            # Read from generator until VAD silence triggers
            for chunk in self._mic.read():
                if self._shutdown_event.is_set():
                    break
                    
                has_speech = self._vad.process_chunk(chunk)
                if self._vad.is_speaking:
                    self._audio_buffer.extend(chunk.data)
                elif self._vad.is_speech_ended() and len(self._audio_buffer) > 0:
                    break # Silence timeout
            
        except Exception as e:
            logger.error(f"Microphone error: {e}")
            self._session.transition(VoiceState.ERROR)
            return None
        finally:
            self._mic.stop()
            
        if not self._audio_buffer:
            self._session.transition(VoiceState.IDLE)
            return None
            
        # Transcribe
        self._session.transition(VoiceState.TRANSCRIBING)
        logger.info("Transcribing audio...")
        try:
            # MicrophoneInput defaults to 16000 hz
            result = self._stt.transcribe(bytes(self._audio_buffer), sample_rate=16000)
            logger.debug(f"STT Result ({result.confidence:.2f}): {result.text}")
            self._session.transition(VoiceState.IDLE)
            return result.text
        except Exception as e:
            logger.error(f"STT error: {e}")
            self._session.transition(VoiceState.ERROR)
            return None

    def speak(self, text: str):
        """Synthesize text safely.

        When a speech queue is configured, the text is enqueued
        there instead of being spoken inline.  The queue's worker
        drives TTS so the calling thread is never blocked on
        synthesis latency.  This is the path the
        :class:`StartupAnnouncer` and :class:`ProgressReporter` use
        to deliver real-time narration.
        """
        safe_text = condense_response(sanitize_for_tts(text))
        if self._speech_queue is not None:
            try:
                from core.services.speech_queue import SpeechItem
                self._speech_queue.enqueue(
                    SpeechItem(
                        text=safe_text,
                        kind="result",
                    )
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"SpeechQueue enqueue failed; falling back to "
                    f"direct TTS: {exc!r}"
                )
        self._session.transition(VoiceState.SPEAKING)
        try:
            req = TTSRequest(text=safe_text)
            self._tts.synthesize(req)
        except Exception as e:
            logger.error(f"TTS error: {e}")
        finally:
            if self._session.state == VoiceState.SPEAKING:
                self._session.transition(VoiceState.IDLE)

    # ------------------------------------------------------- Phase 11 wiring
    def listen_process_respond(self) -> Optional[str]:
        """One full voice turn: listen → engine.process → speak.

        Returns the (sanitized) text that was spoken, or ``None`` if no
        usable speech was captured.  Never raises — any failure in the
        engine is caught and reported as a short TTS-safe error.

        Constraints (per Phase 11 hard rules):

        * Voice NEVER calls Brain, OpenRouter, or executes a capability
          directly.  All it does is capture audio, transcribe it, hand
          the text to ``engine.process()``, and speak the response.
        * TTS NEVER receives raw internal objects.  The response is
          already a plain string when it leaves the engine.
        * If TTS fails, the textual response is still returned to the
          caller — speech is best-effort.
        """
        if self._engine is None:
            logger.warning("VoiceService has no engine; cannot process.")
            return None

        text = self.listen_and_transcribe()
        if not text or not str(text).strip():
            return None

        # Hand off to the canonical pipeline.  Voice does NOT decide
        # what to do — the engine's process() does.
        try:
            response = self._engine.process(text)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"engine.process() raised: {exc!r}")
            # TTS-safe fallback
            self.speak("I encountered an error processing that request.")
            return None

        # Extract the safe user-facing text.  response.text is always
        # a non-empty sanitized string by contract.
        try:
            spoken = str(getattr(response, "text", "") or "").strip()
        except Exception:  # noqa: BLE001
            spoken = ""
        if not spoken:
            return None

        # Apply the same TTS policy that other consumers use.
        self.speak(spoken)
        return spoken

    def run_voice_loop(self, *, max_turns: int = 1) -> int:
        """Run ``listen_process_respond`` for up to ``max_turns`` turns.

        Returns the number of turns that successfully produced a TTS
        response.  Used by ``main.py voice listen`` as the canonical
        voice integration test entry point.
        """
        if max_turns <= 0:
            return 0
        successful = 0
        for _ in range(int(max_turns)):
            if self._shutdown_event.is_set():
                break
            result = self.listen_process_respond()
            if result:
                successful += 1
        return successful
                
    def shutdown(self):
        """Clean up all voice resources deterministically."""
        logger.info("Shutting down VoiceService...")
        self._session.transition(VoiceState.STOPPING)
        self._shutdown_event.set()
        
        self._mic.close()
        self._tts.close()
        self._stt.close()
        
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)
            
        self._session.transition(VoiceState.IDLE)

