"""Probe what the local decision engine does with Chrome queries — using the engine's actual registry."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from main import build_engine

cfg, eng = build_engine()
eng.initialize()
eng.start()
disp = eng.pipeline.app_dispatcher
# Reach into the engine's local decision engine
fast = disp._engine
print("registry size:", len(eng.capabilities.list_names()))
print()

queries = [
    "Open Chrome and search for AI agents",
    "Open Chrome, search for AI agents, and open the second result",
    "search for AI agents",
    "search the web for weather",
    "open the second result",
    "Open Chrome",
    "Open Chrome and open the second result",
]

for q in queries:
    d = fast.classify(q)
    print(f"Query: {q!r}")
    print(f"  matched:   {d.matched}")
    print(f"  not_found: {d.not_found}")
    if d.plan:
        for s in d.plan.steps:
            print(f"  step:      {s.step_id}  cap={s.capability_name}  params={dict(s.parameters)}")
    print()
