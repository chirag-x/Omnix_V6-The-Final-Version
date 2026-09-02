"""
Omnix V6 — Phase 5B architectural isolation tests.

The Intent Interpreter must NOT import any of:
  - subprocess / pyautogui / win32gui / win32api / ctypes
  - core.capability_router
  - core.omnix_engine
  - system.windows.* / system.applications.* / system.input.*
  - system.filesystem.* / system.clipboard.* / system.processes.*

It also must NOT export any name that would let a caller reach the
engine directly.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

import pytest


PACKAGE_DIR = Path("ai/intent")

FORBIDDEN_MODULES = {
    "subprocess",
    "pyautogui",
    "win32gui",
    "win32api",
    "ctypes",
    "core.capability_router",
    "core.omnix_engine",
    "system.windows",
    "system.applications",
    "system.input",
    "system.filesystem",
    "system.clipboard",
    "system.processes",
}

FORBIDDEN_EXPORTS = {
    "OmnixEngine",
    "Engine",
    "CapabilityRouter",
    "execute",
    "run_capability",
}


def _iter_python_files(root: Path) -> Iterable[Path]:
    yield from sorted(root.glob("*.py"))


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found.add(module)
    return found


def _try_except_imports(path: Path) -> bool:
    """Return True if the file contains a try/except ImportError around a
    forbidden import (an attempt to defeat the static check)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    return True
    return False


def _exports_in(path: Path) -> set[str]:
    """Return the set of names that appear in the file as real AST
    references (Name nodes), excluding strings and docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            n = node
            parts: list[str] = []
            while isinstance(n, ast.Attribute):
                parts.append(n.attr)
                n = n.value
            if isinstance(n, ast.Name):
                parts.append(n.id)
                names.add(".".join(reversed(parts)))
    return names


def test_package_exists() -> None:
    assert PACKAGE_DIR.is_dir(), f"Missing package: {PACKAGE_DIR}"


@pytest.mark.parametrize("path", list(_iter_python_files(PACKAGE_DIR)))
def test_no_forbidden_imports(path: Path) -> None:
    imports = _imports_in(path)
    for forbidden in FORBIDDEN_MODULES:
        assert forbidden not in imports, (
            f"{path} imports forbidden module {forbidden!r}"
        )


@pytest.mark.parametrize("path", list(_iter_python_files(PACKAGE_DIR)))
def test_no_try_except_around_imports(path: Path) -> None:
    assert not _try_except_imports(path), (
        f"{path} wraps an import in try/except — likely trying to defeat the static check"
    )


def test_init_does_not_export_engine_or_router() -> None:
    init = PACKAGE_DIR / "__init__.py"
    if not init.exists():
        pytest.skip("__init__.py missing")
    refs = _exports_in(init)
    for name in FORBIDDEN_EXPORTS:
        assert name not in refs, (
            f"{init} references forbidden export {name!r}"
        )
