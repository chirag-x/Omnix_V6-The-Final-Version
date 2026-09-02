# Omnix V6 Scripts

This directory holds the smoke / probe scripts used during Omnix
development.  Most are **one-off investigation scripts** kept for
historical reference.  The phase-numbered scripts are the **canonical**
smoke tests that ship with each release.

## Canonical smoke scripts

Run these to verify the engine end-to-end on a real Windows desktop:

| Script | Phase | What it verifies |
|---|---|---|
| `validate_environment.py` | pre-flight | Verifies required Python deps and Windows APIs are present |
| `phase11_real_world_smoke.py` | 11 | Provider connectivity + LLM happy path |
| `phase12_real_windows_smoke.py` | 12 | Application launch / close loop |
| `phase13_real_windows_smoke.py` | 13 | Multi-step / chained plans |
| `phase16_real_windows_smoke.py` | 16 | Local-first fast path dispatcher |
| `phase17_smoke.py` | 17 | Peak input subsystem (target-aware mouse + keyboard, Unicode, FAILSAFE, thread safety, cancellation, stale-target detection) |
| `smoke_system8.py` | 8 | Voice subsystem smoke |

## Reference probe scripts

The `probe_*.py` and `probe_*.ps1` scripts are one-off investigation
tools.  None of them are imported by the engine or the test suite; they
exist as historical artifacts of specific debugging sessions.

| Script | What it was used for |
|---|---|
| `probe_system2_brain.py` | Brain classification — kept referenced (do not delete) |
| `probe_system9_smoke.py` | System-9 smoke — kept referenced (do not delete) |
| `probe_apps.py` | One-off: list installed apps via the catalog |
| `probe_calc.py` | One-off: launch Calculator for debugging |
| `probe_catalog.py` | One-off: dump the application catalog |
| `probe_chrome.py` | One-off: launch Chrome for debugging |
| `probe_compound.py` | One-off: compound-request classification |
| `probe_decision.py` | One-off: local-decision-engine probe |
| `probe_dispatch.py` | One-off: FastPathDispatcher probe |
| `probe_dispatch2.py` | One-off: FastPathDispatcher probe v2 |
| `probe_engine.py` | One-off: end-to-end engine probe |
| `probe_flakiness.py` | One-off: investigate test flakiness |
| `probe_input.py` | One-off: input-layer probe |
| `probe_local_v2.py` | One-off: local-decision-engine v2 |
| `probe_multi.py` | One-off: multi-step dispatch probe |
| `probe_notepad.py` | One-off: launch Notepad for debugging |
| `probe_paint.py` | One-off: launch Paint for debugging |
| `probe_resolve_exe.py` | One-off: executable-resolution probe |
| `probe_uwp_manifest.py` / `.ps1` | One-off: UWP manifest inspection (Python + PowerShell) |
| `probe_uwp_procs.py` | One-off: UWP process enumeration |
| `probe_pkg.ps1` | One-off: package manager probe (PowerShell) |
| `debug_happy.py` | One-off: debug the happy path |
| `debug_open_chrome.py` | One-off: debug Chrome launch |

The phase-numbered scripts are what you should run; the `probe_*`
scripts are kept only as evidence of past investigation.

## Adding a new smoke script

1. Pick the next phase number (e.g. `phase18_real_windows_smoke.py`).
2. Keep the script under 200 lines; one test per script is fine.
3. Add a row to the **Canonical smoke scripts** table above.
4. Reference it from `docs/PHASE_<N>_REPORT.md` if applicable.
