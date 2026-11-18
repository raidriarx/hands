"""Core Pydantic action schema and speed configuration for desktop control."""

from __future__ import annotations

import math
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, model_validator

MAX_WAIT_SECONDS = 300.0
MAX_TEXT_LENGTH = 10000
MAX_SCROLL_CLICKS = 100
MAX_KEY_LENGTH = 32
MAX_MOVE_DURATION_SEC = 30.0
MAX_PIXELS_PER_SECOND = 20000.0
MAX_POST_ACTION_DELAY_SEC = 30.0
MAX_INTERPOLATION_STEPS = 500


class ActionType(str, Enum):
    """Supported deterministic action types returned by planners."""

    click = "click"
    double_click = "double_click"
    move = "move"
    type_text = "type_text"
    press_keys = "press_keys"
    scroll = "scroll"
    wait = "wait"
    launch_app = "launch_app"
    verify_state = "verify_state"
    request_approval = "request_approval"
    done = "done"
    fail = "fail"

    # Uppercase aliases for convenience
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    MOVE = "move"
    TYPE_TEXT = "type_text"
    PRESS_KEYS = "press_keys"
    SCROLL = "scroll"
    WAIT = "wait"
    LAUNCH_APP = "launch_app"
    VERIFY_STATE = "verify_state"
    REQUEST_APPROVAL = "request_approval"
    DONE = "done"
    FAIL = "fail"


class DesktopAction(BaseModel):
    """Strict structured schema for a single desktop controller action."""

    model_config = ConfigDict(extra="forbid")

    action_type: ActionType
    description: str
    x: Optional[int] = None
    y: Optional[int] = None
    mark_id: Optional[int] = None
    text: Optional[str] = None
    keys: Optional[list[str]] = None
    clicks: Optional[int] = None
    seconds: Optional[float] = None
    app_name: Optional[str] = None
    target_path: Optional[str] = None
    expected_text: Optional[str] = None
    reason: Optional[str] = None
    risk_category: Optional[str] = None

    @model_validator(mode="after")
    def _validate_action_fields(self) -> DesktopAction:
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("description must be a non-empty string")

        if self.x is not None and self.x < 0:
            raise ValueError("x coordinate must be non-negative")
        if self.y is not None and self.y < 0:
            raise ValueError("y coordinate must be non-negative")
        if (self.x is None) != (self.y is None):
            raise ValueError("both x and y coordinates must be provided together")

        if self.text is not None and len(self.text) > MAX_TEXT_LENGTH:
            raise ValueError(f"text length must not exceed {MAX_TEXT_LENGTH} characters")

        if self.clicks is not None and abs(self.clicks) > MAX_SCROLL_CLICKS:
            raise ValueError(f"scroll clicks magnitude must not exceed {MAX_SCROLL_CLICKS}")

        if self.seconds is not None:
            if not math.isfinite(self.seconds) or self.seconds < 0 or self.seconds > MAX_WAIT_SECONDS:
                raise ValueError(f"seconds must be finite and within [0, {MAX_WAIT_SECONDS}]")

        if self.keys is not None:
            for key in self.keys:
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("keys entries must be non-empty strings")
                if len(key) > MAX_KEY_LENGTH:
                    raise ValueError(f"key entry {key!r} exceeds maximum length of {MAX_KEY_LENGTH}")
                if any(ord(ch) < 32 or ord(ch) == 127 for ch in key):
                    raise ValueError("keys entries must not contain control characters or newlines")

        at = self.action_type
        if at in (ActionType.click, ActionType.double_click, ActionType.move):
            has_coords = self.x is not None and self.y is not None
            has_mark = self.mark_id is not None and self.mark_id >= 1
            if not (has_coords or has_mark):
                raise ValueError(f"{at.value} requires non-negative x and y coordinates or mark_id >= 1")
        elif at == ActionType.type_text:
            if self.text is None or len(self.text) == 0:
                raise ValueError("type_text requires a non-empty text string")
        elif at == ActionType.press_keys:
            if not self.keys:
                raise ValueError("press_keys requires a non-empty list of non-empty key strings")
        elif at == ActionType.scroll:
            if self.clicks is None or self.clicks == 0:
                raise ValueError("scroll requires a non-zero clicks integer")
        elif at == ActionType.wait:
            if self.seconds is None:
                raise ValueError("wait requires finite non-negative seconds")
        elif at == ActionType.launch_app:
            if self.app_name is None or not self.app_name.strip():
                raise ValueError("launch_app requires a non-empty app_name")
        elif at == ActionType.verify_state:
            has_expected_text = self.expected_text is not None and bool(self.expected_text.strip())
            has_target_path = self.target_path is not None and bool(self.target_path.strip())
            if not (has_expected_text or has_target_path):
                raise ValueError("verify_state requires expected_text or target_path")
        elif at == ActionType.request_approval:
            if self.reason is None or not self.reason.strip():
                raise ValueError("request_approval requires a non-empty reason")
        elif at == ActionType.fail:
            if self.reason is None or not self.reason.strip():
                raise ValueError("fail requires a non-empty reason")
        elif at == ActionType.done:
            pass

        return self


class SpeedPreset(str, Enum):
    """Named speed profiles for cursor interpolation and pacing."""

    Slow = "Slow"
    Normal = "Normal"
    Fast = "Fast"
    Custom = "Custom"

    # Uppercase aliases for convenience
    SLOW = "Slow"
    NORMAL = "Normal"
    FAST = "Fast"
    CUSTOM = "Custom"


class SpeedConfig(BaseModel):
    """Execution timing and interpolation configuration."""

    model_config = ConfigDict(extra="forbid")

    preset: SpeedPreset = SpeedPreset.Normal
    move_duration_sec: float = 0.5
    pixels_per_second: float = 800.0
    post_action_delay_sec: float = 0.2
    interpolation_steps: int = 20

    @model_validator(mode="after")
    def _validate_timings(self) -> SpeedConfig:
        if (
            not math.isfinite(self.move_duration_sec)
            or self.move_duration_sec <= 0
            or self.move_duration_sec > MAX_MOVE_DURATION_SEC
        ):
            raise ValueError(
                f"move_duration_sec must be finite and within (0, {MAX_MOVE_DURATION_SEC}]"
            )
        if (
            not math.isfinite(self.pixels_per_second)
            or self.pixels_per_second <= 0
            or self.pixels_per_second > MAX_PIXELS_PER_SECOND
        ):
            raise ValueError(
                f"pixels_per_second must be finite and within (0, {MAX_PIXELS_PER_SECOND}]"
            )
        if (
            not math.isfinite(self.post_action_delay_sec)
            or self.post_action_delay_sec < 0
            or self.post_action_delay_sec > MAX_POST_ACTION_DELAY_SEC
        ):
            raise ValueError(
                f"post_action_delay_sec must be finite and within [0, {MAX_POST_ACTION_DELAY_SEC}]"
            )
        if self.interpolation_steps < 1 or self.interpolation_steps > MAX_INTERPOLATION_STEPS:
            raise ValueError(
                f"interpolation_steps must be within [1, {MAX_INTERPOLATION_STEPS}]"
            )
        return self

    @classmethod
    def from_preset(cls, preset: SpeedPreset | str) -> SpeedConfig:
        """Return a copy of the SpeedConfig matching the given preset."""
        if isinstance(preset, SpeedPreset):
            resolved = preset
        elif isinstance(preset, str):
            lookup = {member.value.lower(): member for member in SpeedPreset}
            normalized = preset.strip().lower()
            if normalized not in lookup:
                raise ValueError(f"Unknown speed preset: {preset!r}")
            resolved = lookup[normalized]
        else:
            raise TypeError(f"Unsupported preset type: {type(preset).__name__}")
        return SPEED_PRESETS[resolved].model_copy()


SPEED_PRESETS: dict[SpeedPreset, SpeedConfig] = {
    SpeedPreset.Slow: SpeedConfig(
        preset=SpeedPreset.Slow,
        move_duration_sec=1.0,
        pixels_per_second=400.0,
        post_action_delay_sec=0.5,
        interpolation_steps=35,
    ),
    SpeedPreset.Normal: SpeedConfig(
        preset=SpeedPreset.Normal,
        move_duration_sec=0.5,
        pixels_per_second=800.0,
        post_action_delay_sec=0.2,
        interpolation_steps=20,
    ),
    SpeedPreset.Fast: SpeedConfig(
        preset=SpeedPreset.Fast,
        move_duration_sec=0.2,
        pixels_per_second=1600.0,
        post_action_delay_sec=0.05,
        interpolation_steps=10,
    ),
    SpeedPreset.Custom: SpeedConfig(
        preset=SpeedPreset.Custom,
        move_duration_sec=0.5,
        pixels_per_second=800.0,
        post_action_delay_sec=0.2,
        interpolation_steps=20,
    ),
}
