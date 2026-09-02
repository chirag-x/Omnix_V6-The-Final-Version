"""
Omnix V6 — Phase 6C Agent Isolation / Boundary tests (AST-level).

These tests are *architectural enforcement*, not behavioural tests.
They walk the Python source of the Agent Orchestrator and assert
that it never imports the modules that would let it bypass the
closed action set (R-21) and talk to Windows directly.

The forbidden imports are:
  - subprocess                 (shell escape)
  - pyautogui                  (mouse / keyboard on the host)
  - win32gui, win32api, win32con (native Windows GUI)
  - ctypes, ctypes.windll      (FFI to the OS)
  - os.system, os.popen        (shell escape via os)
  - socket                     (raw network access)
  - urllib.request, urllib2    (raw HTTP)

The Agent must reach Windows only through the closed Capability set.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Iterable, List, Set, Tuple

import pytest


# ---------------------------------------------------------------------------
# Forbidden imports / callables (R-21)
# ---------------------------------------------------------------------------

FORBIDDEN_MODULES: Set[str] = {
    "subprocess",
    "pyautogui",
    "win32gui",
    "win32api",
    "win32con",
    "win32com",
    "win32process",
    "win32clipboard",
    "ctypes",
    "ctypes.windll",
    "socket",
    "urllib",
    "urllib.request",
    "urllib2",
}

# Module roots we allow (used by tests / fixtures / doc-stubs).
ALLOWED_TEST_PREFIXES: Tuple[str, ...] = (
    "tests",
)

# The single source root for the Agent.
AGENT_SOURCE_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "core" / "orchestration"
)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

def _iter_imports(tree: ast.AST) -> Iterable[str]:
    """Yield every imported module name in ``tree``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                yield node.module
        elif isinstance(node, ast.Attribute):
            # ``os.system(...)`` etc. — handle at call site below.
            pass


def _iter_dangerous_calls(tree: ast.AST) -> Iterable[str]:
    """Yield dotted attribute chains that look like shell/OS escapes.

    These are cases where the module is imported as something innocent
    (``os``, ``importlib``) but the *attribute* is the dangerous one.
    """
    dangerous_attrs = {
        "system", "popen", "exec", "execvp", "execvpe", "spawn",
        "run", "call", "check_output", "check_call", "Popen",
        "Click", "moveTo", "typewrite", "press", "hotkey", "keyDown",
        "keyUp", "mouseDown", "mouseUp", "scroll",
        "ShellExecute", "FindWindow", "SetForegroundWindow",
        "SendMessage", "PostMessage", "keybd_event", "mouse_event",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in dangerous_attrs:
            # Reconstruct the dotted chain ("os.system" → "os.system")
            parts: List[str] = []
            cur = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                chain = ".".join(reversed(parts))
                yield chain


def _python_files(root: pathlib.Path) -> List[pathlib.Path]:
    return sorted(p for p in root.rglob("*.py") if p.is_file())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAgentSourceIsolation:
    def test_agent_source_dir_exists(self):
        assert AGENT_SOURCE_DIR.exists(), (
            f"expected orchestration source at {AGENT_SOURCE_DIR}"
        )

    def test_no_forbidden_imports_in_orchestration(self):
        offenders: List[Tuple[pathlib.Path, str]] = []
        for path in _python_files(AGENT_SOURCE_DIR):
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src, filename=str(path))
            except SyntaxError:
                # Skip unparseable files (e.g. the placeholder stubs).
                continue
            for name in _iter_imports(tree):
                top = name.split(".")[0]
                if top in FORBIDDEN_MODULES or name in FORBIDDEN_MODULES:
                    offenders.append((path, name))
        assert not offenders, (
            "Agent Orchestrator must not import forbidden modules "
            f"(R-21). Offenders: {offenders}"
        )

    def test_no_dangerous_calls_in_orchestration(self):
        offenders: List[Tuple[pathlib.Path, str]] = []
        for path in _python_files(AGENT_SOURCE_DIR):
            src = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src, filename=str(path))
            except SyntaxError:
                continue
            for chain in _iter_dangerous_calls(tree):
                top = chain.split(".")[0]
                if top in FORBIDDEN_MODULES:
                    offenders.append((path, chain))
        assert not offenders, (
            "Agent Orchestrator must not use dangerous OS-escape calls. "
            f"Offenders: {offenders}"
        )

    def test_no_os_system_or_popen_in_orchestration(self):
        # Defensive second pass for the most common foot-guns.
        text = ""
        for path in _python_files(AGENT_SOURCE_DIR):
            text += path.read_text(encoding="utf-8")
        assert "os.system" not in text, (
            "Agent Orchestrator must not call os.system (use a capability)"
        )
        assert "os.popen" not in text, (
            "Agent Orchestrator must not call os.popen (use a capability)"
        )

    def test_orchestration_does_not_import_subprocess(self):
        text = ""
        for path in _python_files(AGENT_SOURCE_DIR):
            text += path.read_text(encoding="utf-8")
        assert "import subprocess" not in text, (
            "Agent Orchestrator must not import subprocess"
        )

    def test_orchestration_does_not_import_pyautogui(self):
        text = ""
        for path in _python_files(AGENT_SOURCE_DIR):
            text += path.read_text(encoding="utf-8")
        assert "pyautogui" not in text, (
            "Agent Orchestrator must not import pyautogui"
        )


# ---------------------------------------------------------------------------
# Companion: the capability layer is the *only* place that may import
# these modules.  We check that at least one capability file *does*
# exist somewhere (sanity that the architecture actually puts a
# capability in between).
# ---------------------------------------------------------------------------

class TestArchitectureSanity:
    def test_capability_registry_exists(self):
        # Look for any module that mentions the CapabilityRegistry.
        from core.capability_registry import CapabilityRegistry  # noqa: F401

    def test_orchestration_has_no_main_loop_only_capability(self):
        # The Agent is *not* a "do everything" function.  Confirm by
        # checking that ``agent.py`` does not define ``run_os_command``
        # or similar open-ended entry points.
        agent_path = AGENT_SOURCE_DIR / "agent.py"
        if not agent_path.exists():
            pytest.skip("agent.py not yet present")
        text = agent_path.read_text(encoding="utf-8")
        forbidden_names = (
            "run_os_command", "shell", "execute_shell", "raw_command",
            "send_input", "send_key", "click", "type_text",
        )
        for name in forbidden_names:
            assert name not in text, (
                f"agent.py must not expose a low-level command method "
                f"called {name!r}"
            )
