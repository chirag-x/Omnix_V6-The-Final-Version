"""
Omnix V6 — Execution Cycle Public Interface for Stage 19.0/19.2/19.3.

Public surface for the execution cycle module.
"""

from .cycle import ExecutionCycle, ExecutionPolicy
from .expectation import VerificationExpectation, ExpectationKind
from .result import (
    ExecutionResult,
    ExecutionStatus,
    ExecutionTrace,
    VerificationResult,
    VerificationStatus,
)
from .state import ExecutionState
from .step import ExecutionStep, StepAction
from .preconditions import (
    Precondition,
    PreconditionResult,
    PreconditionStatus,
    PreconditionProvider,
)
from .provider import (
    VerificationProvider,
    ActionExecutor,
    GroundingProvider,
    DefaultActionExecutor,
    DefaultGroundingProvider,
    DefaultVerificationProvider,
    create_default_action_router,
    create_default_grounding_provider,
    create_default_verification_provider,
)
from .sync import (
    SynchronizationStatus,
    SynchronizationContext,
    SynchronizationResult,
    SynchronizationProvider,
    DefaultSynchronizationProvider,
    create_default_synchronization_provider,
)
from .errors import (
    ExecutionError,
    ObservationFailedError,
    GroundingFailedError,
    ActionFailedError,
    VerificationFailedError,
    InvalidConfigurationError,
)

__all__ = [
    # Main classes
    "ExecutionCycle",
    "ExecutionPolicy",
    "ExecutionStep",
    "StepAction",
    "VerificationExpectation",
    "ExpectationKind",
    "ExecutionResult",
    "ExecutionStatus",
    "ExecutionTrace",
    "VerificationResult",
    "VerificationStatus",

    # Provider protocols
    "VerificationProvider",
    "ActionExecutor",
    "GroundingProvider",
    "SynchronizationProvider",

    # Default implementations
    "DefaultActionExecutor",
    "DefaultGroundingProvider",
    "DefaultVerificationProvider",
    "DefaultSynchronizationProvider",
    "create_default_action_router",
    "create_default_grounding_provider",
    "create_default_verification_provider",
    "create_default_synchronization_provider",

    # Synchronization models
    "SynchronizationStatus",
    "SynchronizationContext",
    "SynchronizationResult",

    # Errors
    "ExecutionError",
    "ObservationFailedError",
    "GroundingFailedError",
    "ActionFailedError",
    "VerificationFailedError",
    "InvalidConfigurationError",
]
