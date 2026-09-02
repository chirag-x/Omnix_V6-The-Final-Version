import sounddevice as sd
import numpy as np
import queue
import threading
from typing import Optional, Generator
from ..contracts import AudioChunk, AudioFormat, MicrophoneError

class MicrophoneInput:
    """Canonical microphone abstraction."""
    
    def __init__(self, device: Optional[int] = None, format: Optional[AudioFormat] = None):
        self._device = device
        self._format = format or AudioFormat()
        self._stream: Optional[sd.InputStream] = None
        self._queue: queue.Queue = queue.Queue()
        self._running: bool = False
        self._lock = threading.Lock()
        
    def _callback(self, indata, frames, time, status):
        """Called for each audio block by sounddevice."""
        if status:
            # Handle potential overflow or underflow seamlessly
            pass
            
        if self._running:
            # indata is shape (frames, channels), dtype=np.int16
            chunk_data = indata.tobytes()
            self._queue.put(AudioChunk(
                data=chunk_data,
                format=self._format,
                timestamp=time.inputBufferAdcTime
            ))

    def start(self):
        """Start listening to the microphone."""
        with self._lock:
            if self._running:
                return
                
            try:
                self._stream = sd.InputStream(
                    device=self._device,
                    samplerate=self._format.sample_rate,
                    channels=self._format.channels,
                    dtype='int16',
                    callback=self._callback
                )
                self._stream.start()
                self._running = True
                
                # Clear existing queue
                while not self._queue.empty():
                    self._queue.get_nowait()
                    
            except Exception as e:
                raise MicrophoneError(f"Failed to start microphone: {e}") from e

    def stop(self):
        """Stop listening."""
        with self._lock:
            if not self._running:
                return
            
            self._running = False
            if self._stream:
                self._stream.stop()
                self._stream.close()
                self._stream = None

    def read(self) -> Generator[AudioChunk, None, None]:
        """Generator to continuously read chunks while started."""
        while self._running:
            try:
                chunk = self._queue.get(timeout=0.1)
                yield chunk
            except queue.Empty:
                continue

    def close(self):
        """Release all resources."""
        self.stop()
