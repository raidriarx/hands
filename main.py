"""Entry point for Window B - Autonomous Windows Desktop Controller."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtWidgets import QApplication

from controller.driver import create_driver
from controller.emergency import SafetyController
from controller.gui import (
    APPROVAL_MODE_ASK,
    APPROVAL_MODE_SUPERVISED,
    MILESTONE_4_PRESET,
    DesktopControllerWindow,
)
from controller.planners import _load_dotenv_if_present
from controller.schema import SpeedConfig


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for Window B."""
    parser = argparse.ArgumentParser(
        description="Window B - Autonomous Windows Desktop Controller"
    )
    parser.add_argument(
        "--live-demo-notepad",
        action="store_true",
        help="Run the Milestone 4 Notepad demo after rendering Window B.",
    )
    parser.add_argument(
        "--speed",
        choices=["Slow", "Normal", "Fast", "Custom"],
        default="Normal",
        help="Initial cursor speed preset.",
    )
    parser.add_argument(
        "--backend",
        default="Local Demo / Fallback",
        help="Initial planner backend name.",
    )
    parser.add_argument(
        "--task",
        default="",
        help="Initial desktop task prompt.",
    )
    parser.add_argument(
        "--headless-check",
        action="store_true",
        help="Instantiate Window B, capture a preview screenshot, and exit cleanly.",
    )
    parser.add_argument(
        "--fresh-demo",
        action="store_true",
        help="Remove stale approved_workspace/milestone4_demo.txt before starting.",
    )
    parser.add_argument(
        "--auto-start",
        action="store_true",
        help="Automatically authorize and start the initial task 1 second after launching Window B.",
    )
    parser.add_argument(
        "--approval-mode",
        choices=["ask", "supervised"],
        default="ask",
        help="Approval gate mode: 'ask' prompts on risky actions, 'supervised' auto-approves while keeping hard safety blocks.",
    )
    parser.add_argument(
        "--supervised",
        action="store_true",
        help="Shortcut for --approval-mode supervised (auto-approve while user watches).",
    )
    return parser


def _create_optional_store() -> Optional[Any]:
    """Lazily instantiate SQLiteAuditStore when M3 controller.store is available."""
    try:
        from controller.store import SQLiteAuditStore
    except ImportError:
        return None
    db_path = Path(__file__).resolve().parent / "controller_history.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteAuditStore(db_path)


def main(argv: Optional[list[str]] = None) -> int:
    """Launch Window B Desktop Controller or execute a headless smoke check."""
    _load_dotenv_if_present()
    (Path(__file__).resolve().parent / "approved_workspace").mkdir(parents=True, exist_ok=True)
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    args.task = " ".join(args.task.split())
    args.backend = " ".join(args.backend.split())

    if args.headless_check:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")
    elif (
        not args.live_demo_notepad
        and os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1"
        and args.backend == "Local Demo / Fallback"
    ):
        if os.environ.get("GEMINI_API_KEY", "").strip():
            args.backend = "Gemini API"
        elif os.environ.get("OPENAI_API_KEY", "").strip():
            args.backend = "OpenAI API"

    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv if argv is None else ["main.py", *argv])

    safety = SafetyController()
    speed_cfg = SpeedConfig.from_preset(args.speed)
    driver = create_driver(safety=safety, speed_config=speed_cfg)
    store = _create_optional_store()

    task_text = args.task.strip()
    if args.live_demo_notepad and not task_text:
        task_text = MILESTONE_4_PRESET

    if args.fresh_demo or (args.live_demo_notepad and not args.headless_check):
        stale_demo = Path(__file__).resolve().parent / "approved_workspace" / "milestone4_demo.txt"
        if stale_demo.exists():
            stale_demo.unlink()

    approval_mode = (
        APPROVAL_MODE_SUPERVISED
        if (args.supervised or args.approval_mode == "supervised")
        else APPROVAL_MODE_ASK
    )

    window = DesktopControllerWindow(
        safety=safety,
        driver=driver,
        store=store,
        initial_speed=args.speed,
        initial_task=task_text,
        auto_start_demo=bool((args.live_demo_notepad or args.auto_start) and not args.headless_check),
        initial_backend=args.backend,
        initial_approval_mode=approval_mode,
    )

    if args.headless_check:
        preview = driver.capture_screenshot()
        window.update_screenshot(preview)
        window.set_progress(1, 1, "Headless smoke check complete")
        app.processEvents()
        window.close()
        return 0

    print(
        f"[DesktopController] Window B opened | backend={args.backend} | mode={approval_mode} | corner={window._current_corner}",
        flush=True,
    )
    if window.auto_start_demo:
        print(
            f"[DesktopController] Auto-starting task in 1s: {task_text}",
            flush=True,
        )
    window.show()
    try:
        preview = driver.capture_screenshot()
        window.update_screenshot(preview)
    except Exception:
        pass

    if owns_app:
        return int(app.exec())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
