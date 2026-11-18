"""Pluggable desktop action planners (Gemini, OpenAI, Ollama, Local Demo Fallback)."""

from __future__ import annotations

import base64
import io
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image
from pydantic import BaseModel, Field

from controller.policy import APPROVED_WORKSPACE
from controller.schema import ActionType, DesktopAction

_SYSTEM_INSTRUCTION = (
    "You are an autonomous Windows desktop planner. Respond ONLY with a single JSON object "
    "matching the DesktopAction schema: {action_type, description, x, y, text, keys, clicks, "
    "seconds, app_name, target_path, expected_text, reason, risk_category}. "
    "Valid action_type values: click, double_click, move, type_text, press_keys, scroll, wait, "
    "launch_app, verify_state, request_approval, done, fail. "
    "All file writes must stay inside approved_workspace. Omit null/unused optional fields or set null."
)


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _encode_screenshot_b64(screenshot: Optional[Image.Image]) -> Optional[str]:
    if screenshot is None:
        return None
    buf = io.BytesIO()
    screenshot.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _build_observation_prompt(obs: ObservationContext) -> str:
    payload = {
        "task_text": obs.task_text,
        "step_index": obs.step_index,
        "screen_size": list(obs.screen_size),
        "cursor_pos": list(obs.cursor_pos),
        "active_window_rect": list(obs.active_window_rect) if obs.active_window_rect else None,
        "approved_workspace": obs.approved_workspace,
        "last_error": obs.last_error,
        "recent_history": obs.history[-8:],
    }
    return f"{_SYSTEM_INSTRUCTION}\n\nObservation:\n{json.dumps(payload)}"


class ObservationContext(BaseModel):
    """Structured state snapshot passed to planners before selecting the next step."""

    task_text: str
    step_index: int = 0
    screen_size: tuple[int, int] = (1920, 1080)
    cursor_pos: tuple[int, int] = (960, 540)
    active_window_rect: Optional[tuple[int, int, int, int]] = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    last_error: Optional[str] = None
    approved_workspace: str = str(APPROVED_WORKSPACE)


class BasePlanner(ABC):
    """Abstract base class for AI and deterministic desktop planners."""

    name: str = "BasePlanner"

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return (available, human_readable_status) for this planner backend."""

    @abstractmethod
    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        """Return the next strictly validated DesktopAction given observation state."""


class GeminiPlanner(BasePlanner):
    """Google Gemini REST planner using runtime GEMINI_API_KEY."""

    name: str = "Gemini API"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-2.5-flash",
    ) -> None:
        self._api_key = (
            api_key.strip()
            if api_key is not None
            else os.environ.get("GEMINI_API_KEY", "").strip()
        )
        self.model = model

    def is_available(self) -> tuple[bool, str]:
        if self._api_key:
            return True, "GEMINI_API_KEY configured"
        return False, "Missing GEMINI_API_KEY"

    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        if not self._api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        parts: list[dict[str, Any]] = [{"text": _build_observation_prompt(obs)}]
        img_b64 = _encode_screenshot_b64(screenshot)
        if img_b64:
            parts.append({"inlineData": {"mimeType": "image/png", "data": img_b64}})

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        resp = httpx.post(
            url,
            params={"key": self._api_key},
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.1,
                },
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        return DesktopAction.model_validate_json(_strip_json_fences(raw_text))


class OpenAIPlanner(BasePlanner):
    """OpenAI Chat Completions JSON planner using runtime OPENAI_API_KEY."""

    name: str = "OpenAI API"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
    ) -> None:
        self._api_key = (
            api_key.strip()
            if api_key is not None
            else os.environ.get("OPENAI_API_KEY", "").strip()
        )
        self.model = model

    def is_available(self) -> tuple[bool, str]:
        if self._api_key:
            return True, "OPENAI_API_KEY configured"
        return False, "Missing OPENAI_API_KEY"

    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        user_content: list[dict[str, Any]] = [
            {"type": "text", "text": _build_observation_prompt(obs)}
        ]
        img_b64 = _encode_screenshot_b64(screenshot)
        if img_b64:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                }
            )

        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self.model,
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": _SYSTEM_INSTRUCTION},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["choices"][0]["message"]["content"]
        return DesktopAction.model_validate_json(_strip_json_fences(raw_text))


class OllamaPlanner(BasePlanner):
    """Local Ollama HTTP planner targeting http://localhost:11434."""

    name: str = "Local Ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama3.2-vision",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def is_available(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=1.5)
            if resp.status_code == 200:
                return True, f"Ollama reachable at {self.base_url}"
            return False, f"Ollama returned HTTP {resp.status_code}"
        except Exception as exc:
            return False, f"Ollama unreachable: {exc}"

    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": _build_observation_prompt(obs),
            "format": "json",
            "stream": False,
        }
        img_b64 = _encode_screenshot_b64(screenshot)
        if img_b64:
            payload["images"] = [img_b64]

        resp = httpx.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["response"]
        return DesktopAction.model_validate_json(_strip_json_fences(raw_text))


class LocalDemoFallbackPlanner(BasePlanner):
    """Deterministic local planner supporting Milestone 4 Notepad demo and workspace tasks."""

    name: str = "Local Demo / Fallback"

    _NOTEPAD_MESSAGE = "FunGuys Autonomous Desktop Controller - Milestone 4 Live Verification"

    def is_available(self) -> tuple[bool, str]:
        return True, "Ready (Local Demo)"

    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        lower_task = obs.task_text.lower()
        if "notepad" in lower_task or "milestone 4" in lower_task:
            return self._plan_notepad_demo(obs)

        if "verify" in lower_task or "workspace" in lower_task:
            if obs.step_index == 0:
                return DesktopAction(
                    action_type=ActionType.verify_state,
                    target_path=obs.approved_workspace,
                    description="Verify approved_workspace directory exists",
                )
            return DesktopAction(
                action_type=ActionType.done,
                description="Verified approved_workspace contents",
            )

        if obs.step_index == 0:
            return DesktopAction(
                action_type=ActionType.wait,
                seconds=0.1,
                description=f"Observe desktop state for task: {obs.task_text[:80]}",
            )
        return DesktopAction(
            action_type=ActionType.done,
            description=f"Completed local demo execution for: {obs.task_text[:80]}",
        )

    def _plan_notepad_demo(self, obs: ObservationContext) -> DesktopAction:
        if obs.active_window_rect is not None:
            left, top, right, bottom = obs.active_window_rect
            cx = max(1, (left + right) // 2)
            cy = max(1, (top + bottom) // 2)
        else:
            cx, cy = 520, 420

        demo_file = str(Path(obs.approved_workspace) / "milestone4_demo.txt")
        steps: list[DesktopAction] = [
            DesktopAction(
                action_type=ActionType.launch_app,
                app_name="notepad",
                description="Launch Windows Notepad",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.8,
                description="Wait for Notepad window to initialize",
            ),
            DesktopAction(
                action_type=ActionType.move,
                x=cx,
                y=cy,
                description="Move cursor smoothly into Notepad editor",
            ),
            DesktopAction(
                action_type=ActionType.click,
                x=cx,
                y=cy,
                description="Focus Notepad text editor area",
            ),
            DesktopAction(
                action_type=ActionType.type_text,
                text=self._NOTEPAD_MESSAGE,
                description="Type verification message in Notepad",
            ),
            DesktopAction(
                action_type=ActionType.press_keys,
                keys=["ctrl", "s"],
                description="Open Save dialog in Notepad",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.8,
                description="Wait for Save As dialog",
            ),
            DesktopAction(
                action_type=ActionType.type_text,
                text=demo_file,
                description="Enter approved_workspace file path",
            ),
            DesktopAction(
                action_type=ActionType.press_keys,
                keys=["enter"],
                description="Save milestone4_demo.txt inside approved_workspace",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.8,
                description="Wait for file save to complete",
            ),
            DesktopAction(
                action_type=ActionType.press_keys,
                keys=["alt", "f4"],
                description="Close Notepad before reopening verification",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.5,
                description="Wait for Notepad to close",
            ),
            DesktopAction(
                action_type=ActionType.launch_app,
                app_name="notepad",
                target_path=demo_file,
                description="Reopen saved milestone4_demo.txt in Notepad",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.8,
                description="Wait for reopened Notepad file to render",
            ),
            DesktopAction(
                action_type=ActionType.verify_state,
                target_path=demo_file,
                expected_text=self._NOTEPAD_MESSAGE,
                description="Verify saved file exists in approved_workspace and contents match",
            ),
            DesktopAction(
                action_type=ActionType.done,
                description="Milestone 4 Notepad demonstration completed and verified",
            ),
        ]

        if obs.step_index < len(steps):
            return steps[obs.step_index]
        return steps[-1]


def create_planner(backend_name: str, api_key: Optional[str] = None) -> BasePlanner:
    """Instantiate a planner backend by display name or short alias."""
    normalized = backend_name.strip().lower()
    if normalized in {"local demo / fallback", "local", "fallback", "demo"}:
        return LocalDemoFallbackPlanner()
    if normalized in {"gemini api", "gemini"}:
        return GeminiPlanner(api_key=api_key)
    if normalized in {"openai api", "openai"}:
        return OpenAIPlanner(api_key=api_key)
    if normalized in {"local ollama", "ollama"}:
        return OllamaPlanner()
    raise ValueError(f"Unknown planner backend: {backend_name!r}")
