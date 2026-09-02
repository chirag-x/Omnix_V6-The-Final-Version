"""Probe multi-step parsing."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from core.services.local_decision_engine import LocalActionDecisionEngine, LocalDecision

eng = LocalActionDecisionEngine()
queries = [
    "Open Notepad and type Hello World",
    "Open Notepad",
    "Type Hello World",
    "Open Calculator and compute 2+2",
    "Open Chrome and search for AI agents",
]
for q in queries:
    d = eng.evaluate(q)
    print(f"{q!r}")
    print(f"  matched={d.matched} intent={d.intent!r} verb={d.verb_class!r} target={d.target!r}")
    print(f"  plan_steps={len(d.plan) if d.plan else 0} not_found={d.not_found}")
    if d.plan:
        for s in d.plan:
            print(f"    step: verb={s.verb_class!r} target={s.target!r} args={s.arguments!r}")
    print()
