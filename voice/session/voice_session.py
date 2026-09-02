import threading
from typing import Optional, Callable
from ..contracts import VoiceState, VoiceTransitionError

class VoiceSession:
    """Manages Voice deterministic state machine transitions."""
    
    def __init__(self):
        self._state = VoiceState.IDLE
        self._lock = threading.Lock()
        
    @property
    def state(self) -> VoiceState:
        with self._lock:
            return self._state
            
    def transition(self, target_state: VoiceState):
        """Transition strictly to known targets based on current state."""
        with self._lock:
            if self._state == target_state:
                return # No-op if already there
            
            valid_targets = {
                VoiceState.IDLE: [VoiceState.LISTENING, VoiceState.SPEAKING, VoiceState.STOPPING, VoiceState.ERROR],
                VoiceState.LISTENING: [VoiceState.TRANSCRIBING, VoiceState.IDLE, VoiceState.STOPPING, VoiceState.ERROR],
                VoiceState.TRANSCRIBING: [VoiceState.PROCESSING, VoiceState.IDLE, VoiceState.STOPPING, VoiceState.ERROR],
                VoiceState.PROCESSING: [VoiceState.SPEAKING, VoiceState.IDLE, VoiceState.STOPPING, VoiceState.ERROR],
                VoiceState.SPEAKING: [VoiceState.IDLE, VoiceState.LISTENING, VoiceState.STOPPING, VoiceState.ERROR],
                VoiceState.ERROR: [VoiceState.IDLE, VoiceState.STOPPING],
                VoiceState.STOPPING: [VoiceState.IDLE],
            }
            
            allowed = valid_targets.get(self._state, [])
            if target_state not in allowed and target_state != VoiceState.ERROR:
                raise VoiceTransitionError(f"Cannot transition from {self._state.name} to {target_state.name}")
                
            self._state = target_state
