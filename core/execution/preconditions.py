"""
Omnix V6 — Precondition Models for Stage 19.2.

Defines the precondition model, result model, provider protocol, and
integration with the execution cycle for state-aware execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from uuid import uuid4

# Import existing types where appropriate
from vision.perception_contract import PerceptionResult, PerceptionStatus
from .state import ExecutionState
from .result import ExecutionStatus


class PreconditionKind(str, Enum):
    """
    Closed set of precondition kinds that can be checked.
    These represent facts about the world that must be true before
    an action can be safely performed.
    """
    TARGET_VISIBLE = "target_visible"
    TARGET_PRESENT = "target_present"
    TARGET_INTERACTABLE = "target_interactable"
    WINDOW_EXISTS = "window_exists"
    WINDOW_FOCUSED = "window_focused"
    TARGET_FOCUSED = "target_focused"
    TEXT_PRESENT = "text_present"
    TEXT_CHANGED = "text_changed"


@dataclass(frozen=True)
class Precondition:
    """
    A precondition that must be satisfied before executing an action.

    Preconditions represent facts about the world that must be true
    before an action can be safely performed. They are checked against
    current perception/grounding data to determine if it's safe to proceed.
    """
    precondition_id: str = field(default_factory=lambda: str(uuid4()))
    kind: PreconditionKind = PreconditionKind.TARGET_PRESENT
    target_query: str = ""  # Human-readable description (e.g., "search bar")
    expected_state: str = ""  # Additional expected state details
    timeout_s: float = 5.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.precondition_id:
            object.__setattr__(self, 'precondition_id', str(uuid4()))


@dataclass(frozen=True)
class PreconditionResult:
    """
    Result of checking a precondition against current observations.

    Indicates whether the precondition was satisfied, and provides
    evidence and confidence for the determination.
    """
    precondition_id: str
    status: "PreconditionStatus"
    satisfied: bool
    confidence: float = 0.0  # 0.0 to 1.0
    evidence: Dict[str, Any] = field(default_factory=dict)
    observation_id: Optional[str] = None
    reason: str = ""
    elapsed_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        # Ensure satisfied aligns with status
        if self.status == PreconditionStatus.SATISFIED:
            object.__setattr__(self, 'satisfied', True)
        else:
            object.__setattr__(self, 'satisfied', False)


class PreconditionStatus(str, Enum):
    """
    Status of a precondition check.
    """
    SATISFIED = "satisfied"        # Precondition is met
    NOT_SATISFIED = "not_satisfied"  # Precondition is not met
    INCONCLUSIVE = "inconclusive"    # Cannot determine with confidence
    TIMEOUT = "timeout"              # Check took too long
    CANCELLED = "cancelled"          # Check was cancelled
    ERROR = "error"                  # Error during check


@runtime_checkable
class PreconditionProvider(Protocol):
    """
    Provider interface for checking preconditions.

    Precondition providers evaluate whether specific conditions about
    the world state are true, based on perception/grounding data.
    """
    name: str

    async def check(
        self,
        precondition: Precondition,
        observation: PerceptionResult,
        context: Optional[ExecutionState] = None,
        cancellation_token: Optional[Any] = None,
    ) -> PreconditionResult:
        """
        Check if a precondition is satisfied given current observation and context.

        Args:
            precondition: The precondition to check
            observation: Current perception observation
            context: Optional execution state for additional context
            cancellation_token: Optional token for cancelling the check

        Returns:
            PreconditionResult indicating whether precondition is satisfied
        """
        ...


# Integration with existing ExecutionStatus
def precondition_status_to_execution_status(precondition_status: PreconditionStatus) -> ExecutionStatus:
    """
    Convert a PreconditionStatus to the corresponding ExecutionStatus.

    Args:
        precondition_status: Status from precondition check

    Returns:
        Corresponding ExecutionStatus
    """
    mapping = {
        PreconditionStatus.SATISFIED: ExecutionStatus.SUCCESS,  # This would be handled in calling code
        PreconditionStatus.NOT_SATISFIED: ExecutionStatus.OBSERVATION_FAILED,  # Will be specialized
        PreconditionStatus.INCONCLUSIVE: ExecutionStatus.INCONCLUSIVE,
        PreconditionStatus.TIMEOUT: ExecutionStatus.TIMEOUT,
        PreconditionStatus.CANCELLED: ExecutionStatus.CANCELLED,
        PreconditionStatus.ERROR: ExecutionStatus.OBSERVATION_FAILED,
    }

    # We need a specific PRECONDITION_FAILED status
    if precondition_status == PreconditionStatus.NOT_SATISFIED:
        # This will be handled by adding a new ExecutionStatus value
        return ExecutionStatus.OBSERVATION_FAILED  # Placeholder until we add PRECONDITION_FAILED

    return mapping.get(precondition_status, ExecutionStatus.OBSERVATION_FAILED)


# Extended ExecutionStatus with PRECONDITION_FAILED
# This will be added to ExecutionStatus enum in result.py
PRECONDITION_FAILED_STATUS_NAME = "precondition_failed"