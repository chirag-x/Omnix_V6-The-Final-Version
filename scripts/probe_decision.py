"""Probe compound classification via FastPathDispatcher."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from main import build_engine
from core.services.app_dispatcher import FastPathDispatcher

def main():
    cfg, eng = build_engine()
    # The engine has pipeline; pipeline has app_dispatcher
    pipeline = eng.pipeline
    print("Pipeline attrs containing 'dispatch':", [a for a in dir(pipeline) if 'disp' in a.lower()])
    disp = getattr(pipeline, "app_dispatcher", None) or getattr(pipeline, "_app_dispatcher", None)
    print(f"Dispatcher: {disp}")
    if disp:
        # Inspect the engine inside it
        inner = disp._ensure_engine()
        print(f"Inner engine: {inner}")
        queries = [
            "Open Notepad and type Hello World",
            "open notepad and type hello world",
            "Open Notepad",
            "type Hello World",
            "Type hello",
        ]
        for q in queries:
            d = inner.classify(q)
            print(f"\n{q!r}")
            print(f"  matched={d.matched}")
            print(f"  not_found={d.not_found}")
            print(f"  matched_text={d.matched_text!r}")
            if d.plan:
                for s in d.plan.steps:
                    print(f"    step {s.step_id}: cap={s.capability_name!r} params={dict(s.parameters)}")
            if d.metadata:
                print(f"  metadata={d.metadata}")

main()