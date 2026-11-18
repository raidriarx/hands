"""Tests for Milestone 2A: Action Schema Validation & Triple Emergency Stop."""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from controller.emergency import (
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


class TestEnvironmentSetup:
    """Verify root conftest configures offscreen Qt and deterministic test mode."""

    def test_conftest_env_vars(self) -> None:
        assert os.environ.get("QT_QPA_PLATFORM") == "offscreen"
        assert os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1"


class TestActionSchema:
    """Strict validation tests for all 12 DesktopAction types and SpeedConfig."""

    def test_action_type_enum_has_12_members(self) -> None:
        expected = {
            "click",
            "double_click",
            "move",
            "type_text",
            "press_keys",
            "scroll",
            "wait",
            "launch_app",
            "verify_state",
            "request_approval",
            "done",
            "fail",
        }
        assert len(ActionType) == 12
        assert {member.value for member in ActionType} == expected
        assert ActionType.CLICK is ActionType.click

    @pytest.mark.parametrize(
        "payload",
        [
            {"action_type": "click", "description": "Click OK button", "x": 100, "y": 200},
            {
                "action_type": "double_click",
                "description": "Double click icon",
                "x": 50,
                "y": 60,
            },
            {"action_type": "move", "description": "Move cursor to menu", "x": 0, "y": 0},
            {"action_type": "type_text", "description": "Type greeting", "text": "hello"},
            {
                "action_type": "press_keys",
                "description": "Save document",
                "keys": ["ctrl", "s"],
            },
            {"action_type": "scroll", "description": "Scroll down", "clicks": -3},
            {"action_type": "wait", "description": "Wait for UI", "seconds": 1.5},
            {"action_type": "launch_app", "description": "Launch Notepad", "app_name": "notepad"},
            {
                "action_type": "verify_state",
                "description": "Verify window title",
                "expected_text": "Untitled - Notepad",
            },
            {
                "action_type": "verify_state",
                "description": "Verify file exists",
                "target_path": r"C:\approved\note.txt",
            },
            {
                "action_type": "request_approval",
                "description": "Confirm file deletion",
                "reason": "Deletes temporary draft",
                "risk_category": "destructive",
            },
            {"action_type": "done", "description": "Task completed successfully"},
            {
                "action_type": "fail",
                "description": "Could not locate button",
                "reason": "Target button missing",
            },
        ],
    )
    def test_valid_actions_all_12_types(self, payload: dict[str, object]) -> None:
        action = DesktopAction(**payload)
        assert action.action_type.value == payload["action_type"]
        assert action.description == payload["description"]

    @pytest.mark.parametrize(
        "payload",
        [
            {"action_type": "click", "description": "Missing y", "x": 10},
            {"action_type": "click", "description": "Missing both coords"},
            {"action_type": "double_click", "description": "Missing x", "y": 20},
            {"action_type": "move", "description": "Missing coords"},
            {"action_type": "type_text", "description": "Empty text", "text": ""},
            {"action_type": "type_text", "description": "Missing text"},
            {"action_type": "press_keys", "description": "Empty keys", "keys": []},
            {"action_type": "press_keys", "description": "Blank key", "keys": ["ctrl", " "]},
            {"action_type": "scroll", "description": "Zero clicks", "clicks": 0},
            {"action_type": "scroll", "description": "Missing clicks"},
            {"action_type": "wait", "description": "Negative seconds", "seconds": -0.1},
            {"action_type": "wait", "description": "Missing seconds"},
            {"action_type": "launch_app", "description": "Blank app", "app_name": "  "},
            {"action_type": "verify_state", "description": "Missing verification target"},
            {"action_type": "request_approval", "description": "Missing reason", "reason": "  "},
            {"action_type": "fail", "description": "Missing fail reason"},
            {"action_type": "done", "description": "   "},
            {"action_type": "click", "description": "Negative x", "x": -1, "y": 10},
            {"action_type": "click", "description": "Negative y", "x": 10, "y": -5},
        ],
    )
    def test_invalid_action_payloads_rejected(self, payload: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            DesktopAction(**payload)

    def test_extra_forbid_on_desktop_action(self) -> None:
        with pytest.raises(ValidationError):
            DesktopAction(
                action_type=ActionType.done,
                description="Finished",
                unexpected_field="nope",
            )

    def test_speed_presets_and_config_validation(self) -> None:
        assert {p.value for p in SpeedPreset} == {"Slow", "Normal", "Fast", "Custom"}
        for preset in SpeedPreset:
            cfg = SpeedConfig.from_preset(preset)
            assert cfg.preset == preset
            assert cfg == SPEED_PRESETS[preset]

        assert SpeedConfig.from_preset("fast").preset == SpeedPreset.Fast

        with pytest.raises(ValidationError):
            SpeedConfig(extra_field=123)  # type: ignore[call-arg]

        with pytest.raises(ValidationError):
            SpeedConfig(move_duration_sec=0)

        with pytest.raises(ValidationError):
            SpeedConfig(pixels_per_second=-10)

        with pytest.raises(ValidationError):
            SpeedConfig(post_action_delay_sec=-0.1)

        with pytest.raises(ValidationError):
            SpeedConfig(interpolation_steps=0)


class TestSafetyControllerAndTripleEmergencyStop:
    """Tests for Start Gate, Pause/Resume/Takeover, and Triple Emergency Stop."""

    def test_initial_start_gate_blocks_until_authorized(self) -> None:
        safety = SafetyController()
        assert safety.authorized is False
        assert safety.can_execute() is False
        with pytest.raises(SafetyGateError):
            safety.assert_can_execute()

        assert safety.authorize_start() is True
        assert safety.authorized is True
        assert safety.can_execute() is True
        safety.assert_can_execute(500, 500, 1920, 1080)

    def test_pause_resume_and_manual_takeover_flow(self) -> None:
        safety = SafetyController()
        safety.authorize_start()

        safety.pause()
        assert safety.paused is True
        assert safety.can_execute() is False
        with pytest.raises(SafetyGateError):
            safety.assert_can_execute()

        safety.resume()
        assert safety.paused is False
        assert safety.can_execute() is True

        safety.enter_manual_takeover()
        assert safety.manual_takeover is True
        assert safety.paused is True
        assert safety.can_execute() is False
        with pytest.raises(SafetyGateError):
            safety.resume()

        safety.exit_manual_takeover()
        assert safety.manual_takeover is False
        assert safety.paused is False
        assert safety.can_execute() is True

    def test_gui_button_emergency_stop_and_reset(self) -> None:
        safety = SafetyController()
        safety.authorize_start()

        safety.trigger_emergency_stop("gui_button")
        assert safety.emergency_stopped is True
        assert safety.authorized is False
        assert safety.paused is True
        assert safety.stop_source == "gui_button"
        assert safety.can_execute() is False

        with pytest.raises(EmergencyStopError):
            safety.assert_can_execute()
        with pytest.raises(EmergencyStopError):
            safety.authorize_start()
        with pytest.raises(EmergencyStopError):
            safety.resume()
        with pytest.raises(EmergencyStopError):
            safety.exit_manual_takeover()

        safety.reset_emergency_stop()
        assert safety.emergency_stopped is False
        assert safety.stop_source is None
        assert safety.authorized is False
        assert safety.can_execute() is False

        with pytest.raises(SafetyGateError):
            safety.assert_can_execute()

        safety.authorize_start()
        assert safety.can_execute() is True

    def test_global_hotkey_triggers_f12_and_ctrl_shift_esc(self) -> None:
        safety = SafetyController()
        safety.authorize_start()

        assert safety.simulate_hotkey_f12() is True
        assert safety.emergency_stopped is True
        assert safety.stop_source == StopSource.global_hotkey.value

        safety.reset_emergency_stop()
        safety.authorize_start()
        assert safety.simulate_hotkey_ctrl_shift_esc() is True
        assert safety.emergency_stopped is True
        assert safety.stop_source == "global_hotkey"

        safety.reset_emergency_stop()
        safety.authorize_start()
        assert safety.check_global_hotkey("Ctrl + Shift + Esc") is True
        assert safety.emergency_stopped is True
        assert safety.stop_source == "global_hotkey"

        safety.reset_emergency_stop()
        assert safety.check_global_hotkey("Ctrl+C") is False
        assert safety.emergency_stopped is False

    def test_escape_hotkey_pauses_without_emergency_stop(self) -> None:
        safety = SafetyController()
        safety.authorize_start()

        assert safety.simulate_hotkey_escape() is True
        assert safety.paused is True
        assert safety.emergency_stopped is False
        assert safety.stop_source is None
        assert safety.can_execute() is False

        safety.resume()
        assert safety.paused is False
        assert safety.can_execute() is True

        assert safety.check_global_hotkey("Esc") is True
        assert safety.paused is True
        assert safety.emergency_stopped is False

        safety.resume()
        assert safety.check_global_hotkey("Escape") is True
        assert safety.paused is True
        assert safety.emergency_stopped is False

        # Ctrl+Shift+Esc remains a full emergency stop
        safety.resume()
        assert safety.check_global_hotkey("Ctrl+Shift+Esc") is True
        assert safety.emergency_stopped is True
        assert safety.stop_source == StopSource.global_hotkey.value

    @pytest.mark.parametrize(
        "x,y",
        [
            (0, 0),
            (3, 4),
            (1919, 0),
            (0, 1079),
            (1919, 1079),
            (1920, 1080),
        ],
    )
    def test_corner_failsafe_triggers_on_all_corners(self, x: int, y: int) -> None:
        safety = SafetyController(corner_margin=5)
        safety.authorize_start()

        triggered = safety.check_corner_failsafe(x, y, 1920, 1080)
        assert triggered is True
        assert safety.emergency_stopped is True
        assert safety.stop_source == "corner_failsafe"
        assert safety.can_execute() is False

    @pytest.mark.parametrize(
        "x,y",
        [
            (500, 500),
            (500, 0),
            (0, 500),
            (1919, 500),
            (500, 1079),
        ],
    )
    def test_corner_failsafe_ignores_non_corner_coordinates(self, x: int, y: int) -> None:
        safety = SafetyController(corner_margin=5)
        safety.authorize_start()

        assert safety.check_corner_failsafe(x, y, 1920, 1080) is False
        assert safety.emergency_stopped is False
        assert safety.can_execute() is True

    def test_assert_can_execute_triggers_corner_failsafe_automatically(self) -> None:
        safety = SafetyController(corner_margin=5)
        safety.authorize_start()

        with pytest.raises(EmergencyStopError):
            safety.assert_can_execute(0, 0, 1920, 1080)
        assert safety.emergency_stopped is True
        assert safety.stop_source == "corner_failsafe"

    def test_invalid_stop_source_raises_value_error(self) -> None:
        safety = SafetyController()
        with pytest.raises(ValueError):
            safety.trigger_emergency_stop("unknown_source")
