"""
Omnix V6 - Desktop Capabilities.

Provides implementations for observing and interacting with the desktop environment.
Uses Phase 2 Windows services (InputService, WindowService, ApplicationService).
"""

import asyncio
from typing import Any, Mapping, Optional

from core.capability import CapabilitySpec, CapabilityParameter, ParamType
from core.results import CapabilityResult, CapabilityStatus, ActionResult, ActionStatus
from .base import BaseCapability
from core.errors import OmnixError

# Note: In a complete implementation, these would accept dependencies (like InputService)
# For now, we will instantiate them or load them dynamically based on the platform.
# In a true DI setup, the engine would inject these dependencies. 
# We'll use a factory pattern or lazy loading for the services.

