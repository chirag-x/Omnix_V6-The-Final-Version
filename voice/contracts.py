from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, List, Dict, Any

class VoiceState(Enum):
    """Deterministic states for the Voice Session."""
    IDLE = auto()
    LISTENING = auto()
    TRANSCRIBING = auto()
    PROCESSING = auto()
    SPEAKING = auto()
    STOPPING = auto()
    ERROR = auto()

@dataclass(frozen=True)
class AudioFormat:
    """Canonical internal audio format."""
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2
    encoding: str = "pcm_s16le"

@dataclass
class AudioChunk:
    """A segment of raw audio data."""
    data: bytes
    format: AudioFormat = field(default_factory=AudioFormat)
    timestamp: float = 0.0

@dataclass
class TranscriptionResult:
    """Result from Speech-to-Text provider."""
    text: str
    confidence: float
    language: Optional[str] = None
    duration_s: float = 0.0

@dataclass
class SpeechSegment:
    """A segment of transcribed speech."""
    text: str
    start_time: float = 0.0
    end_time: float = 0.0

@dataclass
class TTSRequest:
    """Request to synthesize text to speech."""
    text: str
    language: str = "en"
    voice_id: Optional[str] = None

@dataclass
class TTSResult:
    """Result from Text-to-Speech provider."""
    success: bool
    duration_s: float = 0.0
    error: Optional[str] = None

class VoiceError(Exception):
    """Base exception for Voice subsystem failures."""
    pass

class MicrophoneError(VoiceError): pass
class VADError(VoiceError): pass
class STTError(VoiceError): pass
class TTSError(VoiceError): pass
class VoiceTransitionError(VoiceError): pass
