"""Diagnostic script: trace "Open Chrome" through every layer.

This is read-only: it does not modify production code.  It builds the
engine, captures the brain result and the agent result, and prints
the relevant state at every boundary.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

# Force the working directory to the project root so the engine can find
# its data files.
project_root = Path(__file__).resolve().parents[1]
import os
os.chdir(project_root)
sys.path.insert(0, str(project_root))

# Use mock provider to keep this deterministic and offline.
os.environ.setdefault("OMNIX_LLM_PROVIDER", "mock")

from loguru import logger
logger.remove()
logger.add(sys.stdout, level="INFO")

from main import build_engine
from core.responses import ResponseStatus
from core.orchestration import AgentState

config, engine = build_engine(project_root, quiet=False, headless=None)
print("=" * 70)
print("ENGINE STATS")
print("=" * 70)
stats = engine.statistics()
print(f"  capabilities_loaded: {stats.get('capabilities_loaded')}")
print(f"  pipeline_available : {stats.get('pipeline_available')}")
print(f"  lifecycle          : {stats.get('lifecycle')}")
print()

print("REGISTRY NAMES (filtered to desktop.application.*)")
print("=" * 70)
names = engine.capabilities.list_names()
print(f"  total: {len(names)}")
for n in sorted(names):
    if n.startswith("desktop.application."):
        print(f"   - {n}")
print()

print("PIPELINE COMPONENTS")
print("=" * 70)
print(f"  pipeline        : {engine.pipeline!r}")
if engine.pipeline is not None:
    print(f"  brain           : {engine.pipeline.brain!r}")
    print(f"  agent           : {engine.pipeline.agent!r}")
    agent = engine.pipeline.agent
    print(f"  agent.multi_step_coordinator: {agent.multi_step_coordinator!r}")
    print(f"  agent.vision_service        : {agent.vision_service!r}")
    print(f"  agent.recovery_engine       : {agent.recovery_engine!r}")
print()

# --- Trace the brain directly ---------------------------------------
text = "Open Chrome"
print(f"BRAIN.handle_text({text!r})")
print("=" * 70)
try:
    brain_result = engine.pipeline.brain.handle_text(text)
    print(f"  status              : {brain_result.status!r}")
    print(f"  intent.kind         : "
          f"{brain_result.intent.kind if brain_result.intent else None}")
    print(f"  intent.parameters   : "
          f"{brain_result.intent.parameters if brain_result.intent else None}")
    print(f"  goal.description    : "
          f"{brain_result.goal.description if brain_result.goal else None}")
    print(f"  goal.metadata       : "
          f"{brain_result.goal.metadata if brain_result.goal else None}")
    if brain_result.plan is not None:
        print(f"  plan.plan_id        : {brain_result.plan.plan_id}")
        print(f"  plan.steps ({len(brain_result.plan.steps)}):")
        for s in brain_result.plan.steps:
            print(f"    step_id={s.step_id} capability={s.capability_name!r} "
                  f"params={dict(s.parameters)}")
    else:
        print(f"  plan                : None")
    if brain_result.error_code:
        print(f"  error_code          : {brain_result.error_code}")
    if brain_result.error_message:
        print(f"  error_message       : {brain_result.error_message}")
except Exception:
    traceback.print_exc()
print()

# --- Trace the agent directly ---------------------------------------
print(f"AGENT.run({text!r}) (synchronous, NOT through pipeline)")
print("=" * 70)
try:
    agent_result = engine.pipeline.agent.run(text)
    print(f"  agent_run_id   : {agent_result.agent_run_id}")
    print(f"  final_state    : {agent_result.final_state!r}")
    print(f"  plan_count     : {agent_result.plan_count}")
    print(f"  attempts       : {agent_result.attempts}")
    print(f"  error          : {agent_result.error!r}")
    if agent_result.metadata:
        print(f"  metadata       : {json.dumps({k:str(v) for k,v in agent_result.metadata.items()}, default=str)}")
    print(f"  plans (history):")
    for i, entry in enumerate(agent_result.plan_history or ()):
        plan = entry.plan
        print(f"    [{i}] plan_id={plan.plan_id} attempt={entry.attempt} "
              f"steps={len(plan.steps)}")
        for s in plan.steps:
            print(f"        step_id={s.step_id} capability={s.capability_name!r} "
                  f"params={dict(s.parameters)}")
    if agent_result.failure_history:
        print(f"  failure_history:")
        for f in agent_result.failure_history:
            print(f"    {f}")
except Exception:
    traceback.print_exc()
print()

# --- Now trace the full pipeline ------------------------------------
print(f"PIPELINE.process({text!r}) — full path through the engine")
print("=" * 70)
try:
    response = engine.process(text)
    print(f"  status        : {response.status!r}")
    print(f"  text          : {response.text!r}")
    print(f"  error         : {response.error!r}")
    print(f"  agent_state   : {response.agent_state!r}")
    print(f"  metadata      : {json.dumps({k:str(v) for k,v in (response.metadata or {}).items()}, default=str)}")
except Exception:
    traceback.print_exc()

engine.stop()
print()
print("DONE")
