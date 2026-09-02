import io
import time
import numpy as np
from typing import Optional
from ..contracts import TranscriptionResult, STTError
from .provider import SpeechToTextProvider

class FasterWhisperProvider(SpeechToTextProvider):
    """
    STT Provider using local faster-whisper.
    Uses lazy loading for the model so it isn't loaded merely on import.
    """
    
    def __init__(self, model_size: str = "tiny.en", device: str = "cuda", compute_type: str = "float16"):
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
                self._model = WhisperModel(
                    self._model_size, 
                    device=self._device, 
                    compute_type=self._compute_type
                )
            except Exception as e:
                raise STTError(f"Failed to load faster-whisper model: {e}") from e

    def transcribe(self, audio: bytes, sample_rate: int) -> TranscriptionResult:
        if not audio:
            return TranscriptionResult(text="", confidence=0.0)
            
        start_t = time.time()
        self._ensure_model()
        
        try:
            # Need to convert raw bytes (int16 pcm) into float32 array normalized to [-1.0, 1.0]
            audio_array = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            
            segments, info = self._model.transcribe(audio_array, beam_size=5)
            
            transcript_text = ""
            # Exhaust the generator
            for segment in segments:
                transcript_text += segment.text + " "
                
            transcript_text = transcript_text.strip()
            duration_s = time.time() - start_t
            
            # Use info.language_probability as a proxy for confidence if available
            confidence = info.language_probability if hasattr(info, "language_probability") else 1.0
            
            return TranscriptionResult(
                text=transcript_text,
                confidence=confidence,
                language=info.language,
                duration_s=duration_s
            )
        except Exception as e:
            raise STTError(f"Transcription failed: {e}") from e

    def close(self):
        self._model = None
