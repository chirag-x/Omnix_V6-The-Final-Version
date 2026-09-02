# Omnix V6 Phase 3 Capability Audit

## 1. Capabilities Implemented

### Filesystem Capabilities
**1. file.read**
* **Inputs/Parameters**: path (PATH, required) 
* **Outputs**: string containing file content (details["content"])
* **Tests**: 	est_file_write_read_roundtrip, 	est_file_read_not_found, 	est_file_path_must_be_absolute
* **Windows Services Routing**: None (uses asyncio.run_in_executor with standard open)
* **Status Action**: Purely observational (returns VERIFIED)
* **Dangerous**: False

**2. file.write**
* **Inputs/Parameters**: path (PATH, required), content (STRING, required)
* **Outputs**: None (details["size"] of bytes written, string confirmation message)
* **Tests**: 	est_file_write_read_roundtrip
* **Windows Services Routing**: None (uses asyncio.run_in_executor with standard open)
* **Status Action**: Execution block (returns EXECUTED)
* **Dangerous**: False

### Process Capabilities
**3. os.run_command**
* **Inputs/Parameters**: command (STRING, required), 	imeout (INTEGER, optional, defaults to 30)
* **Outputs**: stdout str, stderr str, return_code int
* **Tests**: 
    - 	est_run_command_success
    - 	est_run_command_dangerous_chars_blocked
    - 	est_run_command_invalid_timeout
    - 	est_run_command_marked_dangerous
* **Windows Services Routing**: None (uses syncio.create_subprocess_shell)
* **Status Action**: Executed verification (returns VERIFIED based on exit code matching 0)
* **Dangerous**: TRUE (uses shell chars blocklist [<>|&;$\(\)\[\]\*\?~\n\r])

### Desktop Observation Capabilities
**4. desktop.screen_size**
* **Inputs/Parameters**: None
* **Outputs**: width int, height int
* **Tests**: 	est_screen_size_capability
* **Windows Services Routing**: None (uses pyautogui.size())
* **Status Action**: Purely observational (returns VERIFIED)
* **Dangerous**: False

**5. desktop.foreground_window**
* **Inputs/Parameters**: None
* **Outputs**: hwnd int, title str, process_id int, process_name str
* **Tests**: 	est_foreground_window_capability
* **Windows Services Routing**: None (uses win32gui, win32process, psutil)
* **Status Action**: Purely observational (returns VERIFIED)
* **Dangerous**: False

**6. desktop.screenshot**
* **Inputs/Parameters**: save_path (PATH, required)
* **Outputs**: path to saved screenshot PNG
* **Tests**: 	est_screenshot_capability
* **Windows Services Routing**: None (uses pyautogui.screenshot())
* **Status Action**: Pure observational dump (returns VERIFIED) - ONLY observation, NO Vision/AI applied.
* **Dangerous**: False

### Desktop Mouse Capabilities
**7. desktop.mouse_move**
* **Inputs/Parameters**: x (INTEGER, required), y (INTEGER, required), duration (FLOAT, default 0.0)
* **Outputs**: x int, y int, duration float
* **Tests**: 	est_mouse_move_capability
* **Windows Services Routing**: WindowsInputService.move()
* **Status Action**: Dispatched input (returns EXECUTED, NOT VERIFIED)
* **Dangerous**: False

**8. desktop.mouse_click**
* **Inputs/Parameters**: utton (ENUM ['left', 'right', 'middle'], default 'left'), x (INTEGER, optional), y (INTEGER, optional)
* **Outputs**: x int, y int, button str
* **Tests**: 	est_mouse_click_capability
* **Windows Services Routing**: WindowsInputService.click()
* **Status Action**: Dispatched input (returns EXECUTED, NOT VERIFIED)
* **Dangerous**: False

**9. desktop.mouse_double_click**
* **Inputs/Parameters**: x (INTEGER, optional), y (INTEGER, optional)
* **Outputs**: x int, y int
* **Tests**: Included in mouse tests suite
* **Windows Services Routing**: WindowsInputService.click() (with clicks=2)
* **Status Action**: Dispatched input (returns EXECUTED, NOT VERIFIED)
* **Dangerous**: False

**10. desktop.mouse_right_click**
* **Inputs/Parameters**: x (INTEGER, optional), y (INTEGER, optional)
* **Outputs**: x int, y int
* **Tests**: Included in mouse tests suite
* **Windows Services Routing**: WindowsInputService.click() (with utton="right")
* **Status Action**: Dispatched input (returns EXECUTED, NOT VERIFIED)
* **Dangerous**: False

**11. desktop.mouse_scroll**
* **Inputs/Parameters**: clicks (INTEGER, required), x (INTEGER, optional), y (INTEGER, optional), ertical (BOOLEAN, default True)
* **Outputs**: clicks int, x int, y int, vertical bool
* **Tests**: 	est_mouse_scroll_capability
* **Windows Services Routing**: WindowsInputService.scroll()
* **Status Action**: Dispatched input (returns EXECUTED, NOT VERIFIED)
* **Dangerous**: False

**12. desktop.mouse_drag**
* **Inputs/Parameters**: x_end (INTEGER, required), y_end (INTEGER, required), duration (FLOAT, default 0.5)
* **Outputs**: start_x int, start_y int, end_x int, end_y int, duration float
* **Tests**: Included in mouse tests suite
* **Windows Services Routing**: WindowsInputService.drag()
* **Status Action**: Dispatched input (returns EXECUTED, NOT VERIFIED)
* **Dangerous**: False

### Desktop Keyboard Capabilities
**13. desktop.keyboard_type**
* **Inputs/Parameters**: 	ext (STRING, required), interval (FLOAT, default 0.0)
* **Outputs**: text str, interval float
* **Tests**: Included in keyboard tests suite
* **Windows Services Routing**: WindowsInputService.type_text()
* **Status Action**: Dispatched input (returns EXECUTED, NOT VERIFIED)
* **Dangerous**: False

**14. desktop.keyboard_press**
* **Inputs/Parameters**: key (STRING, required)
* **Outputs**: key str
* **Tests**: 	est_keyboard_press_capability, 	est_keyboard_press_missing_key
* **Windows Services Routing**: WindowsInputService.press()
* **Status Action**: Dispatched input (returns EXECUTED, NOT VERIFIED)
* **Dangerous**: False

**15. desktop.keyboard_hotkey**
* **Inputs/Parameters**: keys (STRING, required, comma-separated keys like 'ctrl,c')
* **Outputs**: keys list
* **Tests**: 	est_keyboard_hotkey_capability
* **Windows Services Routing**: WindowsInputService.hotkey()
* **Status Action**: Dispatched input (returns EXECUTED, NOT VERIFIED)
* **Dangerous**: False

### Desktop Application Capabilities
**16. desktop.application_open**
* **Inputs/Parameters**: 
ame (STRING, required)
* **Outputs**: pid int, process_name str, execution_time float
* **Tests**: 	est_application_open_missing_app_name
* **Windows Services Routing**: WindowsApplicationService.open_application()
* **Status Action**: Exe validation (returns VERIFIED on successful PID generation)
* **Dangerous**: False

**17. desktop.application_close**
* **Inputs/Parameters**: 
ame (STRING, required), orce (BOOLEAN, default False)
* **Outputs**: success bool, process_name str
* **Tests**: Included in application tests suite
* **Windows Services Routing**: WindowsApplicationService.close_application()
* **Status Action**: State transition requested (returns EXECUTED)
* **Dangerous**: False

**18. desktop.application_focus**
* **Inputs/Parameters**: 
ame (STRING, required)
* **Outputs**: pid int
* **Tests**: Included in application tests suite
* **Windows Services Routing**: WindowsApplicationService.focus_application()
* **Status Action**: Interaction state change request (returns EXECUTED)
* **Dangerous**: False

**19. desktop.application_is_running**
* **Inputs/Parameters**: 
ame (STRING, required)
* **Outputs**: is_running bool, pids list
* **Tests**: 	est_application_is_running_capability
* **Windows Services Routing**: WindowsApplicationService.is_application_running()
* **Status Action**: Pure observational (returns VERIFIED as state reflection)
* **Dangerous**: False

### Desktop Window Capabilities
**20. desktop.window_list**
* **Inputs/Parameters**: isible_only (BOOLEAN, default True)
* **Outputs**: list of dicts (hwnd, title, class_name, visible)
* **Tests**: 	est_window_list_capability
* **Windows Services Routing**: WindowsWindowService.list_windows()
* **Status Action**: Pure observational (returns VERIFIED)
* **Dangerous**: False

**21. desktop.window_focus**
* **Inputs/Parameters**: hwnd (INTEGER, required)
* **Outputs**: hwnd int, title str
* **Tests**: 	est_window_focus_capability, 	est_window_focus_missing_hwnd
* **Windows Services Routing**: WindowsWindowService.focus_window()
* **Status Action**: Interaction dispatch (returns EXECUTED)
* **Dangerous**: False

**22. desktop.window_minimize**
* **Inputs/Parameters**: hwnd (INTEGER, required)
* **Outputs**: hwnd int
* **Tests**: Included in window tests suite
* **Windows Services Routing**: Uses _is_window() validation from WindowService, then direct win32 call.
* **Status Action**: UI state dispatch (returns EXECUTED)
* **Dangerous**: False

**23. desktop.window_maximize**
* **Inputs/Parameters**: hwnd (INTEGER, required)
* **Outputs**: hwnd int
* **Tests**: Included in window tests suite
* **Windows Services Routing**: Uses _is_window() validation from WindowService, then direct win32 call.
* **Status Action**: UI state dispatch (returns EXECUTED)
* **Dangerous**: False

**24. desktop.window_restore**
* **Inputs/Parameters**: hwnd (INTEGER, required)
* **Outputs**: hwnd int
* **Tests**: Included in window tests suite
* **Windows Services Routing**: Uses _is_window() validation from WindowService, then direct win32 call.
* **Status Action**: UI state dispatch (returns EXECUTED)
* **Dangerous**: False

**25. desktop.window_close**
* **Inputs/Parameters**: hwnd (INTEGER, required)
* **Outputs**: hwnd int
* **Tests**: Included in window tests suite
* **Windows Services Routing**: Uses _is_window() validation from WindowService, then direct win32 call.
* **Status Action**: UI action dispatch (returns EXECUTED)
* **Dangerous**: False

## Total Number of Capabilities
25 standard capabilities have been registered over 6 distinct domains. None are duplicate.
V5 God module pattern avoided via logical domain files.
