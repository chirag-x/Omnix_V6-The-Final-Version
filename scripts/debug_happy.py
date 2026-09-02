"""Quick debug for happy path test."""
from tests.test_system8_agent_orchestration import (
    _Echo,
    _WiredAgent,
    _step,
    InMemoryProgressBroadcaster,
    ProgressPhase,
)

echo = _Echo()
wire = _WiredAgent(
    steps=[
        _step("a", "test.echo", params={"msg": "hi"}),
        _step("b", "test.echo", params={"msg": "there"}),
    ],
    capabilities={"test.echo": echo},
    broadcaster=InMemoryProgressBroadcaster(),
)
result = wire.agent.run("do the test")
print("completed:", result.completed)
print("final_state:", result.final_state)
print("error:", result.error)
print("clarifying_question:", result.clarifying_question)
print("verdicts:", [(sr.step_id, sr.status.value if sr.status else "?") for sr in (result.final_execution_id and [])])
print("events:")
for e in wire.broadcaster.events():
    print(" ", e.phase.value, e.step_id, e.message[:80])
