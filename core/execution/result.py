"""
Omnix V6 — Execution Result Models for Stage 19.0.

Defines ExecutionResult, ExecutionStatus, ExecutionTrace, VerificationResult,
and VerificationStatus for the OBSERVE → GROUND → ACT → VERIFY cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Mapping, Optional, TYPE_CHECKING
from uuid import uuid4

# Import existing status enums to reuse where appropriate
from ..results import (
    ActionStatus,
    ObservationStatus,
    VerificationStatus as CapabilityVerificationStatus,
)

if TYPE_CHECKING:
    from .state import ExecutionState
    from .preconditions import PreconditionResult


class ExecutionStatus(str, Enum):
    """Overall status of an execution cycle."""
    SUCCESS = "success"
    OBSERVATION_FAILED = "observation_failed"
    PRECONDITION_FAILED = "precondition_failed"
    GROUNDING_FAILED = "grounding_failed"
    ACTION_FAILED = "action_failed"
    VERIFICATION_FAILED = "verification_failed"
    SYNCHRONIZATION_FAILED = "synchronization_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INCONCLUSIVE = "inconclusive"


class VerificationStatus(str, Enum):
    """Status of a verification step within the cycle.
    Mirrors the core.results.VerificationStatus enum for consistency.
    """
    SUCCESS = "success"      # maps to VERIFIED
    FAILED = "failed"        # maps to FAILED or MISMATCH
    TIMEOUT = "timeout"      # maps to TIMED_OUT
    INCONCLUSIVE = "inconclusive"  # maps to UNVERIFIED
    CANCELLED = "cancelled"  # maps to CANCELLED


@dataclass(frozen=True)
class ExecutionTrace:
    """Per-phase timestamp and ID captured for diagnostics and audit."""
    observation_id: Optional[str] = None
    observation_timestamp: Optional[float] = None
    action_id: Optional[str] = None
    action_started_at: Optional[float] = None
    action_completed_at: Optional[float] = None
    verification_id: Optional[str] = None
    verification_attempts: int = 0
    # Stage 19.2: State tracking for precondition verification
    pre_state: Optional["ExecutionState"] = None
    post_state: Optional["ExecutionState"] = None
    # Stage 19.3: Synchronization trace
    synchronization_status: Optional[str] = None
    synchronization_observation_id: Optional[str] = None
    synchronization_elapsed_ms: float = 0.0
    synchronization_poll_count: int = 0


@dataclass(frozen=True)
class ExecutionResult:
    """Immutable result of an OBSERVE → GROUND → ACT → VERIFY cycle."""
    execution_id: str
    step_id: str
    status: ExecutionStatus
    observation: Optional[Any] = None       # PerceptionResult from OBSERVE
    resolved_target: Optional[Any] = None   # ResolvedTarget from GROUND
    action_result: Optional[Any] = None     # CapabilityResult from ACT
    verification_result: Optional[Any] = None  # VerificationResult from VERIFY
    trace: ExecutionTrace = field(default_factory=ExecutionTrace)
    # Stage 19.2 additions for state tracking
    pre_state: Optional[Any] = None         # ExecutionState before action
    post_state: Optional[Any] = None        # ExecutionState after action
    precondition_results: tuple = field(default_factory=tuple)  # Precondition check results
    # Stage 19.3: Synchronization result
    synchronization_result: Optional[Any] = None  # SynchronizationResult from SYNCHRONIZE
    started_at: float = 0.0
    completed_at: float = 0.0
    duration_ms: float = 0.0
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Ensure execution_id is set if not provided (should be set by caller)
        if not self.execution_id:
            object.__setattr__(self, 'execution_id', str(uuid4()))
        # Ensure timestamps are set if not provided
        if self.started_at == 0.0:
            object.__setattr__(self, 'started_at', self._time())
        if self.completed_at == 0.0 and self.status != ExecutionStatus.SUCCESS:
            # For failed cases, completed_at may be set by caller
            pass

    @staticmethod
    def _time() -> float:
        return datetime.now().timestamp()

    @property
    def succeeded(self) -> bool:
        """True if the cycle succeeded."""
        return self.status == ExecutionStatus.SUCCESS

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a dictionary for serialization/logging."""
        return {
            "execution_id": self.execution_id,
            "step_id": self.step_id,
            "status": self.status.value,
            "observation": self._to_dict_if_not_none(self.observation),
            "resolved_target": self._to_dict_if_not_none(self.resolved_target),
            "action_result": self._to_dict_if_not_none(self.action_result),
            "verification_result": self._to_dict_if_not_none(self.verification_result),
            "trace": {
                "observation_id": self.trace.observation_id,
                "observation_timestamp": self.trace.observation_timestamp,
                "action_id": self.trace.action_id,
                "action_started_at": self.trace.action_started_at,
                "action_completed_at": self.trace.action_completed_at,
                "verification_id": self.trace.verification_id,
                "verification_attempts": self.trace.verification_attempts,
                "pre_state_id": getattr(self.pre_state, 'state_id', None) if self.pre_state else None,
                "post_state_id": getattr(self.post_state, 'state_id', None) if self.post_state else None,
                "synchronization_status": self.trace.synchronization_status,
                "synchronization_observation_id": self.trace.synchronization_observation_id,
                "synchronization_elapsed_ms": self.trace.synchronization_elapsed_ms,
                "synchronization_poll_count": self.trace.synchronization_poll_count,
            },
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "metadata": dict(self.metadata),
            # Stage 19.2 additions
            "pre_state": self.pre_state.to_dict() if hasattr(self.pre_state, 'to_dict') else (self.pre_state.__dict__ if self.pre_state else None),
            "post_state": self.post_state.to_dict() if hasattr(self.post_state, 'to_dict') else (self.post_state.__dict__ if self.post_state else None),
            "precondition_results": [pr.to_dict() if hasattr(pr, 'to_dict') else str(pr) for pr in self.precondition_results],
            # Stage 19.3 addition
            "synchronization_result": self.synchronization_result.to_dict() if hasattr(self.synchronization_result, 'to_dict') else (None if self.synchronization_result is None else str(self.synchronization_result)),
        }

    @staticmethod
    def _to_dict_if_not_none(obj: Any) -> Any:
        if obj is None:
            return None
        if hasattr(obj, 'to_dict'):
            return obj.to_dict()
        return obj


@dataclass(frozen=True)
class VerificationResult:
    """Result of a verification step."""
    verification_id: str
    status: VerificationStatus
    success: bool
    confidence: float = 1.0
    evidence: Optional[Any] = None            # the PerceptionResult that decided it
    observation_id: Optional[str] = None
    elapsed_ms: float = 0.0
    reason: str = ""
    attempt: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.verification_id:
            object.__setattr__(self, 'verification_id', str(uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "status": self.status.value,
            "success": self.success,
            "confidence": self.confidence,
            "evidence": self._to_dict_if_not_none(self.evidence),
            "observation_id": self.observation_id,
            "elapsed_ms": self.elapsed_ms,
            "reason": self.reason,
            "attempt": self.attempt,
            "metadata": dict(self.metadata),
        }

    @staticmethod
    def _to_dict_if_not_none(obj: Any) -> Any:
        if obj is None:
            return None
        if hasattr(obj, 'to_dict'):
            return obj.to_dict()
        return obj