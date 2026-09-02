"""Tests for event bus and event types"""
import pytest
from core.events.event_types import CapabilityEvent
from core.events.event_bus import EventBus

def test_event_bus():
    bus = EventBus(name="test")
    collected = []

    def handler(evt):
        collected.append(evt)

    sub_id = bus.subscribe("capability.*", handler)
    evt = CapabilityEvent(capability="read", transition="executed")
    bus.publish(evt)

    assert len(collected) == 1
    assert collected[0].capability == "read"

    bus.unsubscribe(sub_id)
    bus.publish(CapabilityEvent(capability="write", transition="executed"))
    assert len(collected) == 1
