"""
Omnix V6 — Wake-word package.

A small, lifecycle-aware wake-word listener.  The listener
monitors the microphone and fires a callback when the configured
wake phrase ("Omnix" by default) is detected.

Design constraints:

* **No LLM** — detection is either an ``openwakeword`` neural
  model or a deterministic text match against a short STT snippet.
* **Lifecycle aware** — the listener can be enabled/disabled at
  runtime.  When disabled, it is fully silent (no microphone I/O).
* **Single source of truth** — the listener does not own TTS or
  STT state; it only produces a wake event.
"""
from .listener import WakeWordListener, WakeEvent, WakeBackend

__all__ = ["WakeWordListener", "WakeEvent", "WakeBackend"]
