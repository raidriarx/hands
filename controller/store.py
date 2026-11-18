"""SQLite audit history store with automatic API key and secret redaction."""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

_AIZA_PATTERN = re.compile(r"(?:AIza|AQ\.)[0-9A-Za-z_\-]{10,}")
_SK_PATTERN = re.compile(r"sk-[0-9A-Za-z_\-]{10,}")
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[0-9A-Za-z_\-\.=]{8,}")
_KV_SECRET_PATTERN = re.compile(
    r"(?i)\b(GEMINI_API_KEY|OPENAI_API_KEY|API_KEY|TOKEN|PASSWORD|SECRET)\s*([:=])\s*([^\s,;]+)"
)


def redact_secrets(text: Optional[str]) -> str:
    """Scrub API keys, bearer tokens, and secret assignments from text."""
    if text is None:
        return ""
    scrubbed = str(text)
    scrubbed = _AIZA_PATTERN.sub("[REDACTED]", scrubbed)
    scrubbed = _SK_PATTERN.sub("[REDACTED]", scrubbed)
    scrubbed = _BEARER_PATTERN.sub("Bearer [REDACTED]", scrubbed)
    scrubbed = _KV_SECRET_PATTERN.sub(r"\1\2[REDACTED]", scrubbed)
    return scrubbed


class SQLiteAuditStore:
    """Thread-safe SQLite store persisting task runs and redacted action logs."""

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        self.db_path: Path = (
            Path(db_path)
            if db_path is not None
            else Path(__file__).resolve().parent.parent / "controller_history.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY,
                    task_text TEXT,
                    backend TEXT,
                    speed_preset TEXT,
                    status TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    step_index INTEGER,
                    action_type TEXT,
                    description TEXT,
                    status TEXT,
                    policy_decision TEXT,
                    approval_status TEXT,
                    verification TEXT,
                    error TEXT,
                    created_at TEXT
                )
                """
            )
            conn.commit()

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def start_run(self, task_text: str, backend: str, speed_preset: str) -> str:
        """Create a new task_runs record and return its generated run_id."""
        run_id = f"run-{uuid4().hex[:12]}"
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_runs (id, task_text, backend, speed_preset, status, started_at, finished_at, error)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    run_id,
                    redact_secrets(task_text),
                    redact_secrets(backend),
                    redact_secrets(speed_preset),
                    "running",
                    self._now_iso(),
                ),
            )
            conn.commit()
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Mark an existing task run as finished with final status and optional error."""
        clean_error = redact_secrets(error) if error is not None else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE task_runs
                SET status = ?, finished_at = ?, error = ?
                WHERE id = ?
                """,
                (
                    redact_secrets(status),
                    self._now_iso(),
                    clean_error,
                    run_id,
                ),
            )
            conn.commit()

    def log_action(
        self,
        run_id: str,
        step_index: int,
        action_type: str,
        description: str,
        status: str,
        policy_decision: str = "allowed",
        approval_status: str = "not_required",
        verification: str = "",
        error: str = "",
    ) -> int:
        """Persist a single redacted action log row and return its row id."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO action_logs (
                    run_id, step_index, action_type, description, status,
                    policy_decision, approval_status, verification, error, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    int(step_index),
                    redact_secrets(action_type),
                    redact_secrets(description),
                    redact_secrets(status),
                    redact_secrets(policy_decision),
                    redact_secrets(approval_status),
                    redact_secrets(verification),
                    redact_secrets(error),
                    self._now_iso(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """Return a task run dictionary by run_id, or None if not found."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent task runs ordered newest first."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_runs ORDER BY started_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_actions_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Return all action logs for a specific run_id ordered chronologically."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM action_logs WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_recent_actions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent action logs ordered newest first."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM action_logs ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
