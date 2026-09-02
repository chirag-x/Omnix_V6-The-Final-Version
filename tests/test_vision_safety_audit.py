"""
AST-based safety audit for the vision/ package.

Phase 7.1 hardening: Vision is *observation only*.  These tests
are a structural check that the implementation never reaches for
side-effectful machinery:

  * No imports of ``pyautogui`` / ``subprocess`` / ``os.system``
    / ``shutil.rmtree`` in vision/ or core/services/vision_service.py.
  * No imports of LLM libraries (anthropic, openai, openrouter,
    requests to chat endpoints) in vision/.
  * No calls to ``mouse.click`` / ``keyboard.type`` /
    ``pyautogui.click`` in vision/.

The audit walks the AST, not the import-time side effects, so it
catches both ``import pyautogui`` and ``from pyautogui import click``
forms.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set, Tuple

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()

# Directories under audit.
VISION_DIR = REPO_ROOT / "vision"
VISION_SERVICE = REPO_ROOT / "core" / "services" / "vision_service.py"

# Forbidden top-level modules that would let vision perform side effects.
FORBIDDEN_MODULES: Set[str] = {
    "pyautogui",
    "subprocess",
    "os.system",
    "shutil",
    "ctypes",
    "win32api",
    "win32gui",
    "win32con",
    "SendInput",
    "requests",
    "urllib",
    "httpx",
    "openai",
    "anthropic",
    "openrouter",
    "google.generativeai",
}

# Forbidden call names — these are side-effect primitives.
FORBIDDEN_CALLS: Set[str] = {
    "os.system",
    "os.popen",
    "subprocess.run",
    "subprocess.call",
    "subprocess.Popen",
    "subprocess.check_output",
    "subprocess.check_call",
    "pyautogui.click",
    "pyautogui.moveTo",
    "pyautogui.doubleClick",
    "pyautogui.rightClick",
    "pyautogui.typewrite",
    "pyautogui.press",
    "pyautogui.hotkey",
    "pyautogui.screenshot",  # vision must go through ScreenshotProvider
    "shutil.rmtree",
}


def _python_files() -> List[Path]:
    files: List[Path] = []
    if VISION_DIR.exists():
        for p in VISION_DIR.rglob("*.py"):
            files.append(p)
    if VISION_SERVICE.exists():
        files.append(VISION_SERVICE)
    return files


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _is_attr_chain(node: ast.AST) -> List[str]:
    """Return ['a','b','c'] for ``a.b.c`` style nodes."""
    parts: List[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return list(reversed(parts))


def _imports_in(tree: ast.Module) -> List[Tuple[str, str]]:
    """Return (top-level-module, full-spec) for every import in ``tree``."""
    out: List[Tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                top = (n.name or "").split(".")[0]
                out.append((top, n.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0]
            for n in node.names:
                out.append((top, f"{mod}.{n.name}"))
    return out


def _calls_in(tree: ast.Module) -> List[List[str]]:
    """Return the attribute-chain for every call in ``tree``."""
    out: List[List[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            chain = _is_attr_chain(node.func)
            if chain:
                out.append(chain)
    return out


# --------------------------------------------------------------------- tests

@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_forbidden_imports(path: Path) -> None:
    tree = _parse(path)
    for top, full in _imports_in(tree):
        assert top not in FORBIDDEN_MODULES, (
            f"{path.relative_to(REPO_ROOT)} imports forbidden module "
            f"{full!r} (top-level {top!r}); vision must be observation-only."
        )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_forbidden_calls(path: Path) -> None:
    tree = _parse(path)
    for chain in _calls_in(tree):
        # Compare to attribute chains, e.g. "pyautogui.click" → ["pyautogui", "click"].
        joined = ".".join(chain)
        assert joined not in FORBIDDEN_CALLS, (
            f"{path.relative_to(REPO_ROOT)} calls forbidden function "
            f"{joined!r}; vision must not perform side effects."
        )


def test_vision_directory_exists() -> None:
    """Sanity: we are auditing a directory that should exist."""
    assert VISION_DIR.exists(), f"vision/ directory missing at {VISION_DIR}"


def test_only_one_perception_router() -> None:
    """Architecture audit: ONE PerceptionRouter class, ONE VisionService class."""
    files = list(VISION_DIR.rglob("*.py")) + [VISION_SERVICE]
    router_classes = 0
    service_classes = 0
    for p in files:
        try:
            tree = _parse(p)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "PerceptionRouter":
                    router_classes += 1
                if node.name == "VisionService":
                    service_classes += 1
    assert router_classes == 1, (
        f"expected exactly one PerceptionRouter class, found {router_classes}"
    )
    assert service_classes == 1, (
        f"expected exactly one VisionService class, found {service_classes}"
    )


def test_only_one_screenshot_provider_protocol() -> None:
    """Architecture audit: ONE ScreenshotProvider Protocol."""
    files = list(VISION_DIR.rglob("*.py"))
    protocol_classes = 0
    for p in files:
        try:
            tree = _parse(p)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ScreenshotProvider":
                # Confirm it is decorated with @runtime_checkable Protocol.
                decs = [
                    ast.unparse(d) if hasattr(ast, "unparse") else ""
                    for d in node.decorator_list
                ]
                if any("Protocol" in d or "runtime_checkable" in d for d in decs):
                    protocol_classes += 1
    assert protocol_classes == 1, (
        f"expected exactly one ScreenshotProvider Protocol, found {protocol_classes}"
    )


def test_only_one_perception_strategy_protocol() -> None:
    files = list(VISION_DIR.rglob("*.py"))
    protocol_classes = 0
    for p in files:
        try:
            tree = _parse(p)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "PerceptionStrategy":
                decs = [
                    ast.unparse(d) if hasattr(ast, "unparse") else ""
                    for d in node.decorator_list
                ]
                bases = [
                    ast.unparse(b) if hasattr(ast, "unparse") else ""
                    for b in node.bases
                ]
                if any("Protocol" in d for d in decs) or any("Protocol" in b for b in bases):
                    protocol_classes += 1
    assert protocol_classes == 1, (
        f"expected exactly one PerceptionStrategy Protocol, found {protocol_classes}"
    )


def test_vision_service_does_not_import_omnix_engine() -> None:
    """Phase 7.1: VisionService must not depend on OmnixEngine directly."""
    tree = _parse(VISION_SERVICE)
    for top, full in _imports_in(tree):
        assert "omnix_engine" not in top, (
            f"vision_service.py imports {full!r}; "
            f"Vision must depend only on ScreenshotProvider."
        )
