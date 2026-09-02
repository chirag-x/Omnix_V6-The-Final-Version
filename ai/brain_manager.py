"""
Omnix V6 — Brain package re-export (Phase 5C+5D).

The historical ``ai.brain_manager`` module was an empty stub.  Phase
5C+5D moves the real implementation into the :mod:`ai.brain`
package; this file is a *re-export shim* so any old import paths
keep working.

The canonical imports are:

    from ai.brain import Brain, LLMPlanner, DeterministicPlanner
    from ai.brain import CapabilitySummary, discover_capabilities
    from ai.brain import validate_plan_payload
    from ai.brain.exceptions import ProviderFailure, ...

This file intentionally does NOT add new logic.  It only re-exports
the public surface for backward compatibility.
"""

from __future__ import annotations

from ai.brain import (
    ALLOWED_SAFETY_CLASSIFICATIONS,
    Brain,
    BrainResult,
    CapabilitySummary,
    DeterministicPlanner,
    LLMCallRecord,
    LLMCallTracker,
    LLMPlanner,
    MAX_PLAN_STEPS,
    MAX_RETRIES,
    MAX_STEP_TIMEOUT_S,
    MAX_SUMMARY_BYTES_PER_CAPABILITY,
    MAX_TOTAL_SUMMARY_BYTES,
    MIN_RETRIES,
    MIN_STEP_TIMEOUT_S,
    RecoveryClassifier,
    RecoveryDecision,
    RecoveryStrategy,
    RequestRouter,
    RoutingDecision,
    StepStatus,
    StepTrace,
    System2Brain,
    System2BrainResult,
    Task,
    TaskFactory,
    TaskKind,
    TaskPriority,
    TaskProgressEvent,
    TaskStatus,
    VerificationRecord,
    discover_capabilities,
    find_capability,
    narrate,
    now,
    required_parameter_names,
    summarize_for_prompt,
    validate_plan_payload,
)
from ai.brain.exceptions import (
    BrainError,
    CannotPlanError,
    CancelledError,
    ClarificationRequired,
    InvalidArgumentError,
    InvalidDependencyError,
    InvalidExpectedEffectError,
    InvalidTimeoutError,
    MalformedPlanPayload,
    PlanSizeExceeded,
    ProviderCancelled,
    ProviderFailure,
    ProviderMalformedResponse,
    ProviderTimeout,
    SafetyClassificationError,
    UnknownCapabilityError,
)
from ai.brain.recovery.classification import FailureKind


__all__ = [
    "ALLOWED_SAFETY_CLASSIFICATIONS",
    "Brain",
    "BrainResult",
    "CapabilitySummary",
    "DeterministicPlanner",
    "LLMCallRecord",
    "LLMCallTracker",
    "LLMPlanner",
    "MAX_PLAN_STEPS",
    "MAX_RETRIES",
    "MAX_STEP_TIMEOUT_S",
    "MAX_SUMMARY_BYTES_PER_CAPABILITY",
    "MAX_TOTAL_SUMMARY_BYTES",
    "MIN_RETRIES",
    "MIN_STEP_TIMEOUT_S",
    "RecoveryClassifier",
    "RecoveryDecision",
    "RecoveryStrategy",
    "RequestRouter",
    "RoutingDecision",
    "StepStatus",
    "StepTrace",
    "System2Brain",
    "System2BrainResult",
    "Task",
    "TaskFactory",
    "TaskKind",
    "TaskPriority",
    "TaskProgressEvent",
    "TaskStatus",
    "VerificationRecord",
    "discover_capabilities",
    "find_capability",
    "narrate",
    "now",
    "required_parameter_names",
    "summarize_for_prompt",
    "validate_plan_payload",
    # Exceptions
    "BrainError",
    "CannotPlanError",
    "CancelledError",
    "ClarificationRequired",
    "FailureKind",
    "InvalidArgumentError",
    "InvalidDependencyError",
    "InvalidExpectedEffectError",
    "InvalidTimeoutError",
    "MalformedPlanPayload",
    "PlanSizeExceeded",
    "ProviderCancelled",
    "ProviderFailure",
    "ProviderMalformedResponse",
    "ProviderTimeout",
    "SafetyClassificationError",
    "UnknownCapabilityError",
]
