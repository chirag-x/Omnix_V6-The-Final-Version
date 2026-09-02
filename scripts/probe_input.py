"""Probe typing via engine."""
import sys
sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

from main import build_engine
from core.capability_router import CapabilityRouter
from core.capability_registry import CapabilityRegistry
import pyautogui

def main():
    cfg, eng = build_engine()
    pipeline = eng.pipeline
    disp = pipeline.app_dispatcher
    router = disp._router

    r = router.route("desktop.keyboard.type", {"text": "hello"})
    print(f"Type hello result: status={r.status} err={r.error}")
    print(f"  action.status={r.action.status if r.action else None}")
    print(f"  action.details={r.action.details if r.action else None}")
    print(f"  Pos before: {pyautogui.position()}")
    print(f"  FAILSAFE: {pyautogui.FAILSAFE}")

main()