"""Root pytest configuration for the Autonomous Windows Desktop Controller."""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DESKTOP_CONTROLLER_TEST_MODE", "1")


def _configure_paths() -> None:
    """Ensure workspace root and .venv site-packages are on sys.path."""
    workspace_root = Path(__file__).resolve().parent
    root_str = str(workspace_root)
    if root_str in sys.path:
        sys.path.remove(root_str)
    sys.path.insert(0, root_str)

    venv_dir = workspace_root / ".venv"
    candidate_site_packages = [
        venv_dir / "Lib" / "site-packages",
        *sorted((venv_dir / "lib").glob("python*/site-packages")),
    ]
    for candidate in candidate_site_packages:
        if candidate.is_dir():
            site.addsitedir(str(candidate))


_configure_paths()


def pytest_configure(config: object) -> None:
    """Enforce offscreen Qt and deterministic test mode during pytest runs."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["DESKTOP_CONTROLLER_TEST_MODE"] = "1"
    _configure_paths()
