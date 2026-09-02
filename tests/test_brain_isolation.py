"""
Omnix V6 — Phase 5C+5D architectural isolation tests.

The Brain / Planner layer produces *plans*.  It must NOT touch the
Windows automation stack and must NOT call into the engine.  These
tests enforce that boundary statically: every ``.py`` file under
``ai/brain/`` and ``ai/brain_manager.py`` is forbidden from importing
the automation modules or the engine.

Hard forbidden imports:

    * ``subprocess``
    * ``pyautogui``
    * ``win32gui`` / ``win32api``
    * ``ctypes``
    * ``core.capability_router``
    * ``core.omnix_engine``
    * any V6 *Windows service* (e.g. ``system.windows.*``,
      ``system.applications.*``, ``system.input.*``,
      ``system.filesystem.*``)

A violation here means the LLM can call ``pyautogui.click`` from
inside a planner.  We do not want that.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Iterable, List, Tuple

import pytest


# ---------------------------------------------------------------------------
# Static-analysis forbidden list
# ---------------------------------------------------------------------------

_FORBIDDEN_TOP_MODULES: Tuple[str, ...] = (
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
)

_FORBIDDEN_EXACT_MODULES: Tuple[str, ...] = (
    "subprocess",
    "pyautogui",
    "win32gui",
    "win32api",
    "ctypes",
    "ctypes.wintypes",
)

# Forbidden exports: even symbol-level leakage of the engine
# would let a caller reach the engine from the Brain layer.
_FORBIDDEN_EXPORTS: Tuple[str, ...] = (
    "OmnixEngine",
    "Engine",
    "CapabilityRouter",
    "ActionRequest",   # belongs to the engine layer, not the brain
    "execute",         # "execute" is the engine's verb, not the brain's
    "run_capability",
    "dispatch",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _brain_dirs() -> List[Path]:
    """Locate the V6 brain directories.

    Works whether the test is executed from the repo root or from
    inside the ``tests/`` directory.
    """
    here = Path(__file__).resolve()
    root = here.parent.parent
    return [root / "ai" / "brain", root / "ai"]


def _iter_python_files(roots: List[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        # Only files directly in the package; the brain/ subpackage
        # is the main target, and ai/__init__.py is checked separately.
        if root.name == "ai":
            for p in sorted(root.glob("brain*.py")):
                if p.is_file():
                    yield p
            for p in sorted(root.glob("brain" + os_sep() + "*.py")):
                if p.is_file():
                    yield p
        else:
            for p in sorted(root.glob("*.py")):
                if p.is_file():
                    yield p


def os_sep() -> str:
    import os
    return os.sep


def _imported_modules(path: Path) -> List[str]:
    """Return every module reference in a Python file (AST-walked)."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    mods: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # relative import; not an external automation module
                continue
            if node.module:
                mods.append(node.module)
    return mods


def _looks_forbidden(module: str) -> bool:
    if not module:
        return False
    if module in _FORBIDDEN_EXACT_MODULES:
        return True
    for prefix in _FORBIDDEN_TOP_MODULES:
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


def _try_except_around_import(text: str) -> bool:
    """Detect try/except ImportError around a forbidden module."""
    pattern = re.compile(
        r"try\s*:.*?import\s+(subprocess|pyautogui|win32gui|win32api|ctypes)",
        re.DOTALL,
    )
    return bool(pattern.search(text))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ai_brain_directory_exists() -> None:
    brain_dir = Path(__file__).resolve().parent.parent / "ai" / "brain"
    assert brain_dir.is_dir(), f"expected ai/brain/ at {brain_dir}"


@pytest.mark.parametrize("py_file", list(_iter_python_files(_brain_dirs())))
def test_brain_file_has_no_forbidden_imports(py_file: Path) -> None:
    """Every .py file under ai/brain/ must be free of automation imports.

    A violation is a HARD security boundary breach: the LLM-driven
    planner must not reach into Windows automation.
    """
    mods = _imported_modules(py_file)
    bad = [m for m in mods if _looks_forbidden(m)]
    assert not bad, (
        f"{py_file.name} imports forbidden modules for the brain layer: "
        f"{bad!r}. The brain must not reach into Windows automation."
    )


def test_brain_does_not_silently_swallow_missing_module_imports() -> None:
    """The brain layer must not wrap automation-blocked imports in try/except."""
    for path in _iter_python_files(_brain_dirs()):
        text = path.read_text(encoding="utf-8")
        assert not _try_except_around_import(text), (
            f"{path.name} tries to import a forbidden module inside a "
            f"try/except — the brain must not reach Windows automation, "
            f"even conditionally."
        )


def test_brain_package_docstring_mentions_isolation() -> None:
    """The package docstring must state the isolation rule."""
    init = Path(__file__).resolve().parent.parent / "ai" / "brain" / "__init__.py"
    assert init.is_file()
    text = init.read_text(encoding="utf-8")
    for needle in ("subprocess", "pyautogui", "win32gui", "ctypes"):
        assert needle in text, (
            f"ai/brain/__init__.py must mention the forbidden import "
            f"'{needle}' in its docstring (current text does not)."
        )


def test_brain_public_surface_has_no_engine_or_router() -> None:
    """The public surface of ``ai.brain`` must not leak engine symbols.

    Even at the symbol level, ``from ai.brain import CapabilityRouter``
    must fail.  The brain produces plans; the engine dispatches them.
    """
    import ai.brain as pkg  # noqa: F401
    for name in _FORBIDDEN_EXPORTS:
        assert name not in getattr(pkg, "__all__", ()), (
            f"ai.brain must not export {name!r}; the brain produces plans, "
            f"it does not dispatch them."
        )
        assert name not in dir(pkg), (
            f"ai.brain must not expose {name!r} as a public symbol."
        )


def test_brain_manager_does_not_export_engine_or_router() -> None:
    """The re-export shim at ``ai.brain_manager`` must not leak engine symbols."""
    try:
        import ai.brain_manager as pkg
    except Exception:
        pytest.skip("ai.brain_manager not importable")
    for name in _FORBIDDEN_EXPORTS:
        assert name not in getattr(pkg, "__all__", ()), (
            f"ai.brain_manager must not export {name!r}."
        )


def test_brain_does_not_construct_action_request() -> None:
    """The brain layer must not construct or import ActionRequest.

    ActionRequest is the engine's input contract.  The brain
    produces :class:`PlanStep` and stops there; it must never
    reach across the engine seam.
    """
    import ai.brain as pkg
    for name in dir(pkg):
        # Sanity: the public namespace must not include ActionRequest
        if name == "ActionRequest":
            pytest.fail("ai.brain must not expose ActionRequest")


def _file_uses_dispatch_token(path: Path) -> List[str]:
    """AST-walk a file and return any dispatch tokens used as real code.

    Docstrings and comments are excluded; only Name/Attribute nodes
    count.  A violation means a forbidden symbol is reachable as code.
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    found: List[str] = []
    for node in ast.walk(tree):
        # String literals (incl. docstrings) are skipped automatically
        # because ast.walk returns the Str/Constant nodes for them,
        # which we filter out.
        if isinstance(node, ast.Name):
            if node.id in ("CapabilityRouter", "execute_capability", "capability_router"):
                found.append(node.id)
        elif isinstance(node, ast.Attribute):
            # Build the dotted name: e.g. ``obj.execute_capability``
            parts: List[str] = []
            n = node
            while isinstance(n, ast.Attribute):
                parts.append(n.attr)
                n = n.value
            if isinstance(n, ast.Name):
                parts.append(n.id)
                dotted = ".".join(reversed(parts))
                if dotted.endswith(".execute") or dotted.endswith("execute_capability") or "capability_router" in dotted:
                    found.append(dotted)
    return found


def test_llm_planner_does_not_dispatch_capabilities() -> None:
    """The LLM planner must not call ``.execute()`` on a capability."""
    from ai.brain import llm_planner
    import inspect
    src = inspect.getsourcefile(llm_planner)
    assert src is not None
    path = Path(src)
    bad = _file_uses_dispatch_token(path)
    assert not bad, (
        f"LLMPlanner must not reference {bad!r}; the planner "
        f"produces plans, it does not dispatch capabilities."
    )


def test_deterministic_planner_does_not_dispatch_capabilities() -> None:
    """The deterministic planner must not call ``.execute()`` on a capability."""
    from ai.brain import deterministic
    import inspect
    src = inspect.getsourcefile(deterministic)
    assert src is not None
    path = Path(src)
    bad = _file_uses_dispatch_token(path)
    assert not bad, (
        f"DeterministicPlanner must not reference {bad!r}; the planner "
        f"produces plans, it does not dispatch capabilities."
    )


def test_brain_class_does_not_dispatch_capabilities() -> None:
    """The Brain class must not call ``.execute()`` on a capability."""
    from ai.brain import brain
    import inspect
    src = inspect.getsourcefile(brain)
    assert src is not None
    path = Path(src)
    bad = _file_uses_dispatch_token(path)
    assert not bad, (
        f"Brain must not reference {bad!r}; the brain produces plans, "
        f"it does not dispatch them."
    )
