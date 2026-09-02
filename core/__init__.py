"""
Omnix V6 — Core Foundation (Phase 1).

Public exports for the engine and system execution foundation.
Imports inside Omnix subsystems should generally point here for 
shared domain models, events, and results to avoid coupling to internal
structure.
"""

from __future__ import annotations

# Configuration
from .configuration import OmnixConfig, load

# Errors
from .errors import (
    CapabilityError,
    ConfigurationError,
    DependencyError,
    ExecutionError,
    ObservationError,
    OmnixError,
    RecoveryError,
    TimeoutError,
    ValidationError,
    VerificationError,
)

# Results
from .results import (
    ActionResult,
    ActionStatus,
    CapabilityResult,
    CapabilityStatus,
    ObservationResult,
    ObservationStatus,
    TaskResult,
    TaskStatus,
    VerificationResult,
    VerificationStatus,
)

# Application Domain State
from .state.domain import TaskState, WorldState, WindowState
from .state.contexts import ConversationContext, EntityContext, UserContext, ConversationTurn, Entity

# Lifecycle & Capabilities
from .lifecycle import LifecycleState, LifecycleMixin
from .capability import Capability, CapabilitySpec

# Events
from .events.event_types import Event, EngineEvent, CapabilityEvent, ErrorEvent
from .events.event_bus import EventBus

# Timing
from .utils.timers import Deadline, CancellationToken, OperationCancelled, with_timeout, run_with_timeout

# Engine
from .omnix_engine import OmnixEngine

__all__ = [
    "OmnixConfig",
    "load",
    "OmnixError",
    "ConfigurationError",
    "DependencyError",
    "CapabilityError",
    "ValidationError",
    "ExecutionError",
    "TimeoutError",
    "ObservationError",
    "VerificationError",
    "RecoveryError",
    "ActionResult",
    "ActionStatus",
    "CapabilityResult",
    "CapabilityStatus",
    "ObservationResult",
    "ObservationStatus",
    "VerificationResult",
    "VerificationStatus",
    "TaskResult",
    "TaskStatus",
    "TaskState",
    "WorldState",
    "WindowState",
    "ConversationContext",
    "EntityContext",
    "UserContext",
    "ConversationTurn",
    "Entity",
    "LifecycleState",
    "LifecycleMixin",
    "Capability",
    "CapabilitySpec",
    "Event",
    "EngineEvent",
    "CapabilityEvent",
    "ErrorEvent",
    "EventBus",
    "Deadline",
    "CancellationToken",
    "OperationCancelled",
    "with_timeout",
    "run_with_timeout",
    "OmnixEngine",
]
