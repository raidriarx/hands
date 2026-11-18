"""Entry point for Window B — Autonomous Windows Desktop Controller."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtWidgets import QApplication

from controller.driver import create_driver
from controller.emergency import SafetyController
from controller.gui import MILESTONE_4_PRESET, DesktopControllerWindow
from controller.schema import SpeedConfig


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for Window B."""
    parser = argparse.ArgumentParser(
        description="Window B — Autonomous Windows Desktop Controller"
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
    return parser


def _create_optional_store() -> Optional[Any]:
    """Lazily instantiate SQLiteAuditStore when M3 controller.store is available."""
    try:
        from controller.store import SQLiteAuditStore
    except ImportError:
        return None
    db_path = Path(__file__).resolve().parent / "approved_workspace" / "audit.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SQLiteAuditStore(db_path)


def main(argv: Optional[list[str]] = None) -> int:
    """Launch Window B Desktop Controller or execute a headless smoke check."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.headless_check:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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

    window = DesktopControllerWindow(
        safety=safety,
        driver=driver,
        store=store,
        initial_speed=args.speed,
        initial_task=task_text,
        auto_start_demo=bool(args.live_demo_notepad and not args.headless_check),
        initial_backend=args.backend,
    )

    if args.headless_check:
        preview = driver.capture_screenshot()
        window.update_screenshot(preview)
        window.set_progress(1, 1, "Headless smoke check complete")
        app.processEvents()
        window.close()
        return 0

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
