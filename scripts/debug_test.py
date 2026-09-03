# Exact copy of the test function
from core.task.models import (
    Task,
    TaskPlan,
    TaskStep,
    TaskStatus,
    TaskKind,
    TaskResult,
    TaskFailure,
    create_task,
    create_task_step,
    create_task_plan
)

def test_task_plan_has_circular_dependency():
    """Test TaskPlan.has_circular_dependency method"""
    # No circular dependency
    step1 = create_task_step(0, "Step 1", "Intent 1", "cap1", dependencies=frozenset())
    step2 = create_task_step(1, "Step 2", "Intent 2", "cap2", dependencies=frozenset([step1.step_id]))
    plan1 = create_task_plan("Test goal", (step1, step2))
    assert plan1.has_circular_dependency() == False

    # Circular dependency: 1 -> 2 -> 3 -> 4 -> 1
    # To create this, we need:
    # Step 1 depends on Step 4
    # Step 2 depends on Step 1
    # Step 3 depends on Step 2
    # Step 4 depends on Step 3

    # Create steps in reverse order so we can reference the correct IDs
    # We'll create them all first to get their IDs, then recreate with proper deps

    # First pass: create steps to get their IDs for the circular dependency
    temp_step1 = create_task_step(0, "Step 1", "Intent 1", "cap1")
    temp_step2 = create_task_step(1, "Step 2", "Intent 2", "cap2")
    temp_step3 = create_task_step(2, "Step 3", "Intent 3", "cap3")
    temp_step4 = create_task_step(3, "Step 4", "Intent 4", "cap4")

    # Second pass: create the actual steps with correct dependencies
    # For circle 1->2->3->4->1:
    # Step 1 should depend on Step 4's ID (temp_step4.step_id)
    # Step 2 should depend on Step 1's ID (temp_step1.step_id)
    # Step 3 should depend on Step 2's ID (temp_step2.step_id)
    # Step 4 should depend on Step 3's ID (temp_step3.step_id)
    step1 = create_task_step(0, "Step 1", "Intent 1", "cap1", dependencies=frozenset([temp_step4.step_id]))
    step2 = create_task_step(1, "Step 2", "Intent 2", "cap2", dependencies=frozenset([temp_step1.step_id]))
    step3 = create_task_step(2, "Step 3", "Intent 3", "cap3", dependencies=frozenset([temp_step2.step_id]))
    step4 = create_task_step(3, "Step 4", "Intent 4", "cap4", dependencies=frozenset([temp_step3.step_id]))

    plan2 = create_task_plan("Test goal", (step1, step2, step3, step4))
    result = plan2.has_circular_dependency()
    print(f"Result: {result}")
    assert result == True

if __name__ == "__main__":
    test_task_plan_has_circular_dependency()
    print("Test passed!")