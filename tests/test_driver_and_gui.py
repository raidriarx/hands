"""Tests for Milestone 2B: Smooth Driver, Window B GUI, and main.py entrypoint."""

from __future__ import annotations

import os
from pathlib import Path

import pyautogui
import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

import main as app_main
from controller.driver import (
    LiveWindowsDesktopDriver,
    MockDryRunDriver,
    compute_smooth_path,
    create_driver,
)
from controller.emergency import (
    EmergencyStopError,
    SafetyController,
    SafetyGateError,
)
from controller.gui import MILESTONE_4_PRESET, DesktopControllerWindow
from controller.schema import SpeedConfig, SpeedPreset


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestComputeSmoothPath:
    """Verify cubic ease-in-out cursor interpolation properties."""

    def test_single_or_zero_steps_returns_exact_target(self) -> None:
        assert compute_smooth_path((10, 20), (300, 400), steps=1) == [(300, 400)]
        assert compute_smooth_path((10, 20), (300, 400), steps=0) == [(300, 400)]

    def test_cubic_ease_in_out_progression_and_endpoints(self) -> None:
        path = compute_smooth_path((100, 200), (900, 600), steps=20)
        assert len(path) == 20
        # First point moves gently away from start
        assert 100 <= path[0][0] < 140
        assert 200 <= path[0][1] < 240
        # Final point equals target exactly
        assert path[-1] == (900, 600)

        # Monotonic progression along x and y
        xs = [pt[0] for pt in path]
        ys = [pt[1] for pt in path]
        assert xs == sorted(xs)
        assert ys == sorted(ys)

        # Ease-in-out: middle step delta is larger than endpoint step delta
        first_delta = path[0][0] - 100
        mid_delta = path[10][0] - path[9][0]
        last_delta = path[-1][0] - path[-2][0]
        assert mid_delta > first_delta
        assert mid_delta > last_delta


class TestMockAndLiveDrivers:
    """Verify MockDryRunDriver safety gates, mid-move aborts, and LiveWindowsDesktopDriver setup."""

    def test_mock_driver_obeys_start_pause_and_takeover_gates(self) -> None:
        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)

        with pytest.raises(SafetyGateError):
            driver.move_to(500, 500)

        safety.authorize_start()
        path = driver.move_to(500, 500)
        assert len(path) == SpeedConfig.from_preset(SpeedPreset.Normal).interpolation_steps
        assert driver.get_cursor_position() == (500, 500)

        safety.pause()
        with pytest.raises(SafetyGateError):
            driver.click(600, 600)
        safety.resume()
        driver.click(600, 600)
        assert driver.get_cursor_position() == (600, 600)

        safety.enter_manual_takeover()
        with pytest.raises(SafetyGateError):
            driver.type_text("blocked")
        safety.exit_manual_takeover()
        driver.type_text("allowed")
        assert "allowed" in driver.typed_texts

    def test_mock_driver_aborts_mid_move_on_corner_failsafe(self) -> None:
        safety = SafetyController(corner_margin=5)
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety, initial_cursor=(960, 540))

        with pytest.raises(EmergencyStopError):
            driver.move_to(2, 2, speed_config=SpeedConfig.from_preset(SpeedPreset.Normal))

        assert safety.emergency_stopped is True
        assert safety.stop_source == "corner_failsafe"
        # Stopped when entering corner margin, without completing extra unsafe steps
        assert driver.cursor_pos != (2, 2)

    def test_mock_driver_aborts_mid_move_on_emergency_stop_trigger(self) -> None:
        safety = SafetyController()
        safety.authorize_start()

        def _trigger_mid_move(_pt: tuple[int, int], idx: int, _total: int) -> None:
            if idx == 5:
                safety.simulate_hotkey_f12()

        driver = MockDryRunDriver(
            safety=safety,
            initial_cursor=(200, 200),
            step_callback=_trigger_mid_move,
        )

        with pytest.raises(EmergencyStopError):
            driver.move_to(1200, 800, speed_config=SpeedConfig.from_preset(SpeedPreset.Normal))

        assert safety.emergency_stopped is True
        assert safety.stop_source == "global_hotkey"
        assert len(driver.move_history) == 5
        assert driver.cursor_pos != (1200, 800)

    def test_mock_driver_screenshot_and_state_verification(self, tmp_path: Path) -> None:
        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)

        meta = driver.launch_app("notepad")
        assert meta["app_name"] == "notepad"
        driver.type_text("Hello M2B")
        driver.press_keys(["ctrl", "s"])
        driver.scroll(-2)
        driver.wait(0.01)

        img = driver.capture_screenshot()
        assert isinstance(img, Image.Image)
        assert img.mode == "RGB"
        assert img.size == (640, 360)

        ok_win, _ = driver.verify_state(expected_text="Hello M2B")
        assert ok_win is True

        driver.virtual_files["demo.txt"] = "Saved content"
        ok_vf, _ = driver.verify_state(target_path="demo.txt", expected_text="Saved")
        assert ok_vf is True

        real_file = tmp_path / "note.txt"
        real_file.write_text("Disk verification", encoding="utf-8")
        ok_fs, _ = driver.verify_state(target_path=str(real_file), expected_text="Disk verification")
        assert ok_fs is True

    def test_live_driver_enforces_pyautogui_failsafe_without_moving_cursor(self) -> None:
        safety = SafetyController()
        pyautogui.FAILSAFE = False
        driver = LiveWindowsDesktopDriver(safety=safety)
        assert isinstance(driver, LiveWindowsDesktopDriver)
        assert pyautogui.FAILSAFE is True
        assert pyautogui.PAUSE == 0.0

        mock_from_env = create_driver(safety)
        assert isinstance(mock_from_env, MockDryRunDriver)
        live_forced = create_driver(safety, force_mock=False)
        assert isinstance(live_forced, LiveWindowsDesktopDriver)


class TestDesktopControllerWindow:
    """Offscreen Qt tests for Window B widgets, safety controls, and public API."""

    def test_widgets_exist_and_speed_presets_sync(self, qapp: QApplication) -> None:
        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(safety=safety, driver=driver)

        required_attrs = [
            "task_input",
            "preset_task_combo",
            "speed_combo",
            "duration_spin",
            "delay_spin",
            "backend_combo",
            "backend_status_label",
            "status_label",
            "current_action_label",
            "progress_bar",
            "screenshot_label",
            "approval_group",
            "approval_details_label",
            "approve_button",
            "deny_button",
            "start_button",
            "pause_button",
            "resume_button",
            "takeover_button",
            "emergency_stop_button",
            "reset_stop_button",
            "history_table",
        ]
        for attr in required_attrs:
            assert hasattr(win, attr), f"Missing required widget attribute: {attr}"

        assert "Window B" in win.windowTitle()
        assert win.preset_task_combo.findText(MILESTONE_4_PRESET) >= 0

        win.speed_combo.setCurrentText("Slow")
        assert win.duration_spin.value() == pytest.approx(1.0)
        assert win.delay_spin.value() == pytest.approx(0.5)
        assert win.current_speed_config().preset == SpeedPreset.Slow

        win.speed_combo.setCurrentText("Fast")
        assert win.duration_spin.value() == pytest.approx(0.2)
        assert win.delay_spin.value() == pytest.approx(0.05)
        assert win.current_speed_config().preset == SpeedPreset.Fast

        win.close()

    def test_safety_buttons_f12_and_reset_behavior(self, qapp: QApplication) -> None:
        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(safety=safety, driver=driver)

        assert safety.authorized is False
        win.start_button.click()
        assert safety.authorized is True

        win.pause_button.click()
        assert safety.paused is True
        win.resume_button.click()
        assert safety.paused is False

        win.takeover_button.click()
        assert safety.manual_takeover is True
        win.takeover_button.click()
        assert safety.manual_takeover is False

        win.emergency_stop_button.click()
        assert safety.emergency_stopped is True
        assert safety.stop_source == "gui_button"
        assert safety.authorized is False

        win.reset_stop_button.click()
        assert safety.emergency_stopped is False
        assert safety.authorized is False  # Never auto-authorizes on reset

        win.start_button.click()
        assert safety.authorized is True
        win._on_f12_shortcut()
        assert safety.emergency_stopped is True
        assert safety.stop_source == "global_hotkey"

        win.close()

    def test_approval_panel_screenshot_progress_and_history(self, qapp: QApplication) -> None:
        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(safety=safety, driver=driver)

        win.request_user_approval(
            reason="Confirm overwrite in approved workspace",
            risk_category="file_overwrite",
            details="approved_workspace/demo.txt",
        )
        assert win.pending_approval is True
        assert win.approve_button.isEnabled() is True
        assert win.deny_button.isEnabled() is True
        assert "file_overwrite" in win.approval_details_label.text()

        win.approve_button.click()
        assert win.pending_approval is False
        assert win.approval_decision is True
        assert win.approve_button.isEnabled() is False

        win.request_user_approval(
            reason="Send external email",
            risk_category="external_communication",
        )
        win.deny_button.click()
        assert win.pending_approval is False
        assert win.approval_decision is False

        img = driver.capture_screenshot()
        win.update_screenshot(img)
        pixmap = win.screenshot_label.pixmap()
        assert pixmap is not None
        assert not pixmap.isNull()

        win.set_progress(3, 5, "Typing text into Notepad")
        assert win.progress_bar.maximum() == 5
        assert win.progress_bar.value() == 3
        assert "Typing text" in win.current_action_label.text()

        rows_before = win.history_table.rowCount()
        win.append_history_row("click", "ok", "Clicked Save", timestamp="12:00:00")
        assert win.history_table.rowCount() == rows_before + 1
        assert win.history_table.item(rows_before, 0).text() == "12:00:00"
        assert win.history_table.item(rows_before, 1).text() == "click"
        assert win.history_table.item(rows_before, 2).text() == "ok"
        assert win.history_table.item(rows_before, 3).text() == "Clicked Save"

        win.close()

    def test_main_headless_check_exits_cleanly(self, qapp: QApplication) -> None:
        rc = app_main.main(["--headless-check", "--speed", "Fast"])
        assert rc == 0
