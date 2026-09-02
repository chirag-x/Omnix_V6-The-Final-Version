#!/usr/bin/env python
"""
Omnix V6 — Environment Validator (Phase 0.5)

Run from the V6 virtual environment:

    .\.venv\Scripts\python.exe scripts\validate_environment.py

Reports PASS / FAIL / OPTIONAL for every V6 subsystem dependency.
Exits 0 if every REQUIRED entry is PASS; non-zero otherwise.

This script is the single source of truth for "Phase 0.5 complete".
It does NOT install anything, does NOT modify the environment, and
does NOT import any code from core/ ai/ vision/ voice/ — it only
checks that the dependency stack resolves and behaves at import time.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    status: str          # "PASS" | "FAIL" | "OPTIONAL"
    detail: str = ""
    error: Optional[str] = field(default=None)


def _run(c: Check) -> Check:
    """Print a single result line."""
    if c.error:
        print(f"  [{c.status:8s}] {c.name:32s} {c.detail}    ({c.error})")
    else:
        print(f"  [{c.status:8s}] {c.name:32s} {c.detail}")
    return c


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


def check_python() -> Check:
    expected = (3, 13, 15)
    actual = sys.version_info[:3]
    ok = actual == expected
    return Check(
        name="Python",
        status="PASS" if ok else "FAIL",
        detail=f"{'.'.join(map(str, actual))} (expected {'.'.join(map(str, expected))})",
    )


def check_pip() -> Check:
    try:
        import pip
        ver = pip.__version__
        return Check("pip", "PASS", f"version {ver}")
    except Exception as e:  # pragma: no cover
        return Check("pip", "FAIL", error=str(e))


def check_import(name: str, *, required: bool = True) -> Check:
    """Try `import <name>`. Required deps FAIL if missing, optional return OPTIONAL."""
    try:
        mod = importlib.import_module(name)
        ver = getattr(mod, "__version__", "n/a")
        return Check(f"import {name}", "PASS", f"version {ver}")
    except Exception as e:
        status = "FAIL" if required else "OPTIONAL"
        return Check(f"import {name}", status, error=type(e).__name__)


def check_torch_cuda() -> List[Check]:
    out: List[Check] = []
    try:
        import torch
        out.append(Check("torch", "PASS", f"version {torch.__version__}"))
        cuda_avail = torch.cuda.is_available()
        if cuda_avail:
            name = torch.cuda.get_device_name(0)
            cap = torch.cuda.get_device_capability(0)
            out.append(Check("torch.cuda", "PASS", f"available; device 0 = {name} (cap {cap})"))
            # Lightweight GPU smoke test
            try:
                t = torch.zeros(2, 2, device="cuda")
                s = (t + 1).sum().item()
                assert s == 4.0
                out.append(Check("torch.cuda.smoke", "PASS", "2x2 zero+1 tensor sum=4.0"))
            except Exception as e:
                out.append(Check("torch.cuda.smoke", "FAIL", error=type(e).__name__ + ": " + str(e)))
        else:
            out.append(Check("torch.cuda", "OPTIONAL", "CUDA not available (CPU fallback)"))
    except Exception as e:
        out.append(Check("torch", "FAIL", error=str(e)))
    return out


def check_yolo_weights(path: Path) -> Check:
    if not path.exists():
        return Check("yolo11n.pt", "FAIL", error=f"missing: {path}")
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb < 4.0:
        return Check("yolo11n.pt", "FAIL", error=f"suspiciously small: {size_mb:.2f} MB")
    return Check("yolo11n.pt", "PASS", f"{size_mb:.2f} MB at {path}")


def check_yolo_load(weights: Path) -> Check:
    """Try to load the YOLO model via ultralytics (smoke test)."""
    try:
        from ultralytics import YOLO  # type: ignore
        model = YOLO(str(weights))
        # Don't actually run inference; loading the model is enough for Phase 0.5.
        return Check("yolo11n.pt.load", "PASS", f"ultralytics loaded model object {type(model).__name__}")
    except Exception as e:
        return Check("yolo11n.pt.load", "FAIL", error=type(e).__name__ + ": " + str(e))


def check_nvidia_smi() -> Check:
    if not shutil.which("nvidia-smi"):
        return Check("nvidia-smi", "OPTIONAL", "binary not on PATH (CPU-only host)")
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else "(no GPU)"
            return Check("nvidia-smi", "PASS", line)
        return Check("nvidia-smi", "FAIL", error=out.stderr.strip()[:120])
    except Exception as e:
        return Check("nvidia-smi", "OPTIONAL", error=type(e).__name__ + ": " + str(e))


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------


REQUIRED_IMPORTS = [
    # base.txt
    "loguru", "rich", "pydantic", "requests", "httpx", "psutil",
    "win32api", "comtypes", "dotenv",
]
OPTIONAL_IMPORTS = [
    # AI tier
    "torch", "torchvision", "torchaudio", "transformers", "sentence_transformers",
    "faiss", "chromadb", "openai", "onnxruntime", "numpy", "sklearn", "PIL",
    # Vision tier
    "ultralytics", "cv2", "easyocr",
    # Voice tier
    "faster_whisper", "speech_recognition", "edge_tts", "pygame", "openwakeword",
    "sounddevice", "mss",
    # Automation tier
    "pyautogui", "keyboard", "mouse",
    # Browser tier
    "selenium",
    # Dev tier
    "pytest",
]


def main() -> int:
    v6_root = Path(__file__).resolve().parent.parent
    print("=" * 72)
    print(f"Omnix V6 — Environment Validator")
    print(f"Project: {v6_root}")
    print(f"Python : {sys.executable}")
    print(f"Ver    : {sys.version}")
    print("=" * 72)

    results: List[Check] = []

    print("\n[core]")
    results.append(_run(check_python()))
    results.append(_run(check_pip()))
    for name in REQUIRED_IMPORTS:
        results.append(_run(check_import(name, required=True)))

    print("\n[ai tier]")
    for name in ["torch", "torchvision", "torchaudio", "transformers",
                 "sentence_transformers", "faiss", "chromadb", "openai",
                 "onnxruntime", "numpy", "sklearn", "PIL"]:
        results.append(_run(check_import(name, required=False)))
    for c in check_torch_cuda():
        results.append(_run(c))
    results.append(_run(check_nvidia_smi()))

    print("\n[vision tier]")
    for name in ["ultralytics", "cv2", "easyocr"]:
        results.append(_run(check_import(name, required=False)))
    yolo = v6_root / "vision" / "models" / "yolo11n.pt"
    results.append(_run(check_yolo_weights(yolo)))
    # Only attempt to load ultralytics if it's importable
    try:
        import ultralytics  # noqa: F401
        results.append(_run(check_yolo_load(yolo)))
    except ImportError:
        results.append(_run(Check("yolo11n.pt.load", "OPTIONAL",
                                   error="ultralytics not installed; defer load test")))

    print("\n[voice tier]")
    for name in ["faster_whisper", "speech_recognition", "edge_tts",
                 "pygame", "openwakeword", "sounddevice", "mss"]:
        results.append(_run(check_import(name, required=False)))

    print("\n[automation tier]")
    for name in ["pyautogui", "keyboard", "mouse"]:
        results.append(_run(check_import(name, required=False)))

    print("\n[browser tier]")
    results.append(_run(check_import("selenium", required=False)))

    print("\n[dev tier]")
    results.append(_run(check_import("pytest", required=False)))

    # Summary
    print("\n" + "=" * 72)
    fail = sum(1 for c in results if c.status == "FAIL")
    opt = sum(1 for c in results if c.status == "OPTIONAL")
    pas = sum(1 for c in results if c.status == "PASS")
    print(f"Summary: PASS={pas}  OPTIONAL={opt}  FAIL={fail}")
    if fail:
        print("Failed checks:")
        for c in results:
            if c.status == "FAIL":
                print(f"  - {c.name}: {c.error or c.detail}")
    print("=" * 72)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
