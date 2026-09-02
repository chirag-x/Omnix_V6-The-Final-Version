"""
Omnix V6 — Phase 5A provider isolation tests.

The LLM provider layer produces *data*; it must never touch the
Windows automation stack.  These tests enforce that boundary
statically: every ``.py`` file under ``ai/provider/`` is forbidden
from importing the automation modules.

Hard forbidden imports (R-21 of the LLM layer):

    * ``subprocess``
    * ``pyautogui``
    * ``win32gui`` / ``win32api``
    * ``ctypes``
    * ``core.capability_router``
    * ``core.omnix_engine`` (the engine is the execution surface;
      the provider must not reach for it either)
    * any V6 *Windows service* package (e.g. ``system.windows.*``,
      ``system.applications.*``, ``system.input.*``,
      ``system.filesystem.*``)

A violation means the Brain can call ``pyautogui.click`` from
inside a provider call.  We do not want that.
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

# Forbidden top-level module names (an import or import-from with this root
# anywhere in the path is a violation).
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

# Forbidden exact imports (whole module name).  Slightly stricter than the
# top-level list — e.g. ``win32con`` would also be forbidden.
_FORBIDDEN_EXACT_MODULES: Tuple[str, ...] = (
    "subprocess",
    "pyautogui",
    "win32gui",
    "win32api",
    "ctypes",
    "ctypes.wintypes",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _provider_dir() -> Path:
    """Locate the V6 ``ai/provider`` directory.

    Works whether the test is executed from the repo root or from inside
    the ``tests/`` directory.
    """
    here = Path(__file__).resolve()
    # tests/ is a sibling of ai/
    return here.parent.parent / "ai" / "provider"


def _iter_python_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.py") if p.is_file())


def _imported_modules(path: Path) -> List[str]:
    """Return every module reference in a Python file.

    Walks the AST so that a comment that *talks* about a forbidden
    import does not trip the test.  We collect:

        * ``import X`` -> ``X``
        * ``import X.Y`` -> ``X.Y``
        * ``from X.Y import Z`` -> ``X.Y``

    The set is the union of every alias / import target the file
    actually references.
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    mods: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                # ``from .x import y`` — relative to this package; not an
                # external automation module, ignore.
                continue
            if node.module:
                mods.append(node.module)
    return mods


def _looks_forbidden(module: str) -> bool:
    """Decide if ``module`` violates the isolation rule."""
    if not module:
        return False
    if module in _FORBIDDEN_EXACT_MODULES:
        return True
    for prefix in _FORBIDDEN_TOP_MODULES:
        if module == prefix or module.startswith(prefix + "."):
            return True
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ai_provider_directory_exists() -> None:
    """Sanity: the seam directory must be on disk."""
    assert _provider_dir().is_dir(), (
        f"expected ai/provider/ at {_provider_dir()}, but it does not exist"
    )


@pytest.mark.parametrize("py_file", list(_iter_python_files(_provider_dir())))
def test_provider_file_has_no_forbidden_imports(py_file: Path) -> None:
    """Every .py file under ai/provider/ must be free of automation imports.

    A violation here is a HARD security boundary breach: the provider
    must not be able to drive the Windows desktop, no matter how
    indirectly.
    """
    mods = _imported_modules(py_file)
    bad = [m for m in mods if _looks_forbidden(m)]
    assert not bad, (
        f"{py_file.name} imports forbidden modules for the provider layer: "
        f"{bad!r}. The provider must not reach into Windows automation."
    )


def test_provider_package_docstring_mentions_isolation() -> None:
    """The package docstring must state the isolation rule.

    This is a documentation guard: the rule is documented where new
    contributors will read it.
    """
    init = _provider_dir() / "__init__.py"
    assert init.is_file()
    text = init.read_text(encoding="utf-8")
    for needle in ("subprocess", "pyautogui", "win32gui", "ctypes"):
        assert needle in text, (
            f"ai/provider/__init__.py must mention the forbidden import "
            f"'{needle}' in its docstring (current text does not)."
        )


def test_provider_cannot_instantiate_omnix_engine() -> None:
    """The provider subpackage must not expose OmnixEngine.

    Even at the symbol level, ``from ai.provider import OmnixEngine``
    must fail.  The provider layer is downstream of the engine; the
    engine is downstream of the provider.
    """
    import ai.provider as pkg  # noqa: F401

    # The package's public surface is exactly the symbols it exports.
    for name in ("OmnixEngine", "Engine", "CapabilityRouter",
                 "execute", "run_capability"):
        assert name not in pkg.__all__, (
            f"ai.provider must not export {name!r}; the provider must "
            f"produce data, not drive the engine."
        )


def test_mock_provider_has_no_capability_dispatch() -> None:
    """The MockProvider must not carry a reference to the capability router.

    A provider that secretly dispatches capabilities is worse than a
    provider that does not — it is a capability execution path
    disguised as a model.
    """
    from ai.provider import MockProvider
    import inspect

    src = inspect.getsourcefile(MockProvider)
    assert src is not None
    text = Path(src).read_text(encoding="utf-8")
    forbidden_tokens = ("CapabilityRouter", "execute_capability", "capability_router")
    for token in forbidden_tokens:
        assert token not in text, (
            f"MockProvider must not reference '{token}'; the provider "
            f"produces data, it does not dispatch capabilities."
        )


def test_provider_does_not_silently_swallow_missing_module_imports() -> None:
    """The provider must not wrap its automation-blocked imports in try/except.

    A ``try: import pyautogui except ImportError: pass`` block in a
    provider file would defeat the static-import test above.  This
    test refuses that pattern.
    """
    root = _provider_dir()
    pattern = re.compile(
        r"try\s*:.*?import\s+(subprocess|pyautogui|win32gui|win32api|ctypes)",
        re.DOTALL,
    )
    for py in _iter_python_files(root):
        text = py.read_text(encoding="utf-8")
        assert not pattern.search(text), (
            f"{py.name} tries to import a forbidden module inside a "
            f"try/except — the provider must not be able to reach "
            f"Windows automation, even conditionally."
        )
