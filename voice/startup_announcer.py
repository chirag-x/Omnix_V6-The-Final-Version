"""
Omnix V6 — Startup announcer.

Speaks "Omnix is ready. How can I help you?" *after* the readiness
gate confirms every critical subsystem is up.  The announcer is
intentionally a tiny wrapper around the SpeechQueue — it does not
touch the TTS provider directly.  Voice still owns the queue.
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from core.services.speech_queue import SpeechItem, SpeechQueue


class StartupAnnouncer:
    def __init__(
        self,
        speech_queue: SpeechQueue,
        *,
        text: str = "Omnix is ready. How can I help you?",
    ) -> None:
        self._queue = speech_queue
        self._text = text
        self._announced = False

    def announce(self) -> None:
        if self._announced:
            return
        item = SpeechItem(
            text=self._text,
            priority=1000,
            kind="announcement",
        )
        self._queue.enqueue(item)
        self._announced = True

    def statistics(self) -> dict:
        return {"type": "StartupAnnouncer", "announced": self._announced}
