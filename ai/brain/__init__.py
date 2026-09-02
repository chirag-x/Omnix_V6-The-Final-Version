"""
Omnix V6 — Brain / Planner package (Phase 5C+5D).

The Brain turns a user utterance into a trusted :class:`Plan`.  It
sits between the Phase 5B :class:`LLMIntentInterpreter` and the
future Agent / PlanExecutor.

Public surface:

    * :class:`Brain`                — the two-stage pipeline
    * :class:`BrainResult`          — the structured result
    * :class:`LLMPlanner`           — the LLM-backed planner
    * :class:`DeterministicPlanner` — the rule-based planner
    * :class:`CapabilitySummary`    — a planner-friendly view of a
                                      capability
    * :func:`discover_capabilities` — capability discovery
    * :func:`validate_plan_payload` — the plan validation gate
    * Exception hierarchy:          :mod:`ai.brain.exceptions`

Architectural isolation (mirrors :mod:`ai.intent`):

    This package MUST NOT import or use any of:

        * :mod:`subprocess`
        * :mod:`pyautogui`
        * :mod:`win32gui` / :mod:`win32api`
        * :mod:`ctypes`
        * :mod:`core.capability_router`
        * :mod:`core.omnix_engine`
        * any V6 *Windows service* (e.g. ``system.windows.*``,
          ``system.applications.*``)
        * any V6 *executor* (the Brain does not import the engine)

    Tests in ``tests/test_brain_isolation.py`` enforce this.
"""

from __future__ import annotations

from .brain import Brain, BrainResult
from .deterministic import DeterministicPlanner
from .llm_tracking import LLMCallTracker
from .narration import TaskProgressEvent, narrate
from .recovery.classification import (
    FailureKind,
    RecoveryClassifier,
    RecoveryDecision,
    RecoveryStrategy,
)
from .router import RequestRouter, RoutingDecision
from .system2 import System2Brain, System2BrainResult
from .task.models import (
    LLMCallRecord,
    StepStatus,
    StepTrace,
    Task,
    TaskFactory,
    TaskKind,
    TaskPriority,
    TaskStatus,
    VerificationRecord,
    now,
)
from .discovery import (
    CapabilitySummary,
    discover_capabilities,
    find_capability,
    required_parameter_names,
    summarize_for_prompt,
    MAX_SUMMARY_BYTES_PER_CAPABILITY,
    MAX_TOTAL_SUMMARY_BYTES,
)
from .exceptions import (
    BrainError,
    CannotPlanError,
    CancelledError,
    ClarificationRequired,
    InvalidArgumentError,
    InvalidBrowserMetadataError,
    InvalidDependencyError,
    InvalidExpectedEffectError,
    InvalidTimeoutError,
    InvalidVisionMetadataError,
    MalformedPlanPayload,
    PlanSizeExceeded,
    ProviderCancelled,
    ProviderFailure,
    ProviderMalformedResponse,
    ProviderTimeout,
    SafetyClassificationError,
    UnknownCapabilityError,
)
from .llm_planner import LLMPlanner
from .validation import (
    validate_plan_payload,
    MAX_PLAN_STEPS,
    MIN_STEP_TIMEOUT_S,
    MAX_STEP_TIMEOUT_S,
    MIN_RETRIES,
    MAX_RETRIES,
    ALLOWED_SAFETY_CLASSIFICATIONS,
)


__all__ = [
    # Brain
    "Brain",
    "BrainResult",
    # System 2 Brain (Phase 17)
    "System2Brain",
    "System2BrainResult",
    "RequestRouter",
    "RoutingDecision",
    "TaskFactory",
    "Task",
    "TaskKind",
    "TaskPriority",
    "TaskStatus",
    "StepStatus",
    "StepTrace",
    "VerificationRecord",
    "LLMCallRecord",
    "LLMCallTracker",
    "TaskProgressEvent",
    "narrate",
    "FailureKind",
    "RecoveryStrategy",
    "RecoveryClassifier",
    "RecoveryDecision",
    "now",
    # Planners
    "LLMPlanner",
    "DeterministicPlanner",
    # Discovery
    "CapabilitySummary",
    "discover_capabilities",
    "summarize_for_prompt",
    "find_capability",
    "required_parameter_names",
    "MAX_SUMMARY_BYTES_PER_CAPABILITY",
    "MAX_TOTAL_SUMMARY_BYTES",
    # Validation
    "validate_plan_payload",
    "MAX_PLAN_STEPS",
    "MIN_STEP_TIMEOUT_S",
    "MAX_STEP_TIMEOUT_S",
    "MIN_RETRIES",
    "MAX_RETRIES",
    "ALLOWED_SAFETY_CLASSIFICATIONS",
    # Exceptions
    "BrainError",
    "CannotPlanError",
    "CancelledError",
    "ClarificationRequired",
    "InvalidArgumentError",
    "InvalidBrowserMetadataError",
    "InvalidDependencyError",
    "InvalidExpectedEffectError",
    "InvalidTimeoutError",
    "InvalidVisionMetadataError",
    "MalformedPlanPayload",
    "PlanSizeExceeded",
    "ProviderCancelled",
    "ProviderFailure",
    "ProviderMalformedResponse",
    "ProviderTimeout",
    "SafetyClassificationError",
    "UnknownCapabilityError",
]
