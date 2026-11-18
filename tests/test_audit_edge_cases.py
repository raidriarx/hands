"""Tests for Milestone 5: Security, Safety, DPI, Focus, and Edge-Case Audit Fixes."""

from __future__ import annotations

import os
import threading
import time
import types
from pathlib import Path
from typing import Optional

import pytest
from PIL import Image
from pydantic import ValidationError
from PySide6.QtWidgets import QApplication

import controller.driver as driver_mod
from controller.driver import (
    LiveWindowsDesktopDriver,
    MockDryRunDriver,
    reconcile_dpi_coordinates,
)
from controller.emergency import SafetyController, SafetyGateError, EmergencyStopError
from controller.engine import DesktopExecutionEngine
from controller.gui import DesktopControllerWindow
from controller.planners import BasePlanner, ObservationContext
from controller.policy import PolicyEngine
from controller.schema import ActionType, DesktopAction, SpeedConfig, SpeedPreset
from controller.store import SQLiteAuditStore


class TestSchemaAuditBoundsAndNonFinite:
    """Verify non-finite values, oversized payloads, and control-char key rejection."""

    @pytest.mark.parametrize("bad_seconds", [float("nan"), float("inf"), float("-inf"), 300.1])
    def test_wait_rejects_nonfinite_and_excessive_seconds(self, bad_seconds: float) -> None:
        with pytest.raises(ValidationError):
            DesktopAction(
                action_type=ActionType.wait,
                seconds=bad_seconds,
                description="Wait invalid duration",
            )

    def test_text_and_scroll_upper_bounds_enforced(self) -> None:
        with pytest.raises(ValidationError):
            DesktopAction(
                action_type=ActionType.type_text,
                text="a" * 10001,
                description="Oversized text payload",
            )

        with pytest.raises(ValidationError):
            DesktopAction(
                action_type=ActionType.scroll,
                clicks=101,
                description="Excessive positive scroll",
            )

        with pytest.raises(ValidationError):
            DesktopAction(
                action_type=ActionType.scroll,
                clicks=-101,
                description="Excessive negative scroll",
            )

    @pytest.mark.parametrize(
        "bad_keys",
        [
            ["ctrl", "a\nb"],
            ["enter\r"],
            ["tab\x00"],
            ["x" * 33],
        ],
    )
    def test_press_keys_rejects_control_chars_and_long_keys(self, bad_keys: list[str]) -> None:
        with pytest.raises(ValidationError):
            DesktopAction(
                action_type=ActionType.press_keys,
                keys=bad_keys,
                description="Invalid key sequence",
            )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"move_duration_sec": float("nan")},
            {"move_duration_sec": float("inf")},
            {"move_duration_sec": 30.1},
            {"pixels_per_second": float("inf")},
            {"pixels_per_second": 20000.1},
            {"post_action_delay_sec": float("nan")},
            {"post_action_delay_sec": 30.1},
            {"interpolation_steps": 501},
        ],
    )
    def test_speed_config_rejects_nonfinite_and_unbounded_timings(
        self,
        kwargs: dict[str, object],
    ) -> None:
        with pytest.raises(ValidationError):
            SpeedConfig(**kwargs)  # type: ignore[arg-type]


class TestPolicyAuditGateRemediations:
    """Verify key-gate, app_name sandbox, and forward-slash/UNC path protections."""

    @pytest.mark.parametrize(
        "keys",
        [
            ["delete"],
            ["del"],
            ["shift", "delete"],
            ["ctrl", "shift", "delete"],
        ],
    )
    def test_destructive_keys_require_approval(self, keys: list[str]) -> None:
        policy = PolicyEngine()
        action = DesktopAction(
            action_type=ActionType.press_keys,
            keys=keys,
            description="Press keyboard shortcut",
        )
        decision = policy.evaluate(action)
        assert decision.allowed is True
        assert decision.requires_approval is True
        assert decision.risk_category == "delete_or_overwrite"

    @pytest.mark.parametrize(
        "keys,desc",
        [
            (["ctrl", "s"], "Open Save dialog in Notepad"),
            (["enter"], "Save milestone4_demo.txt inside approved_workspace"),
            (["alt", "f4"], "Close Notepad window"),
        ],
    )
    def test_benign_notepad_shortcuts_remain_safe(self, keys: list[str], desc: str) -> None:
        policy = PolicyEngine()
        action = DesktopAction(
            action_type=ActionType.press_keys,
            keys=keys,
            description=desc,
        )
        decision = policy.evaluate(action)
        assert decision.allowed is True
        assert decision.requires_approval is False
        assert decision.risk_category == "safe"

    def test_submit_key_requires_form_context(self) -> None:
        policy = PolicyEngine()
        form_action = DesktopAction(
            action_type=ActionType.press_keys,
            keys=["submit"],
            description="Complete web application form",
        )
        decision = policy.evaluate(form_action)
        assert decision.allowed is True
        assert decision.requires_approval is True
        assert decision.risk_category == "submit_form"

    @pytest.mark.parametrize(
        "app_name",
        [
            r"C:\Temp\tool.exe",
            "D:/evil.exe",
            r"\\server\share\x.exe",
            "//server/share/x.exe",
            r"..\outside.exe",
        ],
    )
    def test_outside_sandbox_app_name_is_blocked(self, tmp_path: Path, app_name: str) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        policy = PolicyEngine(approved_workspace=ws)
        action = DesktopAction(
            action_type=ActionType.launch_app,
            app_name=app_name,
            description="Launch external binary",
        )
        decision = policy.evaluate(action)
        assert decision.allowed is False
        assert decision.risk_category == "sandbox_violation"

    def test_bare_and_in_sandbox_app_names_allowed(self, tmp_path: Path) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        policy = PolicyEngine(approved_workspace=ws)

        bare = DesktopAction(
            action_type=ActionType.launch_app,
            app_name="notepad",
            description="Launch Notepad",
        )
        assert policy.evaluate(bare).allowed is True

        inside_exe = DesktopAction(
            action_type=ActionType.launch_app,
            app_name=str(ws / "viewer.exe"),
            description="Launch approved viewer",
        )
        assert policy.evaluate(inside_exe).allowed is True

    @pytest.mark.parametrize(
        "text",
        [
            "type c:/windows/win.ini",
            "copy //server/share/secret.txt",
            r"open \\fileserver\private\data.csv",
        ],
    )
    def test_forward_slash_and_unc_paths_blocked_in_text(self, tmp_path: Path, text: str) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        policy = PolicyEngine(approved_workspace=ws)
        action = DesktopAction(
            action_type=ActionType.type_text,
            text=text,
            description="Enter path in prompt",
        )
        decision = policy.evaluate(action)
        assert decision.allowed is False
        assert decision.risk_category == "sandbox_violation"

    def test_https_url_is_not_misclassified_as_unc(self, tmp_path: Path) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        policy = PolicyEngine(approved_workspace=ws)
        action = DesktopAction(
            action_type=ActionType.type_text,
            text="https://x.com/status",
            description="Publish post to x.com",
        )
        decision = policy.evaluate(action)
        assert decision.allowed is True
        assert decision.requires_approval is True
        assert decision.risk_category == "social_post"


class TestDriverInterruptsFocusAndDPI:
    """Verify mid-typing stop, actual cursor corner failsafe, DPI scaling, and focus verify."""

    def test_reconcile_dpi_coordinates_scales_and_clamps(self) -> None:
        assert reconcile_dpi_coordinates(500, 300, source_scale=1.0, target_scale=1.5) == (750, 450)
        assert reconcile_dpi_coordinates(1000, 800, source_scale=2.0, target_scale=1.0) == (500, 400)
        assert reconcile_dpi_coordinates(3000, 2000, source_scale=1.0, target_scale=1.5, screen_size=(1920, 1080)) == (1919, 1079)
        assert reconcile_dpi_coordinates(-20, -10, screen_size=(1920, 1080)) == (0, 0)

        with pytest.raises(ValueError):
            reconcile_dpi_coordinates(10, 10, source_scale=0.0)
        with pytest.raises(ValueError):
            reconcile_dpi_coordinates(10, 10, screen_size=(0, 1080))

    def test_live_driver_type_text_interrupts_mid_string(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        safety = SafetyController()
        safety.authorize_start()
        driver = LiveWindowsDesktopDriver(safety=safety)
        monkeypatch.setattr(driver, "_poll_global_hotkeys", lambda: None)
        monkeypatch.setattr(driver, "get_screen_size", lambda: (1920, 1080))
        monkeypatch.setattr(driver, "get_cursor_position", lambda: (500, 500))

        typed_chars: list[str] = []

        class _FakePyAutoGUI:
            class FailSafeException(Exception):
                pass

            def write(self, ch: str, interval: float = 0, _pause: bool = False) -> None:
                typed_chars.append(ch)
                if len(typed_chars) == 4:
                    safety.trigger_emergency_stop("gui_button")

        driver._pyautogui = _FakePyAutoGUI()  # type: ignore[assignment]

        with pytest.raises(EmergencyStopError):
            driver.type_text("ABCDEFGHIJ", interval=0.0)

        assert "".join(typed_chars) == "ABCD"
        assert safety.emergency_stopped is True

    def test_live_driver_move_to_aborts_on_actual_cursor_corner_failsafe(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        safety = SafetyController(corner_margin=5)
        safety.authorize_start()
        driver = LiveWindowsDesktopDriver(safety=safety)
        monkeypatch.setattr(driver, "_poll_global_hotkeys", lambda: None)
        monkeypatch.setattr(driver, "get_screen_size", lambda: (1920, 1080))

        cursor_state = {"pos": (500, 500)}
        moved_points: list[tuple[int, int]] = []

        class _FakePyAutoGUI:
            class FailSafeException(Exception):
                pass

            def moveTo(self, px: int, py: int, duration: float = 0, _pause: bool = False) -> None:
                moved_points.append((px, py))
                if len(moved_points) == 2:
                    # Simulate physical user flinging real cursor to top-left corner
                    cursor_state["pos"] = (0, 0)

        driver._pyautogui = _FakePyAutoGUI()  # type: ignore[assignment]
        monkeypatch.setattr(driver, "get_cursor_position", lambda: cursor_state["pos"])

        cfg = SpeedConfig(move_duration_sec=0.01, interpolation_steps=10, post_action_delay_sec=0.0)
        with pytest.raises(EmergencyStopError):
            driver.move_to(900, 600, speed_config=cfg)

        assert safety.emergency_stopped is True
        assert safety.stop_source == "corner_failsafe"
        assert len(moved_points) == 2

    def test_live_driver_verify_state_prefers_foreground_title(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        safety = SafetyController()
        driver = LiveWindowsDesktopDriver(safety=safety)
        monkeypatch.setattr(driver_mod, "_get_foreground_window_title", lambda: "Untitled - Notepad")
        monkeypatch.setattr(driver_mod, "_list_visible_window_titles", lambda: ["Background Window"])

        ok, msg = driver.verify_state(expected_text="Notepad")
        assert ok is True
        assert "foreground" in msg.lower()

    def test_live_driver_launch_app_records_focus_confirmed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        safety = SafetyController()
        safety.authorize_start()
        driver = LiveWindowsDesktopDriver(safety=safety)
        monkeypatch.setattr(driver, "_poll_hotkeys_and_safety", lambda *a, **k: None)
        monkeypatch.setattr(driver_mod.subprocess, "Popen", lambda *a, **k: types.SimpleNamespace(pid=4321))
        monkeypatch.setattr(driver_mod.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            driver_mod,
            "_focus_window_by_title",
            lambda stem, target_pid=None: ((10, 20, 810, 620), True),
        )

        meta = driver.launch_app("notepad")
        assert meta["pid"] == 4321
        assert meta["rect"] == (10, 20, 810, 620)
        assert meta["focus_confirmed"] is True


class _CallbackPlanner(BasePlanner):
    name = "CallbackTestPlanner"

    def __init__(self, fn) -> None:
        self._fn = fn

    def is_available(self) -> tuple[bool, str]:
        return True, "Ready"

    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        return self._fn(obs)


class TestEngineAuditSafetyAndLoops:
    """Verify stuck-loop detection, partial-failure history, post-plan safety, and finalization."""

    def test_stuck_identical_action_loop_fails_safely(self, tmp_path: Path) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)
        planner = _CallbackPlanner(
            lambda _obs: DesktopAction(
                action_type=ActionType.wait,
                seconds=0.01,
                description="Keep waiting forever",
            )
        )
        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            policy=PolicyEngine(approved_workspace=ws),
            planner=planner,
            max_steps=20,
            max_no_op_repeats=5,
        )
        res = engine.run_task("Infinite wait loop")
        assert res.status == "failed"
        assert "stuck no-op loop" in (res.error or "").lower()
        assert res.steps_executed == 4
        assert safety.authorized is False

    def test_partial_failure_recorded_in_history_for_planner(self, tmp_path: Path) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        target = ws / "delayed.txt"
        seen_histories: list[list[dict[str, object]]] = []

        class _FlakyVerifyDriver(MockDryRunDriver):
            def __init__(self, safety_ctrl: SafetyController) -> None:
                super().__init__(safety=safety_ctrl)
                self.calls = 0

            def verify_state(
                self,
                target_path: Optional[str] = None,
                expected_text: Optional[str] = None,
            ) -> tuple[bool, str]:
                self.calls += 1
                if self.calls == 1:
                    return False, "Temporary verify error"
                target.write_text("ok", encoding="utf-8")
                return super().verify_state(target_path=target_path, expected_text=expected_text)

        def _plan(obs: ObservationContext) -> DesktopAction:
            seen_histories.append(list(obs.history))
            if obs.step_index == 0:
                return DesktopAction(
                    action_type=ActionType.verify_state,
                    target_path=str(target),
                    expected_text="ok",
                    description="Verify delayed file",
                )
            return DesktopAction(action_type=ActionType.done, description="Done")

        safety = SafetyController()
        safety.authorize_start()
        engine = DesktopExecutionEngine(
            driver=_FlakyVerifyDriver(safety),
            safety=safety,
            policy=PolicyEngine(approved_workspace=ws),
            planner=_CallbackPlanner(_plan),
            max_retries=2,
        )
        res = engine.run_task("Observe partial failure")
        assert res.status == "completed"
        assert res.retries_used == 1
        assert any(entry.get("status") == "error" for entry in seen_histories[1])
        assert safety.authorized is False

    def test_post_plan_safety_recheck_blocks_done_when_stopped_during_plan(
        self,
        tmp_path: Path,
    ) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()

        def _plan_and_stop(_obs: ObservationContext) -> DesktopAction:
            safety.simulate_hotkey_f12()
            return DesktopAction(action_type=ActionType.done, description="Attempt done after stop")

        engine = DesktopExecutionEngine(
            driver=MockDryRunDriver(safety=safety),
            safety=safety,
            policy=PolicyEngine(approved_workspace=ws),
            planner=_CallbackPlanner(_plan_and_stop),
        )
        res = engine.run_task("Stop during planning")
        assert res.status == "stopped"
        assert res.steps_executed == 0
        assert safety.emergency_stopped is True
        assert safety.authorized is False

    def test_unexpected_exception_finalizes_sqlite_run_and_revokes_auth(
        self,
        tmp_path: Path,
    ) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        store = SQLiteAuditStore(tmp_path / "audit.db")

        class _ExplodingDriver(MockDryRunDriver):
            def capture_screenshot(self) -> Image.Image:
                raise MemoryError("Simulated unexpected screenshot crash")

        engine = DesktopExecutionEngine(
            driver=_ExplodingDriver(safety=safety),
            safety=safety,
            store=store,
            policy=PolicyEngine(approved_workspace=ws),
            planner=_CallbackPlanner(
                lambda _obs: DesktopAction(action_type=ActionType.done, description="Done")
            ),
        )
        with pytest.raises(MemoryError):
            engine.run_task("Crash unexpectedly")

        runs = store.list_runs(limit=5)
        assert len(runs) == 1
        assert runs[0]["status"] == "failed"
        assert "Simulated unexpected screenshot crash" in (runs[0]["error"] or "")
        assert safety.authorized is False


class TestEscapePauseAndMidMoveYield:
    """Verify standalone Esc pauses mid-move, resumes in engine, and binds in Window B."""

    def test_esc_mid_move_stops_cursor_immediately_without_emergency_stop(self) -> None:
        safety = SafetyController()
        safety.authorize_start()

        def _esc_mid_move(_pt: tuple[int, int], idx: int, _total: int) -> None:
            if idx == 5:
                safety.simulate_hotkey_escape()

        driver = MockDryRunDriver(
            safety=safety,
            initial_cursor=(100, 100),
            step_callback=_esc_mid_move,
        )

        with pytest.raises(SafetyGateError):
            driver.move_to(1000, 600, speed_config=SpeedConfig.from_preset(SpeedPreset.Normal))

        assert driver.cursor_pos != (1000, 600)
        assert safety.paused is True
        assert safety.emergency_stopped is False

    def test_engine_pauses_mid_move_on_esc_and_resumes_cleanly(self, tmp_path: Path) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        store = SQLiteAuditStore(tmp_path / "pause_resume.db")

        triggered = {"done": False}
        history_events: list[tuple[str, str, str]] = []

        def _esc_once(_pt: tuple[int, int], idx: int, _total: int) -> None:
            if not triggered["done"] and idx == 4:
                triggered["done"] = True
                safety.simulate_hotkey_escape()

                def _resume_soon() -> None:
                    time.sleep(0.03)
                    safety.resume()

                threading.Thread(target=_resume_soon, daemon=True).start()

        driver = MockDryRunDriver(
            safety=safety,
            initial_cursor=(100, 100),
            step_callback=_esc_once,
        )

        def _plan(obs: ObservationContext) -> DesktopAction:
            if obs.step_index == 0:
                return DesktopAction(
                    action_type=ActionType.move,
                    x=1000,
                    y=600,
                    description="Move cursor across screen",
                )
            return DesktopAction(action_type=ActionType.done, description="Completed after resume")

        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            store=store,
            policy=PolicyEngine(approved_workspace=ws),
            planner=_CallbackPlanner(_plan),
            history_callback=lambda a, s, d: history_events.append((a, s, d)),
        )
        res = engine.run_task("Pause mid-move on Esc and resume")
        assert res.status == "completed"
        assert res.retries_used == 0
        assert driver.cursor_pos == (1000, 600)
        assert any(a == "pause" and s == "paused" for a, s, _ in history_events)
        actions = store.get_actions_for_run(res.run_id)
        assert any(row["action_type"] == "pause" and row["status"] == "paused" for row in actions)

    def test_window_b_has_esc_shortcut_that_pauses_without_estop(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
        app = QApplication.instance() or QApplication([])

        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(safety=safety, driver=driver)

        assert hasattr(win, "esc_shortcut")
        assert win.pause_button.text() == "Pause (Esc)"

        win.start_button.click()
        assert safety.authorized is True
        win._on_esc_shortcut()
        assert safety.paused is True
        assert safety.emergency_stopped is False

        win.close()
        app.processEvents()

    def test_live_driver_press_keys_suppresses_esc_self_pause(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        safety = SafetyController()
        safety.authorize_start()
        driver = LiveWindowsDesktopDriver(safety=safety)
        monkeypatch.setattr(driver, "_poll_hotkeys_and_safety", lambda *a, **k: None)

        seen_suppress: list[bool] = []

        class _FakePyAutoGUI:
            class FailSafeException(Exception):
                pass

            def press(self, key: str, _pause: bool = False) -> None:
                seen_suppress.append(driver._suppress_esc_pause)
                assert key == "esc"

        driver._pyautogui = _FakePyAutoGUI()  # type: ignore[assignment]
        driver.press_keys(["Esc"])
        assert seen_suppress == [True]
        assert driver._suppress_esc_pause is False
        assert safety.paused is False
