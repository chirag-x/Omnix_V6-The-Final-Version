"""
Smoke test for the System 8 Agent Orchestrator wired into the
real production engine.

Boots :func:`main.build_engine`, monkey-patches the ``Agent``'s
progress broadcaster with an in-memory recorder (so we can inspect
the structured events that flow through the Agent), and runs a
request that requires the full Agent loop (the request must NOT
match the local fast-path so the Agent is actually invoked).

Run with::

    python scripts/smoke_system8.py

This is a *diagnostic* script, not a unit test.  It is intentionally
not wired into the pytest harness so the production engine can be
exercised exactly as a user invokes it (boot + process + stop).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.orchestration.progress import (
    CompositeProgressBroadcaster,
    InMemoryProgressBroadcaster,
    LogProgressBroadcaster,
)
from main import build_engine


# A request that the local fast-path *cannot* match, so the full
# Agent loop runs.  The mock provider's deterministic planner will
# produce a multi-step plan, the Agent will dispatch it, and the
# recovery engine will replan on failure.
DEFAULT_REQUEST = (
    "Find me the file with the latest version number on my desktop"
)


def main() -> int:
    recorder = InMemoryProgressBroadcaster()

    _, engine = build_engine(Path.cwd(), quiet=True, headless=True)
    if engine is None:
        print("FATAL: engine did not build", file=sys.stderr)
        return 1

    # Locate the Agent instance and replace its progress broadcaster
    # with a composite of the existing production one plus an
    # in-memory recorder.  This is a read-only diagnostic — it does
    # not modify any production wiring.
    agent = getattr(getattr(engine, "pipeline", None), "agent", None)
    if agent is not None:
        existing = getattr(agent, "progress_broadcaster", None)
        if existing is None:
            agent.progress_broadcaster = recorder
        else:
            agent.progress_broadcaster = CompositeProgressBroadcaster(
                existing, recorder
            )

    request = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REQUEST

    try:
        engine.start()
        response = engine.process(request)
    finally:
        engine.stop()

    print(f"\n=== response ===")
    print(f"status:   {response.status}")
    print(f"text:     {response.text}")
    if response.error:
        print(f"error:    {response.error[:120]}")

    print(f"\n=== progress events ({len(recorder.events())}) ===")
    for ev in recorder.events():
        print(
            f"  {ev.phase.value:>22} | "
            f"step={ev.step_id or '-':<10} | "
            f"plan={ev.plan_id or '-':<22} | "
            f"msg={ev.message[:60]}"
        )

    # Summary by phase
    from collections import Counter

    phases = Counter(ev.phase.value for ev in recorder.events())
    print(f"\nphase distribution: {dict(phases)}")

    # The Agent is considered to have run if at least one step
    # was dispatched.  A clean PLANNING-only failure (no steps
    # dispatched) is reported as a different exit code so CI can
    # tell them apart.
    return 0 if phases.get("step_dispatched", 0) > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
