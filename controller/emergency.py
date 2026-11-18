"""Safety authorization gate, pause/takeover controls, and triple emergency stop."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class SafetyGateError(RuntimeError):
    """Raised when an execution attempt violates a safety gate."""


class EmergencyStopError(SafetyGateError):
    """Raised when execution is blocked due to an active emergency stop."""


class StopSource(str, Enum):
    """Enumerated emergency stop trigger sources."""

    gui_button = "gui_button"
    global_hotkey = "global_hotkey"
    corner_failsafe = "corner_failsafe"

    GUI_BUTTON = "gui_button"
    GLOBAL_HOTKEY = "global_hotkey"
    CORNER_FAILSAFE = "corner_failsafe"


VALID_STOP_SOURCES: tuple[str, ...] = (
    StopSource.gui_button.value,
    StopSource.global_hotkey.value,
    StopSource.corner_failsafe.value,
)
VALID_HOTKEYS: set[str] = {"f12", "ctrl+shift+esc", "ctrl+shift+escape"}
VALID_PAUSE_HOTKEYS: set[str] = {"esc", "escape"}


class SafetyController:
    """Manages Start authorization, pause/manual takeover, and triple emergency stop."""

    def __init__(self, corner_margin: int = 5) -> None:
        if corner_margin < 0:
            raise ValueError("corner_margin must be non-negative")
        self.authorized: bool = False
        self.paused: bool = False
        self.manual_takeover: bool = False
        self.emergency_stopped: bool = False
        self.stop_source: Optional[str] = None
        self.corner_margin: int = corner_margin
        self.stop_events: list[dict[str, Any]] = []

    def authorize_start(self) -> bool:
        """Explicitly authorize execution start; blocked if emergency-stopped."""
        if self.emergency_stopped:
            raise EmergencyStopError(
                f"Cannot authorize start while emergency stop is active (source={self.stop_source})"
            )
        self.authorized = True
        self.paused = False
        self.manual_takeover = False
        return True

    def revoke_authorization(self) -> None:
        """Revoke explicit start authorization."""
        self.authorized = False

    def pause(self) -> None:
        """Pause automated execution."""
        self.paused = True

    def resume(self) -> None:
        """Resume automated execution if authorized and not halted or taken over."""
        if self.emergency_stopped:
            raise EmergencyStopError(
                f"Cannot resume while emergency stop is active (source={self.stop_source})"
            )
        if self.manual_takeover:
            raise SafetyGateError("Cannot resume while manual takeover is active")
        if not self.authorized:
            raise SafetyGateError("Cannot resume before explicit start authorization")
        self.paused = False

    def enter_manual_takeover(self) -> None:
        """Hand control to the user and pause automation."""
        self.manual_takeover = True
        self.paused = True

    def exit_manual_takeover(self) -> None:
        """Return control from manual takeover unless emergency-stopped."""
        if self.emergency_stopped:
            raise EmergencyStopError(
                f"Cannot exit manual takeover while emergency stop is active (source={self.stop_source})"
            )
        self.manual_takeover = False
        self.paused = False

    def trigger_emergency_stop(self, source: str | StopSource = "gui_button") -> None:
        """Activate emergency stop from one of the three sanctioned sources."""
        source_value = source.value if isinstance(source, StopSource) else source
        if source_value not in VALID_STOP_SOURCES:
            raise ValueError(
                f"Invalid emergency stop source {source!r}; expected one of {VALID_STOP_SOURCES}"
            )
        self.emergency_stopped = True
        self.authorized = False
        self.paused = True
        self.stop_source = source_value
        self.stop_events.append(
            {
                "source": source_value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def simulate_hotkey_f12(self) -> bool:
        """Trigger emergency stop via simulated F12 global hotkey."""
        self.trigger_emergency_stop(StopSource.global_hotkey.value)
        return True

    def simulate_hotkey_ctrl_shift_esc(self) -> bool:
        """Trigger emergency stop via simulated Ctrl+Shift+Esc global hotkey."""
        self.trigger_emergency_stop(StopSource.global_hotkey.value)
        return True

    def simulate_hotkey_escape(self) -> bool:
        """Pause execution via simulated Esc hotkey unless emergency-stopped."""
        if not self.emergency_stopped:
            self.pause()
        return True

    def check_global_hotkey(self, hotkey: str) -> bool:
        """Trigger emergency stop for F12 / Ctrl+Shift+Esc or pause for standalone Esc."""
        if not isinstance(hotkey, str):
            return False
        normalized = "+".join(part.strip().lower() for part in hotkey.split("+") if part.strip())
        if normalized in VALID_HOTKEYS:
            self.trigger_emergency_stop(StopSource.global_hotkey.value)
            return True
        if normalized in VALID_PAUSE_HOTKEYS:
            self.simulate_hotkey_escape()
            return True
        return False

    def check_corner_failsafe(
        self,
        x: int,
        y: int,
        width: int = 1920,
        height: int = 1080,
    ) -> bool:
        """Trigger corner_failsafe stop if (x, y) enters any screen corner margin."""
        if width <= 0 or height <= 0:
            raise ValueError("Screen width and height must be positive")
        near_left = x <= self.corner_margin
        near_right = x >= (width - self.corner_margin)
        near_top = y <= self.corner_margin
        near_bottom = y >= (height - self.corner_margin)
        if (near_left or near_right) and (near_top or near_bottom):
            self.trigger_emergency_stop(StopSource.corner_failsafe.value)
            return True
        return False

    def reset_emergency_stop(self) -> None:
        """Clear emergency stop state; requires fresh start authorization before resuming."""
        self.emergency_stopped = False
        self.stop_source = None
        self.paused = False
        self.manual_takeover = False
        self.authorized = False

    def can_execute(self) -> bool:
        """Return True only when authorized, unpaused, not in takeover, and not stopped."""
        return (
            self.authorized
            and not self.paused
            and not self.manual_takeover
            and not self.emergency_stopped
        )

    def assert_can_execute(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        width: int = 1920,
        height: int = 1080,
    ) -> None:
        """Raise SafetyGateError/EmergencyStopError if execution is not permitted."""
        if x is not None and y is not None:
            self.check_corner_failsafe(x, y, width=width, height=height)

        if self.emergency_stopped:
            raise EmergencyStopError(
                f"Execution blocked by emergency stop (source={self.stop_source})"
            )
        if not self.authorized:
            raise SafetyGateError("Execution blocked: explicit start authorization required")
        if self.manual_takeover:
            raise SafetyGateError("Execution blocked: manual takeover is active")
        if self.paused:
            raise SafetyGateError("Execution blocked: controller is paused")

    check_execution_allowed = assert_can_execute
