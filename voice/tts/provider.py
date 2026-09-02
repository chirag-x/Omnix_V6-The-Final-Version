from typing import Protocol
from ..contracts import TTSRequest, TTSResult

class TextToSpeechProvider(Protocol):
    """Protocol for TTS providers."""
    
    def synthesize(self, request: TTSRequest) -> TTSResult:
        """Synthesize text to spoken audio. Blocks until finished (or cancelled)."""
        ...
        
    def stop(self):
        """Interrupt any ongoing synthesis."""
        ...
        
    def is_speaking(self) -> bool:
        """Check if currently speaking."""
        ...
        
    def close(self):
        """Release resources."""
        ...
