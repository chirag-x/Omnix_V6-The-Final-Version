"""
Omnix V6 — EventBus (R-11 / Phase 1 §16).

A *synchronous* event bus with priority and wildcard subscription.

Why sync, not async:
    - The engine's hot path is a worker thread that invokes the
      capability router and waits for the result.  An async bus adds
      bookkeeping (loop, queue, scheduler) for a benefit we will
      not realize in Phase 1.
    - Subsystems that want to *react* to events do so on the bus's
      caller thread.  Long-running reactions must spawn their own
      worker (and respect :class:`~core.utils.timers.CancellationToken`).

Why priority:
    - A ``HealthEvent`` reporting "subsystem degraded" should land
      before a stream of ``WorldEvent`` notifications so the recovery
      layer can act first.

Why wildcards:
    - A debug panel that wants to see *everything* subscribes to
      ``"*"`` once.  An audit log that wants only failures subscribes
      to ``"*.failed"``.

Contract (R-11):
    - Subscribers receive the event by *value* (frozen dataclass).
    - A subscriber that raises does *not* stop other subscribers.
    - The bus is *not* a transactional log; it does not guarantee
      delivery to a subscriber that was registered *after* the event
      was emitted (events are fire-and-forget).
    - The bus is *not* a global singleton; the engine owns one and
      hands it to subsystems (R-1).
"""

from __future__ import annotations

import fnmatch
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from loguru import logger as _loguru

from .event_types import Event


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

SubscriberFn = Callable[[Event], None]


@dataclass(frozen=True)
class Subscription:
    """A registered subscriber.  Returned by :meth:`EventBus.subscribe`."""

    subscriber_id: int
    pattern: str
    priority: int
    fn: SubscriberFn = field(compare=False)
    once: bool = False

    def matches(self, event: Event) -> bool:
        return fnmatch.fnmatchcase(event.name, self.pattern)


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

class EventBus:
    """Synchronous, priority-ordered, wildcard-supporting event bus.

    Threading
    ---------
    All public methods are safe to call from any thread.  Internal
    state is guarded by a single ``RLock``; the lock is held only
    while mutating the subscription list, not while invoking
    subscribers (so a slow subscriber does not block registration).
    """

    def __init__(self, *, name: str = "default") -> None:
        self._name = name
        self._subs: List[Subscription] = []
        self._lock = threading.RLock()
        self._next_id = 1
        # counters for diagnostics
        self._publish_count = 0
        self._deliver_count = 0
        self._error_count = 0

    # ---------------------------------------------------------- name
    @property
    def name(self) -> str:
        return self._name

    # ============================================================ api
    def subscribe(
        self,
        pattern: str,
        fn: SubscriberFn,
        *,
        priority: int = 0,
        once: bool = False,
    ) -> int:
        """Register ``fn`` to be called for events whose name matches ``pattern``.

        Parameters
        ----------
        pattern:
            A shell-style glob (``"capability.*"``, ``"*"``, ``"task.event"``).
            Compared against :attr:`Event.name`.
        priority:
            Higher priority runs first.  Default 0.  Used so health
            events land before informational noise.
        once:
            If ``True``, the subscription is removed after the first
            matching event.  Useful for one-shot wake-up listeners.

        Returns
        -------
        int
            The subscription id; pass to :meth:`unsubscribe` to remove.
        """
        with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            sub = Subscription(
                subscriber_id=sub_id,
                pattern=pattern,
                priority=priority,
                fn=fn,
                once=once,
            )
            # keep the list sorted by descending priority; stable
            # in registration order within the same priority
            self._subs.append(sub)
            self._subs.sort(key=lambda s: (-s.priority, s.subscriber_id))
            return sub_id

    def unsubscribe(self, subscriber_id: int) -> bool:
        """Remove the subscription with this id.  Returns True if removed."""
        with self._lock:
            for i, s in enumerate(self._subs):
                if s.subscriber_id == subscriber_id:
                    del self._subs[i]
                    return True
            return False

    def clear(self) -> None:
        """Remove every subscription.  Used by tests."""
        with self._lock:
            self._subs.clear()

    # ======================================================== publish
    def publish(self, event: Event) -> int:
        """Synchronously deliver ``event`` to every matching subscriber.

        Returns the number of subscribers invoked.  A subscriber that
        raises is logged and skipped; the next subscriber still runs.
        """
        if not isinstance(event, Event):
            raise TypeError(
                f"EventBus.publish expected an Event, got {type(event).__name__}"
            )
        with self._lock:
            subs = [s for s in self._subs if s.matches(event)]
            once_ids: Tuple[int, ...] = tuple(
                s.subscriber_id for s in subs if s.once
            )
        self._publish_count += 1
        delivered = 0
        for s in subs:
            try:
                s.fn(event)
                delivered += 1
                self._deliver_count += 1
            except Exception as exc:  # noqa: BLE001
                # never let a bad subscriber kill the loop
                self._error_count += 1
                _loguru.warning(
                    "EventBus[{}] subscriber {} raised on event {!r}: {}",
                    self._name,
                    s.subscriber_id,
                    event.name,
                    exc,
                )
        if once_ids:
            with self._lock:
                self._subs = [s for s in self._subs if s.subscriber_id not in once_ids]
        return delivered

    # ======================================================= queries
    def subscription_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "type": "EventBus",
                "name": self._name,
                "subscriptions": len(self._subs),
                "publish_count": self._publish_count,
                "deliver_count": self._deliver_count,
                "error_count": self._error_count,
            }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"EventBus(name={self._name!r}, subs={self.subscription_count()})"
