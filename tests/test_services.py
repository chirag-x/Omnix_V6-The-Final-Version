"""Tests for core/service_registry.py and health_monitor.py"""
import pytest
from core.service_registry import ServiceRegistry
from core.health_monitor import HealthMonitor, HealthStatus
from core.errors import ConfigurationError

class DummyService:
    def __init__(self, name="dummy"):
        self.name = name
        self.is_init = False
        
    def initialize(self) -> bool:
        self.is_init = True
        return True
        
    def shutdown(self) -> None:
        self.is_init = False
        
    @property
    def initialized(self) -> bool:
        return self.is_init
        
    def statistics(self):
        return {"status": "ok"}

def test_service_registry():
    registry = ServiceRegistry()
    s1 = DummyService("s1")
    s2 = DummyService("s2")
    
    registry.register(s1, name="s1")
    registry.register(s2, name="s2", dependencies=("s1",))
    
    with pytest.raises(ConfigurationError):
        registry.register(DummyService("s3"), name="s3", dependencies=("missing",))
        
    registry.initialize_all()
    assert s1.is_init
    assert s2.is_init
    
    assert registry.resolve("s1") is s1
    
    registry.shutdown_all()
    assert not s1.is_init
    assert not s2.is_init

def test_health_monitor():
    hm = HealthMonitor()
    s1 = DummyService("s1")
    
    hm.track("s1", s1)
    report = hm.report()
    assert "s1" in report["subsystems"]
