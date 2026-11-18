"""Strict action schema and safety/emergency-stop controller package."""

from __future__ import annotations

from controller.emergency import (
    VALID_HOTKEYS,
    VALID_STOP_SOURCES,
    EmergencyStopError,
    SafetyController,
    SafetyGateError,
    StopSource,
)
from controller.schema import (
    SPEED_PRESETS,
    ActionType,
    DesktopAction,
    SpeedConfig,
    SpeedPreset,
)

__all__ = [
    "ActionType",
    "DesktopAction",
    "EmergencyStopError",
    "SPEED_PRESETS",
    "SafetyController",
    "SafetyGateError",
    "SpeedConfig",
    "SpeedPreset",
    "StopSource",
    "VALID_HOTKEYS",
    "VALID_STOP_SOURCES",
]
