"""
Omnix V6 — Phase 12 (REAL AUTOMATION EXECUTION LAYER) tests.

These tests exercise the Phase 12 deliverables:

  * Real capability execution via the canonical Router.
  * Filesystem create / list / delete capabilities (filesystem.py).
  * Process verification capability (process.is_running).
  * DeterministicPlanner's ``file_delete`` mapping to ``file.delete``.
  * CapabilityRouter's parameter coercion (the dict-vs-tuple fix).
  * PlanExecutor publishing the canonical
    ``REQUEST_ACTION_EXECUTED`` /
    ``REQUEST_OBSERVATION_CAPTURED`` events when an event bus is
    wired in.
  * End-to-end: the registered standard capability set is
    fully dispatchable through the canonical Router for every
    Phase 12 capability.

The tests are deterministic: no LLM, no real subprocess, no real
Windows UI.  Some are smoke tests gated by ``os.name == 'nt'``;
they run on the host and pass without producing side effects
beyond a small temporary directory.

A pytest collection pass that finds these tests green is a
*necessary* signal that the Phase 12 wiring is consistent, but it
is not a proof that real Windows automation works -- the
``scripts/phase12_real_windows_smoke.py`` script exists for that.
"""

from __future__ import annotations

import os
import sys
import tempfile
import asyncio
import types
from typing import Any, Dict, List, Mapping

import pytest

from core.capability import (
    CapabilityParameter,
    CapabilitySpec,
    ParamType,
)
from core.capability_registry import CapabilityRegistry
from core.capability_router import (
    AllowAllSafetyPolicy,
    CapabilityRouter,
    SafetyPolicy,
)
from core.capabilities.filesystem import (
    DirectoryListCapability,
    FileCreateCapability,
    FileDeleteCapability,
    FileReadCapability,
    FileWriteCapability,
    FolderCreateCapability,
    _is_under_reserved_dir,
)
from core.capabilities.process import (
    ProcessIsRunningCapability,
    _is_running_on_windows,
)
from core.results import (
    CapabilityResult,
    CapabilityStatus,
)
from core.events.event_types import (
    RequestEvent,
    REQUEST_ACTION_EXECUTED,
    REQUEST_OBSERVATION_CAPTURED,
    REQUEST_RECOVERY_STARTED,
)  # noqa: F401 — stages used via string pattern in bus subscribe
from core.events.event_bus import EventBus


# ---------------------------------------------------------------------------
# 1. Parameter coercion: CapabilitySpec.parameters as a dict MUST work
# ---------------------------------------------------------------------------

class _RejectUnknown(SafetyPolicy):
    """A safety policy that accepts everything (used to isolate
    parameter-coercion logic from the safety layer in this test)."""

    def is_authorized(self, capability_name, params, request) -> bool:  # noqa: D401
        return True


def _build_router() -> CapabilityRouter:
    registry = CapabilityRegistry()
    registry.register(FileReadCapability())
    registry.register(FileWriteCapability())
    registry.register(FileCreateCapability())
    registry.register(FolderCreateCapability())
    registry.register(FileDeleteCapability())
    registry.register(DirectoryListCapability())
    return CapabilityRouter(registry=registry, safety_policy=_RejectUnknown())


def test_capability_spec_parameters_dict_is_supported_by_coerce():
    """Phase 12 fix: spec.parameters as a dict (the existing pattern)
    must be acceptable to ``coerce_parameters``.
    """
    from core.capability import coerce_parameters
    spec = FileReadCapability().spec
    assert isinstance(spec.parameters, dict)
    out = coerce_parameters(spec, {"path": r"C:\tmp\foo.txt"})
    assert out == {"path": r"C:\tmp\foo.txt"}


def test_capability_spec_parameters_dict_rejects_unknown():
    """Unknown keys must still raise ValidationError when params is a dict."""
    from core.capability import coerce_parameters
    from core.errors import ValidationError
    spec = FileReadCapability().spec
    with pytest.raises(ValidationError):
        coerce_parameters(spec, {"path": r"C:\tmp\foo.txt", "unknown": 1})


def test_capability_spec_parameters_dict_coerces_types():
    """Boolean parameter declared via dict form must be coerced."""
    from core.capability import coerce_parameters
    spec = FileCreateCapability().spec
    out = coerce_parameters(spec, {"path": r"C:\tmp\foo.txt", "overwrite": "true"})
    assert out["overwrite"] is True
    assert out["path"] == r"C:\tmp\foo.txt"


# ---------------------------------------------------------------------------
# 2. Filesystem create / folder.create / delete / list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_create_creates_empty_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fresh.txt")
        cap = FileCreateCapability()
        result = await cap.execute({"path": path})
        assert result.status == CapabilityStatus.VERIFIED
        assert os.path.exists(path)
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == ""


@pytest.mark.asyncio
async def test_file_create_refuses_overwrite_without_flag():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fresh.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("seed")
        cap = FileCreateCapability()
        result = await cap.execute({"path": path})
        assert result.status == CapabilityStatus.FAILED
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == "seed"  # not overwritten


@pytest.mark.asyncio
async def test_file_create_overwrite_true_replaces_content():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "fresh.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("seed")
        cap = FileCreateCapability()
        result = await cap.execute({"path": path, "overwrite": True})
        assert result.status == CapabilityStatus.VERIFIED
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == ""


@pytest.mark.asyncio
async def test_folder_create_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "tree", "leaf")
        cap = FolderCreateCapability()
        r1 = await cap.execute({"path": path})
        r2 = await cap.execute({"path": path})
        assert r1.status == CapabilityStatus.VERIFIED
        assert r2.status == CapabilityStatus.VERIFIED
        assert os.path.isdir(path)


@pytest.mark.asyncio
async def test_folder_create_refuses_over_existing_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "thing")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        cap = FolderCreateCapability()
        result = await cap.execute({"path": path})
        assert result.status == CapabilityStatus.FAILED


@pytest.mark.asyncio
async def test_file_delete_removes_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "victim.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        cap = FileDeleteCapability()
        result = await cap.execute({"path": path})
        assert result.status == CapabilityStatus.VERIFIED
        assert not os.path.exists(path)


@pytest.mark.asyncio
async def test_file_delete_refuses_non_empty_directory():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "dir")
        os.makedirs(path)
        with open(os.path.join(path, "child"), "w") as f:
            f.write("x")
        cap = FileDeleteCapability()
        result = await cap.execute({"path": path})
        assert result.status == CapabilityStatus.FAILED
        assert os.path.isdir(path)  # not removed


@pytest.mark.asyncio
async def test_file_delete_refuses_reserved_system_paths():
    cap = FileDeleteCapability()
    # C:\Windows\System32 is a canonical reserved path.
    result = await cap.execute({"path": r"C:\Windows\System32"})
    assert result.status == CapabilityStatus.FAILED
    assert "reserved" in str(result.error).lower()


def test_is_under_reserved_dir_helper_flags_system32():
    assert _is_under_reserved_dir(r"C:\Windows\System32\drivers\etc\hosts")
    assert not _is_under_reserved_dir(r"C:\Users\chira\Desktop\note.txt")


@pytest.mark.asyncio
async def test_directory_list_returns_entries_sorted():
    with tempfile.TemporaryDirectory() as d:
        for name in ("c.txt", "a.txt", "b.txt"):
            with open(os.path.join(d, name), "w") as f:
                f.write("x")
        cap = DirectoryListCapability()
        result = await cap.execute({"path": d})
        assert result.status == CapabilityStatus.VERIFIED
        assert result.details["entries"] == ["a.txt", "b.txt", "c.txt"]
        assert result.details["count"] == 3


@pytest.mark.asyncio
async def test_directory_list_excludes_hidden_by_default():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "visible.txt"), "w") as f:
            f.write("x")
        with open(os.path.join(d, ".hidden"), "w") as f:
            f.write("x")
        cap = DirectoryListCapability()
        result = await cap.execute({"path": d})
        assert result.status == CapabilityStatus.VERIFIED
        assert ".hidden" not in result.details["entries"]
        # And include_hidden=True brings it back.
        result2 = await cap.execute({"path": d, "include_hidden": True})
        assert ".hidden" in result2.details["entries"]


# ---------------------------------------------------------------------------
# 3. Process observation (read-only verification)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_is_running_reports_pid_for_running_process():
    # The current Python interpreter is, by definition, running.
    cap = ProcessIsRunningCapability()
    result = await cap.execute({"name": sys.executable})
    assert result.status == CapabilityStatus.VERIFIED
    assert result.details["running"] is True
    assert len(result.details["pids"]) >= 1
    assert os.getpid() in result.details["pids"]


@pytest.mark.asyncio
async def test_process_is_running_reports_not_running_cleanly():
    cap = ProcessIsRunningCapability()
    result = await cap.execute({"name": "definitely-not-a-real-process-omnix-v6"})
    assert result.status == CapabilityStatus.VERIFIED
    assert result.details["running"] is False
    assert result.details["pids"] == []


@pytest.mark.asyncio
async def test_process_is_running_requires_name():
    cap = ProcessIsRunningCapability()
    result = await cap.execute({})
    assert result.status == CapabilityStatus.FAILED
    assert "name" in str(result.error).lower()


def test_process_helper_returns_structured_result():
    res = _is_running_on_windows(sys.executable)
    assert res["running"] is True
    assert isinstance(res["pids"], list)


# ---------------------------------------------------------------------------
# 4. Router: the canonical dispatch path for Phase 12 capabilities
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_dispatches_file_create():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "via-router.txt")
        router = _build_router()
        result = router.route("file.create", {"path": path})
        assert result.status == CapabilityStatus.VERIFIED
        assert os.path.exists(path)


@pytest.mark.asyncio
async def test_router_dispatches_directory_list():
    with tempfile.TemporaryDirectory() as d:
        router = _build_router()
        result = router.route("directory.list", {"path": d})
        assert result.status == CapabilityStatus.VERIFIED
        assert "entries" in result.details


@pytest.mark.asyncio
async def test_router_dispatches_folder_create():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "newfolder")
        router = _build_router()
        result = router.route("folder.create", {"path": path})
        assert result.status == CapabilityStatus.VERIFIED
        assert os.path.isdir(path)


@pytest.mark.asyncio
async def test_router_rejects_relative_path_for_file_create():
    router = _build_router()
    result = router.route("file.create", {"path": "relative/foo.txt"})
    assert result.status == CapabilityStatus.FAILED
    assert result.failed is True


# ---------------------------------------------------------------------------
# 5. DeterministicPlanner: file_delete maps to the new file.delete
# ---------------------------------------------------------------------------

def test_deterministic_planner_maps_file_delete_to_file_delete_capability():
    from core.capability_registry import CapabilityRegistry
    from ai.brain.deterministic import DeterministicPlanner
    from core.orchestration import Goal, Intent, IntentKind

    registry = CapabilityRegistry()
    registry.register(FileReadCapability())
    registry.register(FileDeleteCapability())
    planner = DeterministicPlanner(registry=registry)

    target = (
        r"C:\Users\chira\Desktop\phase12-victim.txt"
    )
    goal = Goal(
        goal_id="g1",
        description="delete file",
        metadata={"intent_kind": "file_delete"},
    )
    intent = Intent(
        intent_id="i1",
        kind=IntentKind.FILE_DELETE,
        text="delete the file",
        parameters={"path": target},
    )
    plan = planner.plan(goal, intent=intent)
    assert plan.step_count == 1
    step = plan.steps[0]
    assert step.capability_name == "file.delete"
    assert step.parameters.get("path") == target


def test_deterministic_planner_refuses_file_delete_without_path():
    from core.capability_registry import CapabilityRegistry
    from ai.brain.deterministic import DeterministicPlanner
    from ai.brain.exceptions import CannotPlanError
    from core.orchestration import Goal, Intent, IntentKind

    registry = CapabilityRegistry()
    registry.register(FileReadCapability())
    registry.register(FileDeleteCapability())
    planner = DeterministicPlanner(registry=registry)
    goal = Goal(
        goal_id="g2",
        description="delete file",
        metadata={"intent_kind": "file_delete"},
    )
    intent = Intent(
        intent_id="i2",
        kind=IntentKind.FILE_DELETE,
        text="delete the file",
        parameters={},
    )
    with pytest.raises(CannotPlanError):
        planner.plan(goal, intent=intent)


# ---------------------------------------------------------------------------
# 6. PlanExecutor publishes canonical ACTION_EXECUTED / OBSERVATION
# ---------------------------------------------------------------------------

def _build_minimal_caps_registry():
    """Construct a registry with one trivially-succeeding capability."""
    from core.capability import CallableCapability
    registry = CapabilityRegistry()

    def _ok(params: Mapping[str, Any]) -> CapabilityResult:
        return CapabilityResult(
            capability_name="trivial.ok",
            status=CapabilityStatus.VERIFIED,
            attempted=True,
            executed=True,
            verified=True,
            details={"echo": dict(params)},
        )

    spec = CapabilitySpec(
        name="trivial.ok",
        version="1.0.0",
        description="trivial verified capability",
        parameters=(
            CapabilityParameter(
                name="value",
                type=ParamType.STRING,
                required=False,
                default="",
            ),
        ),
        tags=("test",),
    )
    registry.register(CallableCapability(spec=spec, fn=_ok))
    return registry


def test_plan_executor_publishes_action_executed_to_bus():
    from core.orchestration import (
        ActionKind,
        ExecutionContext,
        Goal,
        Plan,
        PlanStep,
        PlanStatus,
        PlanExecutorImpl,
    )
    from core.orchestration.execution_result import new_correlation_id

    bus = EventBus(name="phase12-test-bus")
    seen_stages: List[str] = []
    bus.subscribe("request.event", lambda evt: seen_stages.append(getattr(evt, "stage", "")))

    registry = _build_minimal_caps_registry()
    router = CapabilityRouter(registry=registry, safety_policy=AllowAllSafetyPolicy())
    executor = PlanExecutorImpl(router=router, event_bus=bus)

    plan = Plan(
        plan_id="p1",
        goal_id="g1",
        status=PlanStatus.DRAFT,
        steps=(
            PlanStep(
                step_id="s1",
                description="trivial",
                action=ActionKind.CAPABILITY_CALL,
                capability_name="trivial.ok",
                parameters={"value": "hello"},
            ),
        ),
    )
    goal = Goal(goal_id="g1", description="trivial goal")
    ctx = ExecutionContext(
        execution_id="exec-1",
        plan=plan,
        goal=goal,
        completed_step_ids=set(),
        failed_step_ids=set(),
    )
    result = executor.execute(ctx)
    assert result.succeeded_step_count == 1
    assert REQUEST_ACTION_EXECUTED in seen_stages
    assert REQUEST_OBSERVATION_CAPTURED in seen_stages


def test_plan_executor_publish_recovery_started_helper_emits_event():
    from core.orchestration import PlanExecutorImpl
    from core.capability_registry import CapabilityRegistry
    from core.capability_router import CapabilityRouter, AllowAllSafetyPolicy

    bus = EventBus(name="phase12-test-bus-2")
    seen: List[RequestEvent] = []
    bus.subscribe("request.event", lambda evt: seen.append(evt))

    registry = CapabilityRegistry()
    router = CapabilityRouter(registry=registry, safety_policy=AllowAllSafetyPolicy())
    executor = PlanExecutorImpl(router=router, event_bus=bus)
    executor.publish_recovery_started(
        correlation_id="cid-1",
        plan_id="p1",
        reason="step failed",
        attempt=2,
    )
    assert any(
        getattr(e, "stage", "") == REQUEST_RECOVERY_STARTED for e in seen
    )


# ---------------------------------------------------------------------------
# 7. CapabilityRouter: the standard registered set is fully wired
# ---------------------------------------------------------------------------

def test_phase12_capabilities_are_registered_in_standard_set():
    from core.capabilities import register_standard_capabilities
    registry = CapabilityRegistry()
    register_standard_capabilities(registry)
    for name in (
        "file.create",
        "folder.create",
        "file.delete",
        "directory.list",
        "process.is_running",
    ):
        assert registry.has(name), f"missing capability: {name}"


def test_file_delete_capability_is_marked_dangerous():
    spec = FileDeleteCapability().spec
    assert spec.dangerous is True


def test_process_is_running_capability_is_not_dangerous():
    spec = ProcessIsRunningCapability().spec
    assert spec.dangerous is False
