"""Probe engine directly."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from main import build_engine

def main():
    cfg, eng = build_engine()
    print("=== Engine built ===")
    queries = [
        "Open Notepad and type Hello World",
        "Open Chrome and search for AI agents",
        "Type hello",
        "What is 2 plus 2",
    ]
    for q in queries:
        print(f"\n>>> {q!r}")
        result = eng.process(q)
        print(f"    status={getattr(result, 'status', '?')!r}")
        print(f"    text={(getattr(result, 'text', '') or '')[:200]!r}")
        print(f"    plan_steps={len(result.plan) if getattr(result, 'plan', None) else 0}")
        print(f"    error={getattr(result, 'error', None)}")

main()
