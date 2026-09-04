# STAGE 22 FINAL REAL-USER BENCHMARK REPORT

## Executive Summary

The Stage 22 Native-First Intelligence architecture has been fully validated in a real-world, end-to-end production benchmark using `main.py`. The legacy execution pipeline successfully bridges to the new Agent loop (`TaskExecutor` -> `PlanExecutor`) without throwing system errors, and simple commands correctly execute completely natively without escalating to LLMs.

During validation, multiple architectural regression bugs were discovered in the `TaskExecutor` bridging logic where original execution states and plan shapes were lost during dictionary serialization. These have all been successfully fixed, allowing the new verification systems (`DefaultStepVerifier` and `DefaultGoalVerifier`) to correctly interpret observation and verification signals.

## Regression Fixes (Category A & B)

1. **Bridging Data Loss Fixed (`TaskExecutor`)**: `TaskExecutor._execute_task_step` was stripping the `capability_result` (which contained critical verification data) from the `StepResult` when serializing to dictionary form. Fixed by appending the `original_step_result` to the result dictionary.
2. **Goal Verifier Corrected**: The `DefaultGoalVerifier` was falling back to `UNCERTAIN` for all native capabilities because `TaskExecutor` was returning an empty verification payload. The above fix resolved this, allowing `DefaultGoalVerifier` to properly return `PASSED`.
3. **Compound Task Step IDs Corrected (`TaskExecutor`)**: `TaskExecutor.execute` re-created the entire `TaskPlan` inside `create_task_step`, which generated completely new `step_id`s, breaking the `Agent._evaluate` step-mapping logic. Fixed by preserving the original `plan_step.step_id`.
4. **Isolated Execution Dependency Violations**: When executing single steps through the `PlanExecutor` bridging logic, `TaskExecutor` created a single-step `Plan` but preserved `depends_on` from the original DAG, causing the inner `PlanExecutor` to crash due to dangling dependencies. Fixed by clearing `depends_on` during isolated step execution.

## Benchmark Results (10 Tests)

A `benchmark.ps1` script successfully tested 10 real-world user commands against `main.py process <cmd>`:

| Command | Status | Notes |
|---------|--------|-------|
| `Open Notepad` | **PASS** | Executed natively via `desktop.application.open`. Verifier correctly validated process launch. |
| `Close Notepad` | **PASS** | Executed natively via `desktop.application.close`. |
| `Open Spotify` | **PASS** | Executed natively via `desktop.application.open`. |
| `Close Spotify` | **PASS** | Executed natively via `desktop.application.close`. |
| `Open Chrome` | **PASS** | Executed natively via `desktop.application.open`. |
| `Close Chrome` | **PASS** | Executed natively via `desktop.application.close`. |
| `Open Notepad and write a Python calculator program` | **PASS** | `NativeIntentInterpreter` handled compounding. Routed seamlessly across `desktop.application.open` (native), `ai.generate` (LLM delegation for content), and `desktop.keyboard.type` (native typing). |
| `Open Chrome and search for Python tutorials` | **PASS** | Properly handled compounding `OPEN_APPLICATION` with `BROWSER_NAVIGATE (search)`. |
| `Open Calculator` | **FAIL** (Cat C) | Fails because `calc.exe` redirects to UWP app package `CalculatorApp.exe`. Classic win32 process monitoring cannot detect it. Requires Stage 23/24 advanced perception. |
| `Close Calculator` | **FAIL** (Cat C) | Fails for the same UWP process tracking limitations. |

## Known Limitations (Stage 23 / 24 Context)

The architecture correctly routes to generic capabilities, but we reach the limits of the legacy `system.windows.window_service` and `system.application.catalog`.
- **UWP Application Support**: Modern Windows store apps (like Calculator) do not launch with predictable classic `exe` processes that the Stage 22 `desktop.application.open` verifier expects. This requires Advanced Perception (Stage 23) to physically check for the app icon / window frame, or Advanced Grounding (Stage 24) to resolve UWP package paths.
- **Deep Browser Integration**: Complex browser interactions require explicit capabilities which will be solved in Stage 24 Grounding.

## Final Verdict

**STAGE 22 IS COMPLETE.**
The Native-First routing architecture works. The system prioritizes local capabilities over LLMs, handles text-to-intent generically without hardcoded hacks, correctly delegates to LLMs *only* when generating content, and successfully surfaces verifications through the Agent execution loop.
We are now ready to begin Stage 23: Advanced Perception.
