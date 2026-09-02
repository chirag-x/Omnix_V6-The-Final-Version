"""Quick probe to verify Phase 16 local engine fixes."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from core.capability_registry import CapabilityRegistry
from system.application.catalog import ApplicationCatalog
from system.application.resolver import ApplicationResolver
from core.services.local_decision_engine import LocalActionDecisionEngine

registry = CapabilityRegistry()
catalog = ApplicationCatalog()
resolver = ApplicationResolver(catalog)

engine = LocalActionDecisionEngine(registry=registry, resolver=resolver)

queries = [
    "Open Notepad",
    "Open Notepad and type Hello World",
    "Open Notepad, type Hello from Omnix, and save it as omnix_test.txt",
    "save it as omnix_test.txt",
    "save the file as omnix_test.txt",
    "Open Chrome and search for AI agents",
    "Open Chrome, search for AI agents, and open the second result",
]

for q in queries:
    print(f"\n{'='*60}\nQuery: {q!r}\n{'='*60}")
    decision = engine.classify(q)
    print(f"matched: {decision.matched}, not_found: {decision.not_found}")
    if decision.plan:
        print(f"  step_count: {len(decision.plan.steps)}")
        for s in decision.plan.steps:
            print(f"    [{s.step_id}] {s.capability_name}  params={dict(s.parameters)}")
