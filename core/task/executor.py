"""
Omnix V6 — Task Executor for Stage 21.

The TaskExecutor drives the execution of TaskPlans by leveraging the existing
PlanExecutor and ExecutionCycle infrastructure. It handles:
- Task-level state management
- Step-by-step execution with dependency resolution
- Recovery and retry logic
- Progress tracking and events
- Final task verification
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Mapping, FrozenSet, Callable
from uuid import uuid4

# Import core orchestration components
from core.orchestration.plan_executor import PlanExecutor
from core.orchestration.execution_result import ExecutionResult, ExecutionOutcome, StepResult, StepState
from core.orchestration.models import (
    ExecutionContext,
    Goal,
    Intent,
    IntentKind,
    Plan,
    ActionKind,
    PlanStep,
    ExpectedEffect,
    Failure,
    FailureKind,
    RecoveryDecision,
    RecoveryAction as OrchestrationRecoveryAction,
)

# Import task models
from .models import (
    Task,
    TaskPlan,
    TaskStep,
    TaskStatus,
    TaskResult,
    TaskKind,
    TaskFailure,
    TaskRecoveryAction,
    create_task,
    create_task_step,
    create_task_plan,
)

# Import execution components for direct access when needed
try:
    from core.execution import (
        ExecutionCycle,
        ExecutionStep,
        StepAction,
        ExecutionPolicy,
        DefaultActionExecutor,
        DefaultVerificationProvider,
        DefaultGroundingProvider,
        DefaultSynchronizationProvider,
    )

    _EXECUTION_CYCLE_AVAILABLE = True
except Exception:
    _EXECUTION_CYCLE_AVAILABLE = False

# TaskExecutor is always available in this module
_TASK_EXECUTOR_AVAILABLE = True

from loguru import logger


@dataclass
class TaskExecutorConfig:
    """Configuration for the TaskExecutor."""

    max_task_retries: int = 2
    enable_step_recovery: bool = True
    enable_task_replanning: bool = True
    progress_callback: Optional[Callable[[TaskResult], None]] = None
    event_publisher: Optional[Callable[[str, Dict[str, Any]], None]] = None


class TaskExecutor:
    """
    Executes TaskPlans by leveraging the existing PlanExecutor infrastructure.

    The TaskExecutor operates at the task level, coordinating:
    1. Task validation and preparation
    2. Step-by-step execution using PlanExecutor
    3. Recovery handling at both step and task levels
    4. Progress tracking and event emission
    5. Final task verification and completion
    """

    def __init__(
        self, plan_executor: PlanExecutor, config: Optional[TaskExecutorConfig] = None
    ):
        """
        Initialize the TaskExecutor.

        Args:
            plan_executor: The PlanExecutor to use for step execution
            config: Optional configuration for the TaskExecutor
        """
        self.plan_executor = plan_executor
        self.config = config or TaskExecutorConfig()
        self._active_tasks: Dict[str, Task] = {}
        self._task_plans: Dict[str, TaskPlan] = {}

        logger.info("TaskExecutor initialized")

    def execute_task(self, task: Task) -> TaskResult:
        """
        Execute a task from start to completion.

        Args:
            task: The task to execute

        Returns:
            TaskResult: The outcome of task execution
        """
        logger.info(f"Starting task execution: {task.task_id} - {task.user_goal}")

        # Store the task for tracking
        self._active_tasks[task.task_id] = task

        # Initialize task execution
        started_task = task.to_builder().started_now().build()
        self._active_tasks[task.task_id] = started_task

        try:
            # Execute the task with retry logic
            result = self._execute_with_retries(started_task)

            # Mark task as completed
            completed_task = started_task.to_builder().completed_now(result).build()
            self._active_tasks[task.task_id] = completed_task

            # Emit completion event
            self._emit_task_event("task_completed", completed_task, result)

            logger.info(
                f"Task execution finished: {task.task_id} - "
                f"Status: {result.status.value}, "
                f"Steps: {result.steps_completed}/{result.steps_total}"
            )

            return result

        except Exception as e:
            logger.error(f"Task execution failed with exception: {e}")

            # Create failure result
            failure = TaskFailure(
                step_id="task_level", failure_kind=FailureKind.INTERNAL, message=str(e)
            )

            result = TaskResult(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                steps_completed=0,
                steps_total=0,
                start_time=started_task.started_at or time.time(),
                end_time=time.time(),
                failure=failure,
            )

            # Mark task as failed
            failed_task = started_task.to_builder().failed_now(result).build()
            self._active_tasks[task.task_id] = failed_task

            # Emit failure event
            self._emit_task_event("task_failed", failed_task, result)

            return result
        finally:
            # Clean up after a delay to allow for inspection
            # In a real implementation, this might be handled differently
            pass

    def _execute_with_retries(self, task: Task) -> TaskResult:
        """Execute task with retry logic at the task level."""
        max_retries = self.config.max_task_retries

        for attempt in range(max_retries + 1):
            try:
                # Get the current task state (may have been updated by previous attempt)
                current_task = self._active_tasks[task.task_id]
                # Attempt to execute the task
                result = self._execute_task_attempt(current_task)

                # If successful or we've exhausted retries, return result
                if result.is_successful or attempt >= max_retries:
                    return result

                # Otherwise, prepare for retry
                logger.info(
                    f"Task attempt {attempt + 1} failed, retrying... "
                    f"({task.task_id})"
                )

                # Update task for retry
                retry_task = current_task.to_builder().retried().build()
                self._active_tasks[task.task_id] = retry_task

            except Exception as e:
                if attempt >= max_retries:
                    raise
                logger.warning(
                    f"Task attempt {attempt + 1} failed with exception, retrying: {e}"
                )

        # Should not reach here, but just in case
        return TaskResult(
            task_id=task.task_id,
            status=TaskStatus.FAILED,
            steps_completed=0,
            steps_total=0,
            start_time=task.started_at or time.time(),
            end_time=time.time(),
            failure=TaskFailure(
                step_id="task_level",
                failure_kind=FailureKind.INTERNAL,
                message="Max retries exceeded",
            ),
        )

    def _execute_task_attempt(self, task: Task) -> TaskResult:
        """Execute a single attempt of a task."""
        logger.info(f"Executing task attempt: {task.task_id}")

        # Validate that we have a plan
        if not task.plan:
            # Try to create a plan from the task (this would normally come from a planner)
            # For now, we'll create a simple linear plan
            task_plan = self._create_simple_plan(task)
            task_with_plan = task.to_builder().with_plan(task_plan).build()
            self._active_tasks[task.task_id] = task_with_plan
        else:
            task_with_plan = task
            task_plan = task.plan

        # Validate the plan
        validation_error = self._validate_task_plan(task_plan)
        if validation_error:
            raise ValueError(f"Invalid task plan: {validation_error}")

        # Store the plan for tracking
        self._task_plans[task_plan.plan_id] = task_plan

        # Execute the plan step by step
        return self._execute_task_plan(task_with_plan, task_plan)

    def _create_simple_plan(self, task: Task) -> TaskPlan:
        """Create a simple linear plan from a task when no planner is available.

        In a full implementation, this would delegate to a TaskPlanner.
        For Stage 21, we create a basic plan that treats the user goal as a single step.
        """
        logger.info(f"Creating simple plan for task: {task.task_id}")

        # Create a single step representing the user goal
        # In practice, this would be decomposed by a planner
        step = TaskStep(
            step_id=str(uuid4()),
            sequence=0,
            description=f"Execute: {task.user_goal}",
            intent=task.user_goal,
            capability="automation.general",  # Placeholder capability
            parameters={},
            expected_result="Goal achieved",
            dependencies=frozenset(),
        )

        plan = TaskPlan(
            plan_id=str(uuid4()),
            user_goal=task.user_goal,
            steps=(step,),
            metadata={"created_by": "TaskExecutor.simple_plan"},
        )

        return plan

    def _validate_task_plan(self, plan: TaskPlan) -> Optional[str]:
        """Validate a task plan for correctness."""
        if not plan.steps:
            return "Plan has no steps"

        # Check for duplicate step IDs
        step_ids = [step.step_id for step in plan.steps]
        if len(step_ids) != len(set(step_ids)):
            return "Plan contains duplicate step IDs"

        # Check for circular dependencies (delegated to TaskPlan)
        if plan.has_circular_dependency():
            return "Plan contains circular dependencies"

        # Validate each step
        for step in plan.steps:
            if not step.capability:
                return f"Step {step.step_id} has no capability specified"

        return None

    def _execute_task_plan(self, task: Task, plan: TaskPlan) -> TaskResult:
        """Execute a task plan step by step."""
        logger.info(f"Executing task plan: {plan.plan_id} with {len(plan.steps)} steps")

        start_time = time.time()
        completed_step_ids: FrozenSet[str] = frozenset()
        failed_step_ids: FrozenSet[str] = frozenset()
        step_results: List[Dict[str, Any]] = []

        # Update task status
        running_task = task.to_builder().status(TaskStatus.RUNNING).build()
        self._active_tasks[task.task_id] = running_task
        self._emit_task_event("task_started", running_task, None)

        try:
            # Execute steps in dependency order
            while len(completed_step_ids) + len(failed_step_ids) < len(plan.steps):
                # Get ready steps (dependencies satisfied)
                ready_steps = plan.get_ready_steps(completed_step_ids)

                # Filter out already completed or failed steps
                ready_steps = [
                    step
                    for step in ready_steps
                    if step.step_id not in completed_step_ids
                    and step.step_id not in failed_step_ids
                ]

                if not ready_steps:
                    # No ready steps - check if we're blocked or done
                    if len(completed_step_ids) + len(failed_step_ids) < len(plan.steps):
                        # We're blocked - wait for recovery or fail
                        blocked_task = (
                            running_task.to_builder().status(TaskStatus.BLOCKED).build()
                        )
                        self._active_tasks[task.task_id] = blocked_task
                        self._emit_task_event("task_blocked", blocked_task, None)

                        # In a full implementation, we would wait for external triggers
                        # For now, we'll treat this as a failure
                        break
                    else:
                        # All steps processed
                        break

                # Execute each ready step (in practice, might execute one at a time)
                for step in ready_steps:
                    logger.info(f"Executing step: {step.step_id} - {step.description}")

                    # Update task status
                    step_running_task = (
                        running_task.to_builder()
                        .status(TaskStatus.STEP_RUNNING)
                        .current_step_id(step.step_id)
                        .build()
                    )
                    self._active_tasks[task.task_id] = step_running_task
                    self._emit_task_event(
                        "step_started", step_running_task, {"step": step.to_dict()}
                    )

                    # Execute the step using PlanExecutor
                    step_result = self._execute_task_step(
                        step, plan, completed_step_ids
                    )

                    # Process step result
                    if step_result.get("success", False):
                        # Step succeeded
                        completed_step_ids = frozenset(
                            list(completed_step_ids) + [step.step_id]
                        )
                        step_results.append(
                            {
                                "step_id": step.step_id,
                                "success": True,
                                "result": step_result,
                            }
                        )

                        # Update task status
                        step_success_task = (
                            step_running_task.to_builder()
                            .status(TaskStatus.STEP_SUCCESS)
                            .build()
                        )
                        self._active_tasks[task.task_id] = step_success_task
                        self._emit_task_event(
                            "step_succeeded",
                            step_success_task,
                            {"step": step.to_dict(), "result": step_result},
                        )

                        logger.info(f"Step succeeded: {step.step_id}")

                    else:
                        # Step failed
                        failed_step_ids = frozenset(
                            list(failed_step_ids) + [step.step_id]
                        )
                        step_results.append(
                            {
                                "step_id": step.step_id,
                                "success": False,
                                "result": step_result,
                                "error": step_result.get("error", "Unknown error"),
                            }
                        )

                        # Update task status
                        step_failure_task = (
                            step_running_task.to_builder()
                            .status(TaskStatus.STEP_FAILURE)
                            .build()
                        )
                        self._active_tasks[task.task_id] = step_failure_task
                        self._emit_task_event(
                            "step_failed",
                            step_failure_task,
                            {
                                "step": step.to_dict(),
                                "result": step_result,
                                "error": step_result.get("error"),
                            },
                        )

                        logger.warning(
                            f"Step failed: {step.step_id} - {step_result.get('error')}"
                        )

                        # Handle step-level recovery if enabled
                        if self.config.enable_step_recovery:
                            # Use step's internal retry_count or our tracking (if step is immutable)
                            # Actually, since step might not be updated, we should track it here
                            if not hasattr(self, "_step_retry_counts"):
                                self._step_retry_counts = {}
                            
                            current_retry_count = self._step_retry_counts.get(step.step_id, 0)
                            
                            # Create a modified step with updated retry count for the recovery handler
                            try:
                                # Try pydantic copy/update
                                if hasattr(step, "model_copy"):
                                    step_for_recovery = step.model_copy(update={"retry_count": current_retry_count})
                                elif hasattr(step, "copy"):
                                    step_for_recovery = step.copy(update={"retry_count": current_retry_count})
                                else:
                                    step_for_recovery = step
                                    # Fallback hack if it's not a standard pydantic model
                                    object.__setattr__(step_for_recovery, "retry_count", current_retry_count)
                            except Exception:
                                step_for_recovery = step
                            
                            recovery_result = self._handle_step_recovery(
                                step_for_recovery,
                                plan,
                                step_result,
                                completed_step_ids,
                                failed_step_ids,
                            )

                            if recovery_result.get("should_retry", False):
                                # Increment retry count
                                self._step_retry_counts[step.step_id] = current_retry_count + 1
                                
                                # Retry the step - remove from failed set and try again
                                failed_step_ids = frozenset(
                                    [
                                        sid
                                        for sid in failed_step_ids
                                        if sid != step.step_id
                                    ]
                                )

                                # Update task for retry
                                retry_task = (
                                    step_failure_task.to_builder()
                                    .status(TaskStatus.RECOVERING)
                                    .build()
                                )
                                self._active_tasks[task.task_id] = retry_task
                                self._emit_task_event(
                                    "step_recovery_started",
                                    retry_task,
                                    {
                                        "step": step.to_dict(),
                                        "recovery_action": recovery_result.get(
                                            "action"
                                        ),
                                    },
                                )

                                # Continue loop to retry the step
                                continue
                            elif recovery_result.get("should_replan", False):
                                # Task-level replan needed
                                logger.info("Step recovery requested task replan")
                                # In full implementation, this would trigger replanning
                                break

                        # If weget here, step failed and no recovery or recovery failed
                        break

                # If step recovery is disabled and we had any failures, break
                if not self.config.enable_step_recovery and len(failed_step_ids) > 0:
                    break

            # Determine final task status
            end_time = time.time()
            steps_completed = len(completed_step_ids)
            steps_total = len(plan.steps)

            if len(failed_step_ids) == 0 and steps_completed == steps_total:
                # All steps succeeded
                final_status = TaskStatus.COMPLETED
                failure = None
            else:
                # Partial success or complete failure
                final_status = TaskStatus.FAILED
                
                # Get the first failure for reporting
                first_failure = next(
                    (r for r in step_results if not r.get("success", False)), None
                )
                
                message = "Task execution failed"
                if steps_completed > 0:
                    message = f"Task partially completed ({steps_completed}/{steps_total} steps). "
                    if first_failure:
                        message += f"Failed at step: {first_failure.get('error', 'Unknown error')}"
                elif first_failure:
                    message = first_failure.get("error", "Step execution failed")
                    
                failure = TaskFailure(
                    step_id=first_failure["step_id"] if first_failure else "unknown",
                    failure_kind=FailureKind.EXECUTION,
                    message=message,
                )

            # Create task result
            result = TaskResult(
                task_id=task.task_id,
                status=final_status,
                steps_completed=steps_completed,
                steps_total=steps_total,
                start_time=start_time,
                end_time=end_time,
                failure=failure,
                metadata={"plan_id": plan.plan_id, "step_results": step_results},
            )

            # Final task verification (placeholder)
            if final_status == TaskStatus.COMPLETED:
                verified_result = self._perform_final_verification(task, plan, result)
                if not verified_result.get("verified", True):
                    # Verification failed
                    result = TaskResult(
                        task_id=task.task_id,
                        status=TaskStatus.FAILED,
                        steps_completed=steps_completed,
                        steps_total=steps_total,
                        start_time=start_time,
                        end_time=end_time,
                        failure=TaskFailure(
                            step_id="verification",
                            failure_kind=FailureKind.VERIFICATION,
                            message="Final task verification failed",
                        ),
                    )

            # Update final task status
            if result.is_successful:
                completed_task = (
                    running_task.to_builder()
                    .status(TaskStatus.COMPLETED)
                    .completed_at(end_time)
                    .build()
                )
            else:
                completed_task = (
                    running_task.to_builder()
                    .status(TaskStatus.FAILED)
                    .completed_at(end_time)
                    .build()
                )

            self._active_tasks[task.task_id] = completed_task
            self._emit_task_event("task_finished", completed_task, result)

            return result

        except Exception as e:
            import traceback
            logger.error(f"Error during task plan execution: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")

            # Create failure result
            result = TaskResult(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                steps_completed=len(completed_step_ids),
                steps_total=len(plan.steps),
                start_time=start_time,
                end_time=time.time(),
                failure=TaskFailure(
                    step_id="task_executor",
                    failure_kind=FailureKind.INTERNAL,
                    message=str(e),
                ),
            )

            # Update task status
            failed_task = (
                running_task.to_builder()
                .status(TaskStatus.FAILED)
                .completed_at(time.time())
                .build()
            )
            self._active_tasks[task.task_id] = failed_task
            self._emit_task_event("task_failed", failed_task, result)

            return result

    def _execute_task_step(
        self, step: TaskStep, plan: TaskPlan, completed_step_ids: FrozenSet[str]
    ) -> Dict[str, Any]:
        """Execute a single task step using the PlanExecutor."""
        logger.debug(f"Executing task step via PlanExecutor: {step.step_id}")

        # Convert TaskStep to PlanStep for execution
        plan_step = self._convert_task_step_to_plan_step(step, plan)

        # Create execution context
        goal = Goal(
            goal_id=str(uuid4()),
            description=step.intent,
            metadata={"task_step_id": step.step_id, "task_plan_id": plan.plan_id},
        )

        intent = Intent(
            intent_id=str(uuid4()),
            kind=IntentKind.COMMAND,
            text=step.intent,
        )

        execution_plan = Plan(
            plan_id=str(uuid4()),
            goal_id=goal.goal_id,
            steps=(plan_step,),
            metadata={"task_step_id": step.step_id, "task_plan_id": plan.plan_id},
        )

        context = ExecutionContext(
            execution_id=str(uuid4()),
            goal=goal,
            intent=intent,
            plan=execution_plan,
            completed_step_ids=frozenset(),
            failed_step_ids=frozenset(),
        )

        # Execute using PlanExecutor
        try:
            execution_result: ExecutionResult = self.plan_executor.execute(context)

            # Convert ExecutionResult to task step result format
            return {
                "success": execution_result.outcome == ExecutionOutcome.COMPLETED,
                "step_id": step.step_id,
                "execution_outcome": execution_result.outcome.value,
                "step_count": execution_result.step_count,
                "succeeded_step_count": execution_result.succeeded_step_count,
                "failed_step_count": execution_result.failed_step_count,
                "duration_ms": execution_result.duration_ms,
                "error": execution_result.error if execution_result.error else None,
                "step_results": [
                    {
                        "step_id": sr.step_id,
                        "status": sr.status.value,
                        "duration_ms": sr.duration_ms,
                        "error": sr.error,
                    }
                    for sr in execution_result.step_results
                ],
            }
        except Exception as e:
            logger.error(f"PlanExecutor execution failed: {e}")
            return {
                "success": False,
                "step_id": step.step_id,
                "error": f"PlanExecutor execution failed: {e}",
                "execution_outcome": "FAILED",
            }

    def _convert_task_step_to_plan_step(
        self, task_step: TaskStep, task_plan: TaskPlan
    ) -> PlanStep:
        """Convert a TaskStep to a PlanStep for execution."""
        # Map task step capability to action kind
        # For simplicity, we'll use CAPABILITY_CALL for most capabilities
        action_kind = None
        if task_step.capability:
            # Try to map common capability patterns
            if any(
                pattern in task_step.capability.lower()
                for pattern in ["mouse", "click", "type", "key"]
            ):
                action_kind = ActionKind.CAPABILITY_CALL
            else:
                action_kind = ActionKind.CAPABILITY_CALL  # Default

        # If no capability or unknown, use a generic action
        if action_kind is None:
            action_kind = ActionKind.CAPABILITY_CALL

        return PlanStep(
            step_id=task_step.step_id,
            description=task_step.description,
            action=action_kind,
            capability_name=task_step.capability or "automation.general",
            parameters=dict(task_step.parameters),
            expected_effect=ExpectedEffect(
                check_name="task_step_completion",
                expected=task_step.expected_result or "Step completed",
                description=f"Expected result for step {task_step.step_id}",
            ),
            depends_on=frozenset(task_step.dependencies),
            timeout_s=task_step.timeout_s,
            metadata={
                "task_step_id": task_step.step_id,
                "task_plan_id": task_plan.plan_id,
                "task_step_description": task_step.description,
                "task_step_intent": task_step.intent,
            },
        )

    def _handle_step_recovery(
        self,
        step: TaskStep,
        plan: TaskPlan,
        step_result: Dict[str, Any],
        completed_step_ids: FrozenSet[str],
        failed_step_ids: FrozenSet[str],
    ) -> Dict[str, Any]:
        """Handle recovery for a failed step."""
        logger.info(f"Handling recovery for step: {step.step_id}")

        # Simple recovery logic - in practice, this would use a recovery engine
        error = step_result.get("error", "")

        # Determine recovery action based on error type
        if "timeout" in error.lower() or "TIMEOUT" in error.upper():
            # Timeout - retry with backoff
            return {
                "should_retry": True,
                "action": TaskRecoveryAction.RETRY_STEP_BACKOFF,
                "backoff_s": min(
                    2.0 * (step.retry_count + 1), 10.0
                ),  # Exponential backoff
            }
        elif "unknown capability" in error.lower() or "not authorized" in error.lower():
            # Configuration issue - unlikely to recover
            return {"should_retry": False, "action": TaskRecoveryAction.GIVE_UP}
        elif step.retry_count < step.max_retries:
            # Generic retry
            return {"should_retry": True, "action": TaskRecoveryAction.RETRY_STEP}
        else:
            # Max retries exceeded
            return {"should_retry": False, "action": TaskRecoveryAction.GIVE_UP}

    def _perform_final_verification(
        self, task: Task, plan: TaskPlan, task_result: TaskResult
    ) -> Dict[str, Any]:
        """Perform final verification of task completion."""
        logger.info(f"Performing final verification for task: {task.task_id}")

        # In a full implementation, this would check that the user goal was actually achieved
        # For Stage 21, we'll do a basic verification

        verification_passed = (
            task_result.is_successful and task_result.steps_completed > 0
        )

        # Additional verification could go here - checking UI state, file existence, etc.

        return {
            "verified": verification_passed,
            "verification_details": {
                "task_goal": task.user_goal,
                "steps_completed": task_result.steps_completed,
                "steps_total": task_result.steps_total,
                "execution_time_ms": task_result.duration_ms,
            },
        }

    def _emit_task_event(
        self, event_type: str, task: Task, result: Optional[TaskResult] = None
    ) -> None:
        """Emit a task-related event."""
        if not self.config.event_publisher:
            return

        try:
            event_data = {
                "task_id": task.task_id,
                "user_goal": task.user_goal,
                "task_kind": task.task_kind.value,
                "status": task.status.value,
                "timestamp": time.time(),
            }

            if result:
                event_data.update(
                    {
                        "steps_completed": result.steps_completed,
                        "steps_total": result.steps_total,
                        "duration_ms": result.duration_ms,
                        "is_successful": result.is_successful,
                    }
                )
                if result.failure:
                    event_data["failure"] = {
                        "step_id": result.failure.step_id,
                        "message": result.failure.message,
                        "failure_kind": result.failure.failure_kind.value,
                    }

            self.config.event_publisher(event_type, event_data)
        except Exception as e:
            logger.warning(f"Failed to emit task event {event_type}: {e}")

    def get_task_status(self, task_id: str) -> Optional[TaskStatus]:
        """Get the current status of a task."""
        task = self._active_tasks.get(task_id)
        return task.status if task else None

    def get_active_tasks(self) -> List[Task]:
        """Get all currently active tasks."""
        return list(self._active_tasks.values())

    def cancel_task(self, task_id: str) -> bool:
        """Cancel an active task."""
        task = self._active_tasks.get(task_id)
        if not task:
            return False

        if task.is_completed:
            return False  # Already completed

        # Update task status
        cancelled_task = (
            task.to_builder()
            .status(TaskStatus.CANCELLED)
            .completed_at(time.time())
            .build()
        )
        self._active_tasks[task_id] = cancelled_task

        # Emit cancellation event
        self._emit_task_event("task_cancelled", cancelled_task, None)

        logger.info(f"Task cancelled: {task_id}")
        return True

    def shutdown(self) -> None:
        """Shutdown the TaskExecutor."""
        logger.info("Shutting down TaskExecutor")
        self._active_tasks.clear()
        self._task_plans.clear()

    def execute(self, context: ExecutionContext) -> ExecutionResult:
        """Execute a plan from the given execution context.

        This method makes the TaskExecutor compatible with the PlanExecutor
        interface by converting ExecutionContext to Task and TaskResult back
        to ExecutionResult.

        Args:
            context: The execution context containing goal and plan

        Returns:
            ExecutionResult: The outcome of plan execution
        """
        logger.debug(f"TaskExecutor.execute called for goal: {context.goal.description}")

        # Create a task from the context goal
        task = create_task(
            user_goal=context.goal.description,
            task_kind=TaskKind.AUTOMATION,  # Default kind, could be inferred from goal
            metadata={
                "execution_id": context.execution_id,
                "correlation_id": context.metadata.get("correlation_id", ""),
                **(context.metadata or {})
            }
        )

        # If context already has a plan, use it; otherwise create a simple plan
        if context.plan:
            # Convert the orchestration Plan to a TaskPlan
            # This is a simplified conversion - in a full implementation,
            # we would map each PlanStep to a TaskStep properly
            task_steps = []
            for i, plan_step in enumerate(context.plan.steps):
                task_step = create_task_step(
                    sequence=i,
                    description=plan_step.description or f"Execute step {i+1}",
                    intent=plan_step.description or "",
                    capability=getattr(plan_step, 'kind', 'automation.general'),
                    parameters=getattr(plan_step, 'parameters', {}),
                    expected_result=getattr(plan_step, 'success_criteria', [""])[0] if getattr(plan_step, 'success_criteria', None) else "",
                    verification_capability="",  # Would need to be mapped
                    dependencies=frozenset(getattr(plan_step, 'dependencies', [])),
                    timeout_s=30.0,  # Default timeout
                    max_retries=3
                )
                task_steps.append(task_step)

            task_plan = create_task_plan(
                user_goal=context.goal.description,
                steps=tuple(task_steps),
                timeout_s=context.plan.timeout_s if hasattr(context.plan, 'timeout_s') else 300.0,
                max_retries=context.plan.max_retries if hasattr(context.plan, 'max_retries') else 2,
                metadata={
                    "plan_id": context.plan.plan_id,
                    "created_by": "TaskExecutor.execute_adapter"
                }
            )

            # Attach the plan to the task
            task = task.to_builder().with_plan(task_plan).build()
        else:
            # No plan provided, let TaskExecutor create a simple plan
            # (this will be handled by _execute_task_attempt -> _create_simple_plan)
            pass

        # Execute the task
        task_result = self.execute_task(task)

        # Convert TaskResult back to ExecutionResult
        # Map TaskStatus to ExecutionOutcome
        outcome_mapping = {
            TaskStatus.COMPLETED: ExecutionOutcome.COMPLETED,
            TaskStatus.FAILED: ExecutionOutcome.FAILED,
            TaskStatus.CANCELLED: ExecutionOutcome.CANCELLED,
            # For running states, we treat as incomplete but not failed yet
            TaskStatus.RUNNING: ExecutionOutcome.COMPLETED if task_result.steps_completed == task_result.steps_total else ExecutionOutcome.FAILED,
            TaskStatus.STEP_SUCCESS: ExecutionOutcome.COMPLETED,
            TaskStatus.STEP_FAILURE: ExecutionOutcome.FAILED,
            # Treat other states as failed for simplicity
            TaskStatus.PENDING: ExecutionOutcome.FAILED,
            TaskStatus.STEP_READY: ExecutionOutcome.FAILED,
            TaskStatus.STEP_RUNNING: ExecutionOutcome.FAILED,
            TaskStatus.RECOVERING: ExecutionOutcome.FAILED,
            TaskStatus.BLOCKED: ExecutionOutcome.BLOCKED,
            TaskStatus.SKIPPED: ExecutionOutcome.PARTIAL,
        }

        outcome = outcome_mapping.get(task_result.status, ExecutionOutcome.FAILED)

        # Create step results (simplified - in reality we'd need to track each step)
        step_results = ()
        if hasattr(task_result, 'metadata') and 'step_results' in task_result.metadata:
            # Convert detailed step result dicts to StepResult objects
            sr_objects = []
            for sr_dict in task_result.metadata['step_results']:
                # The dict might have 'result' with detailed fields, or direct fields
                res = sr_dict.get('result', {})
                status_str = res.get('status') or res.get('execution_outcome') or (
                    'completed' if sr_dict.get('success') else 'failed'
                )
                
                # Map string status to StepState enum
                try:
                    status = StepState(status_str.lower())
                except ValueError:
                    status = StepState.COMPLETED if sr_dict.get('success') else StepState.FAILED
                    
                sr_objects.append(
                    StepResult(
                        step_id=sr_dict.get('step_id', str(uuid4())),
                        capability_name="automation.general",  # Default if unknown
                        status=status,
                        error=sr_dict.get('error'),
                        duration_ms=res.get('duration_ms', 0.0),
                    )
                )
            step_results = tuple(sr_objects)

        execution_result = ExecutionResult(
            execution_id=context.execution_id,
            plan_id=getattr(context.plan, 'plan_id', str(uuid4())) if context.plan else str(uuid4()),
            goal_id=context.goal.goal_id,
            outcome=outcome,
            step_results=step_results,
            started_at=task_result.start_time,
            completed_at=task_result.end_time,
            duration_ms=task_result.duration_ms,
            correlation_id=context.metadata.get("correlation_id", ""),
            error=task_result.failure.message if task_result.failure else "",
            metadata={
                "task_id": task_result.task_id,
                "task_status": task_result.status.value,
                "steps_completed": task_result.steps_completed,
                "steps_total": task_result.steps_total,
                **(task_result.metadata or {})
            }
        )

        logger.debug(f"TaskExecutor.execute completed with outcome: {outcome.value}")
        return execution_result


# Builder pattern for Task modifications (since Task is frozen)
@dataclass
class _TaskBuilder:
    """Builder for creating modified Task instances."""

    task_id: str
    user_goal: str
    task_kind: TaskKind = TaskKind.AUTOMATION
    _status: TaskStatus = TaskStatus.PENDING
    plan: Optional[TaskPlan] = None
    _current_step_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    _completed_at: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def status(self, value: TaskStatus) -> "_TaskBuilder":
        self._status = value
        return self

    def with_plan(self, plan: TaskPlan) -> "_TaskBuilder":
        self.plan = plan
        return self

    def current_step_id(self, step_id: str) -> "_TaskBuilder":
        self._current_step_id = step_id
        return self

    def started_at(self, started_at: Optional[float]) -> "_TaskBuilder":
        self.started_at = started_at
        return self

    def completed_at(self, completed_at: Optional[float]) -> "_TaskBuilder":
        self._completed_at = completed_at
        return self

    def started_now(self) -> "_TaskBuilder":
        self.started_at = time.time()
        return self

    def completed_now(self, result: TaskResult) -> "_TaskBuilder":
        self._completed_at = time.time()
        # In a full implementation, we might store the result with the task
        return self

    def failed_now(self, result: TaskResult) -> "_TaskBuilder":
        self._completed_at = time.time()
        self._status = TaskStatus.FAILED
        return self

    def retried(self) -> "_TaskBuilder":
        # Could increment retry count if we tracked it
        return self

    def build(self) -> Task:
        return Task(
            task_id=self.task_id,
            user_goal=self.user_goal,
            task_kind=self.task_kind,
            status=self._status,
            plan=self.plan,
            current_step_id=self._current_step_id,
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self._completed_at,
            metadata=self.metadata,
        )


# Extension to Task to add builder method
def _add_builder_to_task():
    """Add builder method to Task class."""
    Task.to_builder = lambda self: _TaskBuilder(
        task_id=self.task_id,
        user_goal=self.user_goal,
        task_kind=self.task_kind,
        _status=self.status,
        plan=self.plan,
        _current_step_id=self.current_step_id,
        created_at=self.created_at,
        started_at=self.started_at,
        _completed_at=self.completed_at,
        metadata=self.metadata,
    )


# Apply the extension
_add_builder_to_task()
