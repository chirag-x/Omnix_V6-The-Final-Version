"""
Tests for Canonical Voice subsystem (Phase 10).
These must run without real audio devices.
"""
import pytest
import time
from unittest.mock import MagicMock
import numpy as np

from voice.contracts import (
    AudioFormat, AudioChunk, TranscriptionResult, 
    TTSRequest, TTSResult, VoiceState
)
from voice.session.voice_session import VoiceSession, VoiceTransitionError
from voice.vad.detector import SimpleVAD
from voice.service import VoiceService
from voice.policy import sanitize_for_tts, condense_response

def test_contracts_audio_format():
    fmt = AudioFormat(sample_rate=16000)
    assert fmt.sample_rate == 16000
    assert fmt.channels == 1

def test_contracts_audio_chunk():
    chunk = AudioChunk(data=b"ab12", timestamp=1.5)
    assert chunk.format.sample_rate == 16000
    assert chunk.timestamp == 1.5

def test_voice_session_transitions():
    sess = VoiceSession()
    assert sess.state == VoiceState.IDLE
    
    # Valid transition
    sess.transition(VoiceState.LISTENING)
    assert sess.state == VoiceState.LISTENING
    
    # Invalid transition should throw
    with pytest.raises(VoiceTransitionError):
        sess.transition(VoiceState.SPEAKING)
        
    sess.transition(VoiceState.ERROR)
    assert sess.state == VoiceState.ERROR
    
    sess.transition(VoiceState.IDLE)
    assert sess.state == VoiceState.IDLE

def test_simple_vad():
    vad = SimpleVAD(energy_threshold=0.01, silence_duration_s=0.5)
    
    fmt = AudioFormat(sample_rate=16000)
    
    # Send silence
    silent_data = np.zeros(16000, dtype=np.int16).tobytes()
    chunk1 = AudioChunk(data=silent_data, format=fmt)
    
    assert vad.process_chunk(chunk1) == False
    assert vad.is_speaking == False
    
    # Send high energy
    loud_data = (np.ones(16000, dtype=np.int16) * 1000).tobytes()
    chunk2 = AudioChunk(data=loud_data, format=fmt)
    
    assert vad.process_chunk(chunk2) == True
    assert vad.is_speaking == True
    
    # Back to silence, wait for timeout
    small_silence_data = np.zeros(4000, dtype=np.int16).tobytes()
    chunk_small = AudioChunk(data=small_silence_data, format=fmt)
    assert vad.process_chunk(chunk_small) == True
    assert vad.is_speaking == True
    
    # Next chunk (1 sec) pushes past 0.5s silence limit
    assert vad.process_chunk(chunk1) == False
    assert vad.is_speaking == False
    assert vad.is_speech_ended() == True
    
def test_policy_sanitize():
    assert sanitize_for_tts("My token=123 is valid.") == "I cannot read this out loud for security reasons."
    assert sanitize_for_tts("Password is hello.") == "I cannot read this out loud for security reasons."
    assert sanitize_for_tts("Check my API_KEY! sk-1234") == "I cannot read this out loud for security reasons."
    assert sanitize_for_tts("Hello **bold** *italic*") == "Hello bold italic"

def test_voice_service_initialization():
    service = VoiceService()
    service.initialize()
    service.shutdown()
    
# We mock FasterWhisperProvider and SAPITTSProvider to test logic without real hardware/heavy models
@pytest.fixture
def mock_service():
    srv = VoiceService()
    srv._stt = MagicMock()
    srv._stt.transcribe.return_value = TranscriptionResult(text="mock stt output", confidence=0.99)
    srv._tts = MagicMock()
    srv._tts.synthesize.return_value = TTSResult(success=True)
    srv._tts.is_speaking.return_value = False
    
    # Mock mic so it doesn't try to open pyaudio stream
    srv._mic = MagicMock()
    srv._mic.read.return_value = []
    
    return srv

def test_voice_service_speak(mock_service):
    mock_service.initialize()
    mock_service.speak("Say this testing string.")
    
    req = mock_service._tts.synthesize.call_args[0][0]
    assert req.text == "Say this testing string."
    
    mock_service.shutdown()
    
def test_voice_service_listen_skips_if_no_audio(mock_service):
    mock_service.initialize()
    res = mock_service.listen_and_transcribe()
    assert res is None # Since mic read yields nothing
    mock_service.shutdown()


