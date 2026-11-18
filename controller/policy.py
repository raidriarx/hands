"""Deterministic policy and approval gate engine for desktop automation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from controller.schema import ActionType, DesktopAction

APPROVED_WORKSPACE = Path(
    r"C:\Users\User\Documents\funguys-lab\desktop-controller\approved_workspace"
)

_ELEVATION_PATTERNS = re.compile(
    r"(?i)(?:\brunas\b|-verb\s+runas|\belevat(?:e|ed|ion)?\b|\badministrator\b|\bsudo\b)"
)

_SYSTEM_PATH_PATTERNS = re.compile(
    r"(?i)(?:[a-z]:\\windows\b|\\windows\\system32\b|\bsystem32\b|"
    r"[a-z]:\\program files(?:\s*\(x86\))?\b|\bprogram files\b|"
    r"\\appdata\b|\bappdata\\|"
    r"[a-z]:\\users\\[^\\\s]+\\desktop\b)"
)

_WINDOWS_DRIVE_PATH_PATTERN = re.compile(r"(?i)([a-z]:\\[^\s\"'<>|]+)")
_UNC_PATH_PATTERN = re.compile(r"\\\\[^\\\s]+\\[^\\\s]+")
_WRITE_DELETE_HINTS = re.compile(
    r"(?i)\b(?:write|save|create|mkdir|delete|remove|unlink|erase|overwrite|copy|move|modify|append|edit)\b"
)

_APPROVAL_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "send_message",
        (
            "send email",
            "send message",
            "outlook",
            "gmail",
            "slack",
            "discord",
            "whatsapp",
            "teams",
            "smtp",
        ),
    ),
    (
        "social_post",
        (
            "publish",
            "tweet",
            "post to",
            "linkedin post",
            "facebook",
            "instagram",
            "reddit post",
            "x.com",
        ),
    ),
    (
        "submit_form",
        (
            "submit application",
            "submit form",
            "apply now",
            "checkout",
            "place order",
            "confirm payment",
        ),
    ),
    (
        "delete_or_overwrite",
        (
            "delete",
            "remove",
            "unlink",
            "rm ",
            "del ",
            "erase",
            "overwrite",
        ),
    ),
    (
        "install_or_execute",
        (
            "install",
            "setup.exe",
            ".msi",
            "winget install",
            "choco install",
            "pip install",
            "powershell -",
            "cmd /c",
            "curl |",
        ),
    ),
    (
        "system_settings",
        (
            "regedit",
            "registry",
            "ms-settings",
            "control panel",
            "netsh",
            "bcdedit",
            "firewall",
            "system settings",
            "powershell set-",
        ),
    ),
    (
        "sensitive_access",
        (
            ".env",
            "id_rsa",
            "id_ed25519",
            "credentials",
            "secret",
            "password",
            "cookie",
            "token",
            ".ssh",
            ".aws",
            "admin_token",
        ),
    ),
)

_DOWNLOADS_EXEC_PATTERN = re.compile(
    r"(?i)downloads\\[^\s\"']*\.(?:exe|msi|bat|cmd|ps1|vbs|scr)\b"
)


class PolicyDecision(BaseModel):
    """Structured result of evaluating a DesktopAction against PolicyEngine."""

    allowed: bool
    requires_approval: bool = False
    risk_category: str = "safe"
    reason: str


class PolicyEngine:
    """Enforces workspace sandbox confinement, no admin elevation, and 7 approval gates."""

    def __init__(self, approved_workspace: Optional[Path | str] = None) -> None:
        self.approved_workspace: Path = (
            Path(approved_workspace)
            if approved_workspace is not None
            else APPROVED_WORKSPACE
        )

    def is_within_sandbox(self, target_path: Path | str) -> bool:
        """Return True only if target_path safely resolves inside approved_workspace."""
        raw = str(target_path).strip()
        if not raw:
            return False
        if raw.startswith("\\\\") or raw.startswith("//"):
            return False

        candidate = Path(raw)
        if ".." in candidate.parts:
            return False

        workspace_resolved = self.approved_workspace.resolve(strict=False)
        try:
            resolved = candidate.resolve(strict=False)
        except OSError:
            return False

        if resolved.drive.lower() != workspace_resolved.drive.lower():
            return False

        return resolved == workspace_resolved or resolved.is_relative_to(workspace_resolved)

    def _has_outside_path_in_text(self, text: str, description: str) -> Optional[str]:
        combined = f"{text}\n{description}"

        if _UNC_PATH_PATTERN.search(combined):
            return "Action references a UNC network path outside approved_workspace"

        sanitized_parts: list[str] = []
        last_idx = 0
        for match in _WINDOWS_DRIVE_PATH_PATTERN.finditer(combined):
            path_str = match.group(1).rstrip(".,;:)]}")
            if not self.is_within_sandbox(path_str):
                return f"Action text references path outside approved_workspace: {path_str}"
            # Strip verified sandbox path before checking generic system path patterns
            sanitized_parts.append(combined[last_idx:match.start()])
            last_idx = match.end()
        sanitized_parts.append(combined[last_idx:])
        sanitized = "".join(sanitized_parts)

        if _SYSTEM_PATH_PATTERNS.search(sanitized):
            return "Action references a restricted Windows/system/Desktop path outside approved_workspace"

        if (".ssh" in sanitized.lower() or ".aws" in sanitized.lower()) and _WRITE_DELETE_HINTS.search(sanitized):
            return "Action attempts write/delete against sensitive .ssh/.aws path outside approved_workspace"

        return None

    def evaluate(self, action: DesktopAction) -> PolicyDecision:
        """Deterministically evaluate whether an action is blocked, requires approval, or safe."""
        # 1. Unconditionally block admin / UAC elevation attempts
        elevation_surface = " ".join(
            part
            for part in (action.app_name, action.text, action.description)
            if part
        )
        if _ELEVATION_PATTERNS.search(elevation_surface):
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                risk_category="privilege_escalation",
                reason="Admin / UAC privilege escalation is strictly prohibited",
            )

        # 2. Unconditionally block any target_path outside approved_workspace
        if action.target_path is not None and action.target_path.strip():
            if not self.is_within_sandbox(action.target_path):
                return PolicyDecision(
                    allowed=False,
                    requires_approval=False,
                    risk_category="sandbox_violation",
                    reason=f"Target path {action.target_path!r} is outside approved_workspace",
                )

        # 3. Unconditionally block restricted system/outside paths in text or description
        outside_reason = self._has_outside_path_in_text(
            action.text or "",
            action.description or "",
        )
        if outside_reason is not None:
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                risk_category="sandbox_violation",
                reason=outside_reason,
            )

        # 4. Explicit request_approval always requires operator confirmation
        if action.action_type == ActionType.request_approval:
            return PolicyDecision(
                allowed=True,
                requires_approval=True,
                risk_category=action.risk_category or "explicit_approval",
                reason=action.reason or action.description,
            )

        # 5. Check all 7 approval-required risk categories
        haystack = " ".join(
            str(part)
            for part in (
                action.action_type.value,
                action.risk_category,
                action.target_path,
                action.app_name,
                action.text,
                action.reason,
                action.description,
            )
            if part
        ).lower()

        explicit_cat = (action.risk_category or "").strip().lower()
        for category, keywords in _APPROVAL_CATEGORY_KEYWORDS:
            if explicit_cat == category:
                return PolicyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_category=category,
                    reason=action.reason or f"Requires user approval for {category}: {action.description}",
                )
            if any(kw in haystack for kw in keywords):
                return PolicyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_category=category,
                    reason=action.reason or f"Requires user approval for {category}: {action.description}",
                )

        if _DOWNLOADS_EXEC_PATTERN.search(haystack):
            return PolicyDecision(
                allowed=True,
                requires_approval=True,
                risk_category="install_or_execute",
                reason=action.reason or f"Requires user approval to execute downloaded software: {action.description}",
            )

        # 6. Overwriting existing non-empty file inside approved_workspace requires approval
        if (
            action.target_path
            and action.action_type not in (ActionType.verify_state, ActionType.launch_app)
        ):
            target = Path(action.target_path).resolve(strict=False)
            if target.exists() and target.is_file() and target.stat().st_size > 0:
                return PolicyDecision(
                    allowed=True,
                    requires_approval=True,
                    risk_category="delete_or_overwrite",
                    reason=f"Target file {target.name} already exists and is non-empty; approval required before overwrite",
                )

        # 7. Safe action inside approved_workspace
        return PolicyDecision(
            allowed=True,
            requires_approval=False,
            risk_category="safe",
            reason="Permitted under sandbox policy",
        )
