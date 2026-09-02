# Omnix V6 — Dependency Specification

**Phase 0.5.** Verified against:
- Python 3.13.15 (locked)
- Windows 11 (10.0.26200), 64-bit AMD64
- NVIDIA GeForce RTX 5060 Laptop, 8 GB VRAM
- Driver 596.49 / CUDA Version 13.2 (max capability)
- Venv at `E:\Coding\Omnix\Omnix_V6- The final version\.venv`

## Tiered install (recommended)

```powershell
# Activate the venv first.
.\.venv\Scripts\Activate.ps1

# Phase 0.5 (foundation; engine must boot after this):
pip install -r requirements/base.txt

# Phase 1+ (AI / LLM / embeddings / vector store):
pip install -r requirements/ai.txt

# Phase 1+ (vision: YOLO + OCR):
pip install -r requirements/vision.txt

# Phase 2 (voice: STT + TTS + wake word):
pip install -r requirements/voice.txt

# Phase 3 (Windows automation / input simulation):
pip install -r requirements/automation.txt

# Phase 3 (browser automation via Selenium):
pip install -r requirements/browser.txt

# Phase 5 (dev / testing):
pip install -r requirements/dev.txt
```

## What is NOT in this directory

- **No** `requirements.txt` (single monolithic file) — V6 uses a tiered layout
  so each phase can install exactly the foundation it needs without pulling
  the rest.
- **No** `frozen/` UI dependencies (`PyQt6`, `PyQt6-Qt6`) — V6 has no UI shell
  in scope for the foundation phases. Add a `requirements/ui.txt` only if
  Phase 4 explicitly revives a UI.

## Phase 0.5 boot set

Only `base.txt` is required to satisfy Phase 0.5's "engine importable +
main.py boots to ready" goal. All other tiers are forward-looking.

## Reproducibility

- All pins are absolute (`==`).
- `ai.txt` uses `--extra-index-url https://download.pytorch.org/whl/cu128`
  for the GPU torch wheels; CPU-only installs can drop the index URL and
  use `pip install torch==2.11.0` to get the cpu build.
- Wheels are resolved on Python 3.13.15, Windows AMD64. Other platforms
  will not match the URL pins; that is intentional.
