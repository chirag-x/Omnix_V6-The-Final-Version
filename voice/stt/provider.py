from typing import Protocol, List
from ..contracts import AudioChunk, TranscriptionResult

class SpeechToTextProvider(Protocol):
    """Protocol for STT providers."""
    
    def transcribe(self, audio: bytes, sample_rate: int) -> TranscriptionResult:
        """Transcribe raw audio bytes into text."""
        ...
        
    def close(self):
        """Release any internal resources (e.g. models)."""
        ...
