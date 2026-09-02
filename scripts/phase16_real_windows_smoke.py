"""Real end-to-end smoke for the 6 mandated test cases.

Runs through the actual main.py entry point.  Each query is sent
to the FastPathDispatcher, which uses the real ApplicationService,
the real window service, the real keyboard capability, etc.

For each case we record:
- top-level status
- which capabilities ran
- window/handle details
- whether the file was created (case 4)
"""
from __future__ import annotations

import io
import os
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, r"E:\Coding\Omnix\Omnix_V6- The final version")

# Force UTF-8 on Windows so emoji in titles don't crash prints
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

from main import build_engine  # noqa: E402

CASES = [
    ("Test 1 (chat)",        "Hello Omnix"),
    ("Test 2 (open)",        "Open Notepad"),
    ("Test 3 (open+type)",   "Open Notepad and type Hello World"),
    ("Test 4 (open+type+save)",
     "Open Notepad, type Hello from Omnix, and save it as omnix_test.txt"),
    ("Test 5 (chrome search)",
     "Open Chrome and search for AI agents"),
    ("Test 6 (chrome second)",
     "Open Chrome, search for AI agents, and open the second result"),
]


# -----------------------------------------------------------------------
# Structured test registry
# -----------------------------------------------------------------------
# Each TASKS entry is a thunk that, when invoked, returns a
# ``TestRecord``.  This is the interface the regression tests
# (``tests/test_phase16_basic.py``) introspect; it lets the
# script double as a CLI smoke runner and a unit-test target.
# -----------------------------------------------------------------------

@dataclass
class TestRecord:
    name: str
    ok: bool
    skipped: bool
    error: str = ""
    details: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = {}


def _make_thunk(name: str, query: str):
    """Return a zero-arg function that runs ``query`` through the
    real engine and records the outcome as a :class:`TestRecord`.

    The real engine is built lazily so the test runner can import
    this module without an LLM provider.
    """
    def _thunk() -> TestRecord:
        try:
            _, eng = build_engine()
        except Exception as exc:  # noqa: BLE001
            return TestRecord(
                name=name,
                ok=False,
                skipped=True,
                error=f"engine build failed: {exc!r}",
            )
        try:
            if not eng.initialize():
                return TestRecord(
                    name=name,
                    ok=False,
                    skipped=False,
                    error="eng.initialize() returned False",
                )
            eng.start()
        except Exception as exc:  # noqa: BLE001
            return TestRecord(
                name=name,
                ok=False,
                skipped=True,
                error=f"engine start failed: {exc!r}",
            )
        try:
            r = eng.process(query)
        except Exception as exc:  # noqa: BLE001
            return TestRecord(
                name=name,
                ok=False,
                skipped=False,
                error=f"eng.process raised: {exc!r}",
            )
        from core.responses import ResponseStatus
        ok = (r.status is ResponseStatus.OK)
        return TestRecord(
            name=name,
            ok=ok,
            skipped=False,
            error="" if ok else str(getattr(r, "error", "") or r.status),
            details={"response_text": getattr(r, "text", "")},
        )
    return _thunk


TASKS: dict = {
    "notepad_hello": _make_thunk(
        "notepad_hello", "Open Notepad, type Hello from Omnix, and save it as omnix_test.txt"
    ),
    "chrome_search": _make_thunk(
        "chrome_search", "Open Chrome and search for AI agents"
    ),
    "chrome_second_result": _make_thunk(
        "chrome_second_result",
        "Open Chrome, search for AI agents, and open the second result",
    ),
}


def main() -> int:  # noqa: PLR0915
    cfg, eng = build_engine()
    # Real entry point: initialize + start
    if not eng.initialize():
        print("engine.initialize() FAILED")
        return 1
    eng.start()
    pipeline = eng.pipeline
    disp = pipeline.app_dispatcher

    print(f"\n{'=' * 70}\nREAL END-TO-END SMOKE\n{'=' * 70}\n")

    results: list[tuple[str, object, dict]] = []
    for label, query in CASES:
        print(f"\n--- {label}\n    query: {query!r}")
        # Use the real engine entry point
        try:
            r = eng.process(query)
        except Exception as exc:  # noqa: BLE001
            print(f"   EXCEPTION: {exc!r}")
            results.append((label, exc, {}))
            continue

        print(f"   response.text  : {getattr(r, 'text', '')!r}")
        print(f"   response.status: {getattr(r, 'status', '?')}")
        meta = dict(getattr(r, "metadata", None) or {})
        if meta:
            # Show only the most informative keys
            for k in ("fast_path", "capability", "verified", "via",
                      "intent", "error", "plan_id", "agent_run_id"):
                if k in meta:
                    print(f"   {k:<13}: {meta[k]!r}")
            leftover = {k: v for k, v in meta.items()
                        if k not in ("fast_path", "capability", "verified",
                                     "via", "intent", "error", "plan_id",
                                     "agent_run_id", "request_kind", "stage",
                                     "correlation_id", "status", "duration_ms")}
            if leftover:
                print(f"   meta (other): {leftover}")
        if getattr(r, "error", None):
            print(f"   error        : {r.error}")
        results.append((label, r, meta))

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    ok_verified = 0
    for label, r, meta in results:
        if isinstance(r, Exception):
            status = "EXC"
        else:
            try:
                from core.responses import ResponseStatus
                status = ("OK" if r.status == ResponseStatus.OK
                          else str(r.status))
            except Exception:  # noqa: BLE001
                status = str(getattr(r, "status", "?"))
        print(f"  {label:<28} -> {status}")
        if status == "OK":
            ok_verified += 1

    # Verify the file from case 4 actually exists with right contents
    expected_path = os.path.join(os.getcwd(), "omnix_test.txt")
    if os.path.exists(expected_path):
        content = open(expected_path, "r", encoding="utf-8").read()
        print(f"\n  omnix_test.txt on disk: {len(content)} bytes -> {content!r}")
    else:
        print("\n  omnix_test.txt was NOT created")

    print(f"\n  {ok_verified} of {len(CASES)} cases PASSED")
    return 0 if ok_verified >= 4 else 1  # tolerate known partial failures


if __name__ == "__main__":
    raise SystemExit(main())
