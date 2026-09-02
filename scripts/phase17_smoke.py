"""Phase 17 — non-destructive smoke test.

Exercises the upgraded Application Intelligence subsystem end-to-end
without modifying user state:

* Boots a WindowsApplicationService and prints the health report.
* Resolves 4 apps (notepad, calc, mspaint, chrome — fall back
  gracefully when an app is not installed).
* Attempts a real launch of one of them, prints the LaunchResult,
  and leaves the spawned process running (we never close user
  applications per the Phase 17 non-destructive contract).
* Verifies the structured ``not_found`` path.

Run with::

    python scripts/phase17_smoke.py
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List

# Bootstrap: add the project root to sys.path
import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from system.application import (  # noqa: E402
    ApplicationHealthState,
    LaunchPolicy,
    LaunchResult,
    RunningApp,
    SourceHealth,
    WindowsApplicationService,
)


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def _print_health(service: WindowsApplicationService) -> None:
    report = service.health()
    print("Service health summary:")
    for key in (
        "service",
        "lifecycle",
        "uwp_enabled",
    ):
        print(f"  {key}: {report.get(key)}")
    catalog = report.get("catalog") or {}
    print(f"  catalog.state: {catalog.get('state')}")
    print(f"  catalog.record_count: {catalog.get('record_count')}")
    print(f"  catalog.last_refresh_ms: {catalog.get('last_refresh_ms'):.1f}")
    print(f"  catalog.cache_age_s: {catalog.get('cache_age_s')}")
    print(f"  catalog.miss_count: {catalog.get('miss_count')}")
    print(f"  catalog.miss_targeted_hits: {catalog.get('miss_targeted_hits')}")
    sources = catalog.get("sources") or []
    print(f"  catalog.sources ({len(sources)}):")
    for src in sources:
        err = src.get("last_error")
        marker = "OK" if err is None else f"FAIL: {err}"
        print(
            f"    - {src.get('name'):>14s}  "
            f"enabled={src.get('enabled')!s:5s}  "
            f"records={src.get('last_record_count'):4d}  "
            f"scan={src.get('last_scan_ms'):.1f}ms  "
            f"{marker}"
        )


def _resolve_and_report(service: WindowsApplicationService, name: str) -> Dict[str, Any]:
    res = service.resolve(name)
    rec = res.record
    print(
        f"  {name:>10s}  -> "
        f"is_found={res.is_found!s:5s}  "
        f"reason={res.reason}  "
        f"executable={rec.executable if rec else None!r}  "
        f"source={rec.source if rec else None!r}"
    )
    return {
        "name": name,
        "is_found": res.is_found,
        "reason": res.reason,
        "executable": rec.executable if rec else None,
        "source": rec.source if rec else None,
        "normalized_name": rec.normalized_name if rec else None,
        "executable_path": rec.executable_path if rec else None,
    }


def main() -> int:
    print("Phase 17 — non-destructive smoke test")
    print(f"sys.executable: {sys.executable}")
    print(f"sys.platform:   {sys.platform}")
    started = time.time()

    # 1. Boot the service with UWP on (default) so the report
    #    covers all sources.
    _section("1. Boot service (enable_uwp=True)")
    service = WindowsApplicationService(enable_uwp=True)
    try:
        service._catalog.initialize()
    except Exception as exc:  # noqa: BLE001
        print(f"  catalog.initialize() raised: {exc!r}")
    # Service-level init too, so the lifecycle is fully READY.
    try:
        service.initialize()
    except Exception as exc:  # noqa: BLE001
        print(f"  service.initialize() raised: {exc!r}")

    # 2. Print the health report.
    _section("2. Health report")
    _print_health(service)

    # 3. Resolve a small set of apps.
    _section("3. Resolution (metadata-driven, no hardcoded aliases)")
    target_apps = ["notepad", "calc", "mspaint", "chrome", "code"]
    resolutions: List[Dict[str, Any]] = []
    for name in target_apps:
        resolutions.append(_resolve_and_report(service, name))

    # 4. Pick the first found app and attempt a real launch.
    _section("4. Real launch (non-destructive)")
    found = next((r for r in resolutions if r["is_found"]), None)
    if found is None:
        print("  no installed app found — skipping real launch")
    else:
        app_name = found["name"]
        print(f"  launching {app_name!r} via metadata, no app-specific code path")
        result: LaunchResult = service.launch(
            app_name=app_name,
            policy=LaunchPolicy(wait_for_window=True),
        )
        print(f"  LaunchResult.status        = {result.status}")
        print(f"  LaunchResult.failure_stage = {result.failure_stage}")
        print(f"  LaunchResult.pid           = {result.pid}")
        print(f"  LaunchResult.hwnd          = {result.hwnd}")
        print(f"  LaunchResult.window_title  = {result.window_title}")
        print(f"  LaunchResult.process_name  = {result.process_name}")
        print(f"  LaunchResult.target        = {result.target}")
        print(f"  LaunchResult.elapsed_ms    = {result.elapsed_ms:.1f}")
        print(f"  LaunchResult.is_success    = {result.is_success}")
        if result.error:
            print(f"  LaunchResult.error         = {result.error}")
        if result.notes:
            print(f"  LaunchResult.notes         = {result.notes}")
        print("  NOTE: spawned process is left running (non-destructive contract).")

    # 5. Structured not_found for an unknown name.
    _section("5. Structured not_found (no exception thrown)")
    bogus = service.launch(
        "__phase17_smoke_does_not_exist_zzz__",
        policy=LaunchPolicy(wait_for_window=False),
    )
    print(f"  LaunchResult.status        = {bogus.status}")
    print(f"  LaunchResult.failure_stage = {bogus.failure_stage}")
    print(f"  LaunchResult.error         = {bogus.error}")
    print(f"  LaunchResult.is_success    = {bogus.is_success}")

    # 6. Enumerate currently running apps.
    _section("6. running_apps() snapshot")
    running: List[RunningApp] = service.running_apps()
    print(f"  count: {len(running)}")
    for ra in running[:8]:
        print(
            f"    - {ra.application.display_name!r:<30s}  "
            f"pid={ra.pid:<6d}  process={ra.process_name}"
        )
    if len(running) > 8:
        print(f"    ... and {len(running) - 8} more")

    # 7. async refresh trigger (manual /refresh equivalent)
    _section("7. refresh_async() — non-blocking")
    t = service.refresh_async()
    print(f"  started thread: {t.name!r} (daemon={t.daemon}) alive={t.is_alive()}")
    t.join(timeout=5.0)
    print(f"  after join: alive={t.is_alive()}")

    _section("8. Summary")
    print(f"  total elapsed: {(time.time() - started) * 1000:.1f} ms")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
