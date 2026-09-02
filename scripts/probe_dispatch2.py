"""Probe FastPathDispatcher end-to-end - check what status comes back."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from main import build_engine
from core.results import CapabilityStatus

def main():
    cfg, eng = build_engine()
    pipeline = eng.pipeline
    disp = pipeline.app_dispatcher
    print(f"Dispatcher: {disp}")
    print(f"Router: {disp._router}")
    print(f"Executor: {disp._executor}")

    queries = [
        "Open Notepad",
        "Open Notepad and type Hello World",
        "Open Notepad, type Hello from Omnix, and save it as omnix_test.txt",
    ]
    for q in queries:
        print(f"\n{'='*60}\nQuery: {q!r}\n{'='*60}")
        r = disp.try_dispatch(q)
        print(f"Result type: {type(r).__name__}")
        if r is None:
            print("  -> None (would fall through to Brain)")
            continue
        print(f"  status: {r.status}")
        print(f"  capability: {r.capability_name}")
        print(f"  attempted: {r.attempted}, executed: {r.executed}, verified: {r.verified}, failed: {r.failed}")
        print(f"  details: {dict(r.details or {})}")
        if r.error:
            print(f"  error: {r.error}")

main()
