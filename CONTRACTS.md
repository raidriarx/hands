# Autonomous Windows Desktop Controller — Interface Contracts (`CONTRACTS.md`)

## 1. Architecture Separation
- **AI Reasoning (`controller/planners.py`)** returns strictly structured JSON matching `DesktopAction`.
- **Deterministic Execution (`controller/engine.py`, `controller/policy.py`, `controller/emergency.py`, `controller/driver.py`)** validates, enforces safety & sandbox policy, logs to SQLite, and executes physical or mocked actions.

## 2. Core Data & Action Schema (`controller/schema.py`)
- `ActionType(str, Enum)`: `click`, `double_click`, `move`, `type_text`, `press_keys`, `scroll`, `wait`, `launch_app`, `open_url`, `verify_state`, `view_region`, `view_screen`, `request_approval`, `done`, `fail`.
- `DesktopAction(BaseModel)` (`extra="forbid"`): `action_type`, `description` (plain English), optional `x`, `y`, `left`, `top`, `right`, `bottom`, `mark_id`, `text`, `keys`, `clicks`, `seconds`, `app_name`, `url`, `target_path`, `expected_text`, `reason`, `risk_category`, with strict `@model_validator(mode="after")` validation per action type.
- `SpeedPreset(str, Enum)`: `Slow`, `Normal`, `Fast`, `Custom`.
- `SpeedConfig(BaseModel)`: `preset`, `move_duration_sec`, `pixels_per_second`, `post_action_delay_sec`, `interpolation_steps`.

## 3. Safety & Triple Emergency Stop (`controller/emergency.py`)
- `SafetyController`:
  - Explicit Start Authorization Gate (`authorized: bool = False` initially; `authorize_start()` required before any mouse/keyboard input).
  - `pause()`, `resume()`, `enter_manual_takeover()`, `exit_manual_takeover()`.
  - Triple Emergency Stop triggers: `trigger_emergency_stop(source)` (`gui_button`, `global_hotkey`, `corner_failsafe`), `simulate_hotkey_f12()`, `check_corner_failsafe(x, y, width, height)`.
  - Once emergency-stopped, all further execution is blocked until explicit `reset_emergency_stop()`.

## 4. Policy & Approval Engine (`controller/policy.py`)
- `APPROVED_WORKSPACE` defaults to `<repo_root>/approved_workspace` (overridable via `DESKTOP_CONTROLLER_WORKSPACE` env var or `PolicyEngine(approved_workspace=...)`).
- Deterministic `PolicyEngine.evaluate(action: DesktopAction) -> PolicyDecision(allowed, requires_approval, risk_category, reason)`:
  - Blocks admin/privilege escalation (`runas`, UAC elevation) and path writes/reads outside `approved_workspace`.
  - Requires explicit user approval (`requires_approval=True`) before: sending emails/messages, publishing social posts, submitting applications/web forms, deleting or overwriting important files, installing/executing software, changing Windows/system settings, or accessing sensitive directories/credentials.

## 5. Desktop Driver (`controller/driver.py`)
- `compute_smooth_path(start_xy, end_xy, steps) -> list[tuple[int, int]]` (cubic ease-in-out interpolation, never instant teleportation).
- `BaseDesktopDriver` implemented by `MockDryRunDriver` (for `DESKTOP_CONTROLLER_TEST_MODE=1` / pytest) and `LiveWindowsDesktopDriver` (`pyautogui.FAILSAFE = True`, `PIL.ImageGrab`/`mss`, `pywinauto`/Win32).

## 6. SQLite Audit Store (`controller/store.py`)
- `SQLiteAuditStore(db_path)` persisting `task_runs` and `action_logs` with `redact_secrets()` preventing API keys/tokens from entering SQLite or logs.

## 7. Pluggable Planners, Interpreter, UIA & Execution Loop (`controller/planners.py`, `controller/interpreter.py`, `controller/uia.py`, `controller/engine.py`)
- `BasePlanInterpreter` (`AntigravityCLIInterpreter` + `OllamaPlanInterpreter` via `HybridPlanInterpreter`) classifies tasks using active/visible window context into structured `TaskPlan` objects (supporting web tab/window reuse and adaptive recovery).
- `collect_foreground_ui_elements` (`controller/uia.py`) runs Windows UI Automation (`UIAutomationCore.dll`) on a dedicated single-thread MTA COM worker with task-keyword prioritization (`prioritize_ui_elements`).
- `BasePlanner` implemented by `GeminiPlanner` (`GEMINI_API_KEY`), `OpenAIPlanner` (`OPENAI_API_KEY`), `OllamaPlanner` (`http://127.0.0.1:11434`), and `LocalDemoFallbackPlanner`.
- `DesktopExecutionEngine` executes Observe -> Plan -> Validate -> Policy/Approval Gate -> Act -> Verify with normalized `0..1000` AI viewport coordinates, duplicate/loop guards, and bounded `max_retries`.

## 8. Window B GUI (`controller/gui.py`, `main.py`)
- PySide6 `DesktopControllerWindow(QMainWindow)` running side-by-side with FunGuys Window A, providing task input, speed presets, backend selector + status, live screenshot preview, interactive Approve/Deny panel, Start/Pause/Resume/Manual Takeover/Emergency Stop buttons, and scrollable Action History.