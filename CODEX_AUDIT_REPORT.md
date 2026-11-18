# Milestone 5: Codex & Gemini Architecture, Security & Edge-Case Audit Report (`CODEX_AUDIT_REPORT.md`)

## 1. Executive Summary
This report documents the Milestone 5 (`task-m5-codex-security-audit`) security, safety, schema, driver, and execution-engine audit for **Autonomous Windows Desktop Controller**, along with all deterministic remediations and automated edge-case verification tests in [tests/test_audit_edge_cases.py](tests/test_audit_edge_cases.py).

All 7 genuine findings identified by the read-only `codex exec` (`gpt-6.1-sol`) audit have been remediated across [controller/schema.py](controller/schema.py), [controller/policy.py](controller/policy.py), [controller/driver.py](controller/driver.py), and [controller/engine.py](controller/engine.py), and the full test suite passes (`131 passed` under `QT_QPA_PLATFORM=offscreen` and `DESKTOP_CONTROLLER_TEST_MODE=1`).

---

## 2. FunGuys `codex-gpt-worker` Runtime Limitation Observed in Milestone 2
During Milestone 2 orchestration under FunGuys, the automated `codex-gpt-worker` daemon encountered two host-runtime limitations (kept read-only / documented without modifying `C:\Users\User\Documents\funguys`):
1. **Unsupported `-m sol` model alias under ChatGPT auth**: `funguys/worker/runtime.py` maps Codex roles (`important_coding`, `gruntwork`, `testing`, `fallback`) to model `'sol'` and invokes `codex exec` with `-m sol`. Under ChatGPT account authentication, `codex-cli 0.167.0` rejects this model identifier with:
   `ERROR: The 'sol' model is not supported when using Codex with a ChatGPT account.`
2. **Spurious `@FG:` signal arbitration from prompt-template echo**: `funguys/worker/runtime.py` embeds example `@FG:` directives (`@FG:DISPUTE`, `@FG:HANDOFF`, `@FG:CLAIM`, etc.) inside the worker prompt template and parses stdout via `parse_raw_signals(output)`. When Codex echoes prompt context in error/stdout output, the regex scanner treats template examples as live worker signals and triggers spurious arbitration.

To obtain a genuine Codex architectural and security review without modifying the FunGuys host package, a direct read-only audit was conducted with `codex exec` (`gpt-6.1-sol`, `--sandbox read-only`), producing the 7 findings below.

---

## 3. Genuine `codex exec` (`gpt-6.1-sol`) Audit Findings & Remediations

### Finding 1 — Permission Gate & Sandbox Bypasses (`controller/policy.py`)
- **Finding**:
  - `PolicyEngine.evaluate()` did not inspect `action.keys`, allowing destructive keystrokes (`["delete"]`, `["shift", "delete"]`, `["ctrl", "shift", "delete"]`) to bypass `"delete_or_overwrite"` approval.
  - `PolicyEngine.evaluate()` did not validate `action.app_name` for outside-sandbox file paths (e.g. `C:\Temp\tool.exe`, `D:/evil.exe`, `\\server\share\x.exe`, `..\outside.exe`).
  - `_WINDOWS_DRIVE_PATH_PATTERN` and `_has_outside_path_in_text()` only matched backslashes (`\`), missing forward-slash paths such as `c:/windows/win.ini` and forward-slash UNC shares such as `//server/share`.
- **Remediation in `controller/policy.py`**:
  - Added destructive-key inspection in `evaluate()` so `"delete"` / `"del"` in `action.keys` triggers `requires_approval=True, risk_category="delete_or_overwrite"` (and `"submit"` combined with form keywords triggers `"submit_form"`), while keeping benign Notepad shortcuts (`["ctrl", "s"]`, `["enter"]`, `["alt", "f4"]`) safe.
  - Added `app_name` path-separator / drive / traversal inspection in `evaluate()` blocking outside-sandbox executable paths (`sandbox_violation`) while continuing to permit bare system app names (`"notepad"`, `"calc"`, `"mspaint"`).
  - Updated `_WINDOWS_DRIVE_PATH_PATTERN`, `_UNC_PATH_PATTERN`, `_SYSTEM_PATH_PATTERNS`, `_DOWNLOADS_EXEC_PATTERN`, and `_has_outside_path_in_text()` to detect both `/` and `\` paths (`c:/windows/win.ini`, `//server/share`) without false-positiving on `https://` or `http://` URLs.

### Finding 2 — Non-Interruptible Long Typing & Mid-Wait Safety Polling (`controller/driver.py`)
- **Finding**:
  - `LiveWindowsDesktopDriver.type_text()` delegated the entire string to `pyautogui.write(text, interval=interval)` in a single blocking call, preventing Emergency Stop, Pause, `F12`, `Ctrl+Shift+Esc`, or corner failsafe from interrupting long text mid-string.
  - `LiveWindowsDesktopDriver.wait()` needed explicit finiteness validation and clamped wait bounds alongside safety polling.
- **Remediation in `controller/driver.py`**:
  - Rewrote `LiveWindowsDesktopDriver.type_text()` to iterate character-by-character and invoke `self._poll_hotkeys_and_safety()` before every character (and before post-action delay).
  - Updated `LiveWindowsDesktopDriver.wait()` to reject non-finite durations (`math.isfinite`), clamp durations to `[0.0, 300.0]`, and poll `self._poll_hotkeys_and_safety()` on every slice.

### Finding 3 — Mid-Move Actual Cursor Failsafe & DPI Reconciliation (`controller/driver.py`)
- **Finding**:
  - `LiveWindowsDesktopDriver.move_to()` checked `_poll_hotkeys_and_safety(px, py, sw, sh)` using only the planned path coordinate `(px, py)`, missing a physical user flinging the real cursor into a corner during `step_delay` when the planned point remained near screen center.
  - `LiveWindowsDesktopDriver` did not call `SetProcessDPIAware()`, risking coordinate/screenshot mismatches under Windows display scaling, and lacked a deterministic DPI scaling + clamping helper.
- **Remediation in `controller/driver.py`**:
  - Added `reconcile_dpi_coordinates(x, y, source_scale=1.0, target_scale=1.0, screen_size=(1920, 1080))` to scale coordinates across DPI ratios and clamp within `[0, width-1] x [0, height-1]`.
  - Added best-effort `ctypes.windll.user32.SetProcessDPIAware()` in `LiveWindowsDesktopDriver.__init__()` on Windows (`os.name == "nt"`).
  - Updated `LiveWindowsDesktopDriver.move_to()` to poll BOTH the actual `self.get_cursor_position()` and planned `(px, py)` via `_poll_hotkeys_and_safety()` at every interpolation step.

### Finding 4 — Non-Finite / Unbounded Values & Key Control Characters (`controller/schema.py`)
- **Finding**:
  - `DesktopAction` accepted `float("nan")`, `float("inf")`, and arbitrarily huge `seconds`, `text`, or `clicks` values.
  - `SpeedConfig` accepted `nan`/`inf` and unbounded timings or interpolation steps.
  - `DesktopAction.keys` permitted control characters/newlines (`\n`, `\r`, `\x00`) and unbounded key string lengths.
- **Remediation in `controller/schema.py`**:
  - Imported `math` and enforced `math.isfinite` and upper bounds on `DesktopAction` (`seconds <= 300.0`, `len(text) <= 10000`, `abs(clicks) <= 100`).
  - Validated `DesktopAction.keys` entries contain no ASCII control characters (`ord(ch) < 32` or `127`) and are `<= 32` characters.
  - Enforced `math.isfinite` and upper bounds on `SpeedConfig` (`move_duration_sec <= 30.0`, `pixels_per_second <= 20000.0`, `post_action_delay_sec <= 30.0`, `interpolation_steps <= 500`).

### Finding 5 — Window Focus Confirmation & Active-Title Verification (`controller/driver.py`)
- **Finding**:
  - `_focus_window_by_title()` issued `SetForegroundWindow()` without verifying foreground HWND/PID ownership or using `AttachThreadInput` fallback, and `launch_app()` returned no focus-confirmation signal.
  - `LiveWindowsDesktopDriver.verify_state(expected_text=...)` scanned all visible windows without preferring the active foreground window title first.
- **Remediation in `controller/driver.py`**:
  - Enhanced `_focus_window_by_title()` to match `target_pid` via `GetWindowThreadProcessId` (with title fallback), confirm foreground ownership via `GetForegroundWindow()`, retry briefly with `AttachThreadInput` fallback, and return `(rect, focus_confirmed)`.
  - Recorded `"focus_confirmed": bool` in `launch_app()` metadata on both live and mock drivers.
  - Added `_get_foreground_window_title()` and updated `LiveWindowsDesktopDriver.verify_state(expected_text=...)` to inspect the active foreground window title first before falling back to all visible window titles.

### Finding 6 — Post-Plan/Approval Safety Race & Terminal Authorization Lifecycle (`controller/engine.py`)
- **Finding**:
  - `DesktopExecutionEngine.run_task()` did not re-check `self.safety.assert_can_execute()` after potentially slow `plan_next_step()` or blocking `approval_callback()` calls, allowing `done` or `_execute_action()` to proceed if Emergency Stop occurred mid-plan or mid-approval.
  - `SafetyController.authorized` remained `True` after a run finished, and unexpected exceptions during `run_task()` could leave `task_runs` unfinalized in SQLite.
- **Remediation in `controller/engine.py`**:
  - Added `self.safety.assert_can_execute()` re-checks immediately after `plan_next_step()`, after `approval_callback()`, and prior to terminal (`done`/`fail`) or physical (`_execute_action`) execution.
  - Wrapped `run_task()` in `try ... finally` so unexpected exceptions still call `store.finish_run(run_id, "failed", error=...)` if not already finalized and always revoke `self.safety.revoke_authorization()` on run exit (unless emergency-stopped, which already revokes it).

### Finding 7 — Stuck No-Op Loops, Wall-Clock Deadline & Partial-Failure History (`controller/engine.py`)
- **Finding**:
  - A planner repeating an identical no-op action could spin until `max_steps` or indefinitely in wall-clock time.
  - Failed action attempts during retries were logged to SQLite but omitted from `history` passed in `ObservationContext`, hiding partial failures from the planner.
- **Remediation in `controller/engine.py`**:
  - Added `max_no_op_repeats: int = 5` and `max_wall_seconds: float = 300.0` to `DesktopExecutionEngine.__init__()`.
  - Tracked consecutive identical `(action_type, x, y, text, keys, target_path)` tuples and enforced `max_wall_seconds` deadline so stuck loops fail cleanly.
  - Recorded failed action and planning attempts in `history` with `status: "error"` so the planner observes partial failures on subsequent steps.

---

## 4. Automated Verification Summary
Executed full test suite in isolated staging with `QT_QPA_PLATFORM=offscreen` and `DESKTOP_CONTROLLER_TEST_MODE=1`:

- Command: `C:\Users\User\Documents\funguys-lab\desktop-controller\.venv\Scripts\pytest.exe -v`
- Result: **131 passed in 0.52s** (0 failed, 0 skipped)
  - `tests/test_schema_and_stop.py`: 37 passed
  - `tests/test_driver_and_gui.py`: 11 passed
  - `tests/test_policy_and_engine.py`: 39 passed
  - `tests/test_audit_edge_cases.py`: 44 passed
