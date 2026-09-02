# Phase 2 — V5 Reference Notes

## Application Management (`system/applications/`)
- **V5 Components**: `application_manager.py`, `launch_strategy.py`, `app_discovery.py`
- **Behavior**: Used `subprocess.Popen` heavily. Mapped common aliases (e.g., "chrome") to executable paths.
- **Bugs/Issues**: 
  - `launch` returned success instantly on Process ID creation, failing to verify if the UI actually painted.
  - Large hardcoded `aliases.json` dictionaries to find exe locations.
- **V6 Desired Route**: Create an `ApplicationService` that maps aliases deterministically, fires the shell execute/subprocess gracefully, but uses `ObservationResult` hooks where possible to determine readiness, or defines exact bounds (e.g. `process_created` result vs `window_ready`).

## Windows Management (`system/windows/`)
- **V5 Components**: `window_manager.py`, `focus_manager.py`, `window_tracker.py`
- **Behavior**: Depended highly on PyGetWindow and raw `win32gui` handles.
- **Bugs/Issues**: 
  - Susceptible to silent failures during `SetForegroundWindow` due to Windows OS constraints preventing background apps from stealing focus.
- **V6 Desired Route**: The `WindowService` must wrap `win32gui` gracefully, check OS timeout boundaries, use stable metadata (PID, HWND, Process Name), and fallback to `AttachThreadInput` if focus stealing is blocked. We will return structured failures rather than swallowing focus denial.

## Input Management (`system/input/`)
- **V5 Components**: `input_manager.py`, `keyboard.py`, `mouse.py`
- **Behavior**: Relied largely on `pyautogui`.
- **Bugs/Issues**:
  - `pyautogui` blocks threads directly with internal sleeps.
  - Doesn't integrate well with structured cancellation or graceful failure loops.
  - No explicit targets—blind writes.
- **V6 Desired Route**: Implement `InputService` using standard bindings but wrapping them in V6 Timers/Deadlines where applicable. 

## Process Management (`system/processes/`)
- **V5 Components**: `process_manager.py`, `process_detector.py`
- **Behavior**: Leveraged `psutil` natively.
- **Bugs/Issues**: 
  - Destructive process kills had no safety wrappers, leading to accidental explorer.exe terminations.
- **V6 Desired Route**: Implement `ProcessService` using `psutil`. Implement a rigid `SAFETY_BLACKLIST` preventing core windows operations from being killed via the Capability Router. 

## Filesystem Operations (`system/filesystem/`)
- **V5 Components**: `file_manager.py`, `file_operations.py`
- **Behavior**: Pure `pathlib` and `os` standard execution.
- **Bugs/Issues**:
  - Traversal loops (`../../`) outside intended boundaries weren't strictly sanitized.
- **V6 Desired Route**: `FilesystemService` utilizing normalized absolute paths with strict base-checking tests (so test suite runs only in temp directories).

## Clipboard Management (`system/input/clipboard.py`)
- **V5 Components**: `clipboard.py`
- **Behavior**: Used `pyperclip`.
- **Bugs/Issues**:
  - Clipboard locks (EmptyClipboard failures) caused unhandled crashes.
- **V6 Desired Route**: Implement retry mechanisms using V6 Timers for transient clipboard access locks.
