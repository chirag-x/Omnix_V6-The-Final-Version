import time
import threading
from typing import Optional
from ..contracts import TTSRequest, TTSResult, TTSError
from .provider import TextToSpeechProvider

class SAPITTSProvider(TextToSpeechProvider):
    """
    Offline Text-to-Speech using Windows SAPI through win32com.
    """
    def __init__(self):
        self._speaker = None
        self._speaking = False
        self._lock = threading.RLock()
        
    def _ensure_speaker(self):
        if self._speaker is None:
            try:
                import win32com.client
                self._speaker = win32com.client.Dispatch("SAPI.SpVoice")
            except Exception as e:
                raise TTSError(f"Failed to initialize SAPI SpVoice: {e}") from e

    def synthesize(self, request: TTSRequest) -> TTSResult:
        if not request.text.strip():
            return TTSResult(success=True)
            
        with self._lock:
            self._ensure_speaker()
            self._speaking = True
            
        start_t = time.time()
        try:
            # Flags: SVSFDefault = 0, SVSFlagsAsync = 1
            # By default it is blocking unless ASYNC is passed.
            # Using synchronous speaking here, but allowing cancellation by interrupting 
            # might require async and polling. For Phase 10 baseline, let's keep it simple.
            self._speaker.Speak(request.text, 0)
            duration = time.time() - start_t
            return TTSResult(success=True, duration_s=duration)
        except Exception as e:
            return TTSResult(success=False, error=str(e), duration_s=time.time() - start_t)
        finally:
            with self._lock:
                self._speaking = False

    def stop(self):
        with self._lock:
            if self._speaking and self._speaker:
                try:
                    # SAPI purges the speech queue when passed SVSFPurgeBeforeSpeak (2)
                    self._speaker.Speak("", 2)
                except:
                    pass

    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def close(self):
        with self._lock:
            self.stop()
            self._speaker = None
