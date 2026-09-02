"""
Omnix V6 — Speech queue.

A small, ordered, deduplicated, non-blocking speech queue.  The
voice layer enqueues items; a single worker thread pulls them in
``(priority desc, timestamp asc)`` order and hands each to a TTS
provider.

Design rules
------------

* **Determinism** — the queue's behaviour is fully described by the
  ordering + dedup rules in this file.  No LLM roundtrip.
* **Non-blocking** — :meth:`enqueue` returns immediately.  Callers
  are never blocked on TTS latency.
* **Dedup** — if a new item arrives whose ``text`` matches an item
  that is still queued or being spoken, the older one is dropped.
* **Priority** — items with higher priority are spoken first.  A
  ``result`` or ``failure`` outranks progress narration so the user
  always hears the outcome.
* **Cancellation** — items can be marked superseded; the worker
  drops them before they reach the TTS provider.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

from loguru import logger


@dataclass
class SpeechItem:
    text: str
    priority: int = 0
    correlation_id: str = ""
    kind: str = "progress"           # "progress" | "result" | "failure" | "announcement"
    timestamp: float = field(default_factory=time.time)
    deadline_s: float = 0.0          # max time before being superseded
    superseded: bool = False         # marked dead by cancel_pending
    # When True, the lifecycle gate (attached state controller in a
    # sleep transition) does NOT drop this item.  Use for the
    # "going to sleep" / "I'm awake" announcements and for any
    # critical user-facing line that must play even when the system
    # is otherwise asleep.  Default False: progress / result /
    # failure items are dropped during the sleep transition.
    bypass_sleep: bool = False
    # Optional provenance string for diagnostics — e.g.
    # "voice_response" or "task_narration".  Not used for ordering
    # but surfaced in logs and the spoken log ring.
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "priority": self.priority,
            "correlation_id": self.correlation_id,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "deadline_s": self.deadline_s,
            "superseded": self.superseded,
            "bypass_sleep": self.bypass_sleep,
            "source": self.source,
        }


# Default kind priority: result/failure > announcement > progress.
_KIND_BASE_PRIORITY = {
    "result": 200,
    "failure": 200,
    "announcement": 100,
    "progress": 50,
}


class SpeechQueue:
    """Ordered, deduplicated, priority-aware speech queue.

    The queue is **process-local** (no cross-process IPC).  The
    worker is started lazily on first :meth:`enqueue` so a host that
    never speaks (e.g. CI) does not pay the thread cost.
    """

    def __init__(self, *, on_speak: Optional[Callable[[SpeechItem], None]] = None,
                 autostart: bool = True) -> None:
        """Construct a :class:`SpeechQueue`.

        Parameters
        ----------
        on_speak:
            Callback invoked for each item the worker dequeues.
        autostart:
            When ``True`` (the default) the worker is started
            automatically on the first :meth:`enqueue`.  When
            ``False`` the host must call :meth:`start_worker` to
            begin consuming.  Tests that need to install
            ``on_speak`` after enqueuing use ``autostart=False``.
        """
        self._items: List[SpeechItem] = []
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._spoken_log: List[SpeechItem] = []
        self._on_speak = on_speak
        self._autostart = bool(autostart)
        # Lifecycle gate.  When a RuntimeStateController is attached
        # the queue drops non-bypass items while the controller is
        # in a sleep transition (SLEEPING / WAKING).  This is the
        # "wake-word listener owns the mic, the rest of the system
        # is silent" rule.
        self._state_controller: Optional[Any] = None
        # Metrics
        self._enqueued_total = 0
        self._deduped_total = 0
        self._spoken_total = 0
        self._cancelled_total = 0
        self._gated_during_sleep_total = 0

    def set_on_speak(self, on_speak: Optional[Callable[[SpeechItem], None]]) -> None:
        """Install (or replace) the TTS callback.  The new callback
        is used for every subsequent item the worker dequeues.  The
        method is safe to call from any thread."""
        with self._lock:
            self._on_speak = on_speak

    def attach_state_controller(self, controller: Optional[Any]) -> None:
        """Wire the queue to a :class:`RuntimeStateController`.

        While the controller is in a sleep transition (``SLEEPING`` /
        ``WAKING``) every non-bypass item is dropped at enqueue time.
        This is the "command STT owns the mic during the day,
        wake-word listener owns the mic at night" rule applied to
        outbound speech.  Pass ``None`` to disable the gate.
        """
        with self._lock:
            self._state_controller = controller

    # =========================================================== public
    def enqueue(self, item: SpeechItem) -> None:
        """Enqueue a speech item.  Never blocks."""
        if not isinstance(item, SpeechItem):
            return
        if not item.text:
            return
        # Lifecycle gate: if a controller is attached and the
        # system is in a sleep transition, drop non-bypass items
        # immediately.  Bypass items (e.g. the "going to sleep"
        # / "I'm awake" announcements) always pass.
        if not item.bypass_sleep and self._is_asleep():
            with self._lock:
                self._gated_during_sleep_total += 1
            return
        # Backfill the base priority from the kind if the caller did
        # not set one explicitly.
        if item.priority == 0:
            item.priority = _KIND_BASE_PRIORITY.get(item.kind, 50)
        with self._lock:
            self._enqueued_total += 1
            # Dedup: drop any older pending item with the same text.
            kept: List[SpeechItem] = []
            for existing in self._items:
                if existing.text == item.text and not existing.superseded:
                    self._deduped_total += 1
                    continue
                kept.append(existing)
            kept.append(item)
            # Keep the queue ordered: priority desc, timestamp asc.
            kept.sort(key=lambda x: (-x.priority, x.timestamp))
            self._items = kept
        self._ensure_worker()
        self._wake.set()

    def cancel_pending(self, *, kind: Optional[str] = None) -> int:
        """Mark queued items as superseded.  Returns the count cancelled."""
        with self._lock:
            n = 0
            for it in self._items:
                if it.superseded:
                    continue
                if kind is None or it.kind == kind:
                    it.superseded = True
                    n += 1
            self._cancelled_total += n
            return n

    def pending(self) -> List[SpeechItem]:
        with self._lock:
            return [it for it in self._items if not it.superseded]

    def spoken(self) -> List[SpeechItem]:
        with self._lock:
            return list(self._spoken_log)

    def wait_idle(self, timeout_s: float = 5.0) -> bool:
        """Block (up to ``timeout_s``) until the queue is drained."""
        deadline = time.time() + max(0.0, float(timeout_s))
        while time.time() < deadline:
            with self._lock:
                if not self._items:
                    return True
            time.sleep(0.05)
        return False

    def statistics(self) -> dict:
        with self._lock:
            return {
                "type": "SpeechQueue",
                "pending": sum(1 for it in self._items if not it.superseded),
                "enqueued_total": self._enqueued_total,
                "deduped_total": self._deduped_total,
                "spoken_total": self._spoken_total,
                "cancelled_total": self._cancelled_total,
                "gated_during_sleep_total": self._gated_during_sleep_total,
                "lifecycle_gated": self._state_controller is not None,
            }

    # =========================================================== worker
    def start_worker(self) -> None:
        """Start the worker thread (idempotent).  Useful for hosts
        that constructed the queue with ``autostart=False``."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run,
            name="omnix-speech-queue",
            daemon=True,
        )
        self._worker.start()

    def _ensure_worker(self) -> None:
        if not self._autostart:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self.start_worker()

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._take_next()
            if item is None:
                # No work; wait for a wake or a short tick.
                self._wake.wait(timeout=0.25)
                self._wake.clear()
                continue
            try:
                if self._on_speak is not None:
                    self._on_speak(item)
                else:
                    self._default_speak(item)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"SpeechQueue worker error: {exc!r}")
            with self._lock:
                self._spoken_total += 1
                self._spoken_log.append(item)
                # Bound the log to a small ring.
                if len(self._spoken_log) > 64:
                    self._spoken_log = self._spoken_log[-64:]

    def _take_next(self) -> Optional[SpeechItem]:
        with self._lock:
            now = time.time()
            # Drop superseded and expired items.
            kept: List[SpeechItem] = []
            for it in self._items:
                if it.superseded:
                    continue
                if it.deadline_s and now > it.timestamp + it.deadline_s:
                    continue
                kept.append(it)
            self._items = kept
            if not kept:
                return None
            kept.sort(key=lambda x: (-x.priority, x.timestamp))
            nxt = kept[0]
            self._items = kept[1:]
            return nxt

    def _is_asleep(self) -> bool:
        """True when the attached controller is in a sleep
        transition (``SLEEPING`` or ``WAKING``).

        Reads the controller's state without taking the queue lock —
        the controller's own lock makes this safe.  If no controller
        is attached or the state attribute is missing, returns False
        (the gate is effectively open).
        """
        ctrl = self._state_controller
        if ctrl is None:
            return False
        try:
            st = getattr(ctrl, "state", None)
        except Exception:  # noqa: BLE001
            return False
        if st is None:
            return False
        try:
            return bool(st.is_sleep_transition())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _default_speak(item: SpeechItem) -> None:
        """Default no-op speak.  Hosts inject a real TTS provider via
        ``on_speak=`` (the voice service does this in production)."""
        logger.debug(f"[speech/{item.kind}] {item.text}")

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=1.0)
