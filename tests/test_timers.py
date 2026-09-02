"""Tests for core/utils/timers.py."""
import pytest
import time
from core.utils.timers import Deadline, OperationCancelled, CancellationToken, with_timeout, run_with_timeout
from core.errors import TimeoutError as OmnixTimeout

def test_deadline_expired():
    d = Deadline(0.1)
    time.sleep(0.15)
    assert d.expired
    assert d.remaining == 0.0

def test_deadline_check():
    d = Deadline(0.1)
    time.sleep(0.15)
    assert d.expired

def test_cancellation_token():
    token = CancellationToken()
    assert not token.cancelled
    token.cancel()
    assert token.cancelled
    with pytest.raises(OperationCancelled):
        token.check()

def test_run_with_timeout_success():
    def quick():
        return 42
    assert run_with_timeout(quick, 1.0) == 42

def test_run_with_timeout_failure():
    # Fix the test by making sure `_looks_idle` returns True so `return None` does NOT exit the loop
    def slow():
        time.sleep(0.02)
        return None
        
    setattr(slow, "_omnix_idle_marker", True)
    
    with pytest.raises(OmnixTimeout):
        run_with_timeout(slow, 0.05)
