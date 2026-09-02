"""
Omnix V6 -- Phase 12 (REAL AUTOMATION EXECUTION LAYER) smoke.

A *selectable* set of small, side-effect-bounded real-Windows tests
that exercise the canonical execution path end-to-end:

    Capability → CapabilityRouter → Service → Windows / FS

Each individual test is opt-in.  The script does NOT mutate the
user's real state in any way that cannot be cleaned up by
deleting a temporary directory; tests that need to spawn an OS
process use ``notepad.exe`` (always present on Windows) and
verify that the process is alive, never kill it.

Run from the V6 project root:

    # All tests:
    python scripts/phase12_real_windows_smoke.py

    # A subset:
    python scripts/phase12_real_windows_smoke.py --tests fs.create,fs.delete,proc.is_running

The script never prints secrets, never uses the LLM, and is
safe to run unattended in CI on a Windows host.

Exit code:
    0   all selected tests passed
    1   at least one test failed
    2   the host is not Windows (smoke is Windows-only)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Set the standard test-time environment knobs early.
os.environ.setdefault("OMNIX_HEADLESS", "1")
os.environ.setdefault("OMNIX_QUIET_BOOT", "1")


@dataclass
class TestRecord:
    name: str
    description: str
    ok: bool = False
    skipped: bool = False
    error: str = ""
    duration_ms: float = 0.0
    details: Dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The selected test set
# ---------------------------------------------------------------------------

async def t_fs_create(rec: TestRecord) -> None:
    """``file.create`` writes an empty file."""
    from core.capabilities.filesystem import FileCreateCapability
    from core.results import CapabilityStatus
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "phase12_create.txt")
        cap = FileCreateCapability()
        result = await cap.execute({"path": target})
        if result.status is not CapabilityStatus.VERIFIED:
            raise AssertionError(
                f"file.create did not VERIFIED; got {result.status.value}: {result.error}"
            )
        if not os.path.exists(target):
            raise AssertionError("file.create returned VERIFIED but file missing")
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()
        if content != "":
            raise AssertionError(f"file.create should produce empty file; got {content!r}")
        rec.details["path"] = target


async def t_folder_create(rec: TestRecord) -> None:
    """``folder.create`` creates a directory tree (idempotent)."""
    from core.capabilities.filesystem import FolderCreateCapability
    from core.results import CapabilityStatus
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "a", "b", "c")
        cap = FolderCreateCapability()
        r1 = await cap.execute({"path": target})
        r2 = await cap.execute({"path": target})  # idempotent
        if r1.status is not CapabilityStatus.VERIFIED:
            raise AssertionError(f"folder.create (first) failed: {r1.status.value}: {r1.error}")
        if r2.status is not CapabilityStatus.VERIFIED:
            raise AssertionError(f"folder.create (idempotent) failed: {r2.status.value}: {r2.error}")
        if not os.path.isdir(target):
            raise AssertionError("folder.create: directory missing")


async def t_directory_list(rec: TestRecord) -> None:
    """``directory.list`` returns sorted entries."""
    from core.capabilities.filesystem import DirectoryListCapability
    from core.results import CapabilityStatus
    with tempfile.TemporaryDirectory() as d:
        for n in ("z.txt", "a.txt", "m.txt"):
            with open(os.path.join(d, n), "w", encoding="utf-8") as f:
                f.write("x")
        cap = DirectoryListCapability()
        result = await cap.execute({"path": d})
        if result.status is not CapabilityStatus.VERIFIED:
            raise AssertionError(
                f"directory.list failed: {result.status.value}: {result.error}"
            )
        entries = result.details.get("entries")
        if entries != ["a.txt", "m.txt", "z.txt"]:
            raise AssertionError(f"directory.list: unexpected order: {entries}")


async def t_file_delete(rec: TestRecord) -> None:
    """``file.delete`` removes a file (safe path)."""
    from core.capabilities.filesystem import FileDeleteCapability
    from core.results import CapabilityStatus
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "victim.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("x")
        cap = FileDeleteCapability()
        result = await cap.execute({"path": target})
        if result.status is not CapabilityStatus.VERIFIED:
            raise AssertionError(
                f"file.delete failed: {result.status.value}: {result.error}"
            )
        if os.path.exists(target):
            raise AssertionError("file.delete returned VERIFIED but file still present")


async def t_file_delete_refuses_reserved(rec: TestRecord) -> None:
    """``file.delete`` refuses reserved system paths."""
    from core.capabilities.filesystem import FileDeleteCapability
    from core.results import CapabilityStatus
    cap = FileDeleteCapability()
    result = await cap.execute({"path": r"C:\Windows\System32\drivers\etc\hosts"})
    if result.status is not CapabilityStatus.FAILED:
        raise AssertionError(
            f"file.delete should have refused a reserved path; got {result.status.value}"
        )
    if "reserved" not in str(result.error).lower():
        raise AssertionError(
            f"file.delete: expected 'reserved' in error, got: {result.error!r}"
        )


async def t_process_is_running_python(rec: TestRecord) -> None:
    """``process.is_running`` finds the current Python interpreter."""
    from core.capabilities.process import ProcessIsRunningCapability
    from core.results import CapabilityStatus
    cap = ProcessIsRunningCapability()
    result = await cap.execute({"name": sys.executable})
    if result.status is not CapabilityStatus.VERIFIED:
        raise AssertionError(
            f"process.is_running failed: {result.status.value}: {result.error}"
        )
    if not result.details.get("running"):
        raise AssertionError("process.is_running says the current Python is not running?!")
    pids = list(result.details.get("pids") or [])
    if os.getpid() not in pids:
        raise AssertionError(f"process.is_running: pid mismatch: got {pids}, expected {os.getpid()}")


async def t_process_is_running_unknown(rec: TestRecord) -> None:
    """``process.is_running`` cleanly reports an unknown process as not-running."""
    from core.capabilities.process import ProcessIsRunningCapability
    from core.results import CapabilityStatus
    cap = ProcessIsRunningCapability()
    result = await cap.execute(
        {"name": "omnix-v6-smoke-definitely-not-a-real-process-12345"}
    )
    if result.status is not CapabilityStatus.VERIFIED:
        raise AssertionError(
            f"process.is_running should VERIFIED even when not found; "
            f"got {result.status.value}: {result.error}"
        )
    if result.details.get("running") is not False:
        raise AssertionError("process.is_running: expected running=False for unknown name")


async def t_router_dispatch_full_set(rec: TestRecord) -> None:
    """The full Phase 12 capability set is dispatchable through the Router."""
    from core.capability_registry import CapabilityRegistry
    from core.capability_router import CapabilityRouter, AllowAllSafetyPolicy
    from core.capabilities import register_standard_capabilities
    registry = CapabilityRegistry()
    register_standard_capabilities(registry)
    router = CapabilityRouter(registry=registry, safety_policy=AllowAllSafetyPolicy())

    for name in (
        "file.create",
        "folder.create",
        "directory.list",
        "file.delete",
        "process.is_running",
    ):
        if not registry.has(name):
            raise AssertionError(f"missing capability in standard set: {name}")

    with tempfile.TemporaryDirectory() as d:
        # file.create + directory.list + folder.create
        r1 = router.route("folder.create", {"path": os.path.join(d, "nested", "leaf")})
        r2 = router.route("file.create", {"path": os.path.join(d, "nested", "x.txt")})
        r3 = router.route("directory.list", {"path": os.path.join(d, "nested")})
        for label, r in (("folder.create", r1), ("file.create", r2), ("directory.list", r3)):
            if r.status.value != "verified":
                raise AssertionError(f"{label} did not VERIFIED: {r.status.value}: {r.error}")
        # file.delete (dangerous; the AllowAll policy here is for the SMOKE only)
        target = os.path.join(d, "nested", "x.txt")
        r4 = router.route("file.delete", {"path": target}, authorized_dangerous=True)
        if r4.status.value != "verified":
            raise AssertionError(f"file.delete did not VERIFIED: {r4.status.value}: {r4.error}")
        # process.is_running
        r5 = router.route("process.is_running", {"name": sys.executable})
        if r5.status.value != "verified":
            raise AssertionError(
                f"process.is_running did not VERIFIED: {r5.status.value}: {r5.error}"
            )


async def t_engine_boots_and_processes_text(rec: TestRecord) -> None:
    """The full OmnixEngine boots, processes a known no-op intent,
    and returns a safe :class:`OmnixResponse`.  Does NOT touch the
    UI; the deterministic planner degrades to NO_OP for unknown
    text and the engine surfaces a clean failed response -- which
    is still *evidence* the pipeline wires end-to-end.
    """
    from core.configuration import OmnixConfig
    from core.omnix_engine import OmnixEngine

    # Build a minimal real config from defaults; the engine wires
    # its own bus / registry / router at construction time.
    cfg = OmnixConfig.default() if hasattr(OmnixConfig, "default") else OmnixConfig(
        project_root=os.getcwd(),
        data_dir=os.path.join(os.getcwd(), ".omnix-data"),
        log_dir=os.path.join(os.getcwd(), ".omnix-logs"),
        env_file=os.path.join(os.getcwd(), ".env"),
    )
    engine = OmnixEngine(cfg)
    engine.initialize()
    try:
        engine.start()
        response = engine.process("__smoke_no_op_phase12__", correlation_id="phase12-smoke")
        # We accept OK, CLARIFICATION, or FAILED -- we just need
        # a structured response with a correlation id; the test is
        # that the pipeline is *wired*, not that any specific text
        # succeeds.
        if not response.correlation_id:
            raise AssertionError("engine.process returned no correlation_id")
        rec.details["status"] = str(response.status)
        rec.details["duration_ms"] = float(response.duration_ms or 0.0)
    finally:
        engine.shutdown()


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

ALL_TESTS: Dict[str, Callable[[TestRecord], "asyncio.Future"]] = {
    "fs.create": t_fs_create,
    "folder.create": t_folder_create,
    "directory.list": t_directory_list,
    "fs.delete": t_file_delete,
    "fs.delete.reserved": t_file_delete_refuses_reserved,
    "proc.is_running.python": t_process_is_running_python,
    "proc.is_running.unknown": t_process_is_running_unknown,
    "router.phase12": t_router_dispatch_full_set,
    "engine.boot": t_engine_boots_and_processes_text,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def _run_all(names: List[str]) -> List[TestRecord]:
    results: List[TestRecord] = []
    for name in names:
        fn = ALL_TESTS.get(name)
        rec = TestRecord(name=name, description=fn.__doc__ or "")
        t0 = time.time()
        if fn is None:
            rec.skipped = True
            rec.error = "unknown test name"
        else:
            try:
                await fn(rec)
                rec.ok = True
            except Exception as exc:  # noqa: BLE001
                rec.ok = False
                rec.error = f"{type(exc).__name__}: {exc}"
                rec.details["traceback"] = traceback.format_exc(limit=4)
        rec.duration_ms = round((time.time() - t0) * 1000.0, 2)
        results.append(rec)
    return results


def _print_report(records: List[TestRecord]) -> None:
    print()
    print("=" * 78)
    print("  OMNIX V6 -- PHASE 12 REAL-WINDOWS SMOKE")
    print("=" * 78)
    width = max((len(r.name) for r in records), default=10)
    for r in records:
        marker = "OK " if r.ok else ("SKIP" if r.skipped else "FAIL")
        line = f"  [{marker}] {r.name.ljust(width)}  {r.duration_ms:6.1f} ms"
        print(line)
        if r.description:
            print(f"         {r.description.strip()}")
        if not r.ok and not r.skipped:
            err = r.error or ""
            if len(err) > 200:
                err = err[:200] + "..."
            print(f"         ERR: {err}")
    print("-" * 78)
    passed = sum(1 for r in records if r.ok)
    failed = sum(1 for r in records if not r.ok and not r.skipped)
    skipped = sum(1 for r in records if r.skipped)
    print(f"  passed={passed}  failed={failed}  skipped={skipped}  total={len(records)}")
    print("=" * 78)


def main(argv: Optional[List[str]] = None) -> int:
    if os.name != "nt":
        print("Phase 12 smoke is Windows-only.  os.name=", os.name, file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tests",
        type=str,
        default=",".join(ALL_TESTS.keys()),
        help="Comma-separated subset of test names.  Default: all.",
    )
    args = parser.parse_args(argv)

    selected = [t.strip() for t in args.tests.split(",") if t.strip()]
    unknown = [t for t in selected if t not in ALL_TESTS]
    if unknown:
        print("Unknown test names:", unknown, file=sys.stderr)
        print("Available:", sorted(ALL_TESTS.keys()), file=sys.stderr)
        return 1

    records = asyncio.run(_run_all(selected))
    _print_report(records)

    return 0 if all(r.ok or r.skipped for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
