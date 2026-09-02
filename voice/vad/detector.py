import math
import numpy as np
from typing import Optional
from ..contracts import AudioChunk, VADError

class SimpleVAD:
    """Bounded Voice Activity Detection using energy thresholds."""
    
    def __init__(self, energy_threshold: float = 0.05,
                 silence_duration_s: float = 1.0,
                 speech_pad_s: float = 0.2):
        self._energy_threshold = energy_threshold
        self._silence_duration_s = silence_duration_s
        self._speech_pad_s = speech_pad_s
        
        self.is_speaking = False
        self._silence_accumulated_s = 0.0
        
    def reset(self):
        """Reset the VAD state"""
        self.is_speaking = False
        self._silence_accumulated_s = 0.0
        
    def process_chunk(self, chunk: AudioChunk) -> bool:
        """
        Process an audio chunk and return True if speech is detected.
        Updates internal state to track silence timeouts.
        """
        # Convert bytes to numpy array for energy calculation
        audio_data = np.frombuffer(chunk.data, dtype=np.int16)
        
        # Calculate RMS energy normalized to [0, 1]
        # max int16 is 32768
        if len(audio_data) == 0:
            rms = 0.0
        else:
            rms = np.sqrt(np.mean(np.square(audio_data.astype(np.float32)))) / 32768.0
            
        chunk_duration_s = len(audio_data) / chunk.format.sample_rate
        
        if rms > self._energy_threshold:
            self.is_speaking = True
            self._silence_accumulated_s = 0.0
            return True
        else:
            if self.is_speaking:
                self._silence_accumulated_s += chunk_duration_s
                if self._silence_accumulated_s > self._silence_duration_s:
                    self.is_speaking = False
                    return False
                return True
            else:
                return False

    def is_speech_ended(self) -> bool:
        """True if speech was happening but just ended via silence timeout."""
        return not self.is_speaking and self._silence_accumulated_s > self._silence_duration_s
