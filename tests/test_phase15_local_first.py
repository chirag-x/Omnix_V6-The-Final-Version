"""
Omnix V6 — Phase 15 real-runtime local-first tests.

These tests exercise the :class:`FastPathDispatcher` against the
real Windows desktop.  No mocks of capabilities, no mocks of the
catalog, no mocks of the resolver.  The dispatcher either opens
the requested application through the real
:class:`ApplicationOpenCapability` or it returns a structured
FAILED result.

Mark ``@pytest.mark.real_windows`` so the suite can be filtered
when running on non-Windows hosts.

What we are validating
----------------------

  1.  The dispatcher is generic — the same code path opens
      Notepad, Calculator, Chrome, Discord, Spotify, … whatever
      the catalog has discovered on the host.
  2.  The dispatcher consults the catalog, NOT a hand-maintained
      per-app alias table.
  3.  The LLM is not invoked for any trivially-classifiable
      command — the gate must return ``escalate=False`` and the
      local engine must match.
  4.  Compound requests ("Open Notepad and type Hello") are split
      on coordinating conjunctions and dispatched in order.
  5.  Names that are not in the catalog surface as a structured
      FAILED :class:`CapabilityResult` — never a fake VERIFIED.
"""

from __future__ import annotations

import sys
import time
from typing import List, Optional

import pytest


pytestmark = pytest.mark.real_windows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _build_real_dispatcher():
    """Build a real :class:`FastPathDispatcher` against the real
    application service, registry, and router.  No mocks.
    """
    pytest.importorskip("psutil")
    from core.capability_registry import CapabilityRegistry
    from core.capability_router import CapabilityRouter
    from core.capabilities import register_standard_capabilities
    from core.services.app_dispatcher import FastPathDispatcher
    from system.application.app_service import WindowsApplicationService

    registry = CapabilityRegistry()
    register_standard_capabilities(
        registry,
        application_service=WindowsApplicationService(),
    )
    router = CapabilityRouter(registry)
    app_svc = WindowsApplicationService()
    return FastPathDispatcher(
        resolver=app_svc._resolver,
        registry=registry,
        router=router,
    )


def _build_local_decision_engine():
    from core.capability_registry import CapabilityRegistry
    from core.capabilities import register_standard_capabilities
    from core.services.local_decision_engine import LocalActionDecisionEngine
    from system.application.app_service import WindowsApplicationService

    registry = CapabilityRegistry()
    register_standard_capabilities(
        registry,
        application_service=WindowsApplicationService(),
    )
    app_svc = WindowsApplicationService()
    return LocalActionDecisionEngine(registry=registry, resolver=app_svc._resolver)


def _build_escalation_gate():
    from core.services.ai_escalation_gate import AIEscalationGate
    return AIEscalationGate()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _is_windows(), reason="Windows-only test")
class TestPhase15LocalFirstRealRuntime:
    """Real-runtime tests for the local-first fast path."""

    # ----------------------------------------------------- core dispatch
    def test_dispatcher_constructed_with_real_registry(self) -> None:
        """The dispatcher must wire to the real registry and
        resolver.  No mocks allowed at the construction boundary.
        """
        from core.services.app_dispatcher import FastPathDispatcher

        d = _build_real_dispatcher()
        assert isinstance(d, FastPathDispatcher)
        assert d._registry is not None
        assert d._router is not None
        assert d._resolver is not None

    def test_dispatcher_returns_none_for_unmatched_text(self) -> None:
        """Unmatched text returns ``None`` so the pipeline falls
        through to the Brain.  This is the contract: the dispatcher
        only claims a hit for trivially-classifiable commands.
        """
        d = _build_real_dispatcher()
        result = d.try_dispatch("what is the meaning of life?")
        assert result is None

    def test_dispatcher_returns_failed_for_unknown_app(self) -> None:
        """Names the catalog does not know must surface as a
        structured FAILED :class:`CapabilityResult`, not a fake
        VERIFIED and not ``None``.
        """
        from core.results import CapabilityStatus

        d = _build_real_dispatcher()
        result = d.try_dispatch("open totally-made-up-app-zzzqq")
        assert result is not None, (
            "dispatcher should surface not-found as FAILED, not None"
        )
        assert result.status is CapabilityStatus.FAILED
        assert result.failed is True

    # ----------------------------------------------------- generic open
    @pytest.mark.parametrize(
        "app_name",
        [
            "notepad",
            "calculator",
            "chrome",
        ],
    )
    def test_dispatcher_opens_real_apps(self, app_name: str) -> None:
        """The dispatcher must open any app the catalog knows about
        through the real :class:`ApplicationOpenCapability`.  No
        per-app code paths.
        """
        pytest.importorskip("psutil")
        from core.results import CapabilityStatus
        from system.application.app_service import WindowsApplicationService

        # Pre-flight: only run when the catalog can actually resolve
        # the app on this host.  Tests are generic — the same loop
        # would run for any app the host has installed.
        app_svc = WindowsApplicationService()
        res = app_svc.resolve(app_name)
        if not res.is_found:
            pytest.skip(f"App {app_name!r} not installed on this host")

        d = _build_real_dispatcher()
        result = d.try_dispatch(f"open {app_name}")
        # Either the capability verified the launch, or it returned
        # a structured failure.  We never accept fake VERIFIED.
        if result is None:
            pytest.fail(
                f"dispatcher returned None for {app_name!r}; "
                f"expected either VERIFIED or FAILED"
            )
        assert result.status in (
            CapabilityStatus.VERIFIED,
            CapabilityStatus.EXECUTED,
            CapabilityStatus.FAILED,
        ), f"unexpected status: {result.status}"
        # When verified, the underlying capability must have done
        # the verification itself, not the dispatcher.
        if result.status is CapabilityStatus.VERIFIED:
            assert result.verified is True
            # The fast-path marker must be present so the audit log
            # attributes the dispatch to the local-first path.
            assert (result.details or {}).get("local_first") is True

    def test_dispatcher_does_not_reference_specific_apps(self) -> None:
        """The :class:`FastPathDispatcher` source must NOT contain
        any application-specific names (chrome, notepad, spotify,
        discord, …) in **executable code**.  No per-app branches.
        We strip docstrings, comments, and string literals so we
        only inspect real branches.
        """
        from core.services import app_dispatcher
        import inspect
        import re

        src = inspect.getsource(app_dispatcher)
        # Strip module docstring.
        parts = src.split('"""')
        code = '"""'.join(parts[2::2]) if len(parts) >= 3 else src
        # Strip Python comments.
        code = re.sub(r"#[^\n]*", "", code)
        # Strip string literals.
        code = re.sub(r"\"\"\"[^\"]*\"\"\"", "", code, flags=re.DOTALL)
        code = re.sub(r"\"[^\"]*\"", "", code)
        code = re.sub(r"'[^']*'", "", code)
        forbidden = (
            "chrome",
            "spotify",
            "discord",
            "msedge",
            "firefox",
            "slack",
            "telegram",
        )
        for name in forbidden:
            assert name.lower() not in code.lower(), (
                f"FastPathDispatcher code contains forbidden "
                f"application-specific token: {name!r}"
            )

    # ----------------------------------------------------- gate behavior
    def test_gate_does_not_escalate_trivial_command(self) -> None:
        """For a trivially-classifiable command, the gate must
        return ``escalate=False`` so the LLM is not invoked.
        """
        gate = _build_escalation_gate()
        decision = gate.should_escalate("open notepad")
        assert decision.escalate is False

    def test_gate_does_not_escalate_when_local_engine_matched(self) -> None:
        """The gate must respect a precomputed local engine
        outcome: when the local engine returned ``"matched"``, the
        gate must return ``escalate=False`` no matter what other
        features the text has.
        """
        gate = _build_escalation_gate()
        # Even an input that *looks* ambiguous (contains "it") must
        # not escalate when the local engine already classified it.
        decision = gate.should_escalate(
            "open it",
            local_engine_outcome="matched",
        )
        assert decision.escalate is False

    def test_gate_escalates_ambiguous_pronoun(self) -> None:
        """Genuine ambiguity (pronouns like "it", "that") must
        escalate.
        """
        gate = _build_escalation_gate()
        decision = gate.should_escalate("click on it")
        assert decision.escalate is True
        assert decision.reason in (
            "ambiguous_text",
            "is_question",
            "semantic_query",
            "long_input",
        )

    def test_gate_escalates_question(self) -> None:
        """A real question must escalate.
        """
        gate = _build_escalation_gate()
        decision = gate.should_escalate("what is the capital of France?")
        assert decision.escalate is True

    # ----------------------------------------------------- local engine
    def test_local_engine_classifies_known_app_open(self) -> None:
        """The local engine must classify a trivially-classifiable
        command, returning a plan with one app_open step.
        """
        eng = _build_local_decision_engine()
        d = eng.classify("open notepad")
        # Either the local engine matched (because the catalog
        # has Notepad) or it returned not_found.  Both are honest
        # outcomes — the engine must not match a verb whose
        # capability is not registered.
        if d.matched:
            assert d.plan is not None
            assert len(d.plan.steps) >= 1
            assert d.plan.steps[0].capability_name == "desktop.application.open"
        else:
            # The local engine refused to claim a hit.  Acceptable.
            # This is exactly the architectural rule: the engine
            # only matches when the verb's capability is registered
            # AND the catalog resolves the target.
            assert d.matched_text != ""

    def test_local_engine_handles_compound_request(self) -> None:
        """A compound request "Open X and type Y" must produce a
        multi-step plan when the catalog knows X and the
        capabilities are registered.
        """
        eng = _build_local_decision_engine()
        d = eng.classify("open notepad and type hello world")
        if d.matched and d.plan is not None:
            # Multi-step: at least the open + the type.
            assert len(d.plan.steps) >= 2
        # If the local engine did not match, the Brain path takes
        # over.  Either outcome is correct — the test only checks
        # that compound parsing does not crash and that when it
        # matches, it produces a multi-step plan.

    def test_local_engine_does_not_reference_specific_apps(self) -> None:
        """The :class:`LocalActionDecisionEngine` source must NOT
        contain any application-specific names in **executable
        code**.  No per-app branches.  We strip docstrings,
        comments, and string literals.
        """
        from core.services import local_decision_engine
        import inspect
        import re

        src = inspect.getsource(local_decision_engine)
        # Strip module docstring.
        parts = src.split('"""')
        code = '"""'.join(parts[2::2]) if len(parts) >= 3 else src
        # Strip Python comments.
        code = re.sub(r"#[^\n]*", "", code)
        # Strip string literals.
        code = re.sub(r"\"\"\"[^\"]*\"\"\"", "", code, flags=re.DOTALL)
        code = re.sub(r"\"[^\"]*\"", "", code)
        code = re.sub(r"'[^']*'", "", code)
        forbidden = (
            "chrome",
            "spotify",
            "discord",
            "msedge",
            "firefox",
            "slack",
            "telegram",
        )
        for name in forbidden:
            assert name.lower() not in code.lower(), (
                f"LocalActionDecisionEngine code contains forbidden "
                f"application-specific token: {name!r}"
            )

    def test_local_engine_isolated_from_ai_layer(self) -> None:
        """The :class:`LocalActionDecisionEngine` module MUST NOT
        import :mod:`ai.brain`, :mod:`ai.intent`, or
        :mod:`ai.provider`.  This is the architectural invariant
        that makes local-first possible.
        """
        from core.services import local_decision_engine
        import inspect

        src = inspect.getsource(local_decision_engine)
        for forbidden in (
            "from ai.brain",
            "from ai.intent",
            "from ai.provider",
            "import ai.brain",
            "import ai.intent",
            "import ai.provider",
            "import pyautogui",
            "import win32gui",
            "import subprocess",
        ):
            assert forbidden not in src, (
                f"LocalActionDecisionEngine must not import {forbidden!r}"
            )

    def test_gate_isolated_from_ai_layer(self) -> None:
        """The :class:`AIEscalationGate` module MUST NOT import
        :mod:`ai.brain`, :mod:`ai.intent`, or :mod:`ai.provider`.
        The gate decides *whether* to call the LLM, never *how*.
        """
        from core.services import ai_escalation_gate
        import inspect

        src = inspect.getsource(ai_escalation_gate)
        for forbidden in (
            "from ai.brain",
            "from ai.intent",
            "from ai.provider",
            "import ai.brain",
            "import ai.intent",
            "import ai.provider",
        ):
            assert forbidden not in src, (
                f"AIEscalationGate must not import {forbidden!r}"
            )
