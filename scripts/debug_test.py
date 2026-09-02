import asyncio
from unittest.mock import Mock
from core.execution import ExecutionCycle, ExecutionStep, StepAction, VerificationExpectation
from core.execution.provider import DefaultActionExecutor, DefaultGroundingProvider, DefaultVerificationProvider
from core.capability_router import CapabilityRouter
from core.capability_registry import CapabilityRegistry
from vision.perception_adapter import PerceptionAdapter
from vision.router.perception_router import PerceptionRouter
from vision.router.screenshot_provider import ScreenshotProvider
from core.capabilities import MouseClickCapability, KeyboardTypeCapability
from core.orchestration.cancellation import CancellationToken
from core.grounding.target_resolver import TargetResolver
from core.results import CapabilityResult, CapabilityStatus


class MockPerceptionRouter:
    def __init__(self, return_candidates=None):
        self._candidates = return_candidates or []
        self.call_count = 0

    def find_targets(self, target_query="*", image_path=None):
        self.call_count += 1
        from vision.observations.targets import TargetCandidate
        from core.orchestration.models import ObservationSource

        # Return a mock button candidate
        candidate = TargetCandidate(
            text="Test Button",
            bbox=(100, 100, 200, 150),
            confidence=0.95,
            source_type=ObservationSource.UIA,
            properties={
                "name": "Test Button",
                "automation_id": "testButton",
                "control_type": "Button"
            }
        )
        return [candidate]


class MockScreenshotProvider:
    def capture(self, path=None):
        return b"mock-screenshot-data"


class MockInputService:
    def __init__(self):
        self.call_log = []
        self.initialized = True

    def initialize(self):
        self.initialized = True

    def click(self, x=None, y=None, button="left", clicks=1):
        self.call_log.append({"action": "click", "x": x, "y": y, "button": button, "clicks": clicks})
        from core.results import ActionResult, ActionStatus
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="click",
            details={"success": True, "x": x, "y": y, "button": button, "clicks": clicks}
        )

    def move_mouse(self, x=None, y=None):
        self.call_log.append({"action": "move", "x": x, "y": y})
        from core.results import ActionResult, ActionStatus
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="move",
            details={"success": True, "x": x, "y": y}
        )

    def type_text(self, text="", target=None):
        self.call_log.append({"action": "type", "text": text, "target": target})
        from core.results import ActionResult, ActionStatus
        return ActionResult(
            status=ActionStatus.EXECUTED,
            action_name="type",
            details={"success": True, "text": text, "target": target}
        )


async def debug_test():
    print("Setting up components...")

    # Setup real perception adapter (with mocked dependencies for determinism)
    perception_router = MockPerceptionRouter()
    screenshot_provider = MockScreenshotProvider()
    perception_adapter = PerceptionAdapter(perception_router, screenshot_provider)
    print("Perception adapter created")

    # Setup real grounding provider
    grounding_provider = DefaultGroundingProvider(TargetResolver())
    print("Grounding provider created")

    # Setup real action executor with registry containing real capabilities
    registry = CapabilityRegistry()
    input_service = MockInputService()
    registry.register(MouseClickCapability(input_service))
    registry.register(KeyboardTypeCapability(input_service))
    router = CapabilityRouter(registry)
    action_executor = DefaultActionExecutor(router)
    print("Action executor created")

    # Setup real verification provider
    verification_provider = DefaultVerificationProvider(perception_adapter)
    print("Verification provider created")

    # Create execution cycle with real components
    cycle = ExecutionCycle(
        perception_provider=perception_adapter,
        target_resolver=grounding_provider,
        action_executor=action_executor,
        verification_provider=verification_provider,
    )
    print("Execution cycle created")

    # Create test step with coordinate-based click (no target_query needed)
    step = ExecutionStep(
        step_id="debug-test",
        description="Click at coordinates",
        action=StepAction.CLICK,
        capability_name="desktop.mouse.click",
        parameters={"x": 150, "y": 125},  # Direct coordinates
        expectation=VerificationExpectation.target_visible("Test Button"),
    )
    print("Step created")
    print(f"Step: {step}")

    # Execute
    print("Executing step...")
    result = await cycle.execute(step)

    print(f"Result status: {result.status}")
    print(f"Result: {result}")
    print(f"Observation: {result.observation}")
    if result.observation:
        print(f"Observation status: {result.observation.status}")
        print(f"Observation candidates: {len(result.observation.candidates) if result.observation.candidates else 0}")

    print(f"Action result: {result.action_result}")
    if result.action_result:
        print(f"Action result status: {result.action_result.status}")

    print(f"Verification result: {result.verification_result}")
    if result.verification_result:
        print(f"Verification result status: {result.verification_result.status}")
        print(f"Verification result success: {result.verification_result.success}")

    print("Test completed")


if __name__ == "__main__":
    asyncio.run(debug_test())