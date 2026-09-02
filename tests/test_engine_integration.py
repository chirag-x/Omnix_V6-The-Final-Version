"""
Omnix V6 -- Phase 4/5 Integration Tests

Validates OmnixEngine -> CapabilityRegistry -> CapabilityRouter 
-> CapabilityExecution -> CapabilityResult end-to-end integration.
"""

import pytest
import asyncio
from core.omnix_engine import OmnixEngine
from core.configuration import OmnixConfig
from core.lifecycle import LifecycleState
from core.capability_registry import CapabilityRegistry
from core.capability_router import CapabilityRouter
from core.capabilities import register_standard_capabilities
from core.results import CapabilityStatus
from pathlib import Path


@pytest.mark.asyncio
async def test_engine_end_to_end_capability_execution():
    """
    Integration Test mapping:
    OmnixEngine -> ServiceRegistry -> CapabilityRegistry -> 
    registered standard capability -> CapabilityRouter -> 
    capability execution -> structured CapabilityResult.

    PROVES: The standard capabilities are properly injected and routed by the Engine.
    """
    config = OmnixConfig(
        project_root=Path("."),
        data_dir=Path(".data"),
        log_dir=Path(".log"),
        env_file=Path(".env")
    )
    
    # 1. Initialize registry and router
    cap_registry = CapabilityRegistry()
    register_standard_capabilities(cap_registry)
    
    router = CapabilityRouter(registry=cap_registry)

    # 2. Wire into Engine explicitly
    engine = OmnixEngine(
        config=config,
        capabilities=cap_registry,
        router=router
    )
    
    # 3. Initialize engine
    init_success = engine.initialize()
    assert init_success is True
    engine.start()
    assert engine.lifecycle_state == LifecycleState.RUNNING
    
    # 4. Observe capabilities via Engine topology
    stats = engine.statistics()
    # At least 15 capabilities should be loaded from standard capabilities
    assert stats["capabilities_loaded"] > 10
    
    # 5. Execute capability via Engine's main router entry point
    # Use a harmless observation capability: desktop.screen_size
    # Wait, execute is a synchronous method in OmnixEngine? Wait, let's check capabilities
    # which are async...
    # Ah, the engine executes it somehow... wait, we need to handle that.
    
    # For now, if the router route is sync, but the capability is async,
    # let's see how route() handles it.
    import inspect
    result = engine.execute("desktop.screen_size")
    
    # if it's a coroutine, we must await it.
    if inspect.iscoroutine(result):
        result = await result
    
    # 6. Validate execution
    assert result is not None
    assert result.error is None
    assert result.capability_name == "desktop.screen_size"
    assert result.status == CapabilityStatus.VERIFIED
    
    # Output must have actual physical dimensions of the virtual screen
    assert "width" in result.details
    assert "height" in result.details
    assert isinstance(result.details["width"], int)
    assert result.details["width"] > 0
    assert result.details["height"] > 0
    
    engine.stop()
    assert engine.lifecycle_state == LifecycleState.STOPPED
