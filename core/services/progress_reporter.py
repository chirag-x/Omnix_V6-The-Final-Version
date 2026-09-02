"""
Omnix V6 — Progress reporter.

A small subscriber on the :class:`EventBus` that translates
events into :class:`SpeechItem` objects and feeds them to a
:class:`SpeechQueue`.  The reporter owns the narration rules and
keeps the engine's event-emission code free of speech concerns.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from loguru import logger

from core.events.event_bus import EventBus
from core.events.event_types import (
    CapabilityEvent,
    RequestEvent,
    TaskEvent,
    make_event,
)

from .progress_narration import narrate
from .speech_queue import SpeechItem, SpeechQueue


class ProgressReporter:
    """Subscribes to a bus and feeds a speech queue.

    The reporter is intentionally small: it filters events to the
    ones that have a narration entry and delegates templating to
    :func:`core.services.progress_narration.narrate`.  It does not
    call TTS directly — that is the speech queue's job.
    """

    def __init__(
        self,
        bus: EventBus,
        queue: SpeechQueue,
        *,
        terminal: bool = False,
    ) -> None:
        self._bus = bus
        self._queue = queue
        self._terminal = bool(terminal)
        self._lock = threading.RLock()
        self._unsubscribed = False
        # Track active correlation ids so we can suppress stale
        # narration after the request has already completed.
        self._active_correlation_ids: set[str] = set()

    def attach(self) -> None:
        with self._lock:
            if self._unsubscribed:
                return
            self._bus.subscribe("capability.event", self._on_event)
            self._bus.subscribe("task.event", self._on_event)
            self._bus.subscribe("request.event", self._on_event)

    def detach(self) -> None:
        with self._lock:
            if self._unsubscribed:
                return
            self._unsubscribed = True
        try:
            self._bus.unsubscribe("capability.event", self._on_event)
            self._bus.unsubscribe("task.event", self._on_event)
            self._bus.unsubscribe("request.event", self._on_event)
        except Exception:  # noqa: BLE001
            pass

    def _on_event(self, event: Any) -> None:
        # Filter by correlation when the event carries one so we do
        # not narrate events from concurrent sessions on the same bus.
        if isinstance(event, RequestEvent):
            cid = event.correlation_id
            if event.stage == "received":
                with self._lock:
                    self._active_correlation_ids.add(cid)
            item = narrate(event)
            if item is not None:
                item.correlation_id = cid or item.correlation_id
                if self._terminal:
                    logger.info(f"[progress/{item.kind}] {item.text}")
                self._queue.enqueue(item)
            if event.stage in ("completed", "cancelled", "timed_out", "rejected", "failed"):
                with self._lock:
                    self._active_correlation_ids.discard(cid)
            return
        if isinstance(event, (CapabilityEvent, TaskEvent)):
            cid = ""
            if hasattr(event, "metadata") and isinstance(event.metadata, dict):
                cid = event.metadata.get("correlation_id", "") or ""
            if cid:
                with self._lock:
                    if cid not in self._active_correlation_ids:
                        # Stale event from a previous session — ignore.
                        return
            item = narrate(event)
            if item is not None:
                item.correlation_id = cid or item.correlation_id
                if self._terminal:
                    logger.info(f"[progress/{item.kind}] {item.text}")
                self._queue.enqueue(item)

    def statistics(self) -> dict:
        return {
            "type": "ProgressReporter",
            "attached": not self._unsubscribed,
        }
