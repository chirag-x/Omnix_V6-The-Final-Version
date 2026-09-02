"""Tests for OmnixEngine"""
import pytest
from core.omnix_engine import OmnixEngine
from core.configuration import OmnixConfig
from core.lifecycle import LifecycleState
from pathlib import Path

def test_engine_init_shutdown():
    config = OmnixConfig(
        project_root=Path("."),
        data_dir=Path(".data"),
        log_dir=Path(".log"),
        env_file=Path(".env")
    )
    engine = OmnixEngine(config)
    engine.initialize()
    assert engine.lifecycle_state in (LifecycleState.READY, LifecycleState.RUNNING)
    engine.shutdown()
    assert engine.lifecycle_state == LifecycleState.STOPPED
