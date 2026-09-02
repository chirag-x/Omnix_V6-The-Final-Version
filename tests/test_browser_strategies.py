"""
Browser strategy tests (Phase 8).

Exercises the pure helpers in :mod:`browser.strategies`:

* :class:`TextNormalizer` — canonical text comparison used by
  the Brain / Verifier when checking "did we read the right
  text?" (whitespace, unicode, case)
* :class:`RelativeTargetResolver` — the *static* refinement
  that turns a :class:`BrowserTarget` into the kind/nth/label
  hint the session will dispatch on

Covers:

* normalisation: whitespace collapse, unicode NFKC, case folding
* equality and substring matches
* relative resolution: type guard, nth validation, label
  preservation
* safety: strategies are pure, no I/O
"""

from __future__ import annotations

import pytest

from browser.models.contracts import BrowserTarget, LocatorKind
from browser.strategies.normalize import TextNormalizer
from browser.strategies.relative import (
    RelativeTargetHint,
    RelativeTargetResolver,
)


# ---------------------------------------------------------------------------
# TextNormalizer.normalize
# ---------------------------------------------------------------------------

def test_normalize_collapses_internal_whitespace() -> None:
    assert TextNormalizer.normalize("  hello   world  ") == "hello world"


def test_normalize_handles_tabs_and_newlines() -> None:
    assert TextNormalizer.normalize("a\tb\nc\rd") == "a b c d"


def test_normalize_strips_edges() -> None:
    assert TextNormalizer.normalize("   \n hi \n   ") == "hi"


def test_normalize_handles_fullwidth_space() -> None:
    # U+3000 is the ideographic space; NFKC should fold it to a regular space.
    assert TextNormalizer.normalize("　hi　there　") == "hi there"


def test_normalize_non_string_returns_empty() -> None:
    assert TextNormalizer.normalize(None) == ""
    assert TextNormalizer.normalize(123) == ""
    assert TextNormalizer.normalize(["hi"]) == ""
    assert TextNormalizer.normalize({"hi": 1}) == ""
    assert TextNormalizer.normalize(b"hi") == ""


def test_normalize_empty_string_returns_empty() -> None:
    assert TextNormalizer.normalize("") == ""
    assert TextNormalizer.normalize("   ") == ""


# ---------------------------------------------------------------------------
# TextNormalizer.equals
# ---------------------------------------------------------------------------

def test_equals_ignores_whitespace_and_case() -> None:
    assert TextNormalizer.equals("Sign In", "sign in")
    assert TextNormalizer.equals("  sign   in  ", "SIGN IN")
    assert TextNormalizer.equals("　sign　in", "sign in")


def test_equals_handles_non_strings() -> None:
    assert TextNormalizer.equals(None, "x") is False
    assert TextNormalizer.equals("x", None) is False
    assert TextNormalizer.equals("", "") is True


def test_equals_returns_bool() -> None:
    assert isinstance(TextNormalizer.equals("a", "a"), bool)


# ---------------------------------------------------------------------------
# TextNormalizer.contains
# ---------------------------------------------------------------------------

def test_contains_is_case_insensitive() -> None:
    assert TextNormalizer.contains("Click the SIGN IN button", "sign in") is True


def test_contains_collapses_whitespace() -> None:
    assert TextNormalizer.contains("Welcome  to  the   page", "welcome to the page") is True


def test_contains_returns_false_when_not_present() -> None:
    assert TextNormalizer.contains("hello world", "missing") is False


def test_contains_handles_non_strings() -> None:
    # None on the left: nothing contains nothing → haystack becomes "",
    # needle is "x" → not contained.
    assert TextNormalizer.contains(None, "x") is False
    # None on the right: needle becomes ""; every string contains the
    # empty string.  This is documented behaviour, not a bug.
    assert TextNormalizer.contains("x", None) is True


# ---------------------------------------------------------------------------
# RelativeTargetResolver.refine
# ---------------------------------------------------------------------------

def test_refine_rejects_non_browser_target() -> None:
    with pytest.raises(TypeError):
        RelativeTargetResolver.refine("not a target")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RelativeTargetResolver.refine({"kind": "css", "value": "#x"})  # type: ignore[arg-type]


def test_refine_passes_through_kind_and_value() -> None:
    t = BrowserTarget(kind=LocatorKind.CSS, value="#u")
    h = RelativeTargetResolver.refine(t)
    assert isinstance(h, RelativeTargetHint)
    assert h.primary_kind == LocatorKind.CSS
    assert h.selector_hint == "#u"
    assert h.nth is None
    assert h.label == ""


def test_refine_preserves_label() -> None:
    t = BrowserTarget(kind=LocatorKind.TEXT, value="Sign in", label="sign-in button")
    h = RelativeTargetResolver.refine(t)
    assert h.label == "sign-in button"
    assert h.selector_hint == "Sign in"


def test_refine_preserves_nth() -> None:
    t = BrowserTarget(kind=LocatorKind.CSS, value=".row", nth=2)
    h = RelativeTargetResolver.refine(t)
    assert h.nth == 2


def test_refine_rejects_negative_nth() -> None:
    t = BrowserTarget(kind=LocatorKind.CSS, value=".row", nth=-1)
    with pytest.raises(ValueError):
        RelativeTargetResolver.refine(t)


def test_refine_rejects_non_int_nth() -> None:
    # Bypass the dataclass validator by constructing the field manually.
    t = BrowserTarget(kind=LocatorKind.CSS, value=".row")
    object.__setattr__(t, "nth", "zero")
    with pytest.raises(ValueError):
        RelativeTargetResolver.refine(t)


def test_refine_supports_all_locator_kinds() -> None:
    for kind in LocatorKind:
        t = BrowserTarget(kind=kind, value="v")
        h = RelativeTargetResolver.refine(t)
        assert h.primary_kind == kind


# ---------------------------------------------------------------------------
# RelativeTargetHint.to_dict
# ---------------------------------------------------------------------------

def test_hint_to_dict_minimal() -> None:
    h = RelativeTargetHint(
        primary_kind=LocatorKind.CSS, nth=None, selector_hint="#x",
    )
    d = h.to_dict()
    assert d == {"primary_kind": "css", "nth": None,
                 "selector_hint": "#x", "label": ""}


def test_hint_to_dict_full() -> None:
    h = RelativeTargetHint(
        primary_kind=LocatorKind.TEXT,
        nth=3,
        selector_hint="Sign in",
        label="sign-in",
    )
    d = h.to_dict()
    assert d == {
        "primary_kind": "text",
        "nth": 3,
        "selector_hint": "Sign in",
        "label": "sign-in",
    }


# ---------------------------------------------------------------------------
# Safety: strategies are pure
# ---------------------------------------------------------------------------

def test_normalize_module_does_not_import_subprocess() -> None:
    from browser.strategies import normalize as m
    src = open(m.__file__, encoding="utf-8").read()
    for forbidden in ("import subprocess", "from subprocess",
                      "os.system", "os.popen"):
        assert forbidden not in src, f"forbidden in normalize: {forbidden!r}"


def test_relative_module_does_not_import_subprocess() -> None:
    from browser.strategies import relative as m
    src = open(m.__file__, encoding="utf-8").read()
    for forbidden in ("import subprocess", "from subprocess",
                      "os.system", "os.popen"):
        assert forbidden not in src, f"forbidden in relative: {forbidden!r}"
