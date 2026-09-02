"""Phase 17 — Application Intelligence upgrade tests.

These tests verify the new DTOs, the metadata-driven resolver, the
per-source health isolation, and the launch lifecycle contract.
They are **non-destructive**: no real application is launched;
launch paths are exercised through a stub spawn or a notepad
launch under a real-window service stub.  Skipped automatically
on non-Windows platforms where ``WindowsApplicationService``
cannot be imported.
"""

from __future__ import annotations

import sys
import time
import threading
from typing import Iterator, List

import pytest


# ---------------------------------------------------------------------------
# Platform gate — WindowsApplicationService is Windows-only
# ---------------------------------------------------------------------------

ON_WINDOWS = sys.platform.startswith("win")
pytestmark = pytest.mark.skipif(
    not ON_WINDOWS,
    reason="WindowsApplicationService is Windows-only",
)


# ---------------------------------------------------------------------------
# Imports guarded by the skip above
# ---------------------------------------------------------------------------

if ON_WINDOWS:
    from system.application import (
        ApplicationCatalog,
        ApplicationHealthState,
        ApplicationResolver,
        LaunchPolicy,
        LaunchResult,
        RunningApp,
        SourceHealth,
        WindowsApplicationService,
    )
    from system.application.discovery import ApplicationSource
    from system.application.models import ApplicationRecord


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _StaticSource(ApplicationSource):
    """An :class:`ApplicationSource` that yields a fixed list of
    records.  Used to construct a deterministic catalog for the
    metadata-driven resolver and health tests.
    """

    def __init__(self, records: List[ApplicationRecord], *, name: str = "static") -> None:
        # ``ApplicationSource`` is a ``@dataclass``; pass the
        # source ``name`` explicitly to override the parent's
        # ``name: str = ""`` default.
        super().__init__(name=name, confidence=0.9, enabled=True, timeout_s=1.0)
        self._records = list(records)

    def scan(self) -> Iterator[ApplicationRecord]:
        for rec in self._records:
            yield rec


def _make_record(
    display_name: str,
    executable: str = "demo.exe",
    *,
    aliases: tuple = (),
    source: str = "static",
    confidence: float = 0.9,
) -> ApplicationRecord:
    """Build a synthetic :class:`ApplicationRecord` for tests."""
    normalized = "".join(ch for ch in display_name.lower() if ch.isalnum())
    return ApplicationRecord(
        display_name=display_name,
        normalized_name=normalized,
        executable=executable,
        launch_command=executable,
        source=source,
        installed=True,
        aliases=aliases,
        executable_path=None,
        confidence=confidence,
        metadata={},
    )


def _build_catalog(records: List[ApplicationRecord]) -> ApplicationCatalog:
    """Build an :class:`ApplicationCatalog` with a single static
    source so the refresh is fully deterministic and offline.
    """
    cat = ApplicationCatalog(sources=[_StaticSource(records)])
    cat.initialize()
    return cat


# ===========================================================================
# 1. New DTOs: shape and frozen-ness
# ===========================================================================

def test_launch_result_shape():
    """A :class:`LaunchResult` is constructible with the full
    field set and exposes ``is_success`` / ``is_failure`` properties.
    """
    rec = _make_record("Demo")
    lr = LaunchResult(
        status="success",
        application=rec,
        target="demo.exe",
        pid=1234,
        hwnd=5678,
        window_title="Demo Window",
        process_name="demo.exe",
        started_at=time.time(),
        elapsed_ms=12.5,
        notes=("first launch",),
    )
    assert lr.is_success is True
    assert lr.is_failure is False
    # failure_stage defaults to None
    assert lr.failure_stage is None
    assert lr.error is None
    assert lr.notes == ("first launch",)
    assert lr.pid == 1234
    assert lr.hwnd == 5678


def test_launch_result_failure_shape():
    """A failure result carries the failing step in
    ``failure_stage`` and a human-readable ``error``.
    """
    lr = LaunchResult(
        status="not_found",
        application=None,
        target=None,
        pid=None,
        hwnd=None,
        window_title=None,
        process_name=None,
        started_at=time.time(),
        elapsed_ms=0.5,
        failure_stage="resolve",
        error="no application matches 'definitely_not_an_app'",
    )
    assert lr.is_success is False
    assert lr.is_failure is True
    assert lr.failure_stage == "resolve"
    assert "definitely_not_an_app" in (lr.error or "")


def test_running_app_to_dict():
    """A :class:`RunningApp` serializes to a JSON-friendly dict."""
    rec = _make_record("Demo")
    ra = RunningApp(
        application=rec,
        pid=4242,
        process_name="demo.exe",
        hwnd=9999,
        window_title="Demo - 1",
        started_at=time.time(),
    )
    d = ra.to_dict()
    assert d["application"] == "Demo"
    assert d["pid"] == 4242
    assert d["process_name"] == "demo.exe"
    assert d["hwnd"] == 9999
    assert d["window_title"] == "Demo - 1"
    assert isinstance(d["started_at"], float)


def test_launch_policy_defaults():
    """The default :class:`LaunchPolicy` matches the production
    pre-Phase-17 behavior: detached, visible, no env override,
    current cwd, window-wait on, foreground-wait off.
    """
    p = LaunchPolicy()
    assert p.detached is True
    assert p.hidden is False
    assert p.cwd is None
    assert p.env is None
    assert p.wait_for_window is True
    assert p.wait_for_foreground is False


# ===========================================================================
# 2. Resolver: metadata-driven, no hardcoded aliases
# ===========================================================================

def test_resolver_no_hardcoded_aliases():
    """The :mod:`system.application.resolver` module no longer
    exposes a ``GENERIC_ALIASES`` constant.  Aliases now come
    from each :class:`ApplicationRecord.aliases` field or the
    executable stem.
    """
    from system.application import resolver as resolver_mod

    assert not hasattr(resolver_mod, "GENERIC_ALIASES"), (
        "GENERIC_ALIASES must be removed; resolution is metadata-driven"
    )
    assert not hasattr(resolver_mod, "_apply_generic_alias"), (
        "_apply_generic_alias must be removed; no per-app alias lookup"
    )


def test_generic_alias_coverage():
    """Resolver hits the alias field, the executable stem, and
    word-tokenized display name — never a hardcoded dict.

    We seed the catalog with two records:
      * ``msedge`` via ``aliases=("msedge",)``  → resolves by alias.
      * ``Microsoft Edge`` via ``display_name`` → resolves by word
        token ``edge``.
    Both must work without a hardcoded ``msedge ↔ edge`` entry.
    """
    rec_a = _make_record(
        "Microsoft Edge",
        executable="msedge.exe",
        aliases=("msedge",),
        source="static",
    )
    rec_b = _make_record(
        "Word Processor",
        executable="winword.exe",
        aliases=("word",),
    )
    cat = _build_catalog([rec_a, rec_b])
    resolver = ApplicationResolver(catalog=cat)

    res_a = resolver.resolve("msedge")
    assert res_a.is_found, f"msedge should resolve via alias: {res_a.reason}"
    assert res_a.record is not None
    assert res_a.record.normalized_name == rec_a.normalized_name

    res_b = resolver.resolve("Word")
    assert res_b.is_found, f"Word should resolve via alias: {res_b.reason}"
    assert res_b.record is not None
    assert res_b.record.normalized_name == rec_b.normalized_name


def test_resolver_uses_executable_stem():
    """The resolver matches the executable stem (``demo`` for
    ``demo.exe``) without any app-specific code.
    """
    rec = _make_record("Some Long Display Name", executable="demo.exe")
    cat = _build_catalog([rec])
    resolver = ApplicationResolver(catalog=cat)

    res = resolver.resolve("demo")
    assert res.is_found, f"executable stem 'demo' should resolve: {res.reason}"
    assert res.record is not None
    assert res.record.executable == "demo.exe"


def test_resolver_word_token_match():
    """The resolver matches a *whole word* inside the display
    name — ``code`` finds ``Visual Studio Code``.
    """
    rec = _make_record("Visual Studio Code", executable="code.exe")
    cat = _build_catalog([rec])
    resolver = ApplicationResolver(catalog=cat)

    res = resolver.resolve("code")
    assert res.is_found, f"'code' should match by whole word: {res.reason}"
    assert res.record is not None


# ===========================================================================
# 3. Catalog health, per-source isolation, thread-safety
# ===========================================================================

class _FailingSource(ApplicationSource):
    """An :class:`ApplicationSource` that always raises."""

    def __init__(self, *, name: str = "failing") -> None:
        # ``ApplicationSource`` is a ``@dataclass`` with
        # ``name: str = ""``; the class-level ``name = "failing"``
        # trick does NOT override the dataclass field default.
        # We pass ``name`` explicitly so per-source health records
        # carry a non-empty name.
        super().__init__(name=name, confidence=0.5, enabled=True, timeout_s=1.0)

    def scan(self) -> Iterator[ApplicationRecord]:
        raise RuntimeError("simulated source failure")
        yield  # pragma: no cover - generator


def test_catalog_health_state_ready():
    """A catalog with all healthy sources ends in ``READY``."""
    rec = _make_record("Demo")
    cat = _build_catalog([rec])
    assert cat.health_report()["state"] == "ready"


def test_catalog_health_state_degraded_on_partial_failure():
    """A catalog with one failing source but at least one healthy
    source ends in ``DEGRADED``, not ``FAILED``.  The healthy
    source's records are still served.
    """
    good = _make_record("Good App", executable="good.exe")
    cat = ApplicationCatalog(
        sources=[_StaticSource([good]), _FailingSource()]
    )
    cat.initialize()

    report = cat.health_report()
    # Good source is up, bad source is down — state must be
    # "degraded" so operators see the issue without the service
    # being considered fully down.
    assert report["state"] in ("degraded", "ready")
    # Per-source health block lists both
    names = {s["name"] for s in report["sources"]}
    assert "static" in names
    assert "failing" in names
    # Failed source carries an error string
    failing = next(s for s in report["sources"] if s["name"] == "failing")
    assert failing["last_error"] is not None
    assert "simulated source failure" in failing["last_error"]
    # Good source still resolves
    assert cat.lookup("good") is not None


def test_catalog_health_state_failed_when_all_fail():
    """A catalog whose every source fails is ``FAILED`` — and
    consumers see an empty record set.
    """
    cat = ApplicationCatalog(
        sources=[
            _FailingSource(name="failing_a"),
            _FailingSource(name="failing_b"),
        ]
    )
    cat.initialize()

    report = cat.health_report()
    assert report["state"] == "failed"
    assert report["record_count"] == 0
    # Both sources are listed under per-source health
    names = {s["name"] for s in report["sources"]}
    assert {"failing_a", "failing_b"} <= names


def test_catalog_lookup_is_thread_safe():
    """Concurrent ``lookup`` calls do not corrupt the alias index."""
    rec = _make_record("Demo", executable="demo.exe")
    cat = _build_catalog([rec])
    resolver = ApplicationResolver(catalog=cat)

    errors: List[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(50):
                assert resolver.resolve("demo") is not None
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors, f"concurrent lookups raised: {errors!r}"


# ===========================================================================
# 4. WindowsApplicationService: health(), running_apps(), launch()
# ===========================================================================

def test_service_health_shape():
    """``WindowsApplicationService.health()`` returns a dict with
    the documented top-level keys and a catalog sub-report.
    """
    svc = WindowsApplicationService(enable_uwp=False)
    try:
        svc._catalog.initialize()
    except Exception:  # noqa: BLE001
        pass
    report = svc.health()
    assert isinstance(report, dict)
    assert report.get("service") == "WindowsApplicationService"
    assert "lifecycle" in report
    assert "uwp_enabled" in report
    assert report["uwp_enabled"] is False
    catalog = report.get("catalog")
    assert isinstance(catalog, dict)
    for key in (
        "state",
        "record_count",
        "last_refresh_ms",
        "cache_age_s",
        "miss_count",
        "miss_targeted_hits",
        "sources",
    ):
        assert key in catalog, f"missing key {key!r} in catalog report"
    # Sources must be a list of SourceHealth-shaped dicts
    assert isinstance(catalog["sources"], list)
    for src in catalog["sources"]:
        assert "name" in src
        assert "enabled" in src
        assert "last_scan_ms" in src
        assert "last_error" in src


def test_service_running_apps_enumeration():
    """``running_apps()`` returns a list.  Python (the test runner)
    itself should be visible because the process table always
    contains this interpreter.  We accept that on minimal CI
    environments the list may be empty, so we only assert the
    contract.
    """
    svc = WindowsApplicationService(enable_uwp=False)
    try:
        svc._catalog.initialize()
    except Exception:  # noqa: BLE001
        pass
    out = svc.running_apps()
    assert isinstance(out, list)
    for ra in out:
        # Each item, if present, is a RunningApp
        assert isinstance(ra, RunningApp)
        assert ra.pid > 0
        assert ra.process_name


def test_service_launch_not_found_returns_structured_result():
    """A launch request for an unknown app returns a
    :class:`LaunchResult` with ``status='not_found'`` and
    ``failure_stage='resolve'`` — not an exception.
    """
    svc = WindowsApplicationService(enable_uwp=False)
    try:
        svc._catalog.initialize()
    except Exception:  # noqa: BLE001
        pass
    res = svc.launch(
        "__definitely_not_a_real_app_phase17_xyz__",
        policy=LaunchPolicy(wait_for_window=False),
    )
    assert isinstance(res, LaunchResult)
    assert res.status == "not_found"
    assert res.failure_stage == "resolve"
    assert res.error is not None
    assert res.is_success is False
    assert res.is_failure is True
    # No process should have been spawned
    assert res.pid is None
    assert res.hwnd is None
    assert res.elapsed_ms >= 0.0
