# V6 Dependency Plan

**Phase:** 0.5 — Environment, Dependency & AI/Model Foundation
**Status:** ✅ COMPLETE (validated 2026-08-29)
**Date:** 2026-08-29
**Target interpreter:** Python 3.13.15 (locked; verified at `E:\Program Files\python.exe`)
**Current V6 venv:** `.venv` provisioned with 3.13.15; all tiered requirement files installed
**Validator output:** `44 PASS / 0 OPTIONAL / 0 FAIL` (see `scripts/validate_environment.py`)
**pip check:** `No broken requirements found.`

---

## 0. Phase 0.5 install status (2026-08-29)

| Tier | File | Status | Notes |
|---|---|---|---|
| Core runtime | `requirements/base.txt` | **DONE** | 33 packages installed (see `temp/install_base.log`) |
| AI / LLM | `requirements/ai.txt` | **DONE** | Resumed after torch 2.75 GB wheel interrupted the previous session; clean reinstall fixed the partial `c10_cuda.dll` load. See `temp/install_ai_resume.log`, `temp/install_torch_clean.log`. |
| Vision | `requirements/vision.txt` | **DONE** | ultralytics 8.4.21 + easyocr 1.7.2 + opencv-python 4.13.0.92 (see `temp/install_vision.log`) |
| Voice | `requirements/voice.txt` | **DONE** | Installed as part of `ai.txt` transitive set (faster-whisper, openwakeword, edge-tts, etc.) |
| Automation | `requirements/automation.txt` | **DONE** | Installed as part of `ai.txt` transitive set (PyAutoGUI, keyboard, mouse) |
| Browser | `requirements/browser.txt` | **DONE** | Installed as part of `ai.txt` transitive set (selenium) |
| Dev / testing | `requirements/dev.txt` | **DONE** | pytest 9.1.1 + pytest-asyncio 1.4.0 (bumped from 0.24.0 — was incompatible with pytest 9); see `temp/install_dev.log` |

`scripts/validate_environment.py` is the single source of truth for "Phase 0.5 complete". Run it from the V6 venv:

```powershell
.\.venv\Scripts\python.exe scripts\validate_environment.py
```

---

## 1. Scope

This document is the **dependency plan** for Omnix V6. It enumerates every package V6 needs, identifies the V5 pin as the starting point, and flags Python 3.13 compatibility risks.

**No package will be installed during Phase 0.** Installation begins only after Phase 0 approval, in Phase 0.5.

---

## 2. Why Python 3.13.15 (not 3.11.9)

V5 ran on Python 3.11.9. V6 is locked to 3.13.15 because:

1. The V6 venv is already provisioned with 3.13.15.
2. The user's "My Goal for Omnix" emphasizes long-term growth toward an autonomous agent; 3.13 has the better JIT, error messages, and `typing` features.
3. As of 2026-08-29, the major ML/automation stack (torch 2.11, transformers 5.x, ultralytics 8.4) is 3.13-compatible.

**Risk:** Some V5 pins (e.g. `pydantic 2.12`, `transformers 5.3`) target the bleeding edge. Their 3.13 wheels may differ in ABI from 3.11. Phase 0.5 must verify each wheel on 3.13.15 before declaring "compatible."

---

## 3. Tiered dependency list

### 3.1 Core runtime

| Package | V5 pin | V6 target | Notes |
|---|---|---|---|
| `loguru` | 0.7.3 | 0.7.3 | Logging. Stable. |
| `pydantic` | 2.12.5 | 2.12.5 | Data validation. **Verify 3.13 wheel.** |
| `pydantic_core` | 2.41.5 | 2.41.5 | Pydantic's Rust core. **Verify 3.13 wheel.** |
| `rich` | 14.3.3 | 14.3.3 | Console formatting. Stable. |
| `requests` | 2.32.5 | 2.32.5 | HTTP. Stable. |
| `httpx` | 0.28.1 | 0.28.1 | Async HTTP. Stable. |
| `psutil` | 7.2.2 | 7.2.2 | Process info. **Verify 3.13 wheel.** |
| `pywin32` | 311 | 311 | Windows API. Stable. |
| `comtypes` | 1.4.16 | 1.4.16 | COM. Stable. |
| `pygetwindow` | 0.0.9 | 0.0.9 | Window listing. Stable. |
| `pathlib2` | (transitive) | **2.3.7 (YANKED)** | Installed in Phase 0.5 base; pin is yanked on PyPI. Functional but unmaintained. Cleanup in a future pass. |

### 3.2 ML / Vision

| Package | V5 pin | V6 target | Notes |
|---|---|---|---|
| `torch` | 2.11.0+cu128 | 2.11.0+cu128 | **Verify 3.13 + cu128 wheel exists.** |
| `torchaudio` | 2.11.0+cu128 | 2.11.0+cu128 | Same. |
| `torchvision` | 0.26.0+cu128 | 0.26.0+cu128 | Same. |
| `ultralytics` | 8.4.21 | 8.4.21 | YOLO. **Verify 3.13 wheel.** |
| `ultralytics-thop` | 2.0.18 | 2.0.18 | |
| `opencv-python` | 4.13.0.92 | 4.13.0.92 | |
| `opencv-python-headless` | 5.0.0.93 | 5.0.0.93 | |
| `onnxruntime` | 1.24.3 | 1.24.3 | **Verify 3.13 wheel.** |
| `faiss-cpu` | 1.13.2 | 1.13.2 | **Verify 3.13 wheel.** |
| `sentence-transformers` | 5.2.3 | 5.2.3 | **Verify 3.13 wheel.** |
| `transformers` | 5.3.0 | 5.3.0 | **Verify 3.13 wheel.** |
| `huggingface_hub` | 1.6.0 | 1.6.0 | |
| `numpy` | 2.4.2 | 2.4.2 | |
| `scikit-learn` | 1.8.0 | 1.8.0 | **Verify 3.13 wheel.** |
| `Pillow` | (transitive) | as needed | |

### 3.3 Voice

| Package | V5 pin | V6 target | Notes |
|---|---|---|---|
| `faster-whisper` | 1.2.1 | 1.2.1 | **Verify 3.13 wheel.** |
| `easyocr` | 1.7.2 | 1.7.2 | **Verify 3.13 wheel.** |
| `edge-tts` | 7.2.7 | 7.2.7 | |
| `SpeechRecognition` | 3.14.5 | 3.14.5 | |
| `openwakeword` | 0.6.0 | 0.6.0 | **Verify 3.13 wheel.** |
| `pygame` | 2.6.1 | 2.6.1 | |
| `sounddevice` | 0.5.5 | 0.5.5 | |
| `mss` | 10.1.0 | 10.1.0 | |

### 3.4 Automation / Input

| Package | V5 pin | V6 target | Notes |
|---|---|---|---|
| `PyAutoGUI` | 0.9.54 | 0.9.54 | |
| `keyboard` | 0.13.5 | 0.13.5 | |
| `mouse` | 0.7.1 | 0.7.1 | |
| `selenium` | 4.41.0 | 4.41.0 | **Verify 3.13 wheel.** |

### 3.5 AI

| Package | V5 pin | V6 target | Notes |
|---|---|---|---|
| `openai` | 2.26.0 | 2.26.0 | **Verify 3.13 wheel.** |
| `chromadb` | 1.5.2 | 1.5.2 | **Verify 3.13 wheel.** |

### 3.6 UI

| Package | V5 pin | V6 target | Notes |
|---|---|---|---|
| `PyQt6` | 6.11.0 | 6.11.0 | **Verify 3.13 wheel.** |
| `PyQt6-Qt6` | 6.11.1 | 6.11.1 | |

### 3.7 Testing

| Package | V5 pin | V6 target | Notes |
|---|---|---|---|
| `pytest` | 9.1.1 | 9.1.1 | **Verify 3.13 wheel.** |
| `pytest-asyncio` | (new) | 0.24.x | Add in Phase 0.5 — needed for async adapter tests. |
| `pytest-mock` | (new) | 3.14.x | Add in Phase 0.5 — cleaner mocking than V5's manual Mocks. |

---

## 4. Python 3.13 compatibility — risk register

| Package | Risk | Reason | Mitigation |
|---|---|---|---|
| `torch 2.11.0+cu128` | **High** | Big torch wheels can lag Python releases. | Check `https://download.pytorch.org/whl/cu128/torch/` for `cp313` tag. If absent, fall back to `torch==2.11.0+cu128` source build or use 2.10 if Phase 0.5 deems acceptable. |
| `pydantic 2.12.5` | Medium | Rust core (`pydantic_core`) ABI may differ. | Pin `pydantic_core` to a known-good build for 3.13. |
| `ultralytics 8.4.21` | Medium | Depends on torch + opencv + numpy ABI. | Run smoke test after install. |
| `transformers 5.3.0` | Low | Pure Python + torch. | Install with `--no-deps`, then add deps manually if conflict. |
| `sentence-transformers 5.2.3` | Low | Pure Python + torch. | Same. |
| `onnxruntime 1.24.3` | Medium | C++ ABI. | Verify `cp313` wheel exists; otherwise fall back to 1.23. |
| `faster-whisper 1.2.1` | Low | CTranslate2 binary. | Check `ctranslate2` 3.13 wheel. |
| `easyocr 1.7.2` | Medium | Torch + opencv + Pillow. | Smoke test. |
| `chromadb 1.5.2` | Medium | Rust + pydantic. | Verify 3.13 wheel. |
| `PyAutoGUI 0.9.54` | Low | Pure Python over pywin32. | Safe. |
| `PyQt6 6.11.0` | Low | Wheels for 3.13 standard. | Safe. |
| `selenium 4.41.0` | Low | Pure Python. | Safe. |
| `pytest 9.1.1` | Low | Pure Python. | Safe. |
| `faiss-cpu 1.13.2` | Medium | C++ ABI. | Verify 3.13 wheel. |
| `scikit-learn 1.8.0` | Low | Wheels for 3.13 standard. | Safe. |
| `numpy 2.4.2` | Low | ABI-stable. | Safe. |

---

## 5. CUDA

V5 pinned CUDA 12.8 (`+cu128`). V6 will **stay on cu128** for the GPU build. The V6 venv must be provisioned on a Windows 11 host with a CUDA 12.8-compatible NVIDIA driver.

**Pre-flight (Phase 0.5):**

```powershell
nvidia-smi                       # Confirm driver ≥ 535
python -c "import torch; print(torch.cuda.is_available())"   # Confirm CUDA init
```

If `torch.cuda.is_available()` returns `False` after install, the audio/video subsystems must degrade gracefully — `loguru.warning` and switch to CPU. **No silent fallback** for action success, but degradation for **inference** is allowed (clearly logged).

---

## 6. What gets installed when

| Phase | Action | Why |
|---|---|---|
| Phase 0 (now) | None. | Audit only. |
| Phase 0.5 | Install **core runtime** (3.1) only. | Get `OmnixEngine` importable and `main.py` bootable to "OMNIX V6 IS READY" without crashing. No ML, no voice, no vision, no automation. |
| Phase 1 | Install **ML / Vision** (3.2). | First real feature surface. |
| Phase 2 | Install **Voice** (3.3). | "Hey Omnix" wake word. |
| Phase 3 | Install **Automation / Input** (3.4) + **AI** (3.5). | Can act on the desktop. |
| Phase 4 | Install **UI** (3.6). | First user-visible panels. (If `frozen/` UI is not revived, install only for debug tools.) |
| Phase 5 | Install **Testing** (3.7). | pytest + asyncio + mock. |

This phased install reduces Phase 0.5's blast radius and gives a runnable engine early.

---

## 7. V5 vs V6 requirements diff

The V5 file was UTF-16 LE with 217 lines / 200+ packages. The V6 file is to be created in Phase 0.5 as UTF-8 with **the same package set**, **same pins**, **new line endings (LF or CRLF consistently)**, and **alphabetized within section**.

Section order:

1. `# Omnix V6 — Pinned dependencies`
2. Core runtime
3. ML / Vision
4. Voice
5. Automation / Input
6. AI
7. UI
8. Testing
9. `# Phase 0.5 boot set` (curated subset of core runtime only)

---

## 8. .env

V5 `.env` (4 OpenRouter keys + 1 Groq key) is migrated as-is. **Security todo (R12):**

- Move keys to `keyring` or Windows Credential Manager.
- Generate `.env.example` with placeholder values.
- Add `.env` to `.gitignore` (Phase 0.5 to verify it is not committed).

---

## 9. Phase 0 sign-off

- [x] No package installed.
- [x] No package uninstalled.
- [x] No venv modified.
- [x] No `requirements.txt` created in V6 yet.
- [x] Plan is on disk for Phase 0.5 to execute.

**PHASE 0 COMPLETE — NO SOURCE CODE MODIFIED. WAITING FOR APPROVAL TO BEGIN PHASE 0.5.**

---

## 10. Phase 0.5 sign-off (target)

- [x] `base.txt` installed (`temp/install_base.log` shows SUCCESS)
- [x] `ai.txt` installed (`temp/install_ai_resume.log` + `temp/install_torch_clean.log` show SUCCESS)
- [x] `vision.txt` installed (`temp/install_vision.log` shows SUCCESS)
- [x] `voice.txt` installed (transitive via `ai.txt` set; runtime libs present)
- [x] `automation.txt` installed (transitive via `ai.txt` set; runtime libs present)
- [x] `browser.txt` installed (transitive via `ai.txt` set; runtime libs present)
- [x] `dev.txt` installed (`temp/install_dev.log` shows SUCCESS)
- [x] `scripts/validate_environment.py` reports **44 PASS / 0 OPTIONAL / 0 FAIL**
- [x] `python -c "import torch; print(torch.cuda.is_available())"` returns `True` on this RTX 5060 host
- [x] `torch.cuda.get_device_name(0)` = `NVIDIA GeForce RTX 5060 Laptop GPU` (capability `(12, 0)`)
- [x] YOLO smoke-test on `vision/models/yolo11n.pt` passes (load + GPU inference on synthetic image)
- [x] `pip check` reports `No broken requirements found.`
- [x] No V5 source migrated
- [x] Final report issued

### Issue encountered & resolved during this session

- **Symptom:** `import torch` failed with `OSError: [WinError 127] The specified procedure could not be found. Error loading "torch\lib\c10_cuda.dll" or one of its dependencies.`
- **Cause:** Partial install of the 2.75 GB `torch==2.11.0+cu128` wheel in the previous session left the `torch/lib/` directory in a broken state (DLLs present but the dependency graph in the wheel was incomplete).
- **Fix:** `pip uninstall -y torch torchvision torchaudio` followed by a clean `pip install --extra-index-url https://download.pytorch.org/whl/cu128 torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0`. The clean reinstall landed a complete set of CUDA 12.8 runtime DLLs; `import torch` then succeeds, `torch.cuda.is_available()` returns `True`, and the 2×2 zero+1 GPU smoke test sums to 4.0.
- **Why no other strategy:** No PATH conflict (no other `c10_cuda.dll` on the system); VC++ Redistributable was already installed; the bundled CUDA 12.8 runtime is correct for the driver (596.49 / CUDA 13.2). The DLL itself was not corrupt; the install was simply incomplete.
- **Documentation:** full transcript in `temp/install_torch_clean.log`.

### Pin adjustments during this session

- `requirements/dev.txt`: `pytest-asyncio==0.24.0` → `pytest-asyncio==1.4.0` (the 0.24.x line caps pytest at <9; 1.4.0 supports `pytest<10,>=8.4`, which is the only line compatible with the locked `pytest==9.1.1`).
