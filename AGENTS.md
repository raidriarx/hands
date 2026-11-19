# AGENTS.md — AI Coding Agent Onboarding Guide

High-signal guide for AI coding agents cloning and extending `https://github.com/raidriarx/hands`.

---

## 1. Instant Startup Checklist

Run in Windows PowerShell from the cloned repository root:

```powershell
.\setup.ps1
```

If dependencies are already installed (or verifying manually):

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe main.py --headless-check --speed Slow --backend "Antigravity + Ollama" --supervised
```

Always check [`CONTRACTS.md`](CONTRACTS.md) before modifying schema, safety, policy, or engine APIs.

---

## 2. Architecture & File Map

| Path | Responsibility |
| :--- | :--- |
| `main.py` | CLI entry point (`--speed`, `--backend`, `--task`, `--auto-start`, `--supervised`/`--approval-mode`, `--headless-check`). Creates `approved_workspace/` and `controller_history.db` relative to repo root. |
| `setup.ps1` | Bootstrap script (PowerShell 5.1+ compatible): creates `.venv`, installs `requirements.txt`, ensures `approved_workspace/`, runs pytest + `--headless-check`, and probes Ollama & `agy`. |
| `run_bot.ps1` | Short PowerShell launcher defaulting to `--speed Slow --backend "Antigravity + Ollama" --supervised --auto-start` when task text is supplied. |
| `requirements.txt` | Pip dependencies (`PySide6-Essentials`, `pydantic`, `httpx`, `pillow`, `PyAutoGUI`, `mss`, `pyperclip`, `python-dotenv`, `comtypes`, `pywin32`, `pytest`). |
| `.env.example` | Optional environment template (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `DESKTOP_CONTROLLER_WORKSPACE`). |
| `conftest.py` | Sets `QT_QPA_PLATFORM=offscreen` and `DESKTOP_CONTROLLER_TEST_MODE=1` and adds `.venv` site-packages to `sys.path`. |
| `CONTRACTS.md` | Module separation and interface contracts (`DesktopAction`, `SafetyController`, `PolicyEngine`, drivers, audit store, GUI). |
| `controller/schema.py` | Strictly validated `DesktopAction` (`extra="forbid"`, 15 `ActionType` values) and `SpeedConfig` / `SpeedPreset`. |
| `controller/emergency.py` | `SafetyController` authorization gate, pause/manual-takeover state, and triple E-stop (`gui_button`, `global_hotkey`, `corner_failsafe`). |
| `controller/policy.py` | `PolicyEngine` + portable `APPROVED_WORKSPACE` (`<repo_root>/approved_workspace`, overridable via `DESKTOP_CONTROLLER_WORKSPACE`). Hard-blocks elevation/outside paths; gates 7 risk categories. |
| `controller/driver.py` | `BaseDesktopDriver`, `MockDryRunDriver`, `LiveWindowsDesktopDriver`: smooth cubic cursor interpolation, bot-vs-human pointer ownership tracking, `pyperclip` Unicode typing fallback, smart browser window reuse (`find_matching_browser_window_title`), Win32 focus/window enumeration helpers. |
| `controller/uia.py` | Non-blocking Windows UI Automation (`UIAutomationCore.dll` `FindAllBuildCache`) on a dedicated MTA COM thread (`_UIAWorker`), plus `extract_priority_terms` and `prioritize_ui_elements`. |
| `controller/interpreter.py` | Pre-run & adaptive `TaskPlan` interpreter (`AntigravityCLIInterpreter` via `agy.exe`, `OllamaPlanInterpreter` fallback, `HybridPlanInterpreter`) with active & visible window title awareness and web tab reuse guidance. |
| `controller/planners.py` | `ObservationContext`, `_build_observation_prompt`, `_parse_action_json`, coordinate normalization/viewport zoom helpers, `GeminiPlanner`, `OpenAIPlanner`, `OllamaPlanner`, `LocalDemoFallbackPlanner`, screenshot annotation. |
| `controller/engine.py` | `DesktopExecutionEngine.run_task`: Observe -> Plan -> Validate -> Policy/Approval Gate -> Act -> Verify loop, viewport zoom stack (max depth 2), UIA `mark_id` resolution, retry/cycle/anti-spam/duplicate-`open_url` guards, SQLite + Qt callbacks. |
| `controller/store.py` | `SQLiteAuditStore` (`task_runs`, `action_logs` tables in `controller_history.db`) with `redact_secrets()` scrubbing API keys/tokens. |
| `controller/gui.py` | PySide6 `DesktopControllerWindow` (compact always-on-top corner HUD + `BotCharacterOverlay`), `WDA_EXCLUDEFROMCAPTURE` (`0x11`), `check_and_dodge_bot_cursor()` (dodges bot pointer only), `Ask` / `Supervised` modes, worker signal lifecycle safety. |
| `tests/` | `test_schema_and_stop.py`, `test_driver_and_gui.py`, `test_policy_and_engine.py`, `test_audit_edge_cases.py`. |

---

## 3. Core Contracts & Invariants

1. **Strict Planning vs Execution Split**:
   - `DesktopAction` has `extra="forbid"` and 15 `ActionType` values (`click`, `double_click`, `move`, `type_text`, `press_keys`, `scroll`, `wait`, `launch_app`, `open_url`, `verify_state`, `view_region`, `view_screen`, `request_approval`, `done`, `fail`).
   - All actions are validated via Pydantic and evaluated by `PolicyEngine` before reaching any driver.

2. **Coordinate Spaces (`0..1000` vs Raw Pixels)**:
   - AI vision planners (`GeminiPlanner`, `OpenAIPlanner`, `OllamaPlanner`) **only** see and return **normalized `0..1000` coordinates** relative to the current image (`viewport_mode` = `"full"` or `"region"`).
   - **NEVER leak raw pixel dimensions or coordinates** (`screen_size`, raw `cursor_pos`, raw `som_marks`) into `_build_observation_prompt`. Wait for `tests/test_audit_edge_cases.py::TestBrowserControlAndApiKeyUX::test_build_observation_prompt_never_leaks_raw_pixels` to enforce this.
   - `LocalDemoFallbackPlanner` and custom `BasePlanner` subclasses in tests continue to receive/emit raw screen pixel coordinates.

3. **Safety & Policy Boundaries**:
   - Even when `auto_approve_supervised=True` (`--supervised`), hard blocks (`privilege_escalation`, `sandbox_violation`, outside-workspace paths) **MUST** remain blocked (`allowed=False`).
   - `view_region`, `view_screen`, `wait`, and benign `http://`/`https://` `open_url` actions are exempt from approval prompts (except `install_or_execute`, `system_settings`, `sensitive_access`, or executable downloads).

4. **Capture Exclusion & Cursor Dodging**:
   - Both `DesktopControllerWindow` and `BotCharacterOverlay` apply Win32 `SetWindowDisplayAffinity(hwnd, 0x11)` (`WDA_EXCLUDEFROMCAPTURE`) outside headless/test mode so they never occlude planner screenshots.
   - `check_and_dodge_bot_cursor()` relies on `driver.is_bot_driving_cursor()` so Window B runs away to the farthest corner when the *bot* cursor approaches, while staying still for the user's physical mouse.

5. **Smart Tab/Window Reuse & Messaging Safety**:
   - `find_matching_browser_window_title()` only reuses an existing browser window for clean landing/section URLs (e.g., `instagram.com`, `instagram.com/direct/inbox/`); it intentionally **returns `None`** for query URLs (`?q=...`) or specific content paths (`/reel/...`) and distinguishes `youtube.com` from `music.youtube.com`.
   - `prioritize_ui_elements()` ensures controls matching task keywords (including >=5-char fuzzy containment for slight username typos like 11-vs-12 `k` characters) and core controls (`search`, `message`, `send`, `direct`, `play`) survive above-the-fold browser chrome when `max_elements` truncates the list.
   - `DesktopExecutionEngine` prevents duplicate/near-duplicate message spamming via `_is_duplicate_message()`, resets `active_viewport` back to full-screen when blocking duplicate chat text so incoming replies become visible, and pops the last tracked sent text if `screen_changed_last_step` was `False` (so missed-focus retries remain allowed).

---

## 4. Hard-Won Pitfalls & Environment Gotchas

- **Ollama URL on Windows**: Always use `http://127.0.0.1:11434`, NOT `localhost` (avoids ~1.7s Windows IPv6 fallback latency per request).
- **Antigravity CLI (`agy`) flags**:
  - Run `agy -p <prompt> --output-format json --disable-slash-commands --print-timeout <N>s` in an empty `tempfile.TemporaryDirectory()`.
  - **NEVER** pass `--effort` (unsupported flag, exit code 1) or `--json-schema` (causes timeouts). Instruct raw JSON keys inside the prompt and parse with `_extract_json_object()`.
- **UIA COM Threading**: `UIAutomationCore.dll` / `comtypes` fails with STA/COM errors if initialized directly on worker threads using `pywinauto`. Keep UIA isolated on `_UIAWorker` in `controller/uia.py` with `ole32.CoInitializeEx(None, 0)` (MTA) and temporary `sys.coinit_flags = 0`.
- **Signature Compatibility in Tests**:
  - Custom test subclasses of `BasePlanInterpreter` may define `interpret_task(self, task_text, *, failure_context=None, active_window_title=None)` without `visible_window_titles`. Both `HybridPlanInterpreter` and `DesktopExecutionEngine._call_plan_interpreter` inspect `inspect.signature(...).parameters` before passing `visible_window_titles`—preserve this pattern.
  - Custom test `uia_collector` callbacks passed to `DesktopExecutionEngine` may take only 2 positional args `(viewport, screen_size)` — preserve `self._custom_uia_collector` branching before calling `collect_foreground_ui_elements(..., priority_terms=...)`.
- **Pytest / Qt Test Mode**:
  - Keep `DESKTOP_CONTROLLER_TEST_MODE=1` behavior intact (disables real subprocess `agy`, live COM UIA, `SetWindowDisplayAffinity`, and auto-collapsing details on `start_button.click()`).
  - The `qapp` fixture only exists in `tests/test_driver_and_gui.py`. In `tests/test_audit_edge_cases.py`, use `QApplication.instance() or QApplication([])`.
  - Worker thread signal emissions to closed Qt widgets must stay guarded against `RuntimeError`.
- **PowerShell 5.1 Compatibility**: Keep `setup.ps1` and `run_bot.ps1` compatible with Windows PowerShell 5.1 (no `??`, no `&&`), and verify both parse cleanly via `[System.Management.Automation.Language.Parser]::ParseFile(...)` in pytest.
- **Sandboxed Agent Shells**: If your agent shell runs inside an isolated Windows desktop (`exebox-...`), `GetForegroundWindow()` returns `0` and `EnumWindows` sees no interactive user apps. Run `pytest` and `--headless-check` to verify changes, and ask the user to launch `.\run_bot.ps1 ...` in their interactive desktop session for live testing.

---

## 5. Debugging Live Runs

When a live run misbehaves, inspect these local gitignored artifacts at the repo root:
1. **`controller_history.db`** (SQLite): query `task_runs` and `action_logs` for step-by-step actions, errors, policy decisions, and verifications.
2. **`approved_workspace/latest_step.png`** & **`approved_workspace/latest_view_region.png`**: visual snapshots from the latest observation and active zoom region (written during live runs).
