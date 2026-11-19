"""Deterministic Observe-Plan-Validate-Policy-Act-Verify desktop execution engine."""

from __future__ import annotations

import inspect
import os
import re
import time
import urllib.parse
from typing import Any, Callable, Optional
from uuid import uuid4

from PIL import Image
from pydantic import BaseModel

from controller.driver import (
    BaseDesktopDriver,
    MockDryRunDriver,
    _clean_visible_window_titles,
    _get_foreground_window_title,
    _list_visible_window_titles,
    cleanup_notepads_for_fresh_demo,
    find_window_rect,
    get_foreground_window_rect,
)
from controller.emergency import EmergencyStopError, SafetyController, SafetyGateError
from controller.interpreter import BasePlanInterpreter, HybridPlanInterpreter, TaskPlan
from controller.planners import (
    BasePlanner,
    GeminiPlanner,
    LocalDemoFallbackPlanner,
    ObservationContext,
    OllamaPlanner,
    OpenAIPlanner,
    _extract_song_query,
    _is_youtube_music_task,
    annotate_screenshot_for_planner,
    compute_screenshot_change_ratio,
    create_planner,
    crop_screenshot_to_viewport,
    extract_som_marks,
    is_browser_or_media_task,
    locate_youtube_music_play_button,
    map_viewport_coord,
    normalize_to_viewport,
    scale_normalized_coord,
    subregion_viewport,
)
from controller.policy import PolicyDecision, PolicyEngine
from controller.schema import ActionType, DesktopAction
from controller.store import SQLiteAuditStore
from controller.uia import collect_foreground_ui_elements, extract_priority_terms


def _normalize_message_for_dedup(text: str) -> str:
    """Normalize typed text to lowercase space-separated words for spam deduplication."""
    return re.sub(r"[\W_]+", " ", str(text or "").lower()).strip()


def _is_duplicate_message(candidate: str, recent_texts: list[str]) -> bool:
    """Return True if candidate matches or nearly duplicates recently sent chat text."""
    raw = str(candidate or "").strip()
    if not raw:
        return False
    lower = raw.lower()
    if lower.startswith(("http://", "https://")) or "\\" in raw or "/" in raw:
        return False
    cand_norm = _normalize_message_for_dedup(raw)
    if not cand_norm:
        return False
    cand_words = set(cand_norm.split())
    for prev in recent_texts[-4:]:
        prev_norm = _normalize_message_for_dedup(prev)
        if not prev_norm:
            continue
        if cand_norm == prev_norm:
            return True
        if len(cand_norm) >= 10 and len(prev_norm) >= 10:
            prev_words = set(prev_norm.split())
            min_len = min(len(cand_words), len(prev_words))
            if min_len >= 4:
                overlap = len(cand_words & prev_words) / float(min_len)
                if overlap >= 0.75:
                    return True
    return False


class ExecutionResult(BaseModel):
    """Final outcome summary returned by DesktopExecutionEngine.run_task."""

    run_id: str
    status: str  # "completed", "failed", "stopped", "denied"
    steps_executed: int
    retries_used: int
    error: Optional[str] = None
    summary: str = ""


class DesktopExecutionEngine:
    """Runs bounded Observe -> Plan -> Validate -> Policy -> Act -> Verify loops."""

    def __init__(
        self,
        driver: BaseDesktopDriver,
        safety: SafetyController,
        store: Optional[SQLiteAuditStore] = None,
        policy: Optional[PolicyEngine] = None,
        planner: Optional[BasePlanner] = None,
        backend_name: str = "Local Demo / Fallback",
        api_key: Optional[str] = None,
        max_retries: int = 3,
        max_steps: int = 30,
        approval_callback: Optional[Callable[[DesktopAction, PolicyDecision], bool]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        screenshot_callback: Optional[Callable[[Image.Image], None]] = None,
        history_callback: Optional[Callable[[str, str, str], None]] = None,
        completion_callback: Optional[Callable[[ExecutionResult], None]] = None,
        max_no_op_repeats: int = 5,
        max_wall_seconds: float = 300.0,
        uia_collector: Optional[
            Callable[[tuple[int, int, int, int], tuple[int, int]], list[dict[str, Any]]]
        ] = None,
        plan_interpreter: Optional[BasePlanInterpreter] = None,
        auto_approve_supervised: bool = False,
    ) -> None:
        self.driver = driver
        self.safety = safety
        self.store = store
        self.policy = policy or PolicyEngine()
        self.planner = planner or create_planner(backend_name, api_key=api_key)
        self.backend_name = backend_name
        self._current_task_text: str = ""
        self.max_retries = max(0, int(max_retries))
        self.max_steps = max(1, int(max_steps))
        self.approval_callback = approval_callback
        self.progress_callback = progress_callback
        self.screenshot_callback = screenshot_callback
        self.history_callback = history_callback
        self.completion_callback = completion_callback
        self.max_no_op_repeats = max(0, int(max_no_op_repeats))
        self.max_wall_seconds = max(0.01, float(max_wall_seconds))
        self._custom_uia_collector: bool = uia_collector is not None
        self.uia_collector = uia_collector or collect_foreground_ui_elements
        self._explicit_plan_interpreter = plan_interpreter is not None
        self.plan_interpreter: Optional[BasePlanInterpreter] = plan_interpreter
        self.auto_approve_supervised: bool = bool(auto_approve_supervised)

    def _emit_history(self, action_type: str, status: str, detail: str) -> None:
        if self.history_callback is not None:
            self.history_callback(action_type, status, detail)

    def _emit_progress(self, current: int, total: int, description: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(current, total, description)

    def _emit_screenshot(self, image: Image.Image) -> None:
        if self.screenshot_callback is not None:
            self.screenshot_callback(image)

    def _finish_and_emit(self, result: ExecutionResult) -> ExecutionResult:
        if self.store is not None:
            self.store.finish_run(result.run_id, result.status, error=result.error)
        if not self.safety.emergency_stopped:
            self.safety.revoke_authorization()
        if self.completion_callback is not None:
            self.completion_callback(result)
        return result

    def _wait_if_paused(self) -> None:
        """Poll briefly while paused/takeover; raise immediately on stop or unauth."""
        was_paused = bool(self.safety.paused or self.safety.manual_takeover)
        timeout_sec = (
            0.15 if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1" else 300.0
        )
        deadline = time.monotonic() + timeout_sec
        while (self.safety.paused or self.safety.manual_takeover) and not self.safety.emergency_stopped:
            if not self.safety.authorized:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        self.safety.assert_can_execute()
        if was_paused:
            self.driver.restore_last_app_focus()

    def _active_window_rect(self) -> Optional[tuple[int, int, int, int]]:
        if isinstance(self.driver, MockDryRunDriver):
            if self.driver.launched_apps:
                return (160, 120, 960, 720)
            return None
        lower_task = self._current_task_text.lower()
        if "notepad" in lower_task or "milestone 4" in lower_task:
            rect = find_window_rect("Notepad")
            if rect is not None:
                return rect
        return get_foreground_window_rect()

    def _active_window_title(self) -> Optional[str]:
        if isinstance(self.driver, MockDryRunDriver):
            return self.driver.active_window_text
        return _get_foreground_window_title()

    def _visible_window_titles(self) -> list[str]:
        if isinstance(self.driver, MockDryRunDriver):
            return _clean_visible_window_titles(list(self.driver.visible_window_titles))
        return _clean_visible_window_titles(_list_visible_window_titles())

    def _call_plan_interpreter(
        self,
        interpreter: BasePlanInterpreter,
        task_text: str,
        *,
        failure_context: Optional[str] = None,
        active_window_title: Optional[str] = None,
        visible_window_titles: Optional[list[str]] = None,
    ) -> Optional[TaskPlan]:
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
        except Exception:
            return None

    def _execute_action(self, action: DesktopAction) -> str:
        at = action.action_type
        if at == ActionType.click:
            self.driver.click(int(action.x), int(action.y), double=False)
            return f"Clicked ({action.x}, {action.y})"
        if at == ActionType.double_click:
            self.driver.click(int(action.x), int(action.y), double=True)
            return f"Double-clicked ({action.x}, {action.y})"
        if at == ActionType.move:
            self.driver.move_to(int(action.x), int(action.y))
            return f"Moved cursor to ({action.x}, {action.y})"
        if at == ActionType.type_text:
            self.driver.type_text(str(action.text))
            return "Typed text"
        if at == ActionType.press_keys:
            self.driver.press_keys(list(action.keys or []))
            return f"Pressed keys {action.keys}"
        if at == ActionType.scroll:
            self.driver.scroll(int(action.clicks))
            return f"Scrolled {action.clicks}"
        if at == ActionType.wait:
            self.driver.wait(float(action.seconds))
            return f"Waited {action.seconds}s"
        if at == ActionType.launch_app:
            args = [action.target_path] if action.target_path else None
            self.driver.launch_app(str(action.app_name), args=args)
            return f"Launched {action.app_name}"
        if at == ActionType.open_url:
            browser = (action.app_name or "firefox").strip() or "firefox"
            meta = self.driver.open_url(str(action.url), browser=browser)
            if isinstance(meta, dict) and meta.get("reused_existing_window"):
                matched = meta.get("matched_window_title") or browser
                return (
                    f"Focused existing window {matched!r} instead of opening a new tab "
                    "(if you need a different page on this site, click inside the page or press Ctrl+L in this tab)"
                )
            return f"Opened URL {action.url} in {browser}"
        if at == ActionType.verify_state:
            ok, msg = self.driver.verify_state(
                target_path=action.target_path,
                expected_text=action.expected_text,
            )
            if not ok:
                raise RuntimeError(msg)
            return msg
        if at == ActionType.request_approval:
            return f"Approved: {action.reason}"
        return action.description

    def _verify_task_completion(
        self,
        task_text: str,
        history: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        if any(
            entry.get("action_type") == ActionType.verify_state.value
            and entry.get("status") == "ok"
            for entry in history
        ):
            return True, "Task completion verified"
        if _is_youtube_music_task(task_text.lower()):
            return self.driver.verify_state(expected_text=" - YouTube Music")
        return True, "Task execution completed"

    def run_task(self, task_text: str) -> ExecutionResult:
        """Execute a desktop task through Observe-Plan-Validate-Policy-Act-Verify."""
        self._current_task_text = task_text
        speed_name = self.driver.speed_config.preset.value
        run_id = (
            self.store.start_run(task_text, self.planner.name, speed_name)
            if self.store is not None
            else f"run-{uuid4().hex[:12]}"
        )

        lower_task = task_text.lower()
        is_notepad_demo = (
            isinstance(self.planner, LocalDemoFallbackPlanner)
            and ("notepad" in lower_task or "milestone 4" in lower_task)
        )
        if (
            is_notepad_demo
            and not isinstance(self.driver, MockDryRunDriver)
            and os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1"
        ):
            self.policy.approved_workspace.mkdir(parents=True, exist_ok=True)
            stale_demo = self.policy.approved_workspace / "milestone4_demo.txt"
            if stale_demo.exists():
                stale_demo.unlink()
            cleanup_notepads_for_fresh_demo()

        if isinstance(self.planner, LocalDemoFallbackPlanner):
            if is_notepad_demo:
                progress_total = 16
            elif _is_youtube_music_task(lower_task):
                progress_total = 10
            elif is_browser_or_media_task(task_text):
                progress_total = 8
            else:
                progress_total = self.max_steps
        else:
            progress_total = self.max_steps

        step_index = 0
        steps_executed = 0
        retries_used = 0
        step_retries = 0
        last_error: Optional[str] = None
        history: list[dict[str, Any]] = []
        ai_history: list[dict[str, Any]] = []
        executed_sigs: list[tuple[Any, ...]] = []
        recent_click_coords: list[tuple[int, int]] = []
        prev_screenshot: Optional[Image.Image] = None
        screen_changed_last_step: Optional[bool] = None
        last_action_sig: Optional[tuple[Any, ...]] = None
        consecutive_identical = 0
        active_viewport: Optional[tuple[int, int, int, int]] = None
        consecutive_zooms = 0
        is_ai_planner = isinstance(self.planner, (GeminiPlanner, OpenAIPlanner, OllamaPlanner))
        recent_sent_texts: list[str] = []
        last_open_url_reused: bool = False
        priority_terms = extract_priority_terms(task_text) if is_ai_planner else []
        interpreter: Optional[BasePlanInterpreter] = self.plan_interpreter
        if (
            interpreter is None
            and is_ai_planner
            and not self._explicit_plan_interpreter
            and os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1"
        ):
            interpreter = HybridPlanInterpreter()
        current_plan: Optional[TaskPlan] = None
        replans_used = 0
        wall_deadline = time.monotonic() + self.max_wall_seconds
        run_finished = False
        unexpected_error: Optional[str] = None

        def _complete(result: ExecutionResult) -> ExecutionResult:
            nonlocal run_finished
            run_finished = True
            return self._finish_and_emit(result)

        try:
            if is_ai_planner and interpreter is not None:
                try:
                    self._wait_if_paused()
                    self._emit_progress(0, progress_total, "Interpreting task plan...")
                    initial_active_title = self._active_window_title()
                    initial_visible_titles = self._visible_window_titles()
                    current_plan = self._call_plan_interpreter(
                        interpreter,
                        task_text,
                        active_window_title=initial_active_title,
                        visible_window_titles=initial_visible_titles,
                    )
                    self.safety.assert_can_execute()
                    if current_plan is not None:
                        plan_summary = (
                            f"[{current_plan.source}] {current_plan.strategy}: "
                            f"{current_plan.intent_summary}"
                        )
                        if self.store is not None:
                            self.store.log_action(
                                run_id,
                                0,
                                "plan_interpreter",
                                current_plan.intent_summary,
                                "ok",
                                verification=plan_summary,
                            )
                        self._emit_history("plan_interpreter", "ok", plan_summary)
                except (EmergencyStopError, SafetyGateError) as exc:
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            0,
                            "safety_gate",
                            "Pre-plan safety gate check",
                            "stopped",
                            error=str(exc),
                        )
                    self._emit_history("safety_gate", "stopped", str(exc))
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="stopped",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=str(exc),
                            summary=f"Execution halted by safety gate: {exc}",
                        )
                    )

            while step_index < self.max_steps:
                if time.monotonic() >= wall_deadline:
                    wall_err = f"Exceeded max_wall_seconds ({self.max_wall_seconds}s) without completing task"
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            "timeout",
                            "Wall-clock deadline exceeded",
                            "failed",
                            error=wall_err,
                        )
                    self._emit_history("timeout", "failed", wall_err)
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="failed",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=wall_err,
                            summary=wall_err,
                        )
                    )

                # 1. Observe & Safety Gate
                try:
                    self._wait_if_paused()
                    screenshot = self.driver.capture_screenshot()
                    self._emit_screenshot(screenshot)

                    if step_retries == 0 and prev_screenshot is not None and history:
                        last_entry = history[-1]
                        if (
                            isinstance(self.driver, MockDryRunDriver)
                            or last_entry.get("action_type")
                            in ("wait", "move", "verify_state", "view_region", "view_screen")
                        ):
                            screen_changed_last_step = True
                        else:
                            diff_ratio = compute_screenshot_change_ratio(prev_screenshot, screenshot)
                            screen_changed_last_step = diff_ratio >= 0.002
                        last_entry["screen_changed"] = screen_changed_last_step
                        if ai_history:
                            ai_history[-1]["screen_changed"] = screen_changed_last_step
                        if (
                            not screen_changed_last_step
                            and last_entry.get("action_type") in ("click", "double_click", "type_text", "press_keys")
                        ):
                            if last_entry.get("action_type") == "type_text" and recent_sent_texts:
                                recent_sent_texts.pop()
                            last_error = (
                                f"Self-correction warning: Previous step ({last_entry.get('action_type')}: "
                                f"{last_entry.get('description')}) produced NO visible change on the screen. "
                                "The target element was missed or unfocused. Do NOT repeat the same coordinates "
                                "or action; zoom in with view_region, pick a mark_id from ui_elements, "
                                "try a different coordinate, or navigate directly via Ctrl+L."
                            )
                    if step_retries == 0:
                        prev_screenshot = screenshot

                    screen_size = self.driver.get_screen_size()
                    raw_cursor_pos = self.driver.get_cursor_position()
                    active_rect = self._active_window_rect()
                    active_title = self._active_window_title()
                    visible_titles = self._visible_window_titles()
                    detected_ui_hints: dict[str, Any] = {}
                    navigated_to_yt_search = any(
                        entry.get("action_type") == "type_text"
                        and (
                            "youtube music" in str(entry.get("description", "")).lower()
                            or "music.youtube.com" in str(entry.get("description", "")).lower()
                        )
                        for entry in history
                    )
                    if "youtube" in (active_title or "").lower() and navigated_to_yt_search:
                        px, py = locate_youtube_music_play_button(screenshot, active_rect, screen_size)
                        detected_ui_hints["youtube_music_play_button_xy"] = [px, py]
                    if _is_youtube_music_task(lower_task):
                        detected_ui_hints["direct_search_url_hint"] = (
                            f"https://music.youtube.com/search?q={urllib.parse.quote_plus(_extract_song_query(task_text))}"
                        )

                    full_viewport = (0, 0, max(0, screen_size[0] - 1), max(0, screen_size[1] - 1))
                    current_viewport = active_viewport or full_viewport
                    planner_screenshot = (
                        crop_screenshot_to_viewport(screenshot, current_viewport, screen_size)
                        if (is_ai_planner and active_viewport is not None)
                        else screenshot
                    )

                    uia_elements = (
                        (
                            self.uia_collector(current_viewport, screen_size)
                            if self._custom_uia_collector
                            else collect_foreground_ui_elements(
                                current_viewport,
                                screen_size,
                                priority_terms=priority_terms,
                            )
                        )
                        if is_ai_planner
                        else []
                    )
                    if uia_elements:
                        mark_targets: dict[int, tuple[int, int]] = {
                            int(el["id"]): (int(el["center"][0]), int(el["center"][1]))
                            for el in uia_elements
                        }
                        obs_ui_elements = [
                            {
                                "id": int(el["id"]),
                                "role": el["role"],
                                "name": el["name"],
                                "center": list(
                                    normalize_to_viewport(
                                        int(el["center"][0]),
                                        int(el["center"][1]),
                                        current_viewport,
                                    )
                                ),
                            }
                            for el in uia_elements
                        ]
                        som_marks: dict[int, tuple[int, int]] = {}
                    else:
                        som_marks = extract_som_marks(screenshot, screen_size)
                        mark_targets = dict(som_marks)
                        obs_ui_elements = []

                    if is_ai_planner:
                        norm_cursor = normalize_to_viewport(
                            raw_cursor_pos[0],
                            raw_cursor_pos[1],
                            current_viewport,
                        )
                        norm_active_rect: Optional[tuple[int, int, int, int]] = None
                        if active_rect is not None:
                            nl, nt = normalize_to_viewport(
                                active_rect[0], active_rect[1], current_viewport
                            )
                            nr, nb = normalize_to_viewport(
                                active_rect[2], active_rect[3], current_viewport
                            )
                            norm_active_rect = (nl, nt, nr, nb)

                        ai_hints = dict(detected_ui_hints)
                        if "youtube_music_play_button_xy" in ai_hints:
                            hx, hy = ai_hints["youtube_music_play_button_xy"]
                            vl, vt, vr, vb = current_viewport
                            if vl <= hx <= vr and vt <= hy <= vb:
                                ai_hints["youtube_music_play_button_xy"] = list(
                                    normalize_to_viewport(hx, hy, current_viewport)
                                )
                            else:
                                ai_hints.pop("youtube_music_play_button_xy", None)

                        active_region_norm: Optional[list[int]] = None
                        if active_viewport is not None:
                            al, at_y = normalize_to_viewport(
                                active_viewport[0], active_viewport[1], full_viewport
                            )
                            ar, ab = normalize_to_viewport(
                                active_viewport[2], active_viewport[3], full_viewport
                            )
                            active_region_norm = [al, at_y, ar, ab]

                        obs = ObservationContext(
                            task_text=task_text,
                            step_index=step_index,
                            screen_size=screen_size,
                            cursor_pos=norm_cursor,
                            active_window_rect=norm_active_rect,
                            active_window_title=active_title,
                            visible_window_titles=visible_titles,
                            screen_changed_last_step=screen_changed_last_step,
                            som_marks={},
                            detected_ui_hints=ai_hints,
                            history=list(ai_history),
                            last_error=last_error,
                            approved_workspace=str(self.policy.approved_workspace),
                            viewport_mode="region" if active_viewport is not None else "full",
                            active_region_normalized=active_region_norm,
                            ui_elements=obs_ui_elements,
                            coordinates_normalized=True,
                            task_plan=current_plan.model_dump() if current_plan is not None else None,
                        )
                    else:
                        obs = ObservationContext(
                            task_text=task_text,
                            step_index=step_index,
                            screen_size=screen_size,
                            cursor_pos=raw_cursor_pos,
                            active_window_rect=active_rect,
                            active_window_title=active_title,
                            visible_window_titles=visible_titles,
                            screen_changed_last_step=screen_changed_last_step,
                            som_marks={k: [v[0], v[1]] for k, v in som_marks.items()},
                            detected_ui_hints=detected_ui_hints,
                            history=list(history),
                            last_error=last_error,
                            approved_workspace=str(self.policy.approved_workspace),
                        )

                    if (
                        not isinstance(self.driver, MockDryRunDriver)
                        and os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1"
                    ):
                        try:
                            preview_img = annotate_screenshot_for_planner(
                                screenshot,
                                raw_cursor_pos,
                                screen_size,
                                detected_ui_hints=detected_ui_hints,
                                som_marks=som_marks,
                            )
                            if preview_img is not None:
                                self.policy.approved_workspace.mkdir(parents=True, exist_ok=True)
                                preview_img.save(self.policy.approved_workspace / "latest_step.png")
                            if active_viewport is not None:
                                self.policy.approved_workspace.mkdir(parents=True, exist_ok=True)
                                planner_screenshot.save(
                                    self.policy.approved_workspace / "latest_view_region.png"
                                )
                        except Exception:
                            pass
                except (EmergencyStopError, SafetyGateError) as exc:
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            "safety_gate",
                            "Observe safety gate check",
                            "stopped",
                            error=str(exc),
                        )
                    self._emit_history("safety_gate", "stopped", str(exc))
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="stopped",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=str(exc),
                            summary=f"Execution halted by safety gate: {exc}",
                        )
                    )

                # 2. Plan & Validate
                planned_norm_xy: Optional[tuple[int, int]] = None
                try:
                    planned = self.planner.plan_next_step(obs, planner_screenshot)
                    action = DesktopAction.model_validate(planned)
                    if action.action_type in (ActionType.click, ActionType.double_click, ActionType.move):
                        if action.mark_id is not None:
                            if action.mark_id in mark_targets:
                                action.x, action.y = mark_targets[action.mark_id]
                                if is_ai_planner:
                                    planned_norm_xy = normalize_to_viewport(
                                        action.x, action.y, current_viewport
                                    )
                            elif action.x is None or action.y is None:
                                raise ValueError(
                                    f"Unknown mark_id {action.mark_id}; choose a valid visible mark from {sorted(mark_targets.keys())}"
                                )
                            elif is_ai_planner:
                                planned_norm_xy = (int(action.x), int(action.y))
                                action.x, action.y = map_viewport_coord(
                                    action.x, action.y, current_viewport
                                )
                        elif is_ai_planner and action.x is not None and action.y is not None:
                            planned_norm_xy = (int(action.x), int(action.y))
                            action.x, action.y = map_viewport_coord(
                                action.x, action.y, current_viewport
                            )
                except Exception as exc:
                    err_msg = f"Planner validation failed: {exc}"
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            "plan",
                            "Plan next desktop action",
                            "failed",
                            error=err_msg,
                        )
                    self._emit_history("plan", "failed", err_msg)
                    plan_err_entry = {
                        "step_index": step_index,
                        "action_type": "plan",
                        "description": "Plan next desktop action",
                        "status": "error",
                        "error": err_msg,
                    }
                    history.append(plan_err_entry)
                    ai_history.append(dict(plan_err_entry))
                    if step_retries < self.max_retries:
                        step_retries += 1
                        retries_used += 1
                        last_error = err_msg
                        continue
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="failed",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=err_msg,
                            summary=err_msg,
                        )
                    )

                # Re-check safety gate after potentially slow planning step
                try:
                    self.safety.assert_can_execute()
                except (EmergencyStopError, SafetyGateError) as exc:
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            "safety_gate",
                            "Post-plan safety gate check",
                            "stopped",
                            error=str(exc),
                        )
                    self._emit_history("safety_gate", "stopped", str(exc))
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="stopped",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=str(exc),
                            summary=f"Execution halted by safety gate: {exc}",
                        )
                    )

                # Stuck no-op loop guard on new (non-retry) planned actions
                action_sig = (
                    action.action_type.value,
                    action.x,
                    action.y,
                    action.left,
                    action.top,
                    action.right,
                    action.bottom,
                    action.text,
                    tuple(action.keys) if action.keys else None,
                    action.url,
                    action.target_path,
                )
                if step_retries == 0 and action.action_type not in (ActionType.done, ActionType.fail):
                    if action_sig == last_action_sig:
                        consecutive_identical += 1
                    else:
                        last_action_sig = action_sig
                        consecutive_identical = 1
                    if self.max_no_op_repeats > 0 and consecutive_identical >= self.max_no_op_repeats:
                        loop_err = (
                            f"Stuck no-op loop detected: repeated {action.action_type.value} "
                            f"{consecutive_identical} consecutive times"
                        )
                        if self.store is not None:
                            self.store.log_action(
                                run_id,
                                step_index,
                                action.action_type.value,
                                action.description,
                                "failed",
                                error=loop_err,
                            )
                        self._emit_history(action.action_type.value, "failed", loop_err)
                        return _complete(
                            ExecutionResult(
                                run_id=run_id,
                                status="failed",
                                steps_executed=steps_executed,
                                retries_used=retries_used,
                                error=loop_err,
                                summary=loop_err,
                            )
                        )

                # Multi-step cycle and repeated click-coordinate self-correction guard for AI planners
                if (
                    not isinstance(self.planner, LocalDemoFallbackPlanner)
                    and action.action_type not in (ActionType.done, ActionType.fail)
                ):
                    if (
                        is_ai_planner
                        and action.action_type == ActionType.view_region
                        and consecutive_zooms >= 2
                        and step_retries < self.max_retries
                    ):
                        zoom_warn = (
                            "Already zoomed in 2 levels; click a target in this zoomed view (0..1000) "
                            "or call view_screen to reset."
                        )
                        step_retries += 1
                        retries_used += 1
                        last_error = zoom_warn
                        if self.store is not None:
                            self.store.log_action(
                                run_id,
                                step_index,
                                action.action_type.value,
                                action.description,
                                "retry",
                                error=zoom_warn,
                            )
                        self._emit_history(action.action_type.value, "retry", zoom_warn)
                        continue

                    if (
                        is_ai_planner
                        and action.action_type == ActionType.open_url
                        and last_open_url_reused
                        and action_sig == last_action_sig
                        and step_retries < self.max_retries
                    ):
                        reuse_warn = (
                            "This website is ALREADY open and focused in the current window! "
                            "Do NOT call open_url again — interact directly with the page "
                            "(click, view_region, or Ctrl+L)."
                        )
                        step_retries += 1
                        retries_used += 1
                        last_error = reuse_warn
                        if self.store is not None:
                            self.store.log_action(
                                run_id,
                                step_index,
                                action.action_type.value,
                                action.description,
                                "retry",
                                error=reuse_warn,
                            )
                        self._emit_history(action.action_type.value, "retry", reuse_warn)
                        continue

                    if (
                        is_ai_planner
                        and action.action_type == ActionType.type_text
                        and _is_duplicate_message(str(action.text or ""), recent_sent_texts)
                        and step_retries < self.max_retries
                    ):
                        active_viewport = None
                        consecutive_zooms = 0
                        dup_warn = (
                            "Duplicate text warning: You already typed a matching message recently. "
                            "Do NOT repeat the same message or type the recipient's username into the chat box. "
                            "Check the full chat thread: if waiting for a reply, call wait (5-8s); "
                            "if they replied, write a new contextual reply; if finished, call done."
                        )
                        step_retries += 1
                        retries_used += 1
                        last_error = dup_warn
                        if self.store is not None:
                            self.store.log_action(
                                run_id,
                                step_index,
                                action.action_type.value,
                                action.description,
                                "retry",
                                error=dup_warn,
                            )
                        self._emit_history(action.action_type.value, "retry", dup_warn)
                        continue

                    is_repeat_click = False
                    if (
                        action.action_type in (ActionType.click, ActionType.double_click)
                        and action.x is not None
                        and action.y is not None
                    ):
                        near_last_two = sum(
                            1
                            for cx, cy in recent_click_coords[-2:]
                            if abs(cx - action.x) <= 15 and abs(cy - action.y) <= 15
                        )
                        is_repeat_click = (near_last_two >= 2) or (
                            near_last_two >= 1 and screen_changed_last_step is False
                        )

                    seq = executed_sigs + [action_sig]
                    is_two_step_cycle = (
                        len(seq) >= 4
                        and seq[-4:-2] == seq[-2:]
                        and seq[-2] != seq[-1]
                    )
                    is_three_step_cycle = (
                        len(seq) >= 6
                        and seq[-6:-3] == seq[-3:]
                        and len(set(seq[-3:])) > 1
                    )
                    if (is_repeat_click or is_two_step_cycle or is_three_step_cycle) and step_retries < self.max_retries:
                        if is_ai_planner and planned_norm_xy is not None:
                            coord_note = (
                                f"clicked normalized ({planned_norm_xy[0]}, {planned_norm_xy[1]}) or "
                            )
                        elif not is_ai_planner and action.x is not None and action.y is not None:
                            coord_note = f"clicked ({action.x}, {action.y}) or "
                        else:
                            coord_note = ""
                        loop_warn = (
                            f"Loop detected: You already {coord_note}repeated the same action sequence "
                            "without making progress. Choose a completely different action or navigate "
                            "directly via press_keys ['ctrl', 'l'] to a search URL."
                        )
                        step_retries += 1
                        retries_used += 1
                        last_error = loop_warn
                        if self.store is not None:
                            self.store.log_action(
                                run_id,
                                step_index,
                                action.action_type.value,
                                action.description,
                                "retry",
                                error=loop_warn,
                            )
                        self._emit_history(action.action_type.value, "retry", loop_warn)
                        continue

                # 3. Policy & Approval Gate
                decision = self.policy.evaluate(action)
                if not decision.allowed:
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            action.action_type.value,
                            action.description,
                            "blocked",
                            policy_decision="blocked",
                            approval_status="not_required",
                            error=decision.reason,
                        )
                    self._emit_history(action.action_type.value, "blocked", decision.reason)
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="failed",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=decision.reason,
                            summary=f"Blocked by policy ({decision.risk_category}): {decision.reason}",
                        )
                    )

                approval_status = "not_required"
                policy_label = "allowed"
                if decision.requires_approval:
                    policy_label = "requires_approval"
                    if self.auto_approve_supervised:
                        approval_status = "auto_approved"
                        self._emit_history(
                            "approval",
                            "auto_approved",
                            f"[Supervised/{decision.risk_category}] {decision.reason}",
                        )
                    else:
                        approved = (
                            bool(self.approval_callback(action, decision))
                            if self.approval_callback is not None
                            else False
                        )
                        if self.safety.emergency_stopped:
                            try:
                                self.safety.assert_can_execute()
                            except (EmergencyStopError, SafetyGateError) as exc:
                                if self.store is not None:
                                    self.store.log_action(
                                        run_id,
                                        step_index,
                                        "safety_gate",
                                        "Post-approval safety gate check",
                                        "stopped",
                                        policy_decision=policy_label,
                                        approval_status="stopped",
                                        error=str(exc),
                                    )
                                self._emit_history("safety_gate", "stopped", str(exc))
                                return _complete(
                                    ExecutionResult(
                                        run_id=run_id,
                                        status="stopped",
                                        steps_executed=steps_executed,
                                        retries_used=retries_used,
                                        error=str(exc),
                                        summary=f"Execution halted by safety gate: {exc}",
                                    )
                                )
                        if not approved:
                            if self.store is not None:
                                self.store.log_action(
                                    run_id,
                                    step_index,
                                    action.action_type.value,
                                    action.description,
                                    "denied",
                                    policy_decision=policy_label,
                                    approval_status="denied",
                                    error=decision.reason,
                                )
                            self._emit_history(action.action_type.value, "denied", decision.reason)
                            return _complete(
                                ExecutionResult(
                                    run_id=run_id,
                                    status="denied",
                                    steps_executed=steps_executed,
                                    retries_used=retries_used,
                                    error=f"Approval denied: {decision.reason}",
                                    summary=f"Denied by operator ({decision.risk_category})",
                                )
                            )
                        approval_status = "approved"

                # Re-check safety gate after approval and before terminal/physical execution
                try:
                    self.safety.assert_can_execute()
                except (EmergencyStopError, SafetyGateError) as exc:
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            "safety_gate",
                            "Pre-execution safety gate check",
                            "stopped",
                            policy_decision=policy_label,
                            approval_status=approval_status,
                            error=str(exc),
                        )
                    self._emit_history("safety_gate", "stopped", str(exc))
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="stopped",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=str(exc),
                            summary=f"Execution halted by safety gate: {exc}",
                        )
                    )

                # 4. Terminal actions (done / fail)
                if action.action_type == ActionType.done:
                    ok_done, done_msg = self._verify_task_completion(task_text, history)
                    if not ok_done:
                        verify_err_entry = {
                            "step_index": step_index,
                            "action_type": ActionType.verify_state.value,
                            "description": "Verify task completion before finishing",
                            "status": "error",
                            "error": done_msg,
                        }
                        history.append(verify_err_entry)
                        ai_history.append(dict(verify_err_entry))
                        if self.store is not None:
                            self.store.log_action(
                                run_id,
                                step_index,
                                ActionType.verify_state.value,
                                "Verify task completion before finishing",
                                "retry" if step_retries < self.max_retries else "failed",
                                policy_decision=policy_label,
                                approval_status=approval_status,
                                error=done_msg,
                            )
                        self._emit_history(
                            ActionType.verify_state.value,
                            "retry" if step_retries < self.max_retries else "failed",
                            done_msg,
                        )
                        if step_retries < self.max_retries:
                            step_retries += 1
                            retries_used += 1
                            last_error = done_msg
                            continue
                        return _complete(
                            ExecutionResult(
                                run_id=run_id,
                                status="failed",
                                steps_executed=steps_executed,
                                retries_used=retries_used,
                                error=done_msg,
                                summary=f"Task completion verification failed: {done_msg}",
                            )
                        )
                    self._emit_progress(progress_total, progress_total, action.description)
                    steps_executed += 1
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            action.action_type.value,
                            action.description,
                            "completed",
                            policy_decision=policy_label,
                            approval_status=approval_status,
                            verification="Task completed",
                        )
                    self._emit_history("done", "completed", action.description)
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="completed",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            summary=action.description,
                        )
                    )

                if action.action_type == ActionType.fail:
                    err = action.reason or action.description
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            action.action_type.value,
                            action.description,
                            "failed",
                            policy_decision=policy_label,
                            approval_status=approval_status,
                            error=err,
                        )
                    self._emit_history("fail", "failed", err)
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="failed",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=err,
                            summary=action.description,
                        )
                    )

                # 5. Act & Verify
                self._emit_progress(
                    min(step_index + 1, progress_total),
                    progress_total,
                    action.description,
                )

                def _stop_step(error_msg: str) -> ExecutionResult:
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            action.action_type.value,
                            action.description,
                            "stopped",
                            policy_decision=policy_label,
                            approval_status=approval_status,
                            error=error_msg,
                        )
                    self._emit_history(action.action_type.value, "stopped", error_msg)
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="stopped",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=error_msg,
                            summary=f"Execution stopped: {error_msg}",
                        )
                    )

                try:
                    if action.action_type == ActionType.view_region:
                        assert (
                            action.left is not None
                            and action.top is not None
                            and action.right is not None
                            and action.bottom is not None
                        )
                        active_viewport = subregion_viewport(
                            int(action.left),
                            int(action.top),
                            int(action.right),
                            int(action.bottom),
                            current_viewport,
                        )
                        consecutive_zooms += 1
                        verification_msg = (
                            f"Zoomed into region ({action.left}, {action.top})-({action.right}, {action.bottom})"
                        )
                    elif action.action_type == ActionType.view_screen:
                        active_viewport = None
                        consecutive_zooms = 0
                        verification_msg = "Reset view to full screen"
                    else:
                        verification_msg = self._execute_action(action)
                        if action.action_type in (
                            ActionType.click,
                            ActionType.double_click,
                            ActionType.type_text,
                            ActionType.press_keys,
                            ActionType.scroll,
                            ActionType.launch_app,
                            ActionType.open_url,
                            ActionType.wait,
                        ):
                            active_viewport = None
                            consecutive_zooms = 0
                except EmergencyStopError as exc:
                    return _stop_step(str(exc))
                except SafetyGateError as exc:
                    if (
                        (self.safety.paused or self.safety.manual_takeover)
                        and not self.safety.emergency_stopped
                        and self.safety.authorized
                    ):
                        pause_msg = "Execution paused (Esc / Pause); waiting for Resume"
                        if self.store is not None:
                            self.store.log_action(
                                run_id,
                                step_index,
                                "pause",
                                action.description,
                                "paused",
                                policy_decision=policy_label,
                                approval_status=approval_status,
                                error=str(exc),
                            )
                        self._emit_history("pause", "paused", pause_msg)
                        try:
                            self._wait_if_paused()
                        except (EmergencyStopError, SafetyGateError) as wait_exc:
                            return _stop_step(str(wait_exc))
                        if self.safety.can_execute():
                            continue
                    return _stop_step(str(exc))
                except (RuntimeError, OSError, ValueError) as exc:
                    err_str = str(exc)
                    step_err_entry = {
                        "step_index": step_index,
                        "action_type": action.action_type.value,
                        "description": action.description,
                        "status": "error",
                        "error": err_str,
                    }
                    history.append(step_err_entry)
                    ai_history.append(dict(step_err_entry))
                    if self.store is not None:
                        self.store.log_action(
                            run_id,
                            step_index,
                            action.action_type.value,
                            action.description,
                            "retry" if step_retries < self.max_retries else "failed",
                            policy_decision=policy_label,
                            approval_status=approval_status,
                            error=err_str,
                        )
                    self._emit_history(
                        action.action_type.value,
                        "retry" if step_retries < self.max_retries else "failed",
                        err_str,
                    )
                    if step_retries < self.max_retries:
                        if (
                            is_ai_planner
                            and interpreter is not None
                            and replans_used < 1
                            and (
                                isinstance(exc, FileNotFoundError)
                                or "winerror 2" in err_str.lower()
                                or step_retries >= 1
                            )
                        ):
                            new_plan = self._call_plan_interpreter(
                                interpreter,
                                task_text,
                                failure_context=err_str,
                                active_window_title=active_title,
                                visible_window_titles=visible_titles,
                            )
                            if new_plan is not None:
                                current_plan = new_plan
                                replans_used += 1
                                replan_summary = (
                                    f"[{new_plan.source}] {new_plan.strategy}: "
                                    f"{new_plan.intent_summary}"
                                )
                                if self.store is not None:
                                    self.store.log_action(
                                        run_id,
                                        step_index,
                                        "plan_interpreter",
                                        new_plan.intent_summary,
                                        "replanned",
                                        verification=replan_summary,
                                    )
                                self._emit_history("plan_interpreter", "replanned", replan_summary)
                        if (
                            action.action_type == ActionType.verify_state
                            and _is_youtube_music_task(lower_task)
                        ):
                            rx, ry = locate_youtube_music_play_button(
                                screenshot,
                                obs.active_window_rect,
                                obs.screen_size,
                                retry_index=step_retries + 1,
                            )
                            try:
                                self.driver.click(rx, ry)
                                self.driver.wait(1.2)
                            except (EmergencyStopError, SafetyGateError) as stop_exc:
                                return _stop_step(str(stop_exc))
                            except Exception:
                                pass
                        step_retries += 1
                        retries_used += 1
                        last_error = err_str
                        continue
                    return _complete(
                        ExecutionResult(
                            run_id=run_id,
                            status="failed",
                            steps_executed=steps_executed,
                            retries_used=retries_used,
                            error=err_str,
                            summary=f"Step {step_index} failed after {self.max_retries} retries: {err_str}",
                        )
                    )

                # 6. Record successful step
                step_retries = 0
                last_error = None
                steps_executed += 1
                executed_sigs.append(action_sig)
                if (
                    action.action_type in (ActionType.click, ActionType.double_click)
                    and action.x is not None
                    and action.y is not None
                ):
                    recent_click_coords.append((int(action.x), int(action.y)))
                history.append(
                    {
                        "step_index": step_index,
                        "action_type": action.action_type.value,
                        "description": action.description,
                        "status": "ok",
                        "verification": verification_msg,
                    }
                )
                ai_verification = verification_msg
                last_open_url_reused = (
                    action.action_type == ActionType.open_url
                    and "instead of opening a new tab" in verification_msg
                )
                if is_ai_planner and action.action_type == ActionType.type_text and action.text:
                    txt_val = str(action.text)
                    lower_txt = txt_val.strip().lower()
                    if lower_txt and not (
                        lower_txt.startswith(("http://", "https://"))
                        or "\\" in txt_val
                        or "/" in txt_val
                    ):
                        recent_sent_texts.append(txt_val)
                        recent_sent_texts = recent_sent_texts[-10:]
                    ai_verification = f"Typed {txt_val.replace(chr(10), ' ')[:60]!r}"
                if (
                    is_ai_planner
                    and planned_norm_xy is not None
                    and action.action_type
                    in (ActionType.click, ActionType.double_click, ActionType.move)
                ):
                    verb = {
                        "click": "Clicked",
                        "double_click": "Double-clicked",
                        "move": "Moved cursor to",
                    }[action.action_type.value]
                    ai_verification = f"{verb} normalized ({planned_norm_xy[0]}, {planned_norm_xy[1]})"
                ai_history.append(
                    {
                        "step_index": step_index,
                        "action_type": action.action_type.value,
                        "description": action.description,
                        "status": "ok",
                        "verification": ai_verification,
                    }
                )
                if self.store is not None:
                    self.store.log_action(
                        run_id,
                        step_index,
                        action.action_type.value,
                        action.description,
                        "ok",
                        policy_decision=policy_label,
                        approval_status=approval_status,
                        verification=verification_msg,
                    )
                self._emit_history(action.action_type.value, "ok", verification_msg)
                step_index += 1

            max_err = f"Exceeded max_steps ({self.max_steps}) without completing task"
            return _complete(
                ExecutionResult(
                    run_id=run_id,
                    status="failed",
                    steps_executed=steps_executed,
                    retries_used=retries_used,
                    error=max_err,
                    summary=max_err,
                )
            )
        except Exception as exc:
            unexpected_error = f"Unexpected execution error: {exc}"
            raise
        finally:
            if not run_finished and self.store is not None:
                self.store.finish_run(
                    run_id,
                    "failed",
                    error=unexpected_error or "Run aborted unexpectedly",
                )
            if not self.safety.emergency_stopped:
                self.safety.revoke_authorization()
