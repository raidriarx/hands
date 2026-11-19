"""Subscription-backed task plan interpreter (Antigravity CLI + local Ollama fallback)."""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator


class TaskPlan(BaseModel):
    """High-level execution plan produced before or during desktop task execution."""

    intent_summary: str
    strategy: Literal["web_browser", "desktop_app", "hybrid"]
    starting_url: Optional[str] = None
    desktop_app: Optional[str] = None
    steps: list[str] = Field(default_factory=list)
    success_criteria: str
    reuse_existing_window: bool = False
    source: str = "antigravity_cli"

    @field_validator("starting_url", mode="before")
    @classmethod
    def _clean_starting_url(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        if not cleaned:
            return None
        parsed = urllib.parse.urlparse(cleaned)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        return cleaned

    @field_validator("desktop_app", mode="before")
    @classmethod
    def _clean_desktop_app(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("steps", mode="before")
    @classmethod
    def _clean_steps(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        cleaned: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                cleaned.append(text)
            if len(cleaned) >= 8:
                break
        return cleaned


def resolve_antigravity_executable() -> Optional[str]:
    """Locate the Antigravity CLI executable (`agy.exe`) if installed."""
    which_path = shutil.which("agy")
    if which_path and Path(which_path).is_file():
        return which_path
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidate = Path(local_app_data) / "agy" / "bin" / "agy.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _build_interpreter_prompt(
    task_text: str,
    failure_context: Optional[str] = None,
    active_window_title: Optional[str] = None,
    visible_window_titles: Optional[list[str]] = None,
) -> str:
    extra_context = ""
    if active_window_title:
        extra_context += f"\nCurrent active window title: {active_window_title}"
    if visible_window_titles:
        extra_context += (
            f"\nCurrently visible open windows: "
            f"{json.dumps(list(visible_window_titles)[:12], ensure_ascii=False)}"
        )
    if failure_context:
        extra_context += (
            f"\nPrevious execution attempt failed with: {failure_context}\n"
            "Adapt the plan to recover cleanly (for example, switch from launch_app to open_url "
            "with a direct https:// URL, or try a different UI navigation approach)."
        )

    return (
        "Do NOT call any tools, read files, or run commands. Reply ONLY with a raw JSON object "
        "containing these exact keys:\n"
        "- intent_summary (string)\n"
        "- strategy ('web_browser' | 'desktop_app' | 'hybrid')\n"
        "- starting_url (string http/https URL or null)\n"
        "- desktop_app (bare Windows executable name like 'notepad', 'calc', 'explorer', or null)\n"
        "- reuse_existing_window (boolean: true if the target website/app is already open in an active "
        "or visible window and should be reused instead of opening a new tab)\n"
        "- steps (array of 3-6 concise actionable step strings)\n"
        "- success_criteria (string)\n\n"
        "Classification rules:\n"
        "- Distinguish websites/web services (Instagram, YouTube, YouTube Music, Gmail, Discord Web, "
        "X/Twitter, Reddit, WhatsApp Web, Spotify Web, Google Docs, etc.) from native installed Windows apps.\n"
        "- NEVER assume a website or social platform is an installed .exe desktop app.\n"
        "Tab & Window Reuse:\n"
        "- Inspect 'Current active window title' and 'Currently visible open windows' BEFORE deciding to open a new tab.\n"
        "- If the target website (e.g. Instagram) is ALREADY the active window, set reuse_existing_window=true and "
        "starting_url=null, and make Step 1 stay in the current tab (do NOT call open_url or open a new tab).\n"
        "- If the target website is open in another visible background window, set reuse_existing_window=true and "
        "instruct Step 1 to focus/reuse that existing window instead of opening a duplicate tab.\n"
        "- If a browser is already open on a different site, instruct Step 1 to check the visible browser tab bar "
        "first and click an existing tab for the target site if present; ONLY open a new tab via open_url if no "
        "existing tab for the target site is open or if a brand-new specific search URL (?q=...) is required.\n"
        "- If the target website is NOT open in any visible window, set strategy='web_browser', "
        "reuse_existing_window=false, provide a direct deep starting_url (such as "
        "'https://www.instagram.com/direct/inbox/' for Instagram DMs or "
        "'https://music.youtube.com/search?q=...' for YouTube Music), and make Step 1 use action_type='open_url'.\n"
        "Messaging & Recipient Accuracy:\n"
        "- For messaging/DM tasks (Instagram, Discord, WhatsApp, etc.), include steps to: "
        "(a) locate the exact recipient via ui_elements mark_id, zooming into the left DM sidebar with view_region, "
        "or using the chat Search box (never guessing blindly from full screen); "
        "(b) verify the active conversation header at the top of the right chat pane matches the intended "
        "recipient BEFORE typing or sending; "
        "(c) send ONE message/greeting (press Enter), and if asked to wait for a response, wait 5-8 seconds "
        "and check incoming messages without repeating the same greeting or typing the username into the chat box.\n"
        f"Task: {task_text}{extra_context}"
    )


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extract and parse a JSON object from raw model output (stripping fences if present)."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(text[start : end + 1])
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Interpreter response did not contain a valid JSON object")


class BasePlanInterpreter(ABC):
    """Abstract interface for high-level desktop task plan interpretation."""

    name: str = "BasePlanInterpreter"

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return (available, human_readable_status)."""

    @abstractmethod
    def interpret_task(
        self,
        task_text: str,
        *,
        failure_context: Optional[str] = None,
        active_window_title: Optional[str] = None,
        visible_window_titles: Optional[list[str]] = None,
    ) -> TaskPlan:
        """Interpret a natural-language task into a structured TaskPlan."""

    def interpret_task_safe(
        self,
        task_text: str,
        *,
        failure_context: Optional[str] = None,
        active_window_title: Optional[str] = None,
        visible_window_titles: Optional[list[str]] = None,
    ) -> Optional[TaskPlan]:
        """Return TaskPlan or None if interpretation fails without raising."""
        try:
            return self.interpret_task(
                task_text,
                failure_context=failure_context,
                active_window_title=active_window_title,
                visible_window_titles=visible_window_titles,
            )
        except Exception:
            return None


class AntigravityCLIInterpreter(BasePlanInterpreter):
    """Plan interpreter backed by the user's signed-in Antigravity CLI (`agy`) subscription."""

    name: str = "Antigravity CLI"

    def __init__(
        self,
        executable: Optional[str] = None,
        timeout_sec: float = 40.0,
    ) -> None:
        self.executable = executable
        self.timeout_sec = max(15.0, float(timeout_sec))

    def _resolve_exe(self) -> Optional[str]:
        if self.executable:
            return self.executable
        return resolve_antigravity_executable()

    def is_available(self) -> tuple[bool, str]:
        exe = self._resolve_exe()
        if exe:
            return True, f"Antigravity CLI ready ({exe})"
        return False, "Antigravity CLI (agy) not found"

    def interpret_task(
        self,
        task_text: str,
        *,
        failure_context: Optional[str] = None,
        active_window_title: Optional[str] = None,
        visible_window_titles: Optional[list[str]] = None,
    ) -> TaskPlan:
        exe = self._resolve_exe()
        if not exe:
            raise RuntimeError("Antigravity CLI executable (agy) not found")

        prompt = _build_interpreter_prompt(
            task_text,
            failure_context=failure_context,
            active_window_title=active_window_title,
            visible_window_titles=visible_window_titles,
        )
        print_timeout = f"{max(10, int(self.timeout_sec - 5))}s"
        with tempfile.TemporaryDirectory() as tmp_dir:
            proc = subprocess.run(
                [
                    exe,
                    "-p",
                    prompt,
                    "--output-format",
                    "json",
                    "--disable-slash-commands",
                    "--print-timeout",
                    print_timeout,
                ],
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                shell=False,
            )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise RuntimeError(f"Antigravity CLI exited with code {proc.returncode}: {detail}")

        envelope = _extract_json_object(proc.stdout)
        if envelope.get("status") != "SUCCESS":
            raise RuntimeError(
                f"Antigravity CLI returned non-success status: {envelope.get('error') or envelope.get('status')}"
            )
        inner_raw = str(envelope.get("response") or "")
        plan_dict = _extract_json_object(inner_raw)
        plan_dict["source"] = "antigravity_cli"
        return TaskPlan.model_validate(plan_dict)


class OllamaPlanInterpreter(BasePlanInterpreter):
    """Fallback local Ollama plan interpreter using qwen3.5:latest."""

    name: str = "Local Ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3.5:latest",
        timeout_sec: float = 45.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = max(5.0, float(timeout_sec))

    def is_available(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=1.5)
            if resp.status_code == 200:
                return True, f"Ollama reachable at {self.base_url}"
            return False, f"Ollama returned HTTP {resp.status_code}"
        except Exception as exc:
            return False, f"Ollama unreachable: {exc}"

    def interpret_task(
        self,
        task_text: str,
        *,
        failure_context: Optional[str] = None,
        active_window_title: Optional[str] = None,
        visible_window_titles: Optional[list[str]] = None,
    ) -> TaskPlan:
        prompt = _build_interpreter_prompt(
            task_text,
            failure_context=failure_context,
            active_window_title=active_window_title,
            visible_window_titles=visible_window_titles,
        )
        resp = httpx.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "think": False,
                "options": {"temperature": 0.1},
            },
            timeout=self.timeout_sec,
        )
        resp.raise_for_status()
        data = resp.json()
        plan_dict = _extract_json_object(str(data.get("response") or ""))
        plan_dict["source"] = "ollama"
        return TaskPlan.model_validate(plan_dict)


class HybridPlanInterpreter(BasePlanInterpreter):
    """Tries Antigravity CLI subscription first, falling back to local Ollama."""

    name: str = "Antigravity + Ollama"

    def __init__(
        self,
        primary: Optional[BasePlanInterpreter] = None,
        fallback: Optional[BasePlanInterpreter] = None,
    ) -> None:
        self.primary: BasePlanInterpreter = primary or AntigravityCLIInterpreter()
        self.fallback: BasePlanInterpreter = fallback or OllamaPlanInterpreter()

    def is_available(self) -> tuple[bool, str]:
        ok_p, msg_p = self.primary.is_available()
        if ok_p:
            return True, msg_p
        ok_f, msg_f = self.fallback.is_available()
        if ok_f:
            return True, f"{msg_p}; fallback ready ({msg_f})"
        return False, f"{msg_p}; {msg_f}"

    def interpret_task(
        self,
        task_text: str,
        *,
        failure_context: Optional[str] = None,
        active_window_title: Optional[str] = None,
        visible_window_titles: Optional[list[str]] = None,
    ) -> TaskPlan:
        last_exc: Optional[Exception] = None
        for interpreter in (self.primary, self.fallback):
            available, _ = interpreter.is_available()
            if not available:
                continue
            try:
                params = inspect.signature(interpreter.interpret_task).parameters
                accepts_kwargs = any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
                )
                kwargs: dict[str, Any] = {}
                if accepts_kwargs or "failure_context" in params:
                    kwargs["failure_context"] = failure_context
                if accepts_kwargs or "active_window_title" in params:
                    kwargs["active_window_title"] = active_window_title
                if accepts_kwargs or "visible_window_titles" in params:
                    kwargs["visible_window_titles"] = visible_window_titles
                return interpreter.interpret_task(task_text, **kwargs)
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No plan interpreter backend is available")
