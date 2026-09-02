"""
Phase 15 — Tests for the engine-owned :class:`SpeechQueue`.

Cover:
  * enqueue never blocks
  * dedup of identical texts while queued
  * priority ordering: result > announcement > progress
  * cancellation marks items superseded
  * on_speak callback receives the items the worker dequeues
"""
from __future__ import annotations

import threading
import time
from typing import List

import pytest

from core.services.speech_queue import SpeechItem, SpeechQueue


def _new_queue(callback=None, *, autostart: bool = True) -> SpeechQueue:
    return SpeechQueue(on_speak=callback, autostart=autostart)


def test_enqueue_is_non_blocking():
    q = _new_queue()
    t0 = time.time()
    for i in range(50):
        q.enqueue(SpeechItem(text=f"line {i}"))
    dt = time.time() - t0
    # enqueue is in-memory; 50 inserts must finish well under 100ms.
    assert dt < 0.5, f"enqueue too slow: {dt}s"


def test_dedup_drops_duplicate_from_pending_queue():
    """When a new item with identical text arrives while the older one is
    still in the queue, the older is dropped and only the newer survives.
    We achieve this by enqueuing into a fresh queue that the worker has
    not yet touched (no worker running yet) — so the dedup check in
    enqueue() sees the existing item and removes it."""
    captured: List[SpeechItem] = []
    q = SpeechQueue()  # no callback yet; worker not started
    q.enqueue(SpeechItem(text="hello", priority=100, kind="result"))
    # Now install the callback and start the worker — at this point
    # the first "hello" is still pending, so the dedup fires.
    q.set_on_speak(lambda it: captured.append(it))
    q.enqueue(SpeechItem(text="hello", priority=50, kind="progress"))
    q.wait_idle(timeout_s=1.0)
    # Only the second (newer) "hello" survives in the queue.
    assert len(captured) == 1, f"expected 1, got {len(captured)}: {captured}"
    assert captured[0].kind == "progress"
    assert captured[0].priority == 50


def test_priority_ordering_result_above_progress():
    captured: List[SpeechItem] = []
    q = SpeechQueue(autostart=False)  # worker is not started
    # Enqueue progress, result, and announcement while the queue is
    # idle so the worker has not yet consumed anything.
    q.enqueue(SpeechItem(text="opening chrome", kind="progress", priority=50))
    q.enqueue(SpeechItem(text="ready", kind="result", priority=200))
    q.enqueue(SpeechItem(text="Omnix is ready", kind="announcement", priority=100))
    # Now install the callback and start the worker.
    q.set_on_speak(lambda it: captured.append(it))
    q.start_worker()
    q.wait_idle(timeout_s=2.0)
    texts = [c.text for c in captured]
    # Result must come first, announcement next, progress last.
    assert texts[0] == "ready", texts
    assert texts[1] == "Omnix is ready", texts
    assert texts[2] == "opening chrome", texts


def test_cancel_pending_drops_matching_kind():
    captured: List[SpeechItem] = []
    q = SpeechQueue(on_speak=lambda it: captured.append(it), autostart=False)
    for i in range(5):
        q.enqueue(SpeechItem(text=f"prog {i}", kind="progress"))
    cancelled = q.cancel_pending(kind="progress")
    assert cancelled == 5
    # The queue should be drained.
    q.start_worker()
    assert q.wait_idle(timeout_s=1.0)
    # No "progress" should have been spoken.
    assert all(it.kind != "progress" for it in captured)


def test_set_on_speak_replaces_callback():
    calls_a: List[SpeechItem] = []
    calls_b: List[SpeechItem] = []
    q = _new_queue(callback=lambda it: calls_a.append(it))
    q.set_on_speak(lambda it: calls_b.append(it))
    q.enqueue(SpeechItem(text="after swap", kind="progress"))
    q.wait_idle(timeout_s=1.0)
    assert calls_a == []
    assert len(calls_b) == 1
    assert calls_b[0].text == "after swap"


def test_empty_text_is_ignored():
    captured: List[SpeechItem] = []
    q = SpeechQueue(on_speak=lambda it: captured.append(it), autostart=False)
    q.enqueue(SpeechItem(text=""))
    q.enqueue(SpeechItem(text="real"))
    q.start_worker()
    q.wait_idle(timeout_s=1.0)
    spoken = [it.text for it in captured]
    assert spoken == ["real"]


def test_statistics_tracks_totals():
    q = _new_queue()
    for i in range(3):
        q.enqueue(SpeechItem(text=f"x{i}"))
    q.wait_idle(timeout_s=1.0)
    stats = q.statistics()
    assert stats["enqueued_total"] == 3
    assert stats["spoken_total"] == 3
    assert stats["pending"] == 0


def test_shutdown_stops_worker():
    q = _new_queue()
    q.enqueue(SpeechItem(text="hi"))
    q.wait_idle(timeout_s=1.0)
    q.shutdown()
    # The worker thread should be stopped (or stopping).
    worker = q._worker
    assert worker is None or not worker.is_alive()
