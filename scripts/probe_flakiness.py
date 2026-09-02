"""Probe what happens on repeated Type hello."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from main import build_engine

def main():
    cfg, eng = build_engine()
    for i in range(5):
        try:
            r = eng.process("Type hello")
            print(f"  attempt {i}: status={r.status} text={r.text!r} error={r.error!r}")
        except Exception as e:
            print(f"  attempt {i}: EXCEPTION {e!r}")
        # Force small delay
        import time
        time.sleep(0.3)

main()