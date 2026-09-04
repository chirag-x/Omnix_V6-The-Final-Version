"""
Omnix V6 — Task Models for Stage 21.

Defines the data structures for multi-step task execution:
- Task: User-level goal with metadata
- TaskPlan: Decomposition of task into steps with dependencies
- TaskStep: Individual executable unit within a task plan
- TaskStatus: Lifecycle states for tasks and steps
- TaskResult: Outcome of task execution
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Mapping, FrozenSet
from uuid import uuid4

# Import execution models for compatibility
from core.execution.result import ExecutionStatus
from core.execution.step import StepAction
from core.orchestration.models import (
    Plan,
    PlanStep,
    ExecutionContext,
    ActionKind,
    Failure,
    FailureKind,
    RecoveryDecision,
    RecoveryAction as OrchestrationRecoveryAction
)


class TaskStatus(str, Enum):
    """Lifecycle states for a task."""
    PENDING = "pending"           # Created but not started
    RUNNING = "running"           # Task is actively executing
    STEP_READY = "step_ready"     # Waiting for next step to be ready
    STEP_RUNNING = "step_running" # Currently executing a step
    STEP_SUCCESS = "step_success" # Last step succeeded
    STEP_FAILURE = "step_failure" # Last step failed
    RECOVERING = "recovering"     # Attempting recovery
    COMPLETED = "completed"       # All steps completed successfully
    FAILED = "failed"             # Task failed irrecoverably
    CANCELLED = "cancelled"       # Task was cancelled
    BLOCKED = "blocked"           # Waiting for external dependency
    SKIPPED = "skipped"           # Task was skipped


class TaskKind(str, Enum):
    """Types of tasks that can be executed."""
    AUTOMATION = "automation"     # Pure automation task
    INFORMATION_GATHERING = "information_gathering"  # Research/inquiry task
    CREATION = "creation"         # Creating documents, files, etc.
    MODIFICATION = "modification" # Modifying existing content
    VERIFICATION = "verification" # Checking/validating something
    MAINTENANCE = "maintenance"   # System maintenance tasks


@dataclass(frozen=True)
class TaskFailure:
    """Represents a failure that occurred during task execution."""
    step_id: str
    failure_kind: FailureKind
    message: str
    timestamp: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "failure_kind": self.failure_kind.value,
            "message": self.message,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata)
        }


@dataclass(frozen=True)
class TaskRecoveryAction(str, Enum):
    """Recovery actions available at the task level."""
    RETRY_STEP = "retry_step"           # Retry the failed step
    RETRY_STEP_BACKOFF = "retry_step_backoff"  # Retry with backoff
    SKIP_STEP = "skip_step"             # Skip the failed step
    REPLAN_TASK = "replan_task"         # Replan the entire task
    ESCALATE = "escalate"               # Escalate to higher level
    GIVE_UP = "give_up"                 # Abandon task execution


@dataclass(frozen=True)
class TaskResult:
    """Result of task execution."""
    task_id: str
    status: TaskStatus
    steps_completed: int
    steps_total: int
    start_time: float
    end_time: Optional[float] = None
    failure: Optional[TaskFailure] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        """Total execution duration in milliseconds."""
        end = self.end_time or time.time()
        return (end - self.start_time) * 1000.0

    @property
    def is_successful(self) -> bool:
        """Whether the task completed successfully."""
        return self.status == TaskStatus.COMPLETED

    @property
    def progress_percentage(self) -> float:
        """Execution progress as percentage (0-100)."""
        if self.steps_total == 0:
            return 0.0
        return (self.steps_completed / self.steps_total) * 100.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "steps_completed": self.steps_completed,
            "steps_total": self.steps_total,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": self.duration_ms,
            "failure": self.failure.to_dict() if self.failure else None,
            "metadata": dict(self.metadata),
            "is_successful": self.is_successful,
            "progress_percentage": self.progress_percentage
        }


@dataclass(frozen=True)
class TaskStep:
    """Individual step within a task plan.

    Similar to PlanStep but at the task level with additional metadata
    for user-facing task execution.
    """
    step_id: str                          # Unique identifier for the step
    sequence: int                         # Order in the task (0-indexed)
    description: str                      # Human-readable description
    intent: str                           # User intent for this step

    # What capability to use (maps to CapabilityRouter)
    capability: str                       # Capability name (e.g., "desktop.mouse.click")
    capability_kind: str = ""             # Kind of capability (e.g., "mouse", "keyboard")

    # Target information for grounding
    target: str = ""                      # Human-readable target description
    target_kind: str = ""                 # "coordinate", "element", "ocr", "vision", "window"
    target_hint: Mapping[str, Any] = field(default_factory=dict)

    # Parameters for the capability
    parameters: Mapping[str, Any] = field(default_factory=dict)

    # Expected outcome/verification
    expected_result: str = ""             # What success looks like
    verification_capability: str = ""     # Capability to use for verification

    # Dependencies on other steps (step_ids that must complete first)
    dependencies: FrozenSet[str] = field(default_factory=frozenset)

    # Step-level configuration
    timeout_s: float = 30.0               # Timeout for this step
    retry_count: int = 0                  # Number of retries attempted
    max_retries: int = 3                  # Maximum retries allowed

    # Metadata for tracking and debugging
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Ensure dependencies is a frozenset
        if not isinstance(self.dependencies, frozenset):
            object.__setattr__(self, 'dependencies', frozenset(self.dependencies))

        # Ensure metadata is a mapping
        if not isinstance(self.metadata, Mapping):
            object.__setattr__(self, 'metadata', dict(self.metadata))

        # Ensure parameters is a mapping
        if not isinstance(self.parameters, Mapping):
            object.__setattr__(self, 'parameters', dict(self.parameters))

        # Ensure target_hint is a mapping
        if not isinstance(self.target_hint, Mapping):
            object.__setattr__(self, 'target_hint', dict(self.target_hint))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization/logging."""
        return {
            "step_id": self.step_id,
            "sequence": self.sequence,
            "description": self.description,
            "intent": self.intent,
            "capability": self.capability,
            "capability_kind": self.capability_kind,
            "target": self.target,
            "target_kind": self.target_kind,
            "target_hint": dict(self.target_hint),
            "parameters": dict(self.parameters),
            "expected_result": self.expected_result,
            "verification_capability": self.verification_capability,
            "dependencies": list(self.dependencies),
            "timeout_s": self.timeout_s,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "metadata": dict(self.metadata)
        }

    def with_updated_retry_count(self) -> "TaskStep":
        """Return a new TaskStep with incremented retry count."""
        return TaskStep(
            step_id=self.step_id,
            sequence=self.sequence,
            description=self.description,
            intent=self.intent,
            capability=self.capability,
            capability_kind=self.capability_kind,
            target=self.target,
            target_kind=self.target_kind,
            target_hint=self.target_hint,
            parameters=self.parameters,
            expected_result=self.expected_result,
            verification_capability=self.verification_capability,
            dependencies=self.dependencies,
            timeout_s=self.timeout_s,
            retry_count=self.retry_count + 1,
            max_retries=self.max_retries,
            metadata=self.metadata
        )


@dataclass(frozen=True)
class TaskPlan:
    """A decomposed plan for achieving a user goal.

    Similar to the core Plan but at the task level with additional
    metadata for user-facing task execution.
    """
    plan_id: str                          # Unique identifier for the plan
    user_goal: str                        # Original user goal description
    steps: Tuple[TaskStep, ...]           # Ordered sequence of steps
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    # Task-level configuration
    timeout_s: float = 300.0              # Overall task timeout (5 minutes default)
    max_retries: int = 2                  # Maximum task-level retries

    def __post_init__(self):
        # Ensure steps is a tuple
        if not isinstance(self.steps, tuple):
            object.__setattr__(self, 'steps', tuple(self.steps))

        # Ensure metadata is a mapping
        if not isinstance(self.metadata, Mapping):
            object.__setattr__(self, 'metadata', dict(self.metadata))

    @property
    def step_count(self) -> int:
        """Total number of steps in the plan."""
        return len(self.steps)

    def get_step_by_id(self, step_id: str) -> Optional[TaskStep]:
        """Get a step by its ID."""
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def get_ready_steps(self, completed_step_ids: FrozenSet[str]) -> Tuple[TaskStep, ...]:
        """Get steps whose dependencies are satisfied."""
        ready = []
        for step in self.steps:
            # Skip steps that are already completed
            if step.step_id in completed_step_ids:
                continue
            if step.dependencies.issubset(completed_step_ids):
                ready.append(step)
        return tuple(ready)

    def has_circular_dependency(self) -> bool:
        """Check if the plan has circular dependencies."""
        # Simple DFS-based cycle detection
        visited = set()
        rec_stack = set()

        def has_cycle(step_id: str) -> bool:
            step = self.get_step_by_id(step_id)
            if not step:
                return False

            if step_id in rec_stack:
                return True

            if step_id in visited:
                return False

            visited.add(step_id)
            rec_stack.add(step_id)

            # Check all dependencies
            for dep_id in step.dependencies:
                if has_cycle(dep_id):
                    return True

            rec_stack.remove(step_id)
            return False

        # Check each step
        for step in self.steps:
            if step.step_id not in visited:
                if has_cycle(step.step_id):
                    return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization/logging."""
        return {
            "plan_id": self.plan_id,
            "user_goal": self.user_goal,
            "steps": [step.to_dict() for step in self.steps],
            "created_at": self.created_at,
            "step_count": self.step_count,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "metadata": dict(self.metadata),
            "has_circular_dependency": self.has_circular_dependency()
        }


@dataclass(frozen=True)
class Task:
    """User-level task representing a goal to be achieved.

    This is the highest-level construct that users interact with.
    """
    task_id: str                          # Unique identifier for the task
    user_goal: str                        # Original user goal description
    task_kind: TaskKind = TaskKind.AUTOMATION
    status: TaskStatus = TaskStatus.PENDING
    plan: Optional[TaskPlan] = None       # The execution plan (when available)
    current_step_id: Optional[str] = None # Currently executing step ID
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None    # When execution began
    completed_at: Optional[float] = None  # When execution ended
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Ensure metadata is a mapping
        if not isinstance(self.metadata, Mapping):
            object.__setattr__(self, 'metadata', dict(self.metadata))

    @property
    def duration_ms(self) -> float:
        """Total execution duration in milliseconds."""
        if not self.started_at:
            return 0.0
        end = self.completed_at or time.time()
        return (end - self.started_at) * 1000.0

    @property
    def is_completed(self) -> bool:
        """Whether the task has finished execution."""
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED
        )

    @property
    def is_successful(self) -> bool:
        """Whether the task completed successfully."""
        return self.status == TaskStatus.COMPLETED

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization/logging."""
        return {
            "task_id": self.task_id,
            "user_goal": self.user_goal,
            "task_kind": self.task_kind.value,
            "status": self.status.value,
            "plan": self.plan.to_dict() if self.plan else None,
            "current_step_id": self.current_step_id,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "is_completed": self.is_completed,
            "is_successful": self.is_successful,
            "metadata": dict(self.metadata)
        }


# Factory functions for creating instances
def create_task(
    user_goal: str,
    task_kind: TaskKind = TaskKind.AUTOMATION,
    metadata: Optional[Mapping[str, Any]] = None
) -> Task:
    """Create a new task from a user goal."""
    return Task(
        task_id=str(uuid4()),
        user_goal=user_goal,
        task_kind=task_kind,
        status=TaskStatus.PENDING,
        metadata=metadata or {}
    )


def create_task_step(
    sequence: int,
    description: str,
    intent: str,
    capability: str,
    target: str = "",
    target_kind: str = "",
    parameters: Optional[Mapping[str, Any]] = None,
    expected_result: str = "",
    verification_capability: str = "",
    dependencies: Optional[FrozenSet[str]] = None,
    timeout_s: float = 30.0,
    max_retries: int = 3,
    metadata: Optional[Mapping[str, Any]] = None,
    step_id: Optional[str] = None,
) -> TaskStep:
    """Create a new task step."""
    return TaskStep(
        step_id=step_id if step_id else str(uuid4()),
        sequence=sequence,
        description=description,
        intent=intent,
        capability=capability,
        target=target,
        target_kind=target_kind,
        target_hint={},
        parameters=parameters or {},
        expected_result=expected_result,
        verification_capability=verification_capability,
        dependencies=dependencies or frozenset(),
        timeout_s=timeout_s,
        retry_count=0,
        max_retries=max_retries,
        metadata=metadata or {}
    )


def create_task_plan(
    user_goal: str,
    steps: Tuple[TaskStep, ...],
    timeout_s: float = 300.0,
    max_retries: int = 2,
    metadata: Optional[Mapping[str, Any]] = None
) -> TaskPlan:
    """Create a new task plan."""
    return TaskPlan(
        plan_id=str(uuid4()),
        user_goal=user_goal,
        steps=steps,
        timeout_s=timeout_s,
        max_retries=max_retries,
        metadata=metadata or {}
    )