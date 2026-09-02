# V6 Model Plan

**Phase:** 0.5 — Foundation (validated)
**Status:** Phase 0.5 model validation complete
**Date:** 2026-08-29

---

## 1. Purpose

This document enumerates every AI model Omnix V6 needs, where each model lives, and how it is loaded, versioned, and replaced. No model is downloaded during Phase 0; downloads are scheduled in `V6_PHASE_ROADMAP.md`.

---

## 2. Model inventory

### 2.1 Local (on-device) models

| Model | Where used | Source | License | Size (V5) | V6 status |
|---|---|---|---|---|---|
| **YOLOv11-nano** (`yolo11n.pt`) | `vision/` (UI element detection, screen understanding) | Ultralytics | AGPL-3.0 | 5.6 MB | **PRESENT + VALIDATED** — `vision/models/yolo11n.pt` (5,613,764 bytes). Ultralytics 8.4.21 loads it; GPU inference smoke test passed on RTX 5060. |
| **faster-whisper** (small/medium) | `voice/speech_recognizer.py` | CTranslate2 + Whisper | MIT | ~460 MB (small) | **Re-download on first run.** V5 cached it under `~/.cache/huggingface/`. |
| **easyocr** models (craft + english_g2) | `vision/text_detector.py` | Jaided AI | Apache-2.0 | ~100 MB | **Re-download on first run.** V5 cached it under `~/.EasyOCR/model/`. |
| **sentence-transformers** (`all-MiniLM-L6-v2` or similar) | `memory/memory_manager.py` (semantic recall) | HuggingFace | Apache-2.0 | ~80 MB | **Re-download on first run.** |
| **openwakeword** models (Hey Omnix, alexa, etc.) | `voice/wake_listener.py` | dsacorp | Apache-2.0 | ~50 MB total | **Re-download on first run.** |
| **chromadb** embeddings (uses sentence-transformers above) | `memory/memory_manager.py` | n/a | Apache-2.0 | n/a | Same as sentence-transformers. |
| **onnxruntime** (various UI detection models) | `vision/ui_detector.py` | mixed | mixed | ~10–50 MB each | **Re-download on first run.** |

### 2.2 Remote (cloud) models

| Model | Where used | Provider | Purpose | V5 env var |
|---|---|---|---|---|
| **OpenRouter LLMs** (mix of free + paid) | `ai/brain_manager.py` | OpenRouter (proxy to OpenAI, Anthropic, Meta, Mistral) | Planning, reasoning, conversation | `OPENROUTER_URL` + 4 keys in `.env` |
| **Groq LLM** (`llama-3.3-70b-versatile`) | `ai/brain_manager.py` (fallback) | Groq | Fast inference for simple tasks | `GROQ_API_KEY`, `GROQ_MODEL_NAME` |
| **OpenAI TTS** (used in tests, not production) | `voice/tts_engine.py` (optional) | OpenAI | High-quality TTS | not in V5 `.env` |

### 2.3 Local LLMs (future, not in V5)

V5 did not run a local LLM. V6 **may** add one in Phase 7+ (e.g. via `llama-cpp-python` or `ollama`). For Phase 0, the plan is "remote-first, local-fallback, never both at once."

---

## 3. Where models live in V6

### 3.1 On disk

```
Omnix_V6/
├── vision/
│   └── models/
│       ├── yolo11n.pt                # Phase 0.5 download
│       └── ui_detection/             # Phase 1 download
│           ├── address_bar.onnx
│           └── generic_element.onnx
│
~/.cache/huggingface/                  # Standard HF cache (faster-whisper, sentence-transformers, easyocr)
~/.EasyOCR/model/                      # Standard easyocr cache
~/.cache/openwakeword/                 # Standard openwakeword cache
```

### 3.2 In code

| Model | Loaded by | Cached at |
|---|---|---|
| `yolo11n.pt` | `vision/vision_manager.py` | `vision/models/yolo11n.pt` |
| faster-whisper | `voice/speech_recognizer.py` | `~/.cache/huggingface/` |
| easyocr | `vision/text_detector.py` | `~/.EasyOCR/model/` |
| sentence-transformers | `memory/memory_manager.py` | `~/.cache/huggingface/` |
| openwakeword | `voice/wake_listener.py` | `~/.cache/openwakeword/` |
| Remote LLMs | `ai/brain_manager.py` | n/a (HTTP) |

---

## 4. Model loading policy (V6)

### 4.1 Lazy load

**Rule:** Models are loaded **on first use**, not at engine boot. The engine boots to "OMNIX V6 IS READY" in <5 seconds; model loading happens when a subsystem first needs the model.

**Why:** Avoids boot-time hangs. The user can hear the wake word and speak a command; the model is ready by then.

**Enforcement:** Subsystem `initialize()` registers a *factory* (callable), not a loaded model. The factory is called on first use.

### 4.2 Version pinning

**Rule:** Every model is pinned by **hash or version string** in the loading code:

```python
# vision/vision_manager.py
YOLO_MODEL_PATH = "vision/models/yolo11n.pt"
YOLO_MODEL_VERSION = "yolo11n-2025-09-01"  # bumped in code, not by re-download
```

**Why:** A model upgrade is a code change, not an env-var flip.

**Enforcement:** Code review; the `*_MODEL_VERSION` constant is updated alongside the model.

### 4.3 Graceful degradation

**Rule:** If a model fails to load (missing weights, version mismatch), the subsystem logs a `loguru.error` and:

- **For YOLO:** `vision_service.locate(...)` returns `VisionResult(success=False, error="YOLO unavailable")`. UI click-via-vision is disabled but text-only screen understanding still works.
- **For faster-whisper:** Voice input is disabled; text input via API/CLI still works.
- **For sentence-transformers:** Semantic memory is disabled; keyword-only recall still works.
- **For openwakeword:** Voice wake is disabled; CLI wake (`Hey Omnix` typed) still works.

**Why:** Partial functionality > no functionality. The user is informed (`Error: <subsystem>: <reason>`) but not blocked.

**Enforcement:** Each subsystem's `initialize()` returns `bool`; a `False` is logged but does not block engine boot.

### 4.4 No silent fallback for actions

**Re-statement of R-8 in `V6_ARCHITECTURE_RULES.md`:** Model degradation is allowed (with explicit log). Action success degradation is not.

---

## 5. AI provider policy (V6)

### 5.1 Provider abstraction

**Rule:** `ai/brain_manager.py` is the only authorized entry point. It exposes:

```python
class BrainManager:
    def ask(self, prompt: str, *, system: Optional[str] = None, **kwargs) -> str
    async def aask(self, prompt: str, *, system: Optional[str] = None, **kwargs) -> str
    def classify(self, text: str, *, options: List[str]) -> str
    def embed(self, text: str) -> List[float]
    def health(self) -> Dict[str, Any]
```

The Brain internally picks the provider (OpenRouter vs Groq vs local) based on the task and current health.

### 5.2 Provider selection (V6 Phase 0.5+)

| Task | Primary | Fallback |
|---|---|---|
| Conversation (chat) | OpenRouter (`gpt-4o-mini` or free equivalent) | Groq (`llama-3.3-70b-versatile`) |
| Planning (multi-step) | OpenRouter (`gpt-4o` or equivalent) | Groq |
| Intent classification (fast) | Groq (fast) | Local regex + open-source |
| Embedding | Local sentence-transformers | n/a (no cloud embed) |
| Fallback to local LLM | `ollama`/`llama-cpp` (Phase 7+) | n/a |

### 5.3 Key management

**Rule:** API keys are read from environment variables only. The V5 `.env` is loaded via `python-dotenv` in `main.py` (Phase 0.5 to add). V6 may move keys to `keyring` (Windows Credential Manager) — see R-12 in `V5_V6_MIGRATION_AUDIT.md`.

**Enforcement:** `grep -r "sk-" core/ ai/ skills/ system/ vision/ voice/ memory/ automation/ context/ utils/` returns no matches.

---

## 6. Model size budget

V5's on-disk footprint (rough):

| Category | Size |
|---|---|
| YOLO + UI detection | ~10 MB |
| faster-whisper (small) | ~460 MB |
| easyocr (English) | ~100 MB |
| sentence-transformers (MiniLM) | ~80 MB |
| openwakeword (full) | ~50 MB |
| **Total local models** | **~700 MB** |

V6 budget is the same. Phase 0.5 downloads **only** YOLO (`yolo11n.pt`, 5.6 MB) to keep Phase 0.5 fast. The rest downloads on first use.

---

## 7. What is downloaded when

| Phase | Download | Reason |
|---|---|---|
| Phase 0.5 | `yolo11n.pt` (5.6 MB) | Required for `vision/models/yolo11n.pt` to be a real file (not 0 bytes). |
| Phase 1 | faster-whisper (small) on first voice use | Lazy load. |
| Phase 1 | easyocr on first OCR use | Lazy load. |
| Phase 1 | sentence-transformers on first memory use | Lazy load. |
| Phase 2 | openwakeword on first voice use | Lazy load. |
| Phase 7+ | Local LLM weights (if adopted) | TBD. |

---

## 8. Anti-patterns (model-related)

| Anti-pattern | Why forbidden |
|---|---|
| Hardcoding a model version in a skill | Skills must use the Brain abstraction. |
| Calling `openai.ChatCompletion.create(...)` from a skill | Violates R-12. |
| Loading a model in `__init__` | Violates 4.1 lazy load. |
| Mutating a model file at runtime | Models are immutable. |
| Storing model outputs in semantic memory without dedup | Memory bloat. |
| Re-embedding the same text on every call | Wasteful; cache embeddings. |

---

## 9. Phase 0 sign-off

- [x] No model downloaded.
- [x] No model path modified.
- [x] Plan is on disk for Phase 0.5+.

**PHASE 0 COMPLETE — NO SOURCE CODE MODIFIED. WAITING FOR APPROVAL TO BEGIN PHASE 0.5.**

---

## 10. Phase 0.5 model validation (2026-08-29)

Validated during the resumed Phase 0.5 session:

| Model | On disk | Size | Loader | Status | Notes |
|---|---|---|---|---|---|
| `yolo11n.pt` (YOLOv11-nano) | `vision/models/yolo11n.pt` | 5,613,764 bytes (5.35 MB) | `ultralytics.YOLO` (8.4.21) | **PASS** | Loads cleanly; GPU inference on a 640×640 synthetic frame returned 0 boxes (expected — no COCO class). No download needed; file was already on disk from V5. |
| faster-whisper (small) | not on disk | n/a | `faster_whisper.WhisperModel` (1.2.1) | **DEFER (LAZY)** | Runtime is installed; the model weights download on first `WhisperModel(...)` call to `~/.cache/huggingface/`. |
| easyocr (craft + english_g2) | not on disk | n/a | `easyocr.Reader` (1.7.2) | **DEFER (LAZY)** | Runtime is installed; weights download on first `Reader(['en'])` to `~/.EasyOCR/model/`. |
| sentence-transformers (e.g. all-MiniLM-L6-v2) | not on disk | n/a | `sentence_transformers.SentenceTransformer` (5.2.3) | **DEFER (LAZY)** | Runtime is installed; weights download on first load to `~/.cache/huggingface/`. |
| openwakeword | not on disk | n/a | `openwakeword.Model` (0.6.0) | **DEFER (LAZY)** | Runtime is installed; weights download on first use to `~/.cache/openwakeword/`. |
| ONNX (UI detection) | not on disk | n/a | `onnxruntime.InferenceSession` (1.24.3) | **DEFER** | Runtime is installed; no specific ONNX model is required by Phase 0.5. |
| Remote LLMs (OpenRouter / Groq) | n/a | n/a | `openai.OpenAI` (2.26.0) | **READY** | The OpenAI SDK with the configured `OPENROUTER_URL` / `GROQ_API_KEY` env vars is sufficient; the SDK itself does not bundle model weights. |

**Decision:** No additional models are downloaded in Phase 0.5. Lazy-load semantics (4.1) are preserved — Phase 1+ will trigger each download on first real use, and the cache directories are already configured to the standard locations.

**PHASE 0.5 MODEL VALIDATION COMPLETE.**
