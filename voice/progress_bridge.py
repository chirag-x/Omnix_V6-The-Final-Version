"""
Omnix V6 — Voice progress bridge.

Glues the :class:`core.services.progress_reporter.ProgressReporter`
to the :class:`core.services.speech_queue.SpeechQueue`.  Owning this
in voice/ keeps the core/ services free of voice-specific
imports.
"""

from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from core.events.event_bus import EventBus
from core.services.progress_reporter import ProgressReporter
from core.services.speech_queue import SpeechQueue


class VoiceProgressBridge:
    """Adapts :class:`EventBus` events to the engine-owned
    :class:`SpeechQueue` via a :class:`ProgressReporter`."""

    def __init__(
        self,
        bus: EventBus,
        queue: SpeechQueue,
        *,
        terminal: bool = False,
    ) -> None:
        self._reporter = ProgressReporter(bus, queue, terminal=terminal)

    def attach(self) -> None:
        self._reporter.attach()

    def detach(self) -> None:
        self._reporter.detach()

    def statistics(self) -> dict:
        return self._reporter.statistics()
