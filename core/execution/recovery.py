"""
Omnix V6 — Execution Recovery Models for Stage 19.0.

Defines recovery models, policies, and classifiers for handling execution failures
in the OBSERVE → GROUND → ACT → VERIFY cycle with bounded retry/replan strategies.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Mapping, ClassVar

# Import execution status for mapping failures
from .result import ExecutionStatus
from .errors import ExecutionError


class RecoveryAction(str, Enum):
    """What the recovery engine should do when a failure occurs."""

    RETRY = "retry"                    # Immediate retry of the same step
    RETRY_WITH_BACKOFF = "retry_with_backoff"  # Retry with exponential backoff
    SKIP = "skip"                     # Skip the step and continue
    REPLAN = "replan"                 # Request a new plan from the planner
    ESCALATE = "escalate"             # Escalate to higher-level recovery
    GIVE_UP = "give_up"               # No more recovery attempts


class FailureCategory(str, Enum):
    """Categories of failures for classification purposes."""

    TRANSIENT = "transient"           # Temporary issues that may resolve on retry
    PERSISTENT = "persistent"         # Issues unlikely to resolve without intervention
    CONFIGURATION = "configuration"   # Misconfiguration requiring fix
    RESOURCE = "resource"             # Resource exhaustion or unavailability
    TIMEOUT = "timeout"               # Deadline exceeded
    USER_INTERVENTION = "user_intervention"  # Requires user input/action


@dataclass(frozen=True)
class RecoveryPolicy:
    """Configuration for recovery behavior and bounds."""

    # Retry limits
    max_attempts_per_step: int = 3          # Initial attempt + 2 retries
    base_backoff_s: float = 0.5             # Base delay for exponential backoff
    max_backoff_s: float = 5.0              # Maximum backoff delay

    # Replan limits
    max_replans: int = 2                    # Initial plan + 2 replans

    # Timeout limits
    max_total_runtime_s: float = 120.0      # Hard wall-clock limit

    # Category-specific behaviors
    transient_retry_enabled: bool = True
    persistent_retry_enabled: bool = False
    timeout_replan_enabled: bool = True
    resource_escalate_enabled: bool = True

    # Classification thresholds
    consecutive_failure_threshold: int = 3  # When to consider persistent

    def with_overrides(self, **kwargs: Any) -> "RecoveryPolicy":
        """Create a new policy with overridden values."""
        return replace(self, **kwargs)


@dataclass(frozen=True)
class RecoveryClassifier:
    """Classifies execution failures into categories for recovery decisions."""

    consecutive_failure_threshold: int = 3

    # Mapping of execution status to failure category
    STATUS_TO_CATEGORY: ClassVar[Mapping[ExecutionStatus, FailureCategory]] = {
        ExecutionStatus.OBSERVATION_FAILED: FailureCategory.TRANSIENT,
        ExecutionStatus.GROUNDING_FAILED: FailureCategory.TRANSIENT,
        ExecutionStatus.ACTION_FAILED: FailureCategory.TRANSIENT,
        ExecutionStatus.VERIFICATION_FAILED: FailureCategory.PERSISTENT,
        ExecutionStatus.SYNCHRONIZATION_FAILED: FailureCategory.TRANSIENT,
        ExecutionStatus.TIMEOUT: FailureCategory.TIMEOUT,
        ExecutionStatus.CANCELLED: FailureCategory.USER_INTERVENTION,
        ExecutionStatus.INCONCLUSIVE: FailureCategory.TRANSIENT,
        ExecutionStatus.PRECONDITION_FAILED: FailureCategory.CONFIGURATION,
    }

    # Specific error type mappings
    ERROR_TYPE_TO_CATEGORY: ClassVar[Mapping[type[ExecutionError], FailureCategory]] = {
        # Add specific error type mappings as needed
    }

    def classify_failure(
        self,
        status: ExecutionStatus,
        error: Optional[ExecutionError] = None,
        consecutive_failures: int = 0,
        metadata: Optional[Dict[str, Any]] = None
    ) -> FailureCategory:
        """
        Classify a failure based on execution status, error type, and context.

        Args:
            status: The execution status from the failed cycle
            error: Optional execution error for detailed classification
            consecutive_failures: Number of consecutive failures on this step
            metadata: Additional context for classification

        Returns:
            FailureCategory indicating how to treat the failure
        """
        # Check for specific error type overrides
        if error is not None:
            error_type = type(error)
            if error_type in self.ERROR_TYPE_TO_CATEGORY:
                return self.ERROR_TYPE_TO_CATEGORY[error_type]

        # Base classification from status
        category = self.STATUS_TO_CATEGORY.get(status, FailureCategory.PERSISTENT)

        # Escalate to persistent if too many consecutive failures
        if (consecutive_failures >= self.consecutive_failure_threshold and
            category == FailureCategory.TRANSIENT):
            return FailureCategory.PERSISTENT

        # Check for resource-related indicators in metadata
        if metadata:
            if metadata.get("resource_exhausted") or metadata.get("oom"):
                return FailureCategory.RESOURCE
            if metadata.get("permission_denied"):
                return FailureCategory.CONFIGURATION

        return category

    def get_recovery_action(
        self,
        category: FailureCategory,
        policy: RecoveryPolicy,
        attempts_used: int,
        replans_used: int,
        elapsed_s: float
    ) -> RecoveryAction:
        """
        Determine the appropriate recovery action based on failure category and policy.

        Args:
            category: The classified failure category
            policy: The recovery policy to apply
            attempts_used: Number of attempts already used for this step
            replans_used: Number of replans already used
            elapsed_s: Elapsed runtime in seconds

        Returns:
            RecoveryAction to take
        """
        # Check global limits first
        if elapsed_s >= policy.max_total_runtime_s:
            return RecoveryAction.GIVE_UP

        if attempts_used >= policy.max_attempts_per_step:
            if replans_used < policy.max_replans:
                return RecoveryAction.REPLAN
            else:
                return RecoveryAction.GIVE_UP

        # Category-specific logic
        if category == FailureCategory.TRANSIENT:
            if policy.transient_retry_enabled:
                return RecoveryAction.RETRY_WITH_BACKOFF
            else:
                return RecoveryAction.SKIP

        elif category == FailureCategory.PERSISTENT:
            if policy.persistent_retry_enabled and attempts_used < policy.max_attempts_per_step:
                return RecoveryAction.RETRY
            elif replans_used < policy.max_replans:
                return RecoveryAction.REPLAN
            else:
                return RecoveryAction.GIVE_UP

        elif category == FailureCategory.TIMEOUT:
            if policy.timeout_replan_enabled and replans_used < policy.max_replans:
                return RecoveryAction.REPLAN
            else:
                return RecoveryAction.GIVE_UP

        elif category == FailureCategory.RESOURCE:
            if policy.resource_escalate_enabled:
                return RecoveryAction.ESCALATE
            else:
                return RecoveryAction.GIVE_UP

        elif category == FailureCategory.CONFIGURATION:
            # Configuration issues usually require manual fix
            return RecoveryAction.GIVE_UP

        elif category == FailureCategory.USER_INTERVENTION:
            return RecoveryAction.ESCALATE

        # Default fallback
        return RecoveryAction.GIVE_UP

    def calculate_backoff(
        self,
        attempt: int,
        policy: RecoveryPolicy,
        base_delay: Optional[float] = None
    ) -> float:
        """
        Calculate backoff delay for retry attempts.

        Args:
            attempt: The attempt number (0-indexed)
            policy: The recovery policy
            base_delay: Optional base delay override

        Returns:
            Backoff delay in seconds
        """
        base = base_delay if base_delay is not None else policy.base_backoff_s
        # Exponential backoff with jitter: base * (2^attempt) + random jitter
        import random
        backoff = base * (2 ** attempt)
        jitter = random.uniform(0, backoff * 0.1)  # 10% jitter
        return min(backoff + jitter, policy.max_backoff_s)


@dataclass
class RecoveryContext:
    """Context information for recovery decisions."""

    step_id: str
    execution_status: ExecutionStatus
    error: Optional[ExecutionError] = None
    attempt_count: int = 0
    replan_count: int = 0
    elapsed_s: float = 0.0
    consecutive_failures: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "step_id": self.step_id,
            "execution_status": self.execution_status.value,
            "error": str(self.error) if self.error else None,
            "attempt_count": self.attempt_count,
            "replan_count": self.replan_count,
            "elapsed_s": self.elapsed_s,
            "consecutive_failures": self.consecutive_failures,
            "metadata": self.metadata,
        }


def create_default_recovery_policy() -> RecoveryPolicy:
    """Create a default recovery policy with sensible values."""
    return RecoveryPolicy(
        max_attempts_per_step=3,
        base_backoff_s=0.5,
        max_backoff_s=5.0,
        max_replans=2,
        max_total_runtime_s=120.0,
        transient_retry_enabled=True,
        persistent_retry_enabled=False,
        timeout_replan_enabled=True,
        resource_escalate_enabled=True,
        consecutive_failure_threshold=3,
    )


@dataclass(frozen=True)
class RecoveryResult:
    """Result of a recovery action execution."""
    recovery_id: str
    action_taken: RecoveryAction
    success: bool
    error: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    completed_at: float = 0.0
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "recovery_id": self.recovery_id,
            "action_taken": self.action_taken.value,
            "success": self.success,
            "error": self.error,
            "metadata": dict(self.metadata),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
        }


def create_classifier() -> RecoveryClassifier:
    """Create a recovery classifier instance."""
    return RecoveryClassifier()


# Example usage and testing functions
if __name__ == "__main__":
    # Example of how to use the recovery system
    policy = create_default_recovery_policy()
    classifier = create_classifier()

    # Simulate a failure
    from .result import ExecutionStatus

    context = RecoveryContext(
        step_id="step_123",
        execution_status=ExecutionStatus.ACTION_FAILED,
        attempt_count=1,
        elapsed_s=45.0,
        consecutive_failures=2
    )

    category = classifier.classify_failure(
        context.execution_status,
        consecutive_failures=context.consecutive_failures
    )

    action = classifier.get_recovery_action(
        category,
        policy,
        context.attempt_count,
        context.replan_count,
        context.elapsed_s
    )

    backoff = classifier.calculate_backoff(context.attempt_count, policy)

    print(f"Failure Category: {category}")
    print(f"Recovery Action: {action}")
    print(f"Backoff Delay: {backoff:.2f}s")