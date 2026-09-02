# Omnix V6 Phase 3 Implementation Report

## Overview
Phase 3 (Composable Capability Layer) has been successfully implemented, bridging the gap between Phase 1's abstract Action/Resource engine and Phase 2's concrete Windows OS service boundaries.

The implementation faithfully adheres to the clean architecture constraints established for V6:
- **No LLM reasoning inside capabilities** (strictly deterministic bindings)
- **No Natural Language parsing**
- **Strong state separation** between Observation (returns VERIFIED) and Actuation (returns EXECUTED)
- **Modular Design** (Avoiding the V5 God Module pattern)

## Capabilities Implemented (25 Total)

Detailed capability outlines have been generated in docs/V6_PHASE_3_CAPABILITY_AUDIT.md.

* **Filesystem (2)**: ile.read, ile.write
* **Process (1)**: os.run_command
* **Desktop Observation (3)**: desktop.screen_size, desktop.foreground_window, desktop.screenshot
* **Desktop Mouse (6)**: desktop.mouse_move, mouse_click, mouse_double_click, mouse_right_click, mouse_scroll, mouse_drag
* **Desktop Keyboard (3)**: desktop.keyboard_type, keyboard_press, keyboard_hotkey
* **Desktop Application (4)**: desktop.application_open, pplication_close, pplication_focus, pplication_is_running
* **Desktop Window (6)**: desktop.window_list, window_focus, window_minimize, window_maximize, window_restore, window_close

## Key Architectural Decisions

### 1. Modularization over Monolith
Instead of a single desktop.py holding all implementations, logical groupings were instituted:
- desktop_observation.py
- desktop_mouse.py
- desktop_keyboard.py
- desktop_application.py
- desktop_window.py

This structure ensures clean dependencies and distinct responsibility sets.

### 2. Execution vs Verification States
An intentional decision was made concerning returned CapabilityStatus.
* Operations that alter Windows UI state without returning hard programmatic confirmation (e.g., clicking a mouse, mimicking keystrokes) return CapabilityStatus.EXECUTED. 
* They *do not* return VERIFIED. The system acknowledges the input mechanism fired, but prevents the capability claiming real-world success (like "Login button was actually clicked") merely because dispatch didn't crash.
* Observation operations (screenshot, list windows) return VERIFIED since they retrieve and confirm state physically.

### 3. Security Mechanism for Subprocesses
RunCommandCapability was thoroughly safety-checked:
- Marked explicitly as dangerous=True in CapabilitySpec.
- Added _DANGEROUS_SHELL_CHARS regex intercept to block common pivot techniques (>, <, |, &, ;, $, etc.)
- Strict integer timeouts added and process forcefully killed using syncio.wait_for.

### 4. Direct Dependency Avoidance
Where possible, async standard library loops (un_in_executor) were chosen over introducing narrow third-party modules (like iofiles) to maintain light dependencies. 

## Testing Summary
* Complete test suite execution successful.
* Phase 1 and Phase 2 regression suites passed cleanly (0 regressions).
* Pip requirement check (python -m pip check) passed cleanly.
* **0 files from V5 architecture were ported.** Code is 100% native to the new setup.

## Current Limitations & Notes
1. **PyAutoGUI Dependency**: Desktop Observation currently utilizes pyautogui.screenshot(). Native Windows GDI/BitBlt approaches might be explored in the future to completely sever third-party bounding if latency becomes an issue.
2. **Window Control via HWND**: Interacting directly with window controls expects valid HWNDs retrieved via the observation capabilities. Window state validations depend on win32gui.

## Conclusion
Phase 3 is complete and ready to act as the deterministic translation layer for the future Phase 4 AI Orchestration layer.
