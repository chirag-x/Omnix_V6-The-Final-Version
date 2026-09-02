"""Probe what dispatcher returns for the compound case."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from main import build_engine

def main():
    cfg, eng = build_engine()
    pipeline = eng.pipeline
    disp = pipeline.app_dispatcher
    print(f"Dispatcher router={disp._router}")
    print(f"Dispatcher executor={disp._executor}")

    r = disp.try_dispatch("Open Notepad and type Hello World")
    if r is None:
        print("Result: None")
        return
    print(f"\nResult status={r.status}")
    print(f"Result capability={r.capability_name}")
    print(f"Result verification_status={r.verification.status if r.verification else 'None'}")
    print(f"Result verification_target={r.verification.target if r.verification else 'None'}")
    print(f"Result error={r.error}")
    print(f"Result details={r.details}")
    print(f"Result action_results={len(r.action_results or [])}")
    for ar in r.action_results or []:
        print(f"  action: status={ar.status} action_name={ar.action_name} details={ar.details} err={ar.error}")
    print(f"Result verification result data: {r.verification.result if r.verification else 'N/A'}")

main()