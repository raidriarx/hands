# hands — Autonomous Windows Desktop Controller

An autonomous, safety-gated Windows desktop agent (`Window B`) with strict Pydantic schema validation, workspace sandboxing, smooth human-paced cursor control, Windows UI Automation grounding, and hybrid AI planning (`Antigravity CLI` + local `Ollama` vision, `Gemini`, `OpenAI`, or deterministic fallback).

## Quickstart (Windows PowerShell)

```powershell
git clone https://github.com/raidriarx/hands.git
cd hands
.\setup.ps1
.\run_bot.ps1
```

Or launch Window B and auto-start a task immediately (in Supervised mode at `Slow` speed using `Antigravity + Ollama`):

```powershell
.\run_bot.ps1 open instagram and text My Goldfish hi
```

> **Note:** On a fresh machine, ensure Ollama is running (`http://127.0.0.1:11434`) with the default vision model pulled:
> ```powershell
> ollama pull qwen3.5:latest
> ```
> Recommended environment variable for vision context: `OLLAMA_CONTEXT_LENGTH=16384`.

---

## Planner Backends

Select a backend in Window B or via `.\run_bot.ps1 -Backend "..."`:

1. **`Antigravity + Ollama`** *(default in `run_bot.ps1`)*: Uses signed-in Antigravity CLI (`agy`) for high-level task planning (`TaskPlan` with active/visible window awareness and fallback to local Ollama) paired with local `OllamaPlanner` (`qwen3.5:latest` at `http://127.0.0.1:11434`) for step-by-step vision execution.
2. **`Local Ollama`**: Pure local `qwen3.5:latest` vision planner (`OllamaPlanInterpreter` fallback if planning enabled).
3. **`Gemini API`**: Requires `GEMINI_API_KEY` (via `.env`, environment variable, or the Window B API key field).
4. **`OpenAI API`**: Requires `OPENAI_API_KEY` (`gpt-4o-mini`).
5. **`Local Demo / Fallback`**: Deterministic local planner (`LocalDemoFallbackPlanner`) requiring no LLM or API keys.

See [`.env.example`](.env.example) if you want to configure cloud API keys or a custom sandbox workspace path (`DESKTOP_CONTROLLER_WORKSPACE`).

---

## Safety & Controls

- **Start Authorization Gate**: No mouse or keyboard input executes until Start is pressed (or `--auto-start` / `--live-demo-notepad` is passed explicitly).
- **Access Modes**:
  - `Ask Before Risky` (default in `main.py`): pauses and prompts for approval before sending messages, social posts, web form submissions, destructive keys/overwrites, installers, system settings, or sensitive paths.
  - `Supervised (Auto-Approve)` (`--supervised`, default in `run_bot.ps1`): auto-approves risk-gated actions while keeping hard blocks enforced.
- **Hard Policy Blocks (Never Bypassed)**: Blocks privilege/UAC elevation (`runas`, `sudo`, etc.) and any path read/write/execution outside `approved_workspace/`.
- **Pause & Triple Emergency Stop**:
  - Pause / Resume: `Pause (Esc)` button or standalone `Esc` key.
  - Emergency Stop: red `EMERGENCY STOP` UI button, global `F12` or `Ctrl+Shift+Esc` hotkey, or physical mouse corner failsafe (top-left/corner).
- **Compact Corner HUD**: Runs in a compact always-on-top corner window excluded from screen capture (`WDA_EXCLUDEFROMCAPTURE` / `0x11`). Automatically dodges away to another corner only when the bot-driven cursor approaches it, while staying still when your physical human mouse approaches it.

---

## Verification Commands

```powershell
# Run full automated test suite
.\.venv\Scripts\python.exe -m pytest tests -q

# Run offscreen GUI + CLI startup smoke check
.\.venv\Scripts\python.exe main.py --headless-check --speed Slow --backend "Antigravity + Ollama" --supervised
```

---

## For AI Coding Agents & Contributors

- Read [`AGENTS.md`](AGENTS.md) for the full module map, coordinate-space invariants, COM threading rules, and known Windows/test pitfalls.
- Read [`CONTRACTS.md`](CONTRACTS.md) for formal schema, safety, policy, and execution loop contracts.
