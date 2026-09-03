# Exact copy of the test function from the file
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
    # Step 1 depends on Step 2
    # Step 2 depends on Step 3
    # Step 3 depends on Step 4
    # Step 4 depends on Step 1

    # Create all four steps FIRST to get their IDs
    s1 = create_task_step(0, "Step 1", "Intent 1", "cap1")
    s2 = create_task_step(1, "Step 2", "Intent 2", "cap2")
    s3 = create_task_step(2, "Step 3", "Intent 3", "cap3")
    s4 = create_task_step(3, "Step 4", "Intent 4", "cap4")

    # Now create the ACTUAL steps that will go in the plan, with correct dependencies
    # Using the IDs we just obtained above
    step1 = create_task_step(0, "Step 1", "Intent 1", "cap1", dependencies=frozenset([s2.step_id]))  # Step 1 -> Step 2
    step2 = create_task_step(1, "Step 2", "Intent 2", "cap2", dependencies=frozenset([s3.step_id]))  # Step 2 -> Step 3
    step3 = create_task_step(2, "Step 3", "Intent 3", "cap3", dependencies=frozenset([s4.step_id]))  # Step 3 -> Step 4
    step4 = create_task_step(3, "Step 4", "Intent 4", "cap4", dependencies=frozenset([s1.step_id]))  # Step 4 -> Step 1

    plan2 = create_task_plan("Test goal", (step1, step2, step3, step4))
    result = plan2.has_circular_dependency()
    print(f"Result: {result}")
    print(f"Expected: True")
    assert result == True

if __name__ == "__main__":
    test_task_plan_has_circular_dependency()
    print("Test passed!")