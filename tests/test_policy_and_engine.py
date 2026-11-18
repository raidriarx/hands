"""Tests for Milestone 3: PolicyEngine, SQLiteAuditStore, Planners, and ExecutionEngine."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

import httpx
import pytest
from PIL import Image

from controller.driver import MockDryRunDriver
from controller.emergency import EmergencyStopError, SafetyController
from controller.engine import DesktopExecutionEngine
from controller.planners import (
    BasePlanner,
    GeminiPlanner,
    LocalDemoFallbackPlanner,
    ObservationContext,
    OllamaPlanner,
    OpenAIPlanner,
    create_planner,
)
from controller.policy import APPROVED_WORKSPACE, PolicyDecision, PolicyEngine
from controller.schema import ActionType, DesktopAction
from controller.store import SQLiteAuditStore, redact_secrets


class TestPolicyEngine:
    """Verify sandbox confinement, elevation blocking, and all 7 approval categories."""

    def test_sandbox_confinement_allows_inside_and_blocks_escapes(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "approved_workspace"
        workspace.mkdir()
        policy = PolicyEngine(approved_workspace=workspace)

        inside_file = workspace / "notes" / "demo.txt"
        assert policy.is_within_sandbox(inside_file) is True
        assert policy.is_within_sandbox(str(workspace / "file.txt")) is True

        # Reject parent, root, traversal, UNC, and Windows system paths
        assert policy.is_within_sandbox(tmp_path / "outside.txt") is False
        assert policy.is_within_sandbox(str(workspace / ".." / "escape.txt")) is False
        assert policy.is_within_sandbox(r"C:\Windows\System32\cmd.exe") is False
        assert policy.is_within_sandbox(r"\\server\share\file.txt") is False
        assert policy.is_within_sandbox("") is False

        safe_action = DesktopAction(
            action_type=ActionType.verify_state,
            target_path=str(inside_file),
            description="Verify file inside approved workspace",
        )
        decision = policy.evaluate(safe_action)
        assert decision.allowed is True
        assert decision.requires_approval is False
        assert decision.risk_category == "safe"

        outside_action = DesktopAction(
            action_type=ActionType.verify_state,
            target_path=str(tmp_path / "outside.txt"),
            description="Verify file outside approved workspace",
        )
        outside_decision = policy.evaluate(outside_action)
        assert outside_decision.allowed is False
        assert outside_decision.risk_category == "sandbox_violation"

        traversal_action = DesktopAction(
            action_type=ActionType.verify_state,
            target_path=str(workspace / ".." / "secret.txt"),
            description="Traversal attempt",
        )
        assert policy.evaluate(traversal_action).allowed is False

        sys_text_action = DesktopAction(
            action_type=ActionType.type_text,
            text=r"C:\Windows\System32\drivers\etc\hosts",
            description="Write to Windows system file",
        )
        assert policy.evaluate(sys_text_action).allowed is False

    @pytest.mark.parametrize(
        "app_name,text,desc",
        [
            ("runas", None, "Launch elevated prompt"),
            ("powershell", "-Verb RunAs notepad", "Elevate via PowerShell"),
            ("cmd", None, "Run as Administrator to edit hosts"),
            ("sudo", None, "Sudo command"),
            ("notepad", "elevate process", "Attempt UAC elevate"),
        ],
    )
    def test_admin_elevation_is_unconditionally_blocked(
        self,
        app_name: str,
        text: Optional[str],
        desc: str,
    ) -> None:
        policy = PolicyEngine()
        if text is not None:
            action = DesktopAction(
                action_type=ActionType.type_text,
                text=text,
                description=desc,
            )
        else:
            action = DesktopAction(
                action_type=ActionType.launch_app,
                app_name=app_name,
                description=desc,
            )
        decision = policy.evaluate(action)
        assert decision.allowed is False
        assert decision.risk_category == "privilege_escalation"

    @pytest.mark.parametrize(
        "expected_category,action",
        [
            (
                "send_message",
                DesktopAction(
                    action_type=ActionType.click,
                    x=100,
                    y=200,
                    description="Click Send Email in Outlook",
                ),
            ),
            (
                "social_post",
                DesktopAction(
                    action_type=ActionType.click,
                    x=300,
                    y=200,
                    description="Publish tweet on x.com",
                ),
            ),
            (
                "submit_form",
                DesktopAction(
                    action_type=ActionType.click,
                    x=400,
                    y=250,
                    description="Click Submit Application button",
                ),
            ),
            (
                "delete_or_overwrite",
                DesktopAction(
                    action_type=ActionType.type_text,
                    text="del old_notes.txt",
                    description="Delete file in workspace",
                ),
            ),
            (
                "install_or_execute",
                DesktopAction(
                    action_type=ActionType.launch_app,
                    app_name="setup.exe",
                    description="Install downloaded software",
                ),
            ),
            (
                "system_settings",
                DesktopAction(
                    action_type=ActionType.launch_app,
                    app_name="regedit",
                    description="Open Registry Editor to modify firewall",
                ),
            ),
            (
                "sensitive_access",
                DesktopAction(
                    action_type=ActionType.type_text,
                    text="cat .env",
                    description="Read credentials and token values",
                ),
            ),
        ],
    )
    def test_all_seven_categories_require_explicit_approval(
        self,
        expected_category: str,
        action: DesktopAction,
    ) -> None:
        policy = PolicyEngine()
        decision = policy.evaluate(action)
        assert decision.allowed is True
        assert decision.requires_approval is True
        assert decision.risk_category == expected_category

    def test_overwrite_existing_nonempty_workspace_file_requires_approval(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "approved_workspace"
        workspace.mkdir()
        existing = workspace / "important.txt"
        existing.write_text("existing data", encoding="utf-8")

        policy = PolicyEngine(approved_workspace=workspace)
        overwrite_action = DesktopAction(
            action_type=ActionType.click,
            x=200,
            y=200,
            target_path=str(existing),
            description="Save over existing file",
        )
        decision = policy.evaluate(overwrite_action)
        assert decision.allowed is True
        assert decision.requires_approval is True
        assert decision.risk_category == "delete_or_overwrite"

    def test_explicit_request_approval_always_requires_approval(self) -> None:
        policy = PolicyEngine()
        action = DesktopAction(
            action_type=ActionType.request_approval,
            reason="Confirm external action",
            risk_category="custom_risk",
            description="Ask operator for confirmation",
        )
        decision = policy.evaluate(action)
        assert decision.allowed is True
        assert decision.requires_approval is True
        assert decision.risk_category == "custom_risk"


class TestSQLiteAuditStore:
    """Verify SQLite persistence and automatic API key / token redaction."""

    def test_redact_secrets_scrubs_keys_and_bearer_tokens(self) -> None:
        raw = (
            "Using AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz12345 and "
            "sk-proj1234567890abcdef and Bearer mySecretJwtToken12345 "
            "with GEMINI_API_KEY=AIzaAnotherKey99999999"
        )
        scrubbed = redact_secrets(raw)
        assert "AIzaSyAbCdEf" not in scrubbed
        assert "sk-proj123456" not in scrubbed
        assert "mySecretJwtToken12345" not in scrubbed
        assert "AIzaAnotherKey99999999" not in scrubbed
        assert "[REDACTED]" in scrubbed

    def test_sqlite_store_persists_runs_and_redacts_secrets(self, tmp_path: Path) -> None:
        db_file = tmp_path / "history.db"
        store = SQLiteAuditStore(db_file)

        run_id = store.start_run(
            task_text="Run task with sk-testsecretkey1234567890",
            backend="Gemini API",
            speed_preset="Normal",
        )
        store.log_action(
            run_id=run_id,
            step_index=0,
            action_type="type_text",
            description="Typed Bearer topsecret_token_987654",
            status="ok",
            policy_decision="allowed",
            approval_status="not_required",
            verification="Typed cleanly",
            error="AIzaFakeKey123456789012345",
        )
        store.finish_run(run_id, "completed")

        run = store.get_run(run_id)
        assert run is not None
        assert run["status"] == "completed"
        assert "sk-testsecretkey" not in run["task_text"]
        assert "[REDACTED]" in run["task_text"]

        runs = store.list_runs(limit=10)
        assert len(runs) == 1

        actions = store.get_actions_for_run(run_id)
        assert len(actions) == 1
        assert "topsecret_token_987654" not in actions[0]["description"]
        assert "AIzaFakeKey123456789012345" not in actions[0]["error"]

        recent = store.list_recent_actions(limit=10)
        assert len(recent) == 1

        # Verify raw SQLite bytes contain no secrets either
        with sqlite3.connect(str(db_file)) as conn:
            dump = "\n".join(str(r) for r in conn.execute("SELECT * FROM action_logs").fetchall())
            assert "topsecret_token" not in dump
            assert "AIzaFakeKey" not in dump


class TestPlanners:
    """Verify planner factory, availability checks, and LocalDemoFallbackPlanner."""

    def test_create_planner_and_availability(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        local = create_planner("Local Demo / Fallback")
        assert isinstance(local, LocalDemoFallbackPlanner)
        assert local.is_available()[0] is True

        gemini_missing = create_planner("gemini")
        assert isinstance(gemini_missing, GeminiPlanner)
        assert gemini_missing.is_available()[0] is False

        gemini_with_key = create_planner("Gemini API", api_key="AIzaFakeTestKey12345678")
        assert gemini_with_key.is_available()[0] is True

        openai_missing = create_planner("OpenAI API")
        assert isinstance(openai_missing, OpenAIPlanner)
        assert openai_missing.is_available()[0] is False

        openai_with_key = create_planner("openai", api_key="sk-fakekey1234567890")
        assert openai_with_key.is_available()[0] is True

        def _mock_get(*_args: object, **_kwargs: object) -> httpx.Response:
            return httpx.Response(200, json={"models": []})

        monkeypatch.setattr(httpx, "get", _mock_get)
        ollama = create_planner("Local Ollama")
        assert isinstance(ollama, OllamaPlanner)
        assert ollama.is_available()[0] is True

        with pytest.raises(ValueError):
            create_planner("nonexistent-backend")

    def test_local_demo_planner_notepad_sequence_targets_window_and_workspace(
        self,
        tmp_path: Path,
    ) -> None:
        planner = LocalDemoFallbackPlanner()
        ws = str(tmp_path / "approved_workspace")

        actions: list[DesktopAction] = []
        for idx in range(16):
            obs = ObservationContext(
                task_text="Milestone 4: Notepad Demo",
                step_index=idx,
                active_window_rect=(200, 100, 1000, 700) if idx >= 1 else None,
                approved_workspace=ws,
            )
            act = planner.plan_next_step(obs, screenshot=Image.new("RGB", (64, 64)))
            actions.append(act)

        assert actions[0].action_type == ActionType.launch_app
        assert actions[0].app_name == "notepad"
        assert actions[2].action_type == ActionType.move
        assert (actions[2].x, actions[2].y) == (600, 400)
        assert actions[3].action_type == ActionType.click
        assert actions[4].action_type == ActionType.type_text
        assert "Milestone 4" in str(actions[4].text)
        assert actions[7].action_type == ActionType.type_text
        assert str(Path(ws) / "milestone4_demo.txt") == actions[7].text
        assert actions[12].action_type == ActionType.launch_app
        assert actions[12].target_path == str(Path(ws) / "milestone4_demo.txt")
        assert actions[14].action_type == ActionType.verify_state
        assert actions[15].action_type == ActionType.done


class _ScriptedPlanner(BasePlanner):
    name = "ScriptedTestPlanner"

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


class TestDesktopExecutionEngine:
    """Verify full Observe-Plan-Act-Verify loop, approval gates, retries, and E-stop."""

    def test_notepad_demo_end_to_end_with_mock_driver_and_store(
        self,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "approved_workspace"
        workspace.mkdir()
        demo_file = workspace / "milestone4_demo.txt"
        demo_file.write_text(
            "FunGuys Autonomous Desktop Controller - Milestone 4 Live Verification",
            encoding="utf-8",
        )

        safety = SafetyController()
        safety.authorize_start()
        driver = MockDryRunDriver(safety=safety)
        store = SQLiteAuditStore(tmp_path / "audit.db")
        policy = PolicyEngine(approved_workspace=workspace)
        planner = LocalDemoFallbackPlanner()

        progress_events: list[tuple[int, int, str]] = []
        screenshots: list[Image.Image] = []
        history_events: list[tuple[str, str, str]] = []

        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            store=store,
            policy=policy,
            planner=planner,
            progress_callback=lambda c, t, d: progress_events.append((c, t, d)),
            screenshot_callback=lambda img: screenshots.append(img),
            history_callback=lambda a, s, d: history_events.append((a, s, d)),
        )

        result = engine.run_task("Milestone 4: Notepad Demo")
        assert result.status == "completed"
        assert result.steps_executed == 16
        assert result.retries_used == 0
        assert len(screenshots) == 16
        assert len(progress_events) == 16
        assert any(a == "done" and s == "completed" for a, s, _ in history_events)

        run_record = store.get_run(result.run_id)
        assert run_record is not None
        assert run_record["status"] == "completed"
        assert len(store.get_actions_for_run(result.run_id)) == 16

    def test_approval_callback_allow_vs_deny(self, tmp_path: Path) -> None:
        workspace = tmp_path / "approved_workspace"
        workspace.mkdir()
        actions = [
            DesktopAction(
                action_type=ActionType.click,
                x=120,
                y=150,
                description="Send Email via Outlook",
            ),
            DesktopAction(action_type=ActionType.done, description="Finished"),
        ]

        # Deny case
        safety_deny = SafetyController()
        safety_deny.authorize_start()
        store = SQLiteAuditStore(tmp_path / "approval.db")
        engine_deny = DesktopExecutionEngine(
            driver=MockDryRunDriver(safety=safety_deny),
            safety=safety_deny,
            store=store,
            policy=PolicyEngine(approved_workspace=workspace),
            planner=_ScriptedPlanner(actions),
            approval_callback=lambda _act, _dec: False,
        )
        res_deny = engine_deny.run_task("Send email")
        assert res_deny.status == "denied"
        assert res_deny.steps_executed == 0

        # Allow case
        safety_allow = SafetyController()
        safety_allow.authorize_start()
        engine_allow = DesktopExecutionEngine(
            driver=MockDryRunDriver(safety=safety_allow),
            safety=safety_allow,
            store=store,
            policy=PolicyEngine(approved_workspace=workspace),
            planner=_ScriptedPlanner(actions),
            approval_callback=lambda _act, _dec: True,
        )
        res_allow = engine_allow.run_task("Send email")
        assert res_allow.status == "completed"
        assert res_allow.steps_executed == 2

    def test_retry_recovery_on_transient_verify_failure(self, tmp_path: Path) -> None:
        workspace = tmp_path / "approved_workspace"
        workspace.mkdir()
        target = workspace / "delayed.txt"

        class _FlakyVerifyDriver(MockDryRunDriver):
            def __init__(self, safety_ctrl: SafetyController) -> None:
                super().__init__(safety=safety_ctrl)
                self.verify_calls = 0

            def verify_state(
                self,
                target_path: Optional[str] = None,
                expected_text: Optional[str] = None,
            ) -> tuple[bool, str]:
                self.verify_calls += 1
                if self.verify_calls < 3:
                    return False, "File not flushed yet"
                target.write_text("ready", encoding="utf-8")
                return super().verify_state(target_path=target_path, expected_text=expected_text)

        safety = SafetyController()
        safety.authorize_start()
        driver = _FlakyVerifyDriver(safety)
        planner = _ScriptedPlanner(
            [
                DesktopAction(
                    action_type=ActionType.verify_state,
                    target_path=str(target),
                    expected_text="ready",
                    description="Verify delayed file",
                ),
                DesktopAction(action_type=ActionType.done, description="Done"),
            ]
        )

        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            policy=PolicyEngine(approved_workspace=workspace),
            planner=planner,
            max_retries=3,
        )
        res = engine.run_task("Verify delayed file")
        assert res.status == "completed"
        assert res.retries_used == 2
        assert driver.verify_calls == 3

        # Exhausting max_retries fails cleanly
        safety2 = SafetyController()
        safety2.authorize_start()
        always_fail_driver = MockDryRunDriver(safety=safety2)
        engine_fail = DesktopExecutionEngine(
            driver=always_fail_driver,
            safety=safety2,
            policy=PolicyEngine(approved_workspace=workspace),
            planner=_ScriptedPlanner(
                [
                    DesktopAction(
                        action_type=ActionType.verify_state,
                        target_path=str(workspace / "missing.txt"),
                        description="Verify missing file",
                    )
                ]
            ),
            max_retries=2,
        )
        res_fail = engine_fail.run_task("Fail verify")
        assert res_fail.status == "failed"
        assert res_fail.retries_used == 2

    def test_immediate_abort_on_emergency_stop_without_retries(self, tmp_path: Path) -> None:
        workspace = tmp_path / "approved_workspace"
        workspace.mkdir()
        safety = SafetyController()
        safety.authorize_start()

        def _stop_mid_move(_pt: tuple[int, int], idx: int, _total: int) -> None:
            if idx == 3:
                safety.simulate_hotkey_f12()

        driver = MockDryRunDriver(safety=safety, step_callback=_stop_mid_move)
        planner = _ScriptedPlanner(
            [
                DesktopAction(
                    action_type=ActionType.move,
                    x=1000,
                    y=600,
                    description="Move cursor across screen",
                ),
                DesktopAction(action_type=ActionType.done, description="Done"),
            ]
        )

        engine = DesktopExecutionEngine(
            driver=driver,
            safety=safety,
            policy=PolicyEngine(approved_workspace=workspace),
            planner=planner,
            max_retries=3,
        )
        res = engine.run_task("Move cursor")
        assert res.status == "stopped"
        assert res.retries_used == 0
        assert safety.emergency_stopped is True
