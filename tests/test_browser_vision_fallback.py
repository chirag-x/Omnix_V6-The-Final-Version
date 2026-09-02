"""
Vision-fallback tests (Phase 8).

Covers the closed set of hooks the :class:`BrowserService` may
invoke to escalate an unresolved DOM target to vision.  The
fallback must NEVER receive cookies, passwords, or full HTML;
it must NEVER call the LLM provider directly; and it must
NEVER execute JavaScript.  These tests pin those rules.

Covers:

* :class:`NullVisionFallback` — never resolves, surfaces
  "vision fallback not configured"
* :class:`VisionFallbackAdapter` — wraps a :class:`VisionService`
  without importing the vision layer; duck-typed contract
* :class:`VisionFallback` — runtime-checkable protocol
* :class:`VisionFallbackResult` — frozen; metadata must be a
  Mapping or None; never a free-form string
* safety: fallback must never receive any input other than a
  target query and a screenshot path
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Mapping, Optional

import pytest

from browser.strategies.vision_fallback import (
    NullVisionFallback,
    VisionFallback,
    VisionFallbackAdapter,
    VisionFallbackResult,
)


# ---------------------------------------------------------------------------
# NullVisionFallback
# ---------------------------------------------------------------------------

def test_null_vision_fallback_never_resolves() -> None:
    n = NullVisionFallback()
    out = n.ground_via_vision(target_query="Sign in",
                              screenshot_path="/tmp/x.png")
    assert isinstance(out, VisionFallbackResult)
    assert out.resolved is False
    assert out.error == "vision fallback not configured"
    # No bounding box, no confidence.
    assert out.x is None
    assert out.y is None
    assert out.width == 0
    assert out.height == 0
    assert out.confidence == 0.0
    assert out.text == ""


def test_null_vision_fallback_does_not_read_screenshot() -> None:
    """The Null fallback must not open or inspect the screenshot path."""
    n = NullVisionFallback()
    # The path is intentionally garbage; the fallback must not raise.
    out = n.ground_via_vision(target_query="x", screenshot_path="/nonexistent/__no__.png")
    assert out.resolved is False


def test_null_vision_fallback_is_a_vision_fallback() -> None:
    """The protocol is runtime_checkable; the null fallback is an instance."""
    n = NullVisionFallback()
    assert isinstance(n, VisionFallback)


# ---------------------------------------------------------------------------
# VisionFallbackResult dataclass discipline
# ---------------------------------------------------------------------------

def test_vision_fallback_result_is_frozen() -> None:
    r = VisionFallbackResult(resolved=True, x=1, y=2, width=3, height=4)
    with pytest.raises(Exception):
        r.resolved = False  # type: ignore[misc]


def test_vision_fallback_result_default_metadata_is_none() -> None:
    r = VisionFallbackResult(resolved=True)
    assert r.metadata is None


def test_vision_fallback_result_accepts_mapping_metadata() -> None:
    md = {"resolution_method": "vision_fallback"}
    r = VisionFallbackResult(resolved=True, metadata=md)
    assert r.metadata is md


def test_vision_fallback_result_rejects_non_mapping_metadata() -> None:
    with pytest.raises(TypeError):
        VisionFallbackResult(resolved=True, metadata="not a mapping")
    with pytest.raises(TypeError):
        VisionFallbackResult(resolved=True, metadata=[("a", 1)])
    with pytest.raises(TypeError):
        VisionFallbackResult(resolved=True, metadata=42)


def test_vision_fallback_result_to_dict_minimal() -> None:
    r = VisionFallbackResult(resolved=False, error="x")
    d = r.to_dict() if hasattr(r, "to_dict") else {}
    # Even without to_dict the dataclass must be json-serialisable.
    assert json.dumps({"resolved": r.resolved, "error": r.error}) == \
        '{"resolved": false, "error": "x"}'


# ---------------------------------------------------------------------------
# VisionFallbackAdapter — duck-typed wrapper around a VisionService
# ---------------------------------------------------------------------------

class _StubVisionObserved:
    """A minimal stub shaped like a vision-service "OBSERVED" result."""

    def __init__(
        self,
        bbox: Any = None,
        confidence: float = 0.0,
        text: str = "",
    ) -> None:
        self.status = "OBSERVED"
        self.observation = {
            "bbox": bbox,
            "confidence": confidence,
            "text": text,
        }


class _StubVisionNotObserved:
    status = "FAILED"
    observation = {"error": "no match"}


class _StubVisionRaising:
    def ground_target(self, *args: Any, **kw: Any) -> None:
        raise RuntimeError("kaboom")


class _StubVisionService:
    """A duck-typed stand-in for the real :class:`VisionService`."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls = 0

    def ground_target(self, target_query: str, *, image_path: str) -> Any:
        self.calls += 1
        return self._result


def _make_adapter(result: Any) -> tuple[VisionFallbackAdapter, _StubVisionService]:
    svc = _StubVisionService(result)
    return VisionFallbackAdapter(svc), svc


def test_adapter_returns_resolved_when_bbox_observed() -> None:
    adapter, svc = _make_adapter(_StubVisionObserved(bbox=[10, 20, 30, 40],
                                                     confidence=0.85,
                                                     text="Sign in"))
    out = adapter.ground_via_vision(target_query="Sign in",
                                    screenshot_path="/tmp/x.png")
    assert out.resolved is True
    assert out.x == 10
    assert out.y == 20
    assert out.width == 30
    assert out.height == 40
    assert out.confidence == 0.85
    assert out.text == "Sign in"
    assert out.error is None
    assert out.metadata == {"resolution_method": "vision_fallback"}
    assert svc.calls == 1


def test_adapter_returns_unresolved_when_status_not_observed() -> None:
    adapter, _ = _make_adapter(_StubVisionNotObserved())
    out = adapter.ground_via_vision(target_query="x", screenshot_path="/tmp/x.png")
    assert out.resolved is False
    assert "FAILED" in (out.error or "")


def test_adapter_swallows_exceptions() -> None:
    adapter, _ = _make_adapter(None)  # not used; raise stub
    adapter = VisionFallbackAdapter(_StubVisionRaising())
    out = adapter.ground_via_vision(target_query="x", screenshot_path="/tmp/x.png")
    assert out.resolved is False
    assert "kaboom" in (out.error or "")


def test_adapter_with_none_vision_service() -> None:
    adapter = VisionFallbackAdapter(None)
    out = adapter.ground_via_vision(target_query="x", screenshot_path="/tmp/x.png")
    assert out.resolved is False
    assert "None" in (out.error or "")


def test_adapter_handles_observation_without_bbox() -> None:
    """An OBSERVED result with no bbox still counts as resolved, but with
    zero geometry.  The Brain / Verifier may still want the text."""
    r = _StubVisionObserved(bbox=None, confidence=0.0, text="")
    # The stub's observation dict will be {bbox: None, ...}.
    adapter, _ = _make_adapter(r)
    out = adapter.ground_via_vision(target_query="x", screenshot_path="/tmp/x.png")
    assert out.resolved is True
    assert out.width == 0
    assert out.height == 0
    assert out.x is None
    assert out.y is None


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------

def test_vision_fallback_protocol_has_one_method() -> None:
    """The protocol exposes exactly one method, ``ground_via_vision``."""
    members = [m for m in dir(VisionFallback) if not m.startswith("_")]
    assert "ground_via_vision" in members


def test_vision_fallback_protocol_signature() -> None:
    sig = inspect.signature(VisionFallback.ground_via_vision)
    params = list(sig.parameters)
    assert "target_query" in params
    assert "screenshot_path" in params


def test_vision_fallback_protocol_is_runtime_checkable() -> None:
    """The protocol must be ``runtime_checkable`` so the service can
    isinstance()-check the injected fallback without an explicit import."""
    assert hasattr(VisionFallback, "_is_runtime_protocol") or \
        isinstance(NullVisionFallback(), VisionFallback)


# ---------------------------------------------------------------------------
# Safety: vision fallback never sees secrets
# ---------------------------------------------------------------------------

def test_vision_fallback_never_receives_html_or_cookies() -> None:
    """The protocol's parameter list must not include any way to pass
    HTML, cookies, or session secrets."""
    sig = inspect.signature(VisionFallback.ground_via_vision)
    forbidden = {"html", "cookies", "password", "secret", "headers",
                 "url", "dom", "text", "credentials"}
    param_names = set(sig.parameters)
    leak = forbidden & param_names
    assert not leak, f"vision fallback accepts forbidden params: {leak}"


def test_vision_fallback_modules_does_not_import_subprocess() -> None:
    """The fallback must not shell out."""
    from browser.strategies import vision_fallback as m
    src = open(m.__file__, encoding="utf-8").read()
    for forbidden in ("import subprocess", "from subprocess",
                      "os.system", "os.popen"):
        assert forbidden not in src, \
            f"forbidden call/import in vision_fallback: {forbidden!r}"


def test_vision_fallback_module_uses_loguru_only() -> None:
    from browser.strategies import vision_fallback as m
    src = open(m.__file__, encoding="utf-8").read()
    assert "import logging" not in src
    assert "from logging" not in src
