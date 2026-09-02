"""
Omnix V6 — AI Orchestration Foundation (Phase 4).

This package provides the *domain contracts* for the AI Orchestration
Foundation: the typed data shapes and Protocol interfaces that the
planner, executor, and recovery layer will compose against.

It does **not** build the autonomous agent.  The models here are
frozen dataclasses with explicit ``to_dict`` projections; the
interfaces are :class:`typing.Protocol` declarations with no
implementation.

Architectural invariants honored here (from V6_ARCHITECTURE_RULES.md):

- R-21: ``ActionRequest`` carries only registered capability names.
  No raw shell commands, no eval/exec, no subprocess.  The set of
  allowed action types is closed.
- R-23: ``ExecutionContext`` is a read-only projection over the five
  ``ContextService`` containers; it never mutates them.
- R-24: ``Intent`` is the *internal* structured representation, not a
  user-facing command.  ``Goal`` is the user-facing concept that
  flows into planning.
- R-8 / R-10: All models are immutable (``frozen=True``); mutation
  is expressed via ``with_*`` methods returning new instances.
"""

from __future__ import annotations

# Models (the data shapes)
from .models import (
    # Top-level orchestration
    Goal,
    Intent,
    IntentKind,
    IntentParamType,
    Plan,
    PlanStep,
    PlanStatus,
    # Action & execution
    ActionRequest,
    ActionKind,
    ExecutionContext,
    # Observation loop
    Observation,
    ObservationSource,
    ExpectedEffect,
    # Verification
    Verifier,
    VerificationVerdict,
    # Recovery
    Failure,
    FailureKind,
    RecoveryDecision,
    RecoveryAction,
    # Stats
    count_decorator,
)

# Interface contracts
from .interfaces import (
    IntentInterpreter,
    Planner,
    PlanExecutor,
    RecoveryEngine,
    Orchestrator,
)

# Phase 6A+6B: execution-side result contracts
from .execution_result import (
    ExecutionOutcome,
    ExecutionResult,
    StepResult,
    StepState,
    make_blank_execution_result,
    new_correlation_id,
)

# Phase 6A+6B: the concrete PlanExecutor
from .plan_executor import (
    PlanExecutor as _ConcretePlanExecutor,
    PlanExecutorError,
    InvalidPlanError,
    IdempotencyViolation,
    CancellationRequested,
    DangerousAuthorizer,
)

# Re-export the concrete executor as ``PlanExecutorImpl`` so the
# Protocol keeps the name ``PlanExecutor`` in the type namespace.
# ``PlanExecutor`` in this package is the Protocol from interfaces.py
# (it has been since Phase 4); the concrete implementation is
# importable as ``core.orchestration.PlanExecutorImpl``.
PlanExecutorImpl = _ConcretePlanExecutor

# Phase 6C: Agent Orchestrator contracts
from .agent_result import (
    AgentState,
    AgentResult,
    ObservationEntry,
    PlanHistoryEntry,
    new_agent_run_id,
    make_blank_agent_result,
)

# Phase 6C: Observation / Verifier / Recovery
from .observation import (
    ObservationProvider,
    CapabilityResultObservationProvider,
)
from .verifier import (
    DefaultStepVerifier,
    DefaultGoalVerifier,
    passed_verdict,
    failed_verdict,
    uncertain_verdict,
)
from .verifier_router import (
    VerifierRouter,
    build_default_router,
    DEFAULT_DESKTOP_CAPABILITIES,
)
from .recovery import (
    RecoveryPolicy,
    DefaultRecoveryEngine,
    make_failure,
)
from .failure_classifier import (
    FailureClassifier,
    CODE_TO_KIND,
)

# Alias the concrete recovery engine under its Protocol name for
# convenience.  This lets callers do
# ``from core.orchestration import RecoveryEngine`` and get the
# concrete default, while still being able to import the Protocol
# directly from ``core.orchestration.interfaces``.
# (The Protocol is the ``RecoveryEngine`` in the interfaces import
# block below; the concrete one is also exported as
# ``DefaultRecoveryEngine``.)

# Phase 6C: the concrete Agent
from .agent import (
    Agent,
    AgentPolicy,
)

# Phase 14: multi-step execution layers (additive — do not duplicate Agent/Executor).
from .step_state import (
    StepLifecycle,
    StepExecutionState,
    IllegalStepTransition,
    is_terminal as is_step_terminal,
    can_transition,
    assert_transition,
)
from .multi_step_context import MultiStepContext
from .idempotency import (
    IdempotencyLog,
    IdempotencyEntry,
    idempotency_key,
    canonical_parameters,
    DuplicateActionError,
)
from .preconditions import (
    PreconditionKind,
    PostconditionKind,
    StepPrecondition,
    StepPostcondition,
    PRECONDITIONS_KEY,
    POSTCONDITIONS_KEY,
    preconditions_from_metadata,
    postconditions_from_metadata,
)
from .scroll import (
    ScrollDirection,
    ScrollSurface,
    ScrollStep,
    ScrollPlan,
    build_default_scroll_plan,
)
from .multi_step_coordinator import (
    MultiStepCoordinator,
    MultiStepContextStore,
    IdempotencyStore,
    GroundingProvider,
    WorldStateReader,
    ScrollExecutor,
    PreconditionOutcome,
    PostconditionOutcome,
    IdempotencyOutcome,
    ScrollFallbackOutcome,
    InMemoryMultiStepContextStore,
    InMemoryIdempotencyStore,
)

# System 8: structured multi-step progress events
# (additive — does not duplicate any existing surface).
from .progress import (
    ProgressPhase,
    ProgressEvent,
    ProgressBroadcaster,
    InMemoryProgressBroadcaster,
    LogProgressBroadcaster,
    CompositeProgressBroadcaster,
    make_progress_event,
    new_progress_event_id,
    is_terminal_progress_phase,
)

# System 8: dependency-DAG validation
from .dag import (
    DAGIssueKind,
    DAGIssue,
    DAGValidationResult,
    validate_plan,
    validate_steps,
)

# System 8: bounded retry tracking
from .retry import (
    RetryCounters,
    make_blank_retry_counters,
    RetryTracker,
)

# Phase 4: cooperative cancellation token
from .cancellation import CancellationToken

__all__ = [
    # Models
    "Goal",
    "Intent",
    "IntentKind",
    "IntentParamType",
    "Plan",
    "PlanStep",
    "PlanStatus",
    "ActionRequest",
    "ActionKind",
    "ExecutionContext",
    "Observation",
    "ObservationSource",
    "ExpectedEffect",
    "Verifier",
    "VerificationVerdict",
    "Failure",
    "FailureKind",
    "RecoveryDecision",
    "RecoveryAction",
    "count_decorator",
    # Interfaces
    "IntentInterpreter",
    "Planner",
    "PlanExecutor",
    "Orchestrator",
    "RecoveryEngine",
    # Phase 6A+6B: execution result contracts
    "ExecutionOutcome",
    "ExecutionResult",
    "StepResult",
    "StepState",
    "make_blank_execution_result",
    "new_correlation_id",
    # Phase 6A+6B: concrete PlanExecutor
    "PlanExecutorImpl",
    "PlanExecutorError",
    "InvalidPlanError",
    "IdempotencyViolation",
    "CancellationRequested",
    "DangerousAuthorizer",
    # Phase 6C: agent result contracts
    "AgentState",
    "AgentResult",
    "ObservationEntry",
    "PlanHistoryEntry",
    "new_agent_run_id",
    "make_blank_agent_result",
    # Phase 6C: observation / verifier / recovery
    "ObservationProvider",
    "CapabilityResultObservationProvider",
    "DefaultStepVerifier",
    "DefaultGoalVerifier",
    "passed_verdict",
    "failed_verdict",
    "uncertain_verdict",
    "VerifierRouter",
    "build_default_router",
    "DEFAULT_DESKTOP_CAPABILITIES",
    "RecoveryPolicy",
    "DefaultRecoveryEngine",
    "make_failure",
    "FailureClassifier",
    "CODE_TO_KIND",
    # Phase 6C: concrete Agent
    "Agent",
    "AgentPolicy",
    # Phase 14: multi-step execution layers
    "StepLifecycle",
    "StepExecutionState",
    "IllegalStepTransition",
    "is_step_terminal",
    "MultiStepContext",
    "IdempotencyLog",
    "IdempotencyEntry",
    "idempotency_key",
    "DuplicateActionError",
    "PreconditionKind",
    "PostconditionKind",
    "StepPrecondition",
    "StepPostcondition",
    "PRECONDITIONS_KEY",
    "POSTCONDITIONS_KEY",
    "ScrollDirection",
    "ScrollSurface",
    "ScrollPlan",
    "MultiStepCoordinator",
    "PreconditionOutcome",
    "PostconditionOutcome",
    "IdempotencyOutcome",
    "ScrollFallbackOutcome",
    # System 8: structured progress events
    "ProgressPhase",
    "ProgressEvent",
    "ProgressBroadcaster",
    "InMemoryProgressBroadcaster",
    "LogProgressBroadcaster",
    "CompositeProgressBroadcaster",
    "make_progress_event",
    "new_progress_event_id",
    "is_terminal_progress_phase",
    # System 8: dependency-DAG validation
    "DAGIssueKind",
    "DAGIssue",
    "DAGValidationResult",
    "validate_plan",
    "validate_steps",
    # System 8: bounded retry tracking
    "RetryCounters",
    "make_blank_retry_counters",
    "RetryTracker",
    # Phase 4: cooperative cancellation token
    "CancellationToken",
]
