"""Tests for Milestone 5: Security, Safety, DPI, Focus, and Edge-Case Audit Fixes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Optional

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError
from PySide6.QtWidgets import QApplication, QLineEdit

import controller.driver as driver_mod
from controller.driver import (
    BaseDesktopDriver,
    LiveWindowsDesktopDriver,
    MockDryRunDriver,
    _resolve_app_executable,
    find_matching_browser_window_title,
    reconcile_dpi_coordinates,
)
from controller.emergency import SafetyController, SafetyGateError, EmergencyStopError
from controller.engine import DesktopExecutionEngine
from controller.interpreter import (
    AntigravityCLIInterpreter,
    BasePlanInterpreter,
    HybridPlanInterpreter,
    OllamaPlanInterpreter,
    TaskPlan,
    _build_interpreter_prompt,
)
import main as app_main
import controller.gui as gui_mod
from controller.gui import (
    APPROVAL_MODE_ASK,
    APPROVAL_MODE_SUPERVISED,
    BotCharacterOverlay,
    BotFaceWidget,
    DesktopControllerWindow,
)
from controller.planners import (
    BasePlanner,
    LocalDemoFallbackPlanner,
    ObservationContext,
    OllamaPlanner,
    _build_observation_prompt,
    _extract_song_query,
    _is_youtube_music_task,
    _parse_action_json,
    annotate_screenshot_for_planner,
    compute_screenshot_change_ratio,
    create_foveated_screenshot,
    create_planner,
    crop_screenshot_to_viewport,
    extract_som_marks,
    locate_youtube_music_play_button,
    map_viewport_coord,
    normalize_coord,
    normalize_to_viewport,
    save_api_key_to_dotenv,
    scale_normalized_coord,
    subregion_viewport,
)
from controller.policy import PolicyEngine
from controller.schema import ActionType, DesktopAction, SpeedConfig, SpeedPreset
from controller.store import SQLiteAuditStore, redact_secrets
from controller.uia import extract_priority_terms, prioritize_ui_elements


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


class TestBrowserControlAndApiKeyUX:
    """Verify Windows app path resolution, browser/YouTube Music fallback, and API key UX."""

    def test_resolve_app_executable_for_notepad_and_firefox(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        firefox_path = r"C:\Program Files\Mozilla Firefox\firefox.exe"

        monkeypatch.setattr(
            driver_mod.shutil,
            "which",
            lambda name: r"C:\Windows\system32\notepad.exe" if name == "notepad" else None,
        )
        assert _resolve_app_executable("notepad") == "notepad"

        orig_is_file = Path.is_file

        def _fake_is_file(self: Path) -> bool:
            if str(self) == firefox_path:
                return True
            return orig_is_file(self)

        monkeypatch.setattr(Path, "is_file", _fake_is_file)
        assert _resolve_app_executable("firefox") == firefox_path
        assert _resolve_app_executable(r"C:\custom\app.exe") == r"C:\custom\app.exe"

    def test_local_demo_planner_runs_firefox_youtube_music_end_to_end(
        self,
        tmp_path: Path,
    ) -> None:
        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)
        store = SQLiteAuditStore(tmp_path / "browser_demo.db")
        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            store=store,
            policy=PolicyEngine(approved_workspace=ws),
            planner=LocalDemoFallbackPlanner(),
        )

        res = engine.run_task("open firefox open youtube music and put on a song")
        assert res.status == "completed"
        assert res.steps_executed == 10
        assert "firefox" in driver.launched_apps
        assert ["ctrl", "l"] in driver.pressed_keys
        assert ["enter"] in driver.pressed_keys
        assert any("https://music.youtube.com/search?q=" in txt for txt in driver.typed_texts)
        assert any(entry.get("action") == "click" for entry in driver.action_history)
        actions = store.get_actions_for_run(res.run_id)
        assert any(row["action_type"] == "verify_state" and row["status"] == "ok" for row in actions)
        assert all(row["policy_decision"] == "allowed" for row in actions)

    def test_window_b_api_key_input_switches_backend_and_updates_status(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        app = QApplication.instance() or QApplication([])

        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(safety=safety, driver=driver)

        assert hasattr(win, "api_key_input")
        assert win.api_key_input.echoMode() == QLineEdit.EchoMode.Password
        assert win.preset_task_combo.findText("Open Firefox, open YouTube Music, and put on a song") >= 0
        assert win.backend_combo.currentText() == "Local Demo / Fallback"

        win.api_key_input.setText("AIzaFakeTestKey123456")
        assert win.backend_combo.currentText() == "Gemini API"
        assert "Configured" in win.backend_status_label.text()

        win.backend_combo.setCurrentText("Local Demo / Fallback")
        win.api_key_input.setText("sk-FakeOpenAIKey123456")
        assert win.backend_combo.currentText() == "OpenAI API"
        assert "Configured" in win.backend_status_label.text()

        win.api_key_input.setText("")
        assert "Missing" in win.backend_status_label.text()

        win.close()
        app.processEvents()

    def test_save_api_key_to_dotenv_and_backend_switching_preserves_keys(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        env_file = tmp_path / ".env"
        assert save_api_key_to_dotenv("Gemini API", "AIzaSavedGeminiKey12345", env_path=env_file) == "GEMINI_API_KEY"
        assert save_api_key_to_dotenv("OpenAI API", "sk-SavedOpenAIKey12345", env_path=env_file) == "OPENAI_API_KEY"
        env_text = env_file.read_text(encoding="utf-8")
        assert "GEMINI_API_KEY=AIzaSavedGeminiKey12345" in env_text
        assert "OPENAI_API_KEY=sk-SavedOpenAIKey12345" in env_text

        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        app = QApplication.instance() or QApplication([])
        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(safety=safety, driver=driver)

        assert hasattr(win, "save_key_button")
        win.api_key_input.setText("AIzaGuiSavedKey99999")
        win.save_key_button.click()
        assert win.backend_combo.currentText() == "Gemini API"
        assert "Configured" in win.backend_status_label.text()

        win.backend_combo.setCurrentText("Local Demo / Fallback")
        assert win.backend_status_label.text() == "Ready (Local Demo)"
        assert win.api_key_input.text() == ""

        win.backend_combo.setCurrentText("OpenAI API")
        assert "Missing" in win.backend_status_label.text()
        win.api_key_input.setText("sk-GuiSavedKey88888")
        win.save_key_button.click()
        assert "Configured" in win.backend_status_label.text()

        # Switching back recalls each backend's saved key cleanly
        win.backend_combo.setCurrentText("Gemini API")
        assert win.api_key_input.text() == "AIzaGuiSavedKey99999"
        assert "Configured" in win.backend_status_label.text()

        win.backend_combo.setCurrentText("OpenAI API")
        assert win.api_key_input.text() == "sk-GuiSavedKey88888"
        assert "Configured" in win.backend_status_label.text()

        # Verify AQ. Gemini Express keys auto-select Gemini API, save as GEMINI_API_KEY, and redact cleanly
        win.backend_combo.setCurrentText("Local Demo / Fallback")
        aq_key = "AQ.FakeVertexExpressKey12345"
        win.api_key_input.setText(aq_key)
        assert win.backend_combo.currentText() == "Gemini API"
        assert save_api_key_to_dotenv("Local Demo / Fallback", aq_key, env_path=env_file) == "GEMINI_API_KEY"
        assert f"GEMINI_API_KEY={aq_key}" in env_file.read_text(encoding="utf-8")
        assert aq_key not in redact_secrets(f"Error with {aq_key}")

        win.close()
        app.processEvents()

    def test_locate_youtube_music_play_button_detects_white_pill(self) -> None:
        img = Image.new("RGB", (1920, 1080), (10, 10, 10))
        draw = ImageDraw.Draw(img)
        draw.rectangle([420, 460, 500, 496], fill=(250, 250, 250))

        px, py = locate_youtube_music_play_button(
            img,
            active_window_rect=(0, 0, 1920, 1080),
            screen_size=(1920, 1080),
        )
        assert (px, py) == (460, 478)

    def test_youtube_music_task_fails_when_playback_verification_does_not_match(
        self,
        tmp_path: Path,
    ) -> None:
        class _NoPlaybackDriver(MockDryRunDriver):
            def verify_state(
                self,
                target_path: Optional[str] = None,
                expected_text: Optional[str] = None,
            ) -> tuple[bool, str]:
                if expected_text and expected_text.strip().lower() == "- youtube music":
                    return False, "YouTube Music is open, but no active song playback was detected in the window title"
                return super().verify_state(target_path=target_path, expected_text=expected_text)

        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = _NoPlaybackDriver(safety=safety)
        store = SQLiteAuditStore(tmp_path / "failed_playback.db")
        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            store=store,
            policy=PolicyEngine(approved_workspace=ws),
            planner=LocalDemoFallbackPlanner(),
            max_retries=2,
        )

        res = engine.run_task("Open Firefox, open YouTube Music, and put on a song")
        assert res.status == "failed"
        assert res.retries_used == 2
        assert "no active song playback" in (res.error or "").lower()

    def test_bot_character_face_and_overlay_state_transitions(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
        app = QApplication.instance() or QApplication([])

        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(safety=safety, driver=driver)

        assert isinstance(win.bot_avatar, BotFaceWidget)
        assert isinstance(win.character_overlay, BotCharacterOverlay)
        assert win.bot_avatar.mood == "idle"
        assert win.character_overlay.current_mood == "idle"

        win.start_button.click()
        assert win.bot_avatar.mood == "working"
        assert win.character_overlay.current_mood == "working"

        win.set_progress(4, 10, "Clicking Play on YouTube Music")
        assert "[4/10]" in win.character_overlay.status_text
        win.bot_avatar.grab()
        win.character_overlay._on_anim_tick()

        win.pause_button.click()
        assert win.bot_avatar.mood == "paused"
        assert win.character_overlay.current_mood == "paused"

        win.resume_button.click()
        assert win.bot_avatar.mood == "working"
        assert win.character_overlay.current_mood == "working"

        win.emergency_stop_button.click()
        assert win.bot_avatar.mood == "stopped"
        assert win.character_overlay.current_mood == "stopped"

        win.close()
        app.processEvents()

    def test_annotate_screenshot_and_compute_change_ratio(self) -> None:
        base = Image.new("RGB", (640, 360), (20, 24, 32))
        annotated = annotate_screenshot_for_planner(base, (460, 107), (1920, 1080))
        assert annotated is not None
        assert annotated.size == base.size
        assert compute_screenshot_change_ratio(base, base) == pytest.approx(0.0)
        assert compute_screenshot_change_ratio(base, annotated) > 0.0

        changed = base.copy()
        draw = ImageDraw.Draw(changed)
        draw.rectangle([100, 100, 350, 260], fill=(240, 240, 240))
        assert compute_screenshot_change_ratio(base, changed) > 0.01

    def test_extract_song_query_for_juice_wrld_and_turn_on_song_prompts(self) -> None:
        prompt1 = "open firefox and turn on a juice wrld song"
        prompt2 = "open firefox and open youtube music and start playing juice wrld"
        assert _is_youtube_music_task(prompt1.lower()) is True
        assert _is_youtube_music_task(prompt2.lower()) is True
        assert _extract_song_query(prompt1).lower() == "juice wrld"
        assert _extract_song_query(prompt2).lower() == "juice wrld"

    def test_ai_planner_self_corrects_when_repeating_same_click_cycle(
        self,
        tmp_path: Path,
    ) -> None:
        class _RepeatingClickPlanner(BasePlanner):
            name = "RepeatingClickPlanner"

            def __init__(self) -> None:
                self.seen_errors: list[Optional[str]] = []

            def is_available(self) -> tuple[bool, str]:
                return True, "Ready"

            def plan_next_step(
                self,
                obs: ObservationContext,
                screenshot: Optional[Image.Image] = None,
            ) -> DesktopAction:
                self.seen_errors.append(obs.last_error)
                if obs.last_error and (
                    "Loop detected" in obs.last_error or "Self-correction warning" in obs.last_error
                ):
                    return DesktopAction(
                        action_type=ActionType.done,
                        description="Self-corrected after loop detection",
                    )
                return DesktopAction(
                    action_type=ActionType.click,
                    x=460,
                    y=107,
                    description="Click search bar again",
                )

        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)
        store = SQLiteAuditStore(tmp_path / "loop_correct.db")
        planner = _RepeatingClickPlanner()

        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            store=store,
            policy=PolicyEngine(approved_workspace=ws),
            planner=planner,
            max_retries=3,
        )
        res = engine.run_task("Click search bar in app")
        assert res.status == "completed"
        assert res.retries_used >= 1
        assert any(err and "Loop detected" in err for err in planner.seen_errors)

    def test_create_foveated_screenshot_preserves_full_screen_and_adds_zoom(self) -> None:
        full = Image.new("RGB", (1920, 1080), (20, 24, 32))
        draw = ImageDraw.Draw(full)
        draw.rectangle([420, 460, 500, 496], fill=(250, 250, 250))

        fov = create_foveated_screenshot(full, (460, 478), (1920, 1080))
        assert fov is not None
        assert fov.size == (960, 540)
        # Zoom inset in bottom-right corner should contain bright pixels from the (420..500, 460..496) target
        inset_center = fov.getpixel((960 - 8 - 160, 540 - 8 - 120))
        assert inset_center[0] > 200 and inset_center[1] > 200 and inset_center[2] > 200

    def test_som_marks_extraction_and_mark_id_click_resolution(self, tmp_path: Path) -> None:
        img = Image.new("RGB", (1920, 1080), (15, 15, 15))
        draw = ImageDraw.Draw(img)
        draw.rectangle([800, 360, 920, 404], fill=(245, 245, 245))

        marks = extract_som_marks(img, (1920, 1080))
        assert 1 in marks
        mx, my = marks[1]
        assert 800 <= mx <= 920 and 360 <= my <= 404

        class _MarkDriver(MockDryRunDriver):
            def capture_screenshot(self) -> Image.Image:
                return img

        class _MarkPlanner(BasePlanner):
            name = "MarkPlanner"

            def is_available(self) -> tuple[bool, str]:
                return True, "Ready"

            def plan_next_step(
                self,
                obs: ObservationContext,
                screenshot: Optional[Image.Image] = None,
            ) -> DesktopAction:
                if obs.step_index == 0:
                    return DesktopAction(
                        action_type=ActionType.click,
                        mark_id=1,
                        description="Click mark [1]",
                    )
                return DesktopAction(action_type=ActionType.done, description="Done")

        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = _MarkDriver(safety=safety)
        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            policy=PolicyEngine(approved_workspace=ws),
            planner=_MarkPlanner(),
        )
        res = engine.run_task("Click button")
        assert res.status == "completed"
        assert driver.get_cursor_position() == (mx, my)

    def test_scale_normalized_coord_maps_1000_grid_to_screen(self) -> None:
        assert scale_normalized_coord(0, 1920) == 0
        assert scale_normalized_coord(500, 1920) == 960
        assert scale_normalized_coord(1000, 1920) == 1919
        assert scale_normalized_coord(1000, 1080) == 1079

    def test_viewport_coordinate_math_nested_zoom_and_dpi_crop(self) -> None:
        assert normalize_coord(0, 1920) == 0
        assert normalize_coord(960, 1920) == 500
        assert normalize_coord(1919, 1920) == 1000

        full_vp = (0, 0, 1919, 1079)
        vp1 = subregion_viewport(200, 200, 600, 600, full_vp)
        assert vp1 == (384, 216, 1151, 647)
        assert map_viewport_coord(500, 500, vp1) == (768, 432)
        norm_roundtrip = normalize_to_viewport(768, 432, vp1)
        assert norm_roundtrip == (501, 501)
        assert map_viewport_coord(*norm_roundtrip, vp1) == (768, 432)

        # Level-2 nested zoom inside vp1
        vp2 = subregion_viewport(250, 250, 750, 750, vp1)
        assert vp2[0] > vp1[0] and vp2[1] > vp1[1]
        assert vp2[2] < vp1[2] and vp2[3] < vp1[3]

        # DPI-scaled screenshot (2880x1620 for 1920x1080 logical screen)
        dpi_img = Image.new("RGB", (2880, 1620), (10, 20, 30))
        cropped = crop_screenshot_to_viewport(dpi_img, vp1, (1920, 1080))
        assert cropped.size == (1152, 648)

    def test_policy_readonly_inspection_exempt_from_approval_but_still_sandboxes(self) -> None:
        policy = PolicyEngine()
        zoom_instagram = DesktopAction(
            action_type=ActionType.view_region,
            left=100,
            top=200,
            right=500,
            bottom=600,
            description="Zoom into Instagram DM message bar",
        )
        decision = policy.evaluate(zoom_instagram)
        assert decision.allowed is True
        assert decision.requires_approval is False
        assert decision.risk_category == "safe"

        reset_outlook = DesktopAction(
            action_type=ActionType.view_screen,
            description="Inspect Outlook window full screen",
        )
        reset_decision = policy.evaluate(reset_outlook)
        assert reset_decision.allowed is True
        assert reset_decision.requires_approval is False

        blocked_system = DesktopAction(
            action_type=ActionType.view_region,
            left=0,
            top=0,
            right=400,
            bottom=400,
            description=r"Inspect C:\Windows\System32\cmd.exe",
        )
        assert policy.evaluate(blocked_system).allowed is False

        blocked_runas = DesktopAction(
            action_type=ActionType.view_screen,
            description="Elevate via runas",
        )
        assert policy.evaluate(blocked_runas).allowed is False

    def test_engine_view_region_click_auto_reset_and_zoom_depth_limit(
        self,
        tmp_path: Path,
    ) -> None:
        class _ZoomDriver(MockDryRunDriver):
            def capture_screenshot(self) -> Image.Image:
                img = Image.new("RGB", (1920, 1080), (10, 10, 10))
                draw = ImageDraw.Draw(img)
                draw.rectangle([384, 216, 1152, 648], fill=(220, 40, 40))
                return img

        class _ZoomOllamaPlanner(OllamaPlanner):
            def __init__(self) -> None:
                super().__init__()
                self.seen_obs: list[ObservationContext] = []
                self.seen_sizes: list[tuple[int, int]] = []
                self._calls = 0

            def plan_next_step(
                self,
                obs: ObservationContext,
                screenshot: Optional[Image.Image] = None,
            ) -> DesktopAction:
                self.seen_obs.append(obs)
                self.seen_sizes.append(screenshot.size if screenshot is not None else (0, 0))
                idx = self._calls
                self._calls += 1
                if idx == 0:
                    return DesktopAction(
                        action_type=ActionType.view_region,
                        left=200,
                        top=200,
                        right=600,
                        bottom=600,
                        description="Zoom level 1",
                    )
                if idx == 1:
                    return DesktopAction(
                        action_type=ActionType.view_region,
                        left=100,
                        top=100,
                        right=900,
                        bottom=900,
                        description="Zoom level 2",
                    )
                if idx == 2:
                    # 3rd consecutive zoom should trigger zoom depth guard retry
                    return DesktopAction(
                        action_type=ActionType.view_region,
                        left=200,
                        top=200,
                        right=800,
                        bottom=800,
                        description="Zoom level 3 should retry",
                    )
                if idx == 3:
                    return DesktopAction(
                        action_type=ActionType.view_screen,
                        description="Reset to full screen",
                    )
                if idx == 4:
                    return DesktopAction(
                        action_type=ActionType.view_region,
                        left=200,
                        top=200,
                        right=600,
                        bottom=600,
                        description="Zoom target region",
                    )
                if idx == 5:
                    return DesktopAction(
                        action_type=ActionType.click,
                        x=500,
                        y=500,
                        description="Click center of zoomed region",
                    )
                return DesktopAction(action_type=ActionType.done, description="Completed")

        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = _ZoomDriver(safety=safety)
        planner = _ZoomOllamaPlanner()
        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            policy=PolicyEngine(approved_workspace=ws),
            planner=planner,
            uia_collector=lambda _vp, _ss: [],
        )
        res = engine.run_task("Zoom and click target")
        assert res.status == "completed"
        assert res.retries_used == 1
        assert driver.get_cursor_position() == (768, 432)

        assert planner.seen_obs[0].viewport_mode == "full"
        assert planner.seen_sizes[0] == (1920, 1080)
        assert planner.seen_obs[1].viewport_mode == "region"
        assert planner.seen_sizes[1] == (768, 432)
        assert planner.seen_obs[3].last_error and "Already zoomed in 2 levels" in planner.seen_obs[3].last_error
        assert planner.seen_obs[4].viewport_mode == "full"
        assert planner.seen_obs[5].viewport_mode == "region"
        # After click, viewport auto-resets back to full for verification step
        assert planner.seen_obs[6].viewport_mode == "full"
        # AI history contains normalized coordinates, not raw pixel (768, 432)
        assert any(
            entry.get("verification") == "Clicked normalized (500, 500)"
            for entry in planner.seen_obs[6].history
        )

    def test_build_observation_prompt_never_leaks_raw_pixels(self) -> None:
        obs = ObservationContext(
            task_text="open firefox and open youtube music and start playing juice wrld",
            step_index=4,
            screen_size=(1920, 1080),
            cursor_pos=(1200, 700),
            active_window_rect=(0, 0, 1919, 1079),
            active_window_title="YouTube Music — Mozilla Firefox",
            som_marks={1: [846, 516]},
            detected_ui_hints={"youtube_music_play_button_xy": [441, 478]},
            coordinates_normalized=False,
        )
        prompt = _build_observation_prompt(obs)
        assert "Observation:\n" in prompt
        obs_json = prompt.split("Observation:\n", 1)[1]
        assert '"screen_size"' not in obs_json
        assert "1920" not in obs_json
        assert "1080" not in obs_json
        assert "1200" not in obs_json
        assert "700" not in obs_json
        assert "846" not in obs_json
        assert "441" not in obs_json
        parsed_obs = json.loads(obs_json)
        assert parsed_obs["coordinate_space"] == "normalized_0_to_1000_of_current_image"
        assert parsed_obs["cursor_pos"] == [625, 649]
        # 441 / 1919 * 1000 = 230, 478 / 1079 * 1000 = 443
        assert parsed_obs["detected_ui_hints"]["youtube_music_play_button_xy"] == [230, 443]
        assert "som_marks" not in parsed_obs

    def test_uia_grounding_and_mark_id_precedence(self, tmp_path: Path) -> None:
        uia_items = [
            {
                "id": 1,
                "role": "Button",
                "name": "Shuffle",
                "rect": [820, 395, 920, 435],
                "center": [870, 415],
            }
        ]

        class _UIAOllamaPlanner(OllamaPlanner):
            def __init__(self) -> None:
                super().__init__()
                self.seen_obs: list[ObservationContext] = []

            def plan_next_step(
                self,
                obs: ObservationContext,
                screenshot: Optional[Image.Image] = None,
            ) -> DesktopAction:
                self.seen_obs.append(obs)
                if obs.step_index == 0:
                    return DesktopAction(
                        action_type=ActionType.click,
                        mark_id=1,
                        x=100,
                        y=100,
                        description="Click Shuffle via mark_id",
                    )
                return DesktopAction(action_type=ActionType.done, description="Done")

        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)
        planner = _UIAOllamaPlanner()
        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            policy=PolicyEngine(approved_workspace=ws),
            planner=planner,
            uia_collector=lambda _vp, _ss: uia_items,
        )
        res = engine.run_task("Click Shuffle")
        assert res.status == "completed"
        assert planner.seen_obs[0].ui_elements == [
            {"id": 1, "role": "Button", "name": "Shuffle", "center": [453, 385]}
        ]
        # mark_id=1 takes precedence over fallback (100, 100)
        assert driver.get_cursor_position() == (870, 415)

    def test_parse_action_json_handles_aliases_extra_fields_and_non_yt_verify(self) -> None:
        act1 = _parse_action_json(
            '{"action_type": "left_click", "x": 453.4, "y": "384.6", "marks": [{"mark_id": 1}], "description": ""}'
        )
        assert act1.action_type == ActionType.click
        assert (act1.x, act1.y) == (453, 385)
        assert act1.description == "Execute click"

        act2 = _parse_action_json(
            '{"action_type": "press_key", "key": "l", "modifiers": ["ctrl"], "description": "Focus URL"}'
        )
        assert act2.action_type == ActionType.press_keys
        assert act2.keys == ["ctrl", "l"]

        act3 = _parse_action_json(
            '{"action_type": "verify_state", "description": "Verify Instagram"}',
            task_text="send hi to friend on instagram",
            active_window_title="Instagram — Mozilla Firefox",
        )
        assert act3.expected_text == "Instagram — Mozilla Firefox"

        act4 = _parse_action_json('{"action_type": "wait", "milliseconds": 500}')
        assert act4.action_type == ActionType.wait
        assert act4.seconds == pytest.approx(0.5)

        act5 = _parse_action_json('{"action_type": "scroll", "direction": "up", "amount": 4}')
        assert act5.action_type == ActionType.scroll
        assert act5.clicks == 4

        long_desc = "A" * 400
        act6 = _parse_action_json(f'{{"action_type": "done", "description": "{long_desc}"}}')
        assert len(act6.description) == 240

    def test_bot_character_overlay_exclude_from_capture(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
        _ = QApplication.instance() or QApplication([])

        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        overlay = BotCharacterOverlay(driver=driver)
        assert overlay._apply_exclude_from_capture() is False

        calls: list[tuple[int, int]] = []

        def _fake_set_affinity(hwnd: int, affinity: int) -> int:
            calls.append((hwnd, affinity))
            return 1

        monkeypatch.setattr(overlay, "_is_headless_or_test_mode", lambda: False)
        monkeypatch.setattr(overlay, "winId", lambda: 123456)
        fake_user32 = types.SimpleNamespace(SetWindowDisplayAffinity=_fake_set_affinity)
        fake_windll = types.SimpleNamespace(user32=fake_user32)
        monkeypatch.setattr(gui_mod.ctypes, "windll", fake_windll, raising=False)
        monkeypatch.setattr(gui_mod.os, "name", "nt")

        assert overlay._apply_exclude_from_capture() is True
        assert calls == [(123456, 0x11)]
        overlay.close()

    def test_policy_open_url_and_wait_exempt_while_messaging_actions_require_approval(self) -> None:
        policy = PolicyEngine()

        nav_ig = DesktopAction(
            action_type=ActionType.open_url,
            url="https://www.instagram.com/direct/inbox/",
            description="Open Instagram Direct Messages in Firefox",
        )
        nav_decision = policy.evaluate(nav_ig)
        assert nav_decision.allowed is True
        assert nav_decision.requires_approval is False
        assert nav_decision.risk_category == "safe"

        wait_ig = DesktopAction(
            action_type=ActionType.wait,
            seconds=2.0,
            description="Wait for Instagram messages to load",
        )
        wait_decision = policy.evaluate(wait_ig)
        assert wait_decision.allowed is True
        assert wait_decision.requires_approval is False

        type_ig = DesktopAction(
            action_type=ActionType.type_text,
            text="hi",
            description="Type hi into Instagram message box",
        )
        type_decision = policy.evaluate(type_ig)
        assert type_decision.allowed is True
        assert type_decision.requires_approval is True
        assert type_decision.risk_category == "social_post"

        risky_url = DesktopAction(
            action_type=ActionType.open_url,
            url="https://example.com/downloads/setup.exe",
            description="Open downloaded installer link",
        )
        risky_decision = policy.evaluate(risky_url)
        assert risky_decision.allowed is True
        assert risky_decision.requires_approval is True
        assert risky_decision.risk_category == "install_or_execute"

    def test_antigravity_cli_interpreter_and_hybrid_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured_cmds: list[list[str]] = []
        inner_plan = {
            "intent_summary": "Open Instagram DMs and message My Goldfish",
            "strategy": "web_browser",
            "starting_url": "https://www.instagram.com/direct/inbox/",
            "desktop_app": None,
            "steps": [
                "Use open_url to navigate to https://www.instagram.com/direct/inbox/.",
                "Click the conversation with My Goldfish.",
                "Type hi and send after user approval.",
            ],
            "success_criteria": "Message hi sent to My Goldfish.",
        }
        envelope = {"status": "SUCCESS", "response": json.dumps(inner_plan)}

        def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            captured_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(envelope), stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        cli = AntigravityCLIInterpreter(executable=r"C:\fake\agy.exe", timeout_sec=35.0)
        plan = cli.interpret_task('open instagram and text My Goldfish "hi"')
        assert plan.strategy == "web_browser"
        assert plan.starting_url == "https://www.instagram.com/direct/inbox/"
        assert plan.source == "antigravity_cli"
        assert len(plan.steps) == 3
        assert "--json-schema" not in captured_cmds[0]
        assert "--effort" not in captured_cmds[0]

        class _FailingPrimary(AntigravityCLIInterpreter):
            def is_available(self) -> tuple[bool, str]:
                return True, "Ready"

            def interpret_task(
                self,
                task_text: str,
                *,
                failure_context: Optional[str] = None,
                active_window_title: Optional[str] = None,
            ) -> TaskPlan:
                raise RuntimeError("agy temporary failure")

        class _FallbackOllama(OllamaPlanInterpreter):
            def is_available(self) -> tuple[bool, str]:
                return True, "Ready"

            def interpret_task(
                self,
                task_text: str,
                *,
                failure_context: Optional[str] = None,
                active_window_title: Optional[str] = None,
            ) -> TaskPlan:
                return TaskPlan(
                    intent_summary="Fallback web plan",
                    strategy="web_browser",
                    starting_url="https://www.instagram.com",
                    desktop_app=None,
                    steps=["Open Instagram via open_url."],
                    success_criteria="Instagram open",
                    source="ollama",
                )

        hybrid = HybridPlanInterpreter(primary=_FailingPrimary(), fallback=_FallbackOllama())
        fallback_plan = hybrid.interpret_task("open instagram")
        assert fallback_plan.source == "ollama"
        assert fallback_plan.starting_url == "https://www.instagram.com"

    def test_engine_uses_plan_interpreter_open_url_and_adaptive_replan_on_winerror2(
        self,
        tmp_path: Path,
    ) -> None:
        class _RecordingInterpreter(AntigravityCLIInterpreter):
            def __init__(self) -> None:
                super().__init__(executable=r"C:\fake\agy.exe")
                self.calls: list[Optional[str]] = []

            def is_available(self) -> tuple[bool, str]:
                return True, "Ready"

            def interpret_task(
                self,
                task_text: str,
                *,
                failure_context: Optional[str] = None,
                active_window_title: Optional[str] = None,
            ) -> TaskPlan:
                self.calls.append(failure_context)
                if failure_context:
                    return TaskPlan(
                        intent_summary="Switch to web browser for Instagram",
                        strategy="web_browser",
                        starting_url="https://www.instagram.com/direct/inbox/",
                        desktop_app=None,
                        steps=["Use open_url to open https://www.instagram.com/direct/inbox/."],
                        success_criteria="Instagram inbox open",
                        source="antigravity_cli",
                    )
                return TaskPlan(
                    intent_summary="Open Instagram",
                    strategy="hybrid",
                    starting_url="https://www.instagram.com",
                    desktop_app="instagram",
                    steps=["Open Instagram"],
                    success_criteria="Instagram open",
                    source="antigravity_cli",
                )

        class _WinError2Driver(MockDryRunDriver):
            def launch_app(
                self,
                app_name: str,
                args: Optional[list[str]] = None,
            ) -> dict[str, object]:
                if app_name.lower() == "instagram":
                    raise FileNotFoundError(
                        "Desktop app 'instagram' was not found on Windows (WinError 2)."
                    )
                return super().launch_app(app_name, args=args)

        class _AdaptiveOllamaPlanner(OllamaPlanner):
            def __init__(self) -> None:
                super().__init__()
                self.seen_plans: list[Optional[dict[str, object]]] = []
                self._calls = 0

            def plan_next_step(
                self,
                obs: ObservationContext,
                screenshot: Optional[Image.Image] = None,
            ) -> DesktopAction:
                self.seen_plans.append(obs.task_plan)
                idx = self._calls
                self._calls += 1
                if idx == 0:
                    return DesktopAction(
                        action_type=ActionType.launch_app,
                        app_name="instagram",
                        description="Try launching Instagram app",
                    )
                if idx == 1:
                    assert obs.task_plan is not None
                    url = str(obs.task_plan["starting_url"])
                    return DesktopAction(
                        action_type=ActionType.open_url,
                        url=url,
                        description="Open Instagram DMs in browser",
                    )
                return DesktopAction(action_type=ActionType.done, description="Instagram opened")

        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = _WinError2Driver(safety=safety)
        store = SQLiteAuditStore(tmp_path / "interpreter_replan.db")
        interpreter = _RecordingInterpreter()
        planner = _AdaptiveOllamaPlanner()

        approval_calls: list[str] = []

        def _approve_once(action: DesktopAction, _dec: object) -> bool:
            approval_calls.append(action.action_type.value)
            return True

        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            store=store,
            policy=PolicyEngine(approved_workspace=ws),
            planner=planner,
            approval_callback=_approve_once,
            uia_collector=lambda _vp, _ss: [],
            plan_interpreter=interpreter,
        )
        res = engine.run_task("open instagram")
        assert res.status == "completed"
        assert "launch_app" in approval_calls
        assert "open_url" not in approval_calls
        assert res.retries_used == 1
        assert interpreter.calls[0] is None
        assert interpreter.calls[1] and "WinError 2" in interpreter.calls[1]
        assert "https://www.instagram.com/direct/inbox/" in driver.opened_urls
        assert planner.seen_plans[0] is not None
        assert planner.seen_plans[1] is not None
        assert planner.seen_plans[1]["starting_url"] == "https://www.instagram.com/direct/inbox/"

        logged = store.get_actions_for_run(res.run_id)
        interp_logs = [row for row in logged if row["action_type"] == "plan_interpreter"]
        assert [row["status"] for row in interp_logs] == ["ok", "replanned"]

    def test_ollama_loopback_default_antigravity_factory_and_gui_status(self) -> None:
        assert OllamaPlanner().base_url == "http://127.0.0.1:11434"
        hybrid_planner = create_planner("Antigravity + Ollama")
        assert isinstance(hybrid_planner, OllamaPlanner)
        assert hybrid_planner.name == "Antigravity + Ollama"

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
        app = QApplication.instance() or QApplication([])
        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(safety=safety, driver=driver)
        assert win.backend_combo.findText("Antigravity + Ollama") >= 0
        win.backend_combo.setCurrentText("Antigravity + Ollama")
        assert "Antigravity + Ollama" in win.backend_status_label.text()
        win.close()
        app.processEvents()

    def test_live_driver_unicode_clipboard_typing_and_helpful_winerror2(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        copied: list[str] = []
        hotkeys: list[tuple[str, ...]] = []
        fake_pyperclip = types.SimpleNamespace(
            paste=lambda: "old clipboard",
            copy=lambda val: copied.append(str(val)),
        )
        monkeypatch.setitem(sys.modules, "pyperclip", fake_pyperclip)

        safety = SafetyController()
        safety.authorize_start()
        driver = LiveWindowsDesktopDriver(safety=safety)
        monkeypatch.setattr(driver, "_poll_hotkeys_and_safety", lambda *a, **k: None)
        monkeypatch.setattr(
            driver._pyautogui,
            "hotkey",
            lambda *args, **_kwargs: hotkeys.append(tuple(str(a) for a in args)),
        )
        monkeypatch.setattr("controller.driver.time.sleep", lambda _s: None)

        driver.type_text("hi 👋 สวัสดี")
        assert copied == ["hi 👋 สวัสดี", "old clipboard"]
        assert hotkeys == [("ctrl", "v")]

        def _missing_exe(*_args: object, **_kwargs: object) -> object:
            raise FileNotFoundError(2, "The system cannot find the file specified", "instagram")

        monkeypatch.setattr(subprocess, "Popen", _missing_exe)
        with pytest.raises(FileNotFoundError) as exc_info:
            driver.launch_app("instagram")
        assert "open_url" in str(exc_info.value)
        assert "Do NOT call request_approval" in str(exc_info.value)

    def test_supervised_auto_approve_allows_risky_without_prompt_but_blocks_escalation(
        self,
        tmp_path: Path,
    ) -> None:
        class _SupervisedPlanner(BasePlanner):
            name = "SupervisedPlanner"

            def __init__(self, actions: list[DesktopAction]) -> None:
                self._actions = actions

            def is_available(self) -> tuple[bool, str]:
                return True, "Ready"

            def plan_next_step(
                self,
                obs: ObservationContext,
                screenshot: Optional[Image.Image] = None,
            ) -> DesktopAction:
                idx = min(obs.step_index, len(self._actions) - 1)
                return self._actions[idx]

        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)
        store = SQLiteAuditStore(tmp_path / "supervised.db")

        approval_called = False

        def _should_not_be_called(_act: DesktopAction, _dec: object) -> bool:
            nonlocal approval_called
            approval_called = True
            return False

        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            store=store,
            policy=PolicyEngine(approved_workspace=ws),
            planner=_SupervisedPlanner(
                [
                    DesktopAction(
                        action_type=ActionType.type_text,
                        text="hi",
                        description="Type hi into Instagram DM",
                    ),
                    DesktopAction(action_type=ActionType.done, description="Message sent"),
                ]
            ),
            approval_callback=_should_not_be_called,
            auto_approve_supervised=True,
        )
        res = engine.run_task("text My Goldfish hi")
        assert res.status == "completed"
        assert approval_called is False
        assert "hi" in driver.typed_texts
        rows = store.get_actions_for_run(res.run_id)
        assert rows[0]["approval_status"] == "auto_approved"

        # Hard blocks (privilege escalation / outside sandbox) stay blocked even in supervised mode
        safety2 = SafetyController()
        safety2.authorize_start()
        engine_blocked = DesktopExecutionEngine(
            driver=MockDryRunDriver(safety=safety2),
            safety=safety2,
            store=store,
            policy=PolicyEngine(approved_workspace=ws),
            planner=_SupervisedPlanner(
                [
                    DesktopAction(
                        action_type=ActionType.launch_app,
                        app_name="runas",
                        description="Escalate privileges",
                    )
                ]
            ),
            auto_approve_supervised=True,
        )
        res_blocked = engine_blocked.run_task("Run elevated command")
        assert res_blocked.status == "failed"
        assert "privilege" in (res_blocked.error or "").lower()

    def test_driver_bot_vs_human_cursor_ownership(self) -> None:
        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)
        assert driver.is_bot_driving_cursor() is False

        driver.begin_bot_pointer_action((1750, 120))
        driver.record_bot_pointer_step((1700, 130))
        assert driver.is_bot_driving_cursor((1705, 132)) is True
        assert driver.get_bot_target_position() == (1750, 120)

        # Human yanks mouse >120px away mid-move -> no longer bot-driven
        assert driver.is_bot_driving_cursor((400, 700)) is False

        driver.end_bot_pointer_action((1750, 120))
        assert driver.get_bot_target_position() is None
        # Within grace_sec right at final bot spot -> True, but if human moved >12px -> False
        assert driver.is_bot_driving_cursor((1750, 120), grace_sec=1.0) is True
        assert driver.is_bot_driving_cursor((1700, 120), grace_sec=1.0) is False

    def test_compact_corner_ui_dodges_bot_cursor_but_stays_still_for_human(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
        app = QApplication.instance() or QApplication([])

        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(
            safety=safety,
            driver=driver,
            initial_approval_mode=APPROVAL_MODE_SUPERVISED,
        )
        assert win.width() <= 420 and win.height() <= 600
        assert win._current_corner == "top_right"
        assert win.is_supervised_mode() is True

        win.set_compact_mode(True)
        assert win._compact_mode is True
        assert win.width() <= 340 and win.height() <= 140
        assert win.compact_toggle_button.text() == "Expand"

        win.set_compact_mode(False)
        assert win._compact_mode is False
        assert win.compact_toggle_button.text() == "Compact"

        # Bot approaches top-right corner -> Window B runs away to bottom_left
        driver.begin_bot_pointer_action((1750, 120))
        driver.record_bot_pointer_step((1750, 120))
        dodged = win.check_and_dodge_bot_cursor((1750, 120))
        assert dodged is True
        assert win._current_corner == "bottom_left"

        # Human approaches the new corner (bottom_left) -> Window B stays completely still
        driver.end_bot_pointer_action((1750, 120))
        pos_before = (win.x(), win.y())
        human_center = (win.x() + win.width() // 2, win.y() + win.height() // 2)
        assert win.check_and_dodge_bot_cursor(human_center) is False
        assert win._current_corner == "bottom_left"
        assert (win.x(), win.y()) == pos_before

        win.close()
        app.processEvents()

    def test_window_b_exclude_from_capture_and_cli_supervised_flag(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
        app = QApplication.instance() or QApplication([])

        args = app_main.build_argument_parser().parse_args(["--supervised"])
        assert args.supervised is True

        safety = SafetyController()
        driver = MockDryRunDriver(safety=safety)
        win = DesktopControllerWindow(safety=safety, driver=driver)
        assert win._apply_exclude_from_capture() is False

        calls: list[tuple[int, int]] = []

        def _fake_set_affinity(hwnd: int, affinity: int) -> int:
            calls.append((hwnd, affinity))
            return 1

        monkeypatch.setattr(win, "_is_headless_or_test_mode", lambda: False)
        monkeypatch.setattr(win, "winId", lambda: 654321)
        fake_user32 = types.SimpleNamespace(SetWindowDisplayAffinity=_fake_set_affinity)
        fake_windll = types.SimpleNamespace(user32=fake_user32)
        monkeypatch.setattr(gui_mod.ctypes, "windll", fake_windll, raising=False)
        monkeypatch.setattr(gui_mod.os, "name", "nt")

        assert win._apply_exclude_from_capture() is True
        assert calls == [(654321, 0x11)]
        win.close()
        app.processEvents()

    def test_wrapped_cli_args_normalized_and_run_bot_ps1_parses(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
        _ = QApplication.instance() or QApplication([])
        rc = app_main.main(
            [
                "--headless-check",
                "--backend",
                "Antigravity +\n    Ollama",
                "--supervised",
                "--task",
                "open\n    instagram",
            ]
        )
        assert rc == 0

        ps1_path = Path(__file__).resolve().parent.parent / "run_bot.ps1"
        assert ps1_path.is_file()
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    f"$errs = $null; "
                    f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ps1_path}', [ref]$null, [ref]$errs); "
                    f"if ($errs.Count -gt 0) {{ $errs | ForEach-Object {{ Write-Error $_.Message }}; exit 1 }}"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        assert proc.returncode == 0, proc.stderr

    def test_find_matching_browser_window_title_and_live_open_url_reuse(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        visible = [
            "Window B - Autonomous Windows Desktop Controller (Instagram)",
            "Instagram Notes - Notepad",
            "Instagram • Messages — Mozilla Firefox",
            "YouTube Music — Mozilla Firefox",
        ]
        assert (
            find_matching_browser_window_title("https://www.instagram.com/direct/inbox/", visible)
            == "Instagram • Messages — Mozilla Firefox"
        )
        assert (
            find_matching_browser_window_title("https://www.instagram.com/", visible)
            == "Instagram • Messages — Mozilla Firefox"
        )
        assert (
            find_matching_browser_window_title("https://music.youtube.com/search?q=juice+wrld", visible)
            is None
        )
        assert (
            find_matching_browser_window_title("https://www.instagram.com/reel/123/", visible)
            is None
        )
        assert (
            find_matching_browser_window_title(
                "https://www.youtube.com/",
                ["YouTube Music — Mozilla Firefox"],
            )
            is None
        )
        assert (
            find_matching_browser_window_title(
                "https://www.youtube.com/",
                ["Home - YouTube — Mozilla Firefox"],
            )
            == "Home - YouTube — Mozilla Firefox"
        )
        assert (
            find_matching_browser_window_title(
                "https://music.youtube.com/",
                ["YouTube Music — Mozilla Firefox"],
            )
            == "YouTube Music — Mozilla Firefox"
        )

        safety = SafetyController()
        safety.authorize_start()
        driver = LiveWindowsDesktopDriver(safety=safety)
        monkeypatch.setattr(driver, "_poll_hotkeys_and_safety", lambda *a, **k: None)
        monkeypatch.setattr(
            driver_mod,
            "_list_visible_window_titles",
            lambda: [
                "Window B - Autonomous Windows Desktop Controller",
                "Instagram • Messages — Mozilla Firefox",
            ],
        )
        monkeypatch.setattr(
            driver_mod,
            "_get_foreground_window_title",
            lambda: "Window B - Autonomous Windows Desktop Controller",
        )
        focus_calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            driver_mod,
            "_focus_window_by_title",
            lambda title, target_pid=None, maximize=False: (
                focus_calls.append((title, maximize)) or ((0, 0, 1920, 1080), True)
            ),
        )
        popen_calls: list[list[str]] = []
        monkeypatch.setattr(
            driver_mod.subprocess,
            "Popen",
            lambda cmd, **kwargs: popen_calls.append(list(cmd)) or types.SimpleNamespace(pid=5555),
        )
        monkeypatch.setattr(
            driver_mod,
            "_resolve_app_executable",
            lambda name: r"C:\Program Files\Mozilla Firefox\firefox.exe" if name == "firefox" else name,
        )
        monkeypatch.setattr(driver_mod.shutil, "which", lambda _n: "firefox")
        monkeypatch.setattr(driver_mod.time, "sleep", lambda _s: None)

        reused_meta = driver.open_url("https://www.instagram.com/direct/inbox/")
        assert reused_meta["reused_existing_window"] is True
        assert reused_meta["matched_window_title"] == "Instagram • Messages — Mozilla Firefox"
        assert len(popen_calls) == 0
        assert len(focus_calls) == 1

        new_tab_meta = driver.open_url("https://music.youtube.com/search?q=juice+wrld")
        assert new_tab_meta["reused_existing_window"] is False
        assert len(popen_calls) == 1
        assert popen_calls[0][1] == "https://music.youtube.com/search?q=juice+wrld"

    def test_plan_interpreter_receives_visible_windows_and_supports_reuse_existing_window(
        self,
        tmp_path: Path,
    ) -> None:
        prompt = _build_interpreter_prompt(
            "open instagram",
            active_window_title="Home — Mozilla Firefox",
            visible_window_titles=[
                "Home — Mozilla Firefox",
                "Instagram • Messages — Mozilla Firefox",
            ],
        )
        assert "Current active window title: Home — Mozilla Firefox" in prompt
        assert "Instagram • Messages — Mozilla Firefox" in prompt
        assert "reuse_existing_window" in prompt

        class _VisibleWindowInterpreter(BasePlanInterpreter):
            name = "VisibleWindowInterpreter"

            def __init__(self) -> None:
                self.seen_active: list[Optional[str]] = []
                self.seen_visible: list[Optional[list[str]]] = []

            def is_available(self) -> tuple[bool, str]:
                return True, "Ready"

            def interpret_task(
                self,
                task_text: str,
                *,
                failure_context: Optional[str] = None,
                active_window_title: Optional[str] = None,
                visible_window_titles: Optional[list[str]] = None,
            ) -> TaskPlan:
                self.seen_active.append(active_window_title)
                self.seen_visible.append(list(visible_window_titles or []))
                return TaskPlan(
                    intent_summary="Reuse existing Instagram browser window",
                    strategy="web_browser",
                    starting_url=None,
                    desktop_app=None,
                    reuse_existing_window=True,
                    steps=["Reuse existing Instagram window instead of opening a new tab."],
                    success_criteria="Instagram focused",
                    source="test",
                )

        class _ReuseGuardOllamaPlanner(OllamaPlanner):
            def __init__(self) -> None:
                super().__init__()
                self.seen_obs: list[ObservationContext] = []
                self._calls = 0

            def plan_next_step(
                self,
                obs: ObservationContext,
                screenshot: Optional[Image.Image] = None,
            ) -> DesktopAction:
                self.seen_obs.append(obs)
                idx = self._calls
                self._calls += 1
                if idx in (0, 1):
                    return DesktopAction(
                        action_type=ActionType.open_url,
                        url="https://www.instagram.com/direct/inbox/",
                        description="Open Instagram Inbox",
                    )
                return DesktopAction(action_type=ActionType.done, description="Reused existing browser window")

        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)
        driver.active_window_text = "Untitled - Notepad"
        driver.visible_window_titles = ["Instagram • Messages — Mozilla Firefox"]

        interpreter = _VisibleWindowInterpreter()
        planner = _ReuseGuardOllamaPlanner()
        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            policy=PolicyEngine(approved_workspace=ws),
            planner=planner,
            uia_collector=lambda _vp, _ss: [],
            plan_interpreter=interpreter,
        )
        res = engine.run_task("open instagram")
        assert res.status == "completed"
        assert res.retries_used == 1
        assert interpreter.seen_active == ["Untitled - Notepad"]
        assert interpreter.seen_visible == [["Instagram • Messages — Mozilla Firefox"]]
        assert driver.opened_urls == []
        assert driver.active_window_text == "Instagram • Messages — Mozilla Firefox"
        assert planner.seen_obs[0].visible_window_titles == ["Instagram • Messages — Mozilla Firefox"]
        assert "visible_window_titles" in _build_observation_prompt(planner.seen_obs[0])
        assert planner.seen_obs[2].last_error and "ALREADY open" in planner.seen_obs[2].last_error

    def test_uia_prioritize_ui_elements_keeps_contacts_and_message_box_past_browser_chrome(self) -> None:
        raw_items: list[dict[str, object]] = []
        for idx in range(50):
            cx = 40 + (idx % 25) * 70
            cy = 20 if idx < 25 else 85
            raw_items.append(
                {
                    "role": "Button" if idx >= 25 else "TabItem",
                    "name": f"Toolbar Control {idx}",
                    "rect": [cx - 20, cy - 10, cx + 20, cy + 10],
                    "center": [cx, cy],
                }
            )
        raw_items.append(
            {
                "role": "ListItem",
                "name": "kkkkkkkkkkkk Active now",
                "rect": [60, 460, 260, 500],
                "center": [160, 478],
            }
        )
        raw_items.append(
            {
                "role": "Edit",
                "name": "Message...",
                "rect": [400, 965, 1600, 1005],
                "center": [576, 987],
            }
        )

        terms = extract_priority_terms("open instagram and text kkkkkkkkkkk hi")
        result = prioritize_ui_elements(
            raw_items,
            (1920, 1080),
            (0, 0, 1919, 1079),
            max_elements=40,
            priority_terms=terms,
        )
        assert len(result) == 40
        assert [el["id"] for el in result] == list(range(1, 41))
        names = [el["name"] for el in result]
        assert "kkkkkkkkkkkk Active now" in names
        assert "Message..." in names

    def test_engine_blocks_duplicate_message_spam_and_resets_zoom_but_allows_failed_focus_retry(
        self,
        tmp_path: Path,
    ) -> None:
        class _SpamAttemptOllamaPlanner(OllamaPlanner):
            def __init__(self) -> None:
                super().__init__()
                self.seen_obs: list[ObservationContext] = []
                self._calls = 0

            def plan_next_step(
                self,
                obs: ObservationContext,
                screenshot: Optional[Image.Image] = None,
            ) -> DesktopAction:
                self.seen_obs.append(obs)
                idx = self._calls
                self._calls += 1
                if idx == 0:
                    return DesktopAction(
                        action_type=ActionType.type_text,
                        text="Hi! I'm your new friend. How are you?",
                        description="Type initial greeting on Instagram",
                    )
                if idx == 1:
                    return DesktopAction(
                        action_type=ActionType.press_keys,
                        keys=["enter"],
                        description="Send initial greeting",
                    )
                if idx == 2:
                    return DesktopAction(
                        action_type=ActionType.view_region,
                        left=200,
                        top=850,
                        right=400,
                        bottom=960,
                        description="Zoom into message box",
                    )
                if idx == 3:
                    return DesktopAction(
                        action_type=ActionType.type_text,
                        text="Hello! I am your new friend. How are you?",
                        description="Repeat intro message in zoomed view",
                    )
                if idx == 4:
                    assert obs.viewport_mode == "full"
                    assert obs.last_error and "Duplicate text warning" in obs.last_error
                    return DesktopAction(
                        action_type=ActionType.wait,
                        seconds=6.0,
                        description="Wait for reply without spamming",
                    )
                return DesktopAction(action_type=ActionType.done, description="Done waiting")

        ws = tmp_path / "approved_workspace"
        ws.mkdir()
        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)
        planner = _SpamAttemptOllamaPlanner()
        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            policy=PolicyEngine(approved_workspace=ws),
            planner=planner,
            uia_collector=lambda _vp, _ss: [],
            auto_approve_supervised=True,
        )
        res = engine.run_task("open instagram and text kkkkkkkkkkk. Introduce yourself")
        assert res.status == "completed"
        assert res.retries_used == 1
        assert "Hello! I am your new friend. How are you?" not in driver.typed_texts
        assert driver.typed_texts == ["Hi! I'm your new friend. How are you?"]

        # Part B: Live-like driver where Step 0 typing produces NO visible change -> re-typing after click is allowed
        class _StaticScreenLiveLikeDriver(BaseDesktopDriver):
            def __init__(self, safety_ctrl: SafetyController) -> None:
                super().__init__(safety=safety_ctrl)
                self.typed_texts: list[str] = []
                self._img = Image.new("RGB", (640, 360), (20, 20, 20))

            def get_screen_size(self) -> tuple[int, int]:
                return (1920, 1080)

            def get_cursor_position(self) -> tuple[int, int]:
                return (960, 540)

            def capture_screenshot(self) -> Image.Image:
                return self._img

            def move_to(self, x: int, y: int, speed_config: Optional[SpeedConfig] = None) -> list[tuple[int, int]]:
                return [(x, y)]

            def click(
                self,
                x: int,
                y: int,
                button: str = "left",
                double: bool = False,
                speed_config: Optional[SpeedConfig] = None,
            ) -> None:
                return None

            def type_text(self, text: str, interval: float = 0.02) -> None:
                self.typed_texts.append(text)

            def press_keys(self, keys: list[str]) -> None:
                return None

            def scroll(self, clicks: int) -> None:
                return None

            def wait(self, seconds: float) -> None:
                return None

            def launch_app(self, app_name: str, args: Optional[list[str]] = None) -> dict[str, object]:
                return {}

            def open_url(self, url: str, browser: str = "firefox") -> dict[str, object]:
                return {}

            def verify_state(
                self,
                target_path: Optional[str] = None,
                expected_text: Optional[str] = None,
            ) -> tuple[bool, str]:
                return True, "ok"

        class _RefocusRetryOllamaPlanner(OllamaPlanner):
            def __init__(self) -> None:
                super().__init__()
                self.seen_errors: list[Optional[str]] = []

            def plan_next_step(
                self,
                obs: ObservationContext,
                screenshot: Optional[Image.Image] = None,
            ) -> DesktopAction:
                self.seen_errors.append(obs.last_error)
                if obs.step_index == 0:
                    return DesktopAction(
                        action_type=ActionType.type_text,
                        text="hi",
                        description="Type hi into unfocused box",
                    )
                if obs.step_index == 1:
                    return DesktopAction(
                        action_type=ActionType.click,
                        x=300,
                        y=910,
                        description="Click input box to focus it",
                    )
                if obs.step_index == 2:
                    return DesktopAction(
                        action_type=ActionType.type_text,
                        text="hi",
                        description="Re-type hi after focusing box",
                    )
                return DesktopAction(action_type=ActionType.done, description="Sent hi")

        safety2 = SafetyController()
        safety2.authorize_start()
        live_like_driver = _StaticScreenLiveLikeDriver(safety2)
        refocus_planner = _RefocusRetryOllamaPlanner()
        engine2 = DesktopExecutionEngine(
            driver=live_like_driver,
            safety=safety2,
            policy=PolicyEngine(approved_workspace=ws),
            planner=refocus_planner,
            uia_collector=lambda _vp, _ss: [],
            auto_approve_supervised=True,
        )
        res2 = engine2.run_task("text kkkkkkkkkkk hi")
        assert res2.status == "completed"
        assert res2.retries_used == 0
        assert live_like_driver.typed_texts == ["hi", "hi"]
        assert not any(err and "Duplicate text warning" in err for err in refocus_planner.seen_errors)
