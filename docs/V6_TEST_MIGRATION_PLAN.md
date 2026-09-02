# V6 Test Migration Plan

**Phase:** 0 — Forensic Audit
**Status:** Complete
**Date:** 2026-08-29

---

## 1. Purpose

V5 had 9 test files; V6 has none. This document plans the migration of every V5 test to V6, with the standardizations mandated by `V6_ARCHITECTURE_RULES.md` R-19 ("Tests are pytest, not scripts").

---

## 2. V5 test inventory

| # | File | Lines (V5) | Phase | Style | Real-Windows? | Mocks? | Coverage |
|---|---|---|---|---|---|---|---|
| 1 | `test_connected_loop.py` | ~170 | 10 | pytest | No | MockBrain, MockMemory, MockScreen, MockAutomation | Engine wiring + multi-step agent loop |
| 2 | `test_phase16_uia_smoke.py` | ~125 | 16 | `main()` script | Yes (foreground) | none | UIA enumeration; Chrome address bar |
| 3 | `test_pipeline.py` | ~70 | — | module-level script | No | FakeBrain | Intent → command processor → task planner |
| 4 | `test_real_execution.py` | ~540 | 17 | `main()` script | Yes (psutil) | none | 5 real commands; Notepad clipboard |
| 5 | `test_real_loop.py` | ~100 | 11 | `main()` script | Mixed | none | Closed-loop trace; replan |
| 6 | `test_routing_reliability.py` | ~270 | 12 | `main()` script | No | none | 7 routing cases; "open SkynetTerminal9000" |
| 7 | `test_ui_compound_reliability.py` | ~455 | 15 | `main()` script | No | none | 5 UI compound cases |
| 8 | `test_verification_recovery.py` | ~860 | 11 | pytest | No | StubStep, StubGoal, StubRecovery, FakeSkill | Verifier/Recovery semantics; replan budget |
| 9 | `test_vision_action_reliability.py` | ~830 | 16b | `main()` script | Yes (window mgr) | none | 5 vision+click cases; 8-state ladder |

**Total V5 test surface:** ~3,420 lines.

---

## 3. V6 test target

### 3.1 Standardization (R-19)

Every V6 test is pytest-discoverable:

```python
def test_<thing>():
    ...

def test_<other>(<fixture>):
    ...

class TestX:
    def test_<case_1>(self):
        ...
    def test_<case_2>(self):
        ...
```

The `if __name__ == "__main__":` block is preserved **only** for "manual entry" tests that need foreground real-Windows access. These tests are guarded by `pytest.mark.real_windows` and skipped in CI:

```python
import pytest

@pytest.mark.real_windows
def test_<name>():
    ...
    if __name__ == "__main__":
        main()  # pragma: no cover
```

### 3.2 Directory layout

```
Omnix_V6/
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # NEW: shared fixtures (engine, mocks, env)
│   ├── helpers/
│   │   ├── __init__.py
│   │   ├── mocks.py                # NEW: MockBrain, MockMemory, etc.
│   │   ├── real_windows.py         # NEW: gated helpers
│   │   └── assertions.py           # NEW: shared assertion helpers
│   ├── test_connected_loop.py      # Phase 10
│   ├── test_phase16_uia_smoke.py   # Phase 16 (real-windows marked)
│   ├── test_pipeline.py            # Pipeline
│   ├── test_real_execution.py      # Phase 17 (real-windows marked)
│   ├── test_real_loop.py           # Phase 11
│   ├── test_routing_reliability.py # Phase 12
│   ├── test_ui_compound_reliability.py # Phase 15
│   ├── test_verification_recovery.py  # Phase 11
│   └── test_vision_action_reliability.py # Phase 16b (real-windows marked)
│
├── pytest.ini                       # NEW: configuration
└── pyproject.toml                   # NEW: tool config (pytest, ruff, mypy)
```

### 3.3 `pytest.ini`

```ini
[pytest]
testpaths = tests
python_files = test_*.py
python_classes = Test*
python_functions = test_*
markers =
    real_windows: requires real Windows hardware (foreground apps, microphone, GPU)
    headless: safe to run in CI (no real hardware)
addopts = -ra --strict-markers --tb=short
```

### 3.4 `conftest.py`

```python
"""
Shared pytest fixtures for Omnix V6 tests.
"""

import os
import pytest

# Force headless boot BEFORE importing core
os.environ.setdefault("OMNIX_HEADLESS", "1")
os.environ.setdefault("OMNIX_QUIET_BOOT", "1")


@pytest.fixture(scope="session")
def engine():
    """Boot a single OmnixEngine instance for the whole test session."""
    from core.omnix_engine import OmnixEngine
    eng = OmnixEngine(auto_start=True)
    yield eng
    eng.shutdown()


@pytest.fixture
def mock_brain(monkeypatch):
    """Replace ai.brain_manager.BrainManager with a deterministic fake."""
    from tests.helpers.mocks import MockBrain
    return MockBrain()
```

### 3.5 `tests/helpers/mocks.py` (NEW)

Centralizes all V5 inline mock classes:

```python
"""
Shared test mocks for Omnix V6.

V5 had mock classes duplicated across test files. V6 centralizes them.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MockBrain:
    """Fake AI service that returns canned responses."""
    ask_calls: List[str] = field(default_factory=list)

    def ask(self, prompt: str, *, system: Optional[str] = None, **kwargs) -> str:
        self.ask_calls.append(prompt)
        return '{"intent": "conversation", "confidence": 0.9}'

    async def aask(self, *args, **kwargs):
        return self.ask(*args, **kwargs)

    def classify(self, text: str, *, options: List[str]) -> str:
        return options[0]

    def embed(self, text: str) -> List[float]:
        return [0.0] * 384

    def status(self) -> Dict[str, Any]:
        return {"ready": True, "provider": "mock"}


@dataclass
class MockMemoryService:
    remember_calls: List[str] = field(default_factory=list)
    recall_calls: List[str] = field(default_factory=list)
    def remember(self, *args, **kwargs): self.remember_calls.append(args[0] if args else None)
    def recall(self, *args, **kwargs): self.recall_calls.append(args[0] if args else None)


@dataclass
class MockScreenObserver:
    capture_calls: int = 0
    def get_latest_frame(self): self.capture_calls += 1; return None


@dataclass
class MockAutomationService:
    execute_calls: List[Any] = field(default_factory=list)
    def execute(self, plan, **kwargs):
        self.execute_calls.append(plan)
        from core.services.automation_service import AutomationResult
        return AutomationResult(success=True, value={"executed": True})
```

---

## 4. Per-test migration plan

### 4.1 `test_connected_loop.py` (Phase 10)

**V5 status:** Already pytest-style. Migrate as-is, replace inline Mocks with `tests.helpers.mocks` imports.

**V6 target:**

```python
import pytest
from tests.helpers.mocks import MockBrain, MockMemoryService, MockScreenObserver, MockAutomationService

def test_open_chrome_runs_full_loop(engine, monkeypatch):
    """Phase 10: open chrome should fire automation + memory."""
    brain = MockBrain()
    memory = MockMemoryService()
    screen = MockScreenObserver()
    automation = MockAutomationService()

    monkeypatch.setattr(engine, "brain_manager", brain)
    monkeypatch.setattr(engine, "memory_service", memory)
    monkeypatch.setattr(engine, "screen_observer", screen)
    monkeypatch.setattr(engine, "automation_service", automation)

    result = engine.execute("hey omnix, open chrome")
    assert result.success
    assert len(automation.execute_calls) >= 1
    assert len(memory.remember_calls) >= 1


@pytest.mark.parametrize("text,expected_intent", [
    ("open chrome", "open_app"),
    ("what time is it", "question"),
    ("search for AI agents", "search"),
])
def test_intent_classification(engine, text, expected_intent):
    """Phase 10: classifier returns the right intent for sample commands."""
    intent = engine.intent_classifier.classify(text)
    assert intent.name == expected_intent
```

### 4.2 `test_verification_recovery.py` (Phase 11)

**V5 status:** Already pytest-style. Migrate as-is, centralize stubs in `tests/helpers/mocks.py`.

**V6 target:** Same shape; the Stub classes move to `tests/helpers/mocks.py`. Add a `test_recovery_alternative_invokes_a_different_skill` parametrized over 3 alternative-skill strategies.

### 4.3 `test_pipeline.py` (Pipeline)

**V5 status:** Module-level script. Migrate to pytest with `FakeBrain` moved to `tests/helpers/mocks.py`.

**V6 target:**

```python
def test_intent_classification_for_search_query():
    from core.planning.intent_classifier import IntentClassifier
    classifier = IntentClassifier()
    intent = classifier.classify("Search AI agents")
    assert intent.name == "search"

def test_command_processor_compound_command():
    from core.planning.command_processor import CommandProcessor
    from tests.helpers.mocks import MockBrain
    processor = CommandProcessor(brain=MockBrain())
    plan = processor.create_plan("open chrome and search AI agents")
    assert len(plan.steps) >= 2

def test_task_planner_simple_command():
    from core.planning.task_planner import TaskPlanner
    from tests.helpers.mocks import MockBrain
    planner = TaskPlanner(brain=MockBrain())
    plan = planner.plan("open notepad")
    assert plan.steps[0].action == "open_application"
```

### 4.4 `test_routing_reliability.py` (Phase 12)

**V5 status:** `main()` script with 7 cases. Migrate to a parametrized pytest.

**V6 target:**

```python
import pytest

CASES = [
    ("open chrome", "open_app", "open_chrome"),
    ("open SkynetTerminal9000", "open_app", "fallback_or_fail"),
    ("what was I working on yesterday", "question", "memory_recall"),
    ("search for AI agents", "search", "web_search"),
    ("play believer", "media", "play_media"),
    ("type hello world", "input", "type_text"),
    ("click the save button", "ui", "click_element"),
]

@pytest.mark.parametrize("text,expected_intent,expected_action", CASES)
def test_routing_reliability(engine, text, expected_intent, expected_action):
    from core.planning.intent_classifier import IntentClassifier
    intent = IntentClassifier().classify(text)
    assert intent.name == expected_intent, f"intent mismatch for '{text}'"
```

### 4.5 `test_real_loop.py` (Phase 11)

**V5 status:** `main()` script. Migrate to a single pytest with `OMNIX_HEADLESS=0` for one end-to-end smoke (gated).

**V6 target:**

```python
@pytest.mark.real_windows
def test_full_loop_open_calculator():
    """Phase 11: end-to-end voice-to-action loop on a real Windows host."""
    import os
    os.environ["OMNIX_HEADLESS"] = "0"
    from core.omnix_engine import OmnixEngine
    eng = OmnixEngine(auto_start=True)
    try:
        result = eng.execute("open calculator")
        assert result.success
    finally:
        eng.shutdown()


def test_replan_budget_terminates_at_max():
    """Phase 11: replan loop terminates at max_replan_attempts."""
    # Uses engine fixture (headless), forces a failing goal
    ...
```

### 4.6 `test_real_execution.py` (Phase 17)

**V5 status:** `main()` script with 5 real commands. Migrate to 5 `@pytest.mark.real_windows` tests, with the `main()` block preserved for manual runs.

**V6 target:** 5 tests, each setting `OMNIX_HEADLESS=0`, booting engine, executing, asserting on `psutil`-validated running processes, Notepad clipboard readback.

### 4.7 `test_phase16_uia_smoke.py` (Phase 16)

**V5 status:** `main()` script, `OMNIX_HEADLESS=0`. Migrate as one `@pytest.mark.real_windows` test, preserving the `main()` block.

**V6 target:** Single pytest test that uses ctypes to find Chrome foreground, then probes UIA.

### 4.8 `test_ui_compound_reliability.py` (Phase 15)

**V5 status:** `main()` script with 5 cases. Migrate to 5 parametrized pytest tests.

### 4.9 `test_vision_action_reliability.py` (Phase 16b)

**V5 status:** `main()` script with 5 real-Windows vision+click cases. Migrate to 5 `@pytest.mark.real_windows` tests, preserving `main()` for manual runs.

---

## 5. New tests to add (V6 has coverage V5 lacked)

| New test | Why | Phase |
|---|---|---|
| `test_service_wrapper_contract.py` | Every `*Service` returns the canonical `*Result` shape. | 1 |
| `test_result_normalization.py` | `normalize_result` collapses all known skill return shapes correctly. | 1 |
| `test_event_bus_pubsub.py` | Subscribe → publish → listener fires. Async listener support. Wildcards. | 1 |
| `test_orchestrator_initialize_shutdown.py` | Engine starts and stops cleanly 100 times in a row. | 1 |
| `test_subsystem_lifecycle_uniformity.py` | Every subsystem has `initialize`/`shutdown`/`initialized`/`statistics`/`__repr__`. | 1 |
| `test_logging_no_stdlib_logging.py` | Static check: no `import logging` in production code. | 0.5 |
| `test_no_frozen_directory.py` | Static check: no `frozen/` in V6. | 0.5 |
| `test_env_var_gates_respected.py` | `OMNIX_HEADLESS=1` blocks real mic/cam. | 0.5 |
| `test_brain_is_only_ai_entry.py` | Static check: no `import openai` outside `ai/`. | 0.5 |
| `test_memory_via_service_only.py` | Static check: `memory_coordinator` only imported by `core/services/memory_service.py`. | 0.5 |

---

## 6. CI

**V6 target:** GitHub Actions (or local script if no remote) runs:

```yaml
- pytest -m "not real_windows"   # fast tests, ~30s
- pytest -m "real_windows"        # gated, run nightly or on demand
```

`real_windows` tests are NOT run on every commit. They require a Windows host with Chrome, Notepad, calculator, etc.

---

## 7. Phase 0 sign-off

- [x] No test run.
- [x] No test file created yet.
- [x] Plan is on disk for Phase 0.5+.

**PHASE 0 COMPLETE — NO SOURCE CODE MODIFIED. WAITING FOR APPROVAL TO BEGIN PHASE 0.5.**
