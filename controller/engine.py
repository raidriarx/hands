"""Deterministic Observe-Plan-Validate-Policy-Act-Verify desktop execution engine."""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional
from uuid import uuid4

from PIL import Image
from pydantic import BaseModel

from controller.driver import BaseDesktopDriver, MockDryRunDriver, find_window_rect
from controller.emergency import EmergencyStopError, SafetyController, SafetyGateError
from controller.planners import (
    BasePlanner,
    LocalDemoFallbackPlanner,
    ObservationContext,
    create_planner,
)
from controller.policy import PolicyDecision, PolicyEngine
from controller.schema import ActionType, DesktopAction
from controller.store import SQLiteAuditStore


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
        max_retries: int = 3,
        max_steps: int = 30,
        approval_callback: Optional[Callable[[DesktopAction, PolicyDecision], bool]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        screenshot_callback: Optional[Callable[[Image.Image], None]] = None,
        history_callback: Optional[Callable[[str, str, str], None]] = None,
        completion_callback: Optional[Callable[[ExecutionResult], None]] = None,
        max_no_op_repeats: int = 5,
        max_wall_seconds: float = 300.0,
    ) -> None:
        self.driver = driver
        self.safety = safety
        self.store = store
        self.policy = policy or PolicyEngine()
        self.planner = planner or create_planner(backend_name)
        self.backend_name = backend_name
        self.max_retries = max(0, int(max_retries))
        self.max_steps = max(1, int(max_steps))
        self.approval_callback = approval_callback
        self.progress_callback = progress_callback
        self.screenshot_callback = screenshot_callback
        self.history_callback = history_callback
        self.completion_callback = completion_callback
        self.max_no_op_repeats = max(0, int(max_no_op_repeats))
        self.max_wall_seconds = max(0.01, float(max_wall_seconds))

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

    def _active_window_rect(self) -> Optional[tuple[int, int, int, int]]:
        if isinstance(self.driver, MockDryRunDriver):
            if any("notepad" in app.lower() for app in self.driver.launched_apps):
                return (160, 120, 960, 720)
            return None
        return find_window_rect("Notepad")

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

    def run_task(self, task_text: str) -> ExecutionResult:
        """Execute a desktop task through Observe-Plan-Validate-Policy-Act-Verify."""
        speed_name = self.driver.speed_config.preset.value
        run_id = (
            self.store.start_run(task_text, self.planner.name, speed_name)
            if self.store is not None
            else f"run-{uuid4().hex[:12]}"
        )

        is_notepad_demo = (
            isinstance(self.planner, LocalDemoFallbackPlanner)
            and ("notepad" in task_text.lower() or "milestone 4" in task_text.lower())
        )
        progress_total = 16 if is_notepad_demo else self.max_steps

        step_index = 0
        steps_executed = 0
        retries_used = 0
        step_retries = 0
        last_error: Optional[str] = None
        history: list[dict[str, Any]] = []
        last_action_sig: Optional[tuple[Any, ...]] = None
        consecutive_identical = 0
        wall_deadline = time.monotonic() + self.max_wall_seconds
        run_finished = False
        unexpected_error: Optional[str] = None

        def _complete(result: ExecutionResult) -> ExecutionResult:
            nonlocal run_finished
            run_finished = True
            return self._finish_and_emit(result)

        try:
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
                    obs = ObservationContext(
                        task_text=task_text,
                        step_index=step_index,
                        screen_size=self.driver.get_screen_size(),
                        cursor_pos=self.driver.get_cursor_position(),
                        active_window_rect=self._active_window_rect(),
                        history=list(history),
                        last_error=last_error,
                        approved_workspace=str(self.policy.approved_workspace),
                    )
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
                try:
                    planned = self.planner.plan_next_step(obs, screenshot)
                    action = DesktopAction.model_validate(planned)
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
                    history.append(
                        {
                            "step_index": step_index,
                            "action_type": "plan",
                            "description": "Plan next desktop action",
                            "status": "error",
                            "error": err_msg,
                        }
                    )
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
                if step_retries == 0 and action.action_type not in (ActionType.done, ActionType.fail):
                    action_sig = (
                        action.action_type.value,
                        action.x,
                        action.y,
                        action.text,
                        tuple(action.keys) if action.keys else None,
                        action.target_path,
                    )
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
                    verification_msg = self._execute_action(action)
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
                    history.append(
                        {
                            "step_index": step_index,
                            "action_type": action.action_type.value,
                            "description": action.description,
                            "status": "error",
                            "error": err_str,
                        }
                    )
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
                history.append(
                    {
                        "step_index": step_index,
                        "action_type": action.action_type.value,
                        "description": action.description,
                        "status": "ok",
                        "verification": verification_msg,
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
