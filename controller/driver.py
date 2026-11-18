"""Smooth interpolated desktop drivers (mock dry-run and live Windows)."""

from __future__ import annotations

import ctypes
import os
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

from PIL import Image, ImageDraw, ImageGrab

from controller.emergency import EmergencyStopError, SafetyController, StopSource
from controller.schema import SpeedConfig, SpeedPreset

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
VK_F12 = 0x7B


def compute_smooth_path(
    start_xy: tuple[int, int],
    end_xy: tuple[int, int],
    steps: int = 20,
) -> list[tuple[int, int]]:
    """Compute a cubic ease-in-out interpolated cursor path between two points."""
    sx, sy = int(start_xy[0]), int(start_xy[1])
    ex, ey = int(end_xy[0]), int(end_xy[1])
    if steps <= 1:
        return [(ex, ey)]

    dx = ex - sx
    dy = ey - sy
    path: list[tuple[int, int]] = []
    for i in range(1, steps + 1):
        t = i / steps
        eased = 3.0 * t * t - 2.0 * t * t * t
        px = int(round(sx + dx * eased))
        py = int(round(sy + dy * eased))
        path.append((px, py))

    path[-1] = (ex, ey)
    return path


def find_window_rect(title_substring: str) -> Optional[tuple[int, int, int, int]]:
    """Find the bounding rectangle (left, top, right, bottom) of a visible window."""
    if os.name != "nt" or not title_substring.strip():
        return None

    user32 = ctypes.windll.user32
    needle = title_substring.lower()
    matched_rect: list[tuple[int, int, int, int]] = []

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle in buf.value.lower():
            rect = RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                matched_rect.append((rect.left, rect.top, rect.right, rect.bottom))
                return False
        return True

    user32.EnumWindows(enum_proc_type(_callback), 0)
    return matched_rect[0] if matched_rect else None


def _focus_window_by_title(title_substring: str) -> Optional[tuple[int, int, int, int]]:
    """Attempt to focus a visible Win32 window matching title_substring and return its rect."""
    if os.name != "nt" or not title_substring.strip():
        return None

    user32 = ctypes.windll.user32
    needle = title_substring.lower()
    matched: list[tuple[int, tuple[int, int, int, int]]] = []

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle in buf.value.lower():
            rect = RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                matched.append((hwnd, (rect.left, rect.top, rect.right, rect.bottom)))
                return False
        return True

    user32.EnumWindows(enum_proc_type(_callback), 0)
    if not matched:
        return None

    hwnd, bounds = matched[0]
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return bounds


def _list_visible_window_titles() -> list[str]:
    """Return titles of all visible top-level windows on Windows."""
    if os.name != "nt":
        return []

    user32 = ctypes.windll.user32
    titles: list[str] = []
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value:
                titles.append(buf.value)
        return True

    user32.EnumWindows(enum_proc_type(_callback), 0)
    return titles


class BaseDesktopDriver(ABC):
    """Abstract interface for deterministic desktop observation and input."""

    def __init__(
        self,
        safety: SafetyController,
        speed_config: Optional[SpeedConfig] = None,
    ) -> None:
        self.safety = safety
        self.speed_config = speed_config or SpeedConfig.from_preset(SpeedPreset.Normal)

    def _resolve_speed(self, speed_config: Optional[SpeedConfig] = None) -> SpeedConfig:
        return speed_config or self.speed_config

    @abstractmethod
    def get_screen_size(self) -> tuple[int, int]:
        """Return (width, height) of the primary desktop screen."""

    @abstractmethod
    def get_cursor_position(self) -> tuple[int, int]:
        """Return current (x, y) cursor coordinates."""

    @abstractmethod
    def capture_screenshot(self) -> Image.Image:
        """Capture and return a PIL Image of the current screen state."""

    @abstractmethod
    def move_to(
        self,
        x: int,
        y: int,
        speed_config: Optional[SpeedConfig] = None,
    ) -> list[tuple[int, int]]:
        """Move the cursor along a smooth path to (x, y) and return the path."""

    @abstractmethod
    def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        double: bool = False,
        speed_config: Optional[SpeedConfig] = None,
    ) -> None:
        """Smoothly move to (x, y) and perform a single or double mouse click."""

    @abstractmethod
    def type_text(self, text: str, interval: float = 0.02) -> None:
        """Type text at the active focus."""

    @abstractmethod
    def press_keys(self, keys: list[str]) -> None:
        """Press a key or chord combination."""

    @abstractmethod
    def scroll(self, clicks: int) -> None:
        """Scroll vertically by clicks (positive up, negative down)."""

    @abstractmethod
    def wait(self, seconds: float) -> None:
        """Wait for the requested duration while respecting safety gates."""

    @abstractmethod
    def launch_app(
        self,
        app_name: str,
        args: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Launch an application without elevation and return launch metadata."""

    @abstractmethod
    def verify_state(
        self,
        target_path: Optional[str] = None,
        expected_text: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Verify file existence/content or active window text."""


class MockDryRunDriver(BaseDesktopDriver):
    """In-memory dry-run driver for deterministic tests and headless simulation."""

    def __init__(
        self,
        safety: SafetyController,
        speed_config: Optional[SpeedConfig] = None,
        screen_size: tuple[int, int] = (1920, 1080),
        initial_cursor: tuple[int, int] = (960, 540),
        step_callback: Optional[Callable[[tuple[int, int], int, int], None]] = None,
    ) -> None:
        super().__init__(safety=safety, speed_config=speed_config)
        self.screen_size: tuple[int, int] = screen_size
        self.cursor_pos: tuple[int, int] = initial_cursor
        self.move_history: list[tuple[int, int]] = []
        self.action_history: list[dict[str, Any]] = []
        self.typed_texts: list[str] = []
        self.pressed_keys: list[list[str]] = []
        self.launched_apps: list[str] = []
        self.virtual_files: dict[str, str] = {}
        self.active_window_text: str = "Desktop Ready"
        self.step_callback = step_callback

    def get_screen_size(self) -> tuple[int, int]:
        return self.screen_size

    def get_cursor_position(self) -> tuple[int, int]:
        return self.cursor_pos

    def capture_screenshot(self) -> Image.Image:
        canvas_w, canvas_h = 640, 360
        img = Image.new("RGB", (canvas_w, canvas_h), (24, 28, 38))
        draw = ImageDraw.Draw(img)

        # Draw simulated window frame
        draw.rectangle([28, 28, canvas_w - 28, canvas_h - 28], outline=(72, 130, 195), width=2)
        draw.rectangle([28, 28, canvas_w - 28, 60], fill=(38, 52, 74))
        app_label = self.launched_apps[-1] if self.launched_apps else "Desktop"
        draw.text((40, 38), f"MockApp: {app_label}", fill=(230, 238, 250))

        body_preview = (self.active_window_text or "")[:80]
        draw.text((44, 84), f"State: {body_preview}", fill=(190, 210, 230))
        draw.text(
            (44, 112),
            f"Cursor: {self.cursor_pos}  Actions: {len(self.action_history)}",
            fill=(150, 175, 200),
        )

        # Draw visible cursor crosshair at scaled cursor_pos
        sw, sh = self.screen_size
        cx = max(0, min(canvas_w - 1, int(round(self.cursor_pos[0] * (canvas_w - 1) / max(sw - 1, 1)))))
        cy = max(0, min(canvas_h - 1, int(round(self.cursor_pos[1] * (canvas_h - 1) / max(sh - 1, 1)))))
        draw.line([(cx - 10, cy), (cx + 10, cy)], fill=(255, 92, 92), width=2)
        draw.line([(cx, cy - 10), (cx, cy + 10)], fill=(255, 92, 92), width=2)
        draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], outline=(255, 225, 105), width=2)
        return img

    def move_to(
        self,
        x: int,
        y: int,
        speed_config: Optional[SpeedConfig] = None,
    ) -> list[tuple[int, int]]:
        cfg = self._resolve_speed(speed_config)
        sw, sh = self.screen_size
        self.safety.assert_can_execute(self.cursor_pos[0], self.cursor_pos[1], sw, sh)

        path = compute_smooth_path(self.cursor_pos, (x, y), steps=cfg.interpolation_steps)
        total = len(path)
        for idx, (px, py) in enumerate(path):
            if self.step_callback is not None:
                self.step_callback((px, py), idx, total)
            self.safety.assert_can_execute(px, py, sw, sh)
            self.cursor_pos = (px, py)
            self.move_history.append((px, py))

        self.action_history.append({"action": "move", "x": x, "y": y, "steps": len(path)})
        return path

    def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        double: bool = False,
        speed_config: Optional[SpeedConfig] = None,
    ) -> None:
        self.move_to(x, y, speed_config=speed_config)
        sw, sh = self.screen_size
        self.safety.assert_can_execute(self.cursor_pos[0], self.cursor_pos[1], sw, sh)
        self.action_history.append(
            {
                "action": "double_click" if double else "click",
                "x": x,
                "y": y,
                "button": button,
                "double": double,
            }
        )

    def type_text(self, text: str, interval: float = 0.02) -> None:
        sw, sh = self.screen_size
        self.safety.assert_can_execute(self.cursor_pos[0], self.cursor_pos[1], sw, sh)
        self.typed_texts.append(text)
        if self.active_window_text == "Desktop Ready":
            self.active_window_text = text
        else:
            self.active_window_text += text
        self.action_history.append({"action": "type_text", "text": text, "interval": interval})

    def press_keys(self, keys: list[str]) -> None:
        sw, sh = self.screen_size
        self.safety.assert_can_execute(self.cursor_pos[0], self.cursor_pos[1], sw, sh)
        normalized = [str(k) for k in keys]
        self.pressed_keys.append(normalized)
        self.action_history.append({"action": "press_keys", "keys": normalized})

    def scroll(self, clicks: int) -> None:
        sw, sh = self.screen_size
        self.safety.assert_can_execute(self.cursor_pos[0], self.cursor_pos[1], sw, sh)
        self.action_history.append({"action": "scroll", "clicks": clicks})

    def wait(self, seconds: float) -> None:
        sw, sh = self.screen_size
        self.safety.assert_can_execute(self.cursor_pos[0], self.cursor_pos[1], sw, sh)
        self.action_history.append({"action": "wait", "seconds": seconds})

    def launch_app(
        self,
        app_name: str,
        args: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        sw, sh = self.screen_size
        self.safety.assert_can_execute(self.cursor_pos[0], self.cursor_pos[1], sw, sh)
        self.launched_apps.append(app_name)
        self.active_window_text = f"{app_name} - Untitled"
        info = {
            "app_name": app_name,
            "args": list(args or []),
            "pid": 1000 + len(self.launched_apps),
            "rect": (160, 120, 960, 720),
            "mock": True,
        }
        self.action_history.append({"action": "launch_app", **info})
        return info

    def verify_state(
        self,
        target_path: Optional[str] = None,
        expected_text: Optional[str] = None,
    ) -> tuple[bool, str]:
        if target_path:
            if target_path in self.virtual_files:
                content = self.virtual_files[target_path]
                if expected_text and expected_text not in content:
                    return False, f"Expected text {expected_text!r} not found in virtual file {target_path}"
                return True, f"Verified virtual file {target_path}"

            path = Path(target_path)
            if not path.exists():
                return False, f"Target path does not exist: {target_path}"
            if expected_text:
                content = path.read_text(encoding="utf-8", errors="replace")
                if expected_text not in content:
                    return False, f"Expected text {expected_text!r} not found in {target_path}"
            return True, f"Verified filesystem path {target_path}"

        if expected_text:
            if expected_text in self.active_window_text:
                return True, f"Verified expected text {expected_text!r} in active window"
            return False, f"Expected text {expected_text!r} not found in active window"

        return False, "verify_state requires target_path or expected_text"


class LiveWindowsDesktopDriver(BaseDesktopDriver):
    """Live Windows desktop driver using pyautogui, PIL/mss, and Win32 safety polling."""

    def __init__(
        self,
        safety: SafetyController,
        speed_config: Optional[SpeedConfig] = None,
    ) -> None:
        super().__init__(safety=safety, speed_config=speed_config)
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.0
        self._pyautogui = pyautogui

    def _poll_global_hotkeys(self) -> None:
        if os.name != "nt":
            return
        user32 = ctypes.windll.user32
        f12_down = bool(user32.GetAsyncKeyState(VK_F12) & 0x8000)
        ctrl_down = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
        shift_down = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
        esc_down = bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
        if f12_down or (ctrl_down and shift_down and esc_down):
            self.safety.trigger_emergency_stop(StopSource.global_hotkey.value)

    def _poll_hotkeys_and_safety(
        self,
        x: Optional[int] = None,
        y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> None:
        self._poll_global_hotkeys()
        if width is None or height is None:
            width, height = self.get_screen_size()
        if x is None or y is None:
            x, y = self.get_cursor_position()
        self.safety.assert_can_execute(x, y, width, height)

    def get_screen_size(self) -> tuple[int, int]:
        size = self._pyautogui.size()
        return int(size.width), int(size.height)

    def get_cursor_position(self) -> tuple[int, int]:
        pos = self._pyautogui.position()
        return int(pos.x), int(pos.y)

    def capture_screenshot(self) -> Image.Image:
        try:
            return ImageGrab.grab(all_screens=False)
        except Exception:
            import mss

            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                raw = sct.grab(monitor)
                return Image.frombytes("RGB", raw.size, raw.rgb)

    def move_to(
        self,
        x: int,
        y: int,
        speed_config: Optional[SpeedConfig] = None,
    ) -> list[tuple[int, int]]:
        cfg = self._resolve_speed(speed_config)
        sw, sh = self.get_screen_size()
        start = self.get_cursor_position()
        self._poll_hotkeys_and_safety(start[0], start[1], sw, sh)

        path = compute_smooth_path(start, (x, y), steps=cfg.interpolation_steps)
        step_delay = cfg.move_duration_sec / max(len(path), 1)
        try:
            for px, py in path:
                self._poll_hotkeys_and_safety(px, py, sw, sh)
                self._pyautogui.moveTo(px, py, duration=0, _pause=False)
                if step_delay > 0:
                    time.sleep(step_delay)
        except self._pyautogui.FailSafeException as exc:
            self.safety.trigger_emergency_stop(StopSource.corner_failsafe.value)
            raise EmergencyStopError("PyAutoGUI corner failsafe triggered during move_to") from exc
        return path

    def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        double: bool = False,
        speed_config: Optional[SpeedConfig] = None,
    ) -> None:
        cfg = self._resolve_speed(speed_config)
        self.move_to(x, y, speed_config=cfg)
        sw, sh = self.get_screen_size()
        self._poll_hotkeys_and_safety(x, y, sw, sh)
        try:
            if double:
                self._pyautogui.doubleClick(x=x, y=y, button=button, _pause=False)
            else:
                self._pyautogui.click(x=x, y=y, button=button, _pause=False)
        except self._pyautogui.FailSafeException as exc:
            self.safety.trigger_emergency_stop(StopSource.corner_failsafe.value)
            raise EmergencyStopError("PyAutoGUI corner failsafe triggered during click") from exc
        if cfg.post_action_delay_sec > 0:
            time.sleep(cfg.post_action_delay_sec)

    def type_text(self, text: str, interval: float = 0.02) -> None:
        self._poll_hotkeys_and_safety()
        try:
            self._pyautogui.write(text, interval=interval, _pause=False)
        except self._pyautogui.FailSafeException as exc:
            self.safety.trigger_emergency_stop(StopSource.corner_failsafe.value)
            raise EmergencyStopError("PyAutoGUI corner failsafe triggered during type_text") from exc
        if self.speed_config.post_action_delay_sec > 0:
            time.sleep(self.speed_config.post_action_delay_sec)

    def press_keys(self, keys: list[str]) -> None:
        self._poll_hotkeys_and_safety()
        normalized = [k.strip().lower() for k in keys if k.strip()]
        try:
            if len(normalized) == 1:
                self._pyautogui.press(normalized[0], _pause=False)
            elif len(normalized) > 1:
                self._pyautogui.hotkey(*normalized, _pause=False)
        except self._pyautogui.FailSafeException as exc:
            self.safety.trigger_emergency_stop(StopSource.corner_failsafe.value)
            raise EmergencyStopError("PyAutoGUI corner failsafe triggered during press_keys") from exc
        if self.speed_config.post_action_delay_sec > 0:
            time.sleep(self.speed_config.post_action_delay_sec)

    def scroll(self, clicks: int) -> None:
        self._poll_hotkeys_and_safety()
        try:
            self._pyautogui.scroll(clicks, _pause=False)
        except self._pyautogui.FailSafeException as exc:
            self.safety.trigger_emergency_stop(StopSource.corner_failsafe.value)
            raise EmergencyStopError("PyAutoGUI corner failsafe triggered during scroll") from exc

    def wait(self, seconds: float) -> None:
        deadline = time.monotonic() + max(seconds, 0.0)
        while True:
            self._poll_hotkeys_and_safety()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))

    def launch_app(
        self,
        app_name: str,
        args: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        self._poll_hotkeys_and_safety()
        cmd = [app_name, *(args or [])]
        proc = subprocess.Popen(cmd, shell=False)
        time.sleep(0.5)
        stem = Path(app_name).stem
        rect = _focus_window_by_title(stem) or find_window_rect(stem)
        return {
            "app_name": app_name,
            "args": list(args or []),
            "pid": proc.pid,
            "rect": rect,
            "mock": False,
        }

    def verify_state(
        self,
        target_path: Optional[str] = None,
        expected_text: Optional[str] = None,
    ) -> tuple[bool, str]:
        if target_path:
            path = Path(target_path)
            if not path.exists():
                return False, f"Target path does not exist: {target_path}"
            if expected_text:
                content = path.read_text(encoding="utf-8", errors="replace")
                if expected_text not in content:
                    return False, f"Expected text {expected_text!r} not found in {target_path}"
            return True, f"Verified file on disk: {target_path}"

        if expected_text:
            titles = _list_visible_window_titles()
            if any(expected_text.lower() in title.lower() for title in titles):
                return True, f"Verified window title containing {expected_text!r}"
            return False, f"No visible window title matched {expected_text!r}"

        return False, "verify_state requires target_path or expected_text"


def create_driver(
    safety: SafetyController,
    speed_config: Optional[SpeedConfig] = None,
    force_mock: Optional[bool] = None,
) -> BaseDesktopDriver:
    """Create either MockDryRunDriver or LiveWindowsDesktopDriver based on mode."""
    use_mock = (
        force_mock
        if force_mock is not None
        else os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1"
    )
    if use_mock:
        return MockDryRunDriver(safety=safety, speed_config=speed_config)
    return LiveWindowsDesktopDriver(safety=safety, speed_config=speed_config)
