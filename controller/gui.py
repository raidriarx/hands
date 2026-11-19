"""Window B PySide6 GUI for the Autonomous Windows Desktop Controller."""

from __future__ import annotations

import ctypes
import math
import os
import socket
import threading
import time
from datetime import datetime
from typing import Any, Optional

from PIL import Image
from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QShortcut,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from controller.driver import (
    VK_CONTROL,
    VK_ESCAPE,
    VK_F12,
    VK_SHIFT,
    BaseDesktopDriver,
    create_driver,
)
from controller.emergency import (
    EmergencyStopError,
    SafetyController,
    SafetyGateError,
    StopSource,
)
from controller.interpreter import resolve_antigravity_executable
from controller.planners import get_saved_api_key_for_backend, save_api_key_to_dotenv
from controller.schema import SpeedConfig, SpeedPreset

MILESTONE_4_PRESET = (
    "Milestone 4: Notepad Demo (Write, Save to Approved Workspace, Reopen & Verify)"
)

DEFAULT_TASK_PRESETS: tuple[str, ...] = (
    MILESTONE_4_PRESET,
    "Open Firefox, open YouTube Music, and put on a song",
    "Open Notepad and summarize safety rules",
    "Verify approved workspace directory contents",
    "Custom Task",
)

BACKEND_OPTIONS: tuple[str, ...] = (
    "Local Demo / Fallback",
    "Gemini API",
    "OpenAI API",
    "Local Ollama",
    "Antigravity + Ollama",
)

APPROVAL_MODE_ASK = "Ask Before Risky"
APPROVAL_MODE_SUPERVISED = "Supervised (Auto-Approve)"
APPROVAL_MODE_OPTIONS: tuple[str, ...] = (
    APPROVAL_MODE_ASK,
    APPROVAL_MODE_SUPERVISED,
)

COMPACT_WINDOW_EXPANDED_SIZE = (400, 580)
COMPACT_WINDOW_HUD_SIZE = (320, 118)
CORNER_MARGIN_PX = 18
BOT_DODGE_MARGIN_PX = 70

_MOOD_PALETTE: dict[str, tuple[str, str, str]] = {
    "working": ("#38bdf8", "#fbbf24", "#0f172a"),
    "completed": ("#4ade80", "#22c55e", "#052e16"),
    "paused": ("#fb923c", "#f59e0b", "#451a03"),
    "approval": ("#fb923c", "#f59e0b", "#451a03"),
    "failed": ("#f87171", "#ef4444", "#450a0a"),
    "stopped": ("#f87171", "#ef4444", "#450a0a"),
    "idle": ("#94a3b8", "#60a5fa", "#1e293b"),
}


class BotFaceWidget(QWidget):
    """Circular bot character face drawn with QPainter reflecting execution state."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.mood: str = "idle"
        self.look_dx: float = 0.0
        self.look_dy: float = 0.0
        self._blink: bool = False
        self._phase: float = 0.0
        self.setMinimumSize(48, 48)

    def set_state(
        self,
        mood: str,
        look_dx: float = 0.0,
        look_dy: float = 0.0,
        blink: bool = False,
        phase: float = 0.0,
    ) -> None:
        """Update mood, pupil look offset, blink state, and repaint."""
        self.mood = mood if mood in _MOOD_PALETTE else "idle"
        self.look_dx = max(-2.5, min(2.5, float(look_dx)))
        self.look_dy = max(-2.5, min(2.5, float(look_dy)))
        self._blink = bool(blink)
        self._phase = float(phase)
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        w = float(self.width())
        h = float(self.height())
        side = max(24.0, min(w, h) - 6.0)
        cx = w / 2.0
        cy = h / 2.0
        radius = side / 2.0

        glow_hex, fill_hex, stroke_hex = _MOOD_PALETTE.get(self.mood, _MOOD_PALETTE["idle"])
        glow_color = QColor(glow_hex)
        glow_color.setAlpha(110 if self.mood == "working" else 75)
        pulse = 1.5 * math.sin(self._phase) if self.mood == "working" else 0.0
        glow_radius = radius + 2.5 + pulse

        # Outer aura ring
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glow_color))
        painter.drawEllipse(QPointF(cx, cy), glow_radius, glow_radius)

        # Circular head
        painter.setPen(QPen(QColor(stroke_hex), 2.0))
        painter.setBrush(QBrush(QColor(fill_hex)))
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # Eyes
        eye_offset_x = radius * 0.34
        eye_y = cy - radius * 0.14
        eye_r = max(2.8, radius * 0.17)
        pupil_r = max(1.5, eye_r * 0.56)

        if self._blink:
            painter.setPen(QPen(QColor(stroke_hex), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for sign in (-1.0, 1.0):
                ex = cx + sign * eye_offset_x
                painter.drawLine(QPointF(ex - eye_r, eye_y), QPointF(ex + eye_r, eye_y))
        else:
            for sign in (-1.0, 1.0):
                ex = cx + sign * eye_offset_x
                painter.setPen(QPen(QColor(stroke_hex), 1.2))
                painter.setBrush(QBrush(QColor("#ffffff")))
                painter.drawEllipse(QPointF(ex, eye_y), eye_r, eye_r)

                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(stroke_hex)))
                painter.drawEllipse(
                    QPointF(ex + self.look_dx * 0.7, eye_y + self.look_dy * 0.7),
                    pupil_r,
                    pupil_r,
                )

        # Mouth
        painter.setPen(QPen(QColor(stroke_hex), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        mouth_y = cy + radius * 0.25
        mouth_w = radius * 0.62

        if self.mood in ("working", "completed", "idle"):
            arc_rect = QRectF(cx - mouth_w / 2.0, mouth_y - radius * 0.16, mouth_w, radius * 0.34)
            painter.drawArc(arc_rect, 200 * 16, 140 * 16)
        elif self.mood in ("paused", "approval"):
            o_r = max(2.2, radius * 0.14)
            painter.drawEllipse(QPointF(cx, mouth_y + 1.0), o_r, o_r)
        else:
            path = QPainterPath()
            path.moveTo(cx - mouth_w / 2.0, mouth_y + radius * 0.12)
            path.quadTo(cx, mouth_y - radius * 0.18, cx + mouth_w / 2.0, mouth_y + radius * 0.12)
            painter.drawPath(path)


class BotCharacterOverlay(QWidget):
    """Floating, click-through circular bot character that moves near the cursor while active."""

    def __init__(self, driver: BaseDesktopDriver, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.driver = driver
        self.current_mood: str = "idle"
        self.status_text: str = "Idle"
        self._phase: float = 0.0
        self._tick_count: int = 0
        self._last_cursor: Optional[tuple[int, int]] = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        self.face_widget = BotFaceWidget(self)
        self.face_widget.setFixedSize(56, 56)
        layout.addWidget(self.face_widget)

        self.badge_label = QLabel("Idle", self)
        self.badge_label.setStyleSheet(
            "background-color: rgba(15, 23, 42, 220);"
            "color: #f8fafc;"
            "font-weight: bold;"
            "padding: 6px 12px;"
            "border-radius: 14px;"
            "border: 1px solid rgba(148, 163, 184, 140);"
        )
        layout.addWidget(self.badge_label)

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(40)
        self._anim_timer.timeout.connect(self._on_anim_tick)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_overlay)

    @staticmethod
    def _is_headless_or_test_mode() -> bool:
        return (
            os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1"
            or os.environ.get("QT_QPA_PLATFORM") == "offscreen"
        )

    def set_bot_state(self, mood: str, status_text: str) -> None:
        """Update character mood and floating pill badge text."""
        self.current_mood = mood
        self.status_text = status_text
        self.badge_label.setText(status_text)
        self.face_widget.set_state(mood, phase=self._phase)
        self.adjustSize()

    def _apply_exclude_from_capture(self) -> bool:
        """Exclude the floating overlay window from screen captures on Windows 10 2004+ / 11."""
        if os.name != "nt" or self._is_headless_or_test_mode():
            return False
        try:
            wda_exclude_from_capture = 0x00000011
            return bool(
                ctypes.windll.user32.SetWindowDisplayAffinity(
                    int(self.winId()),
                    wda_exclude_from_capture,
                )
            )
        except Exception:
            return False

    def show_overlay(self) -> None:
        """Display the floating character overlay without stealing focus."""
        self._hide_timer.stop()
        if self._is_headless_or_test_mode():
            return
        self._on_anim_tick()
        self.show()
        self._apply_exclude_from_capture()
        self.raise_()
        if not self._anim_timer.isActive():
            self._anim_timer.start()

    def hide_overlay(self) -> None:
        """Stop animation timer and hide the floating character overlay."""
        self._hide_timer.stop()
        if self._anim_timer.isActive():
            self._anim_timer.stop()
        self.hide()

    def hide_after_delay(self, delay_ms: int) -> None:
        """Hide the overlay after a brief completion/stop banner pause."""
        if self._is_headless_or_test_mode():
            self.hide_overlay()
            return
        self._hide_timer.start(max(0, int(delay_ms)))

    def _on_anim_tick(self) -> None:
        try:
            cx, cy = self.driver.get_cursor_position()
            sw, sh = self.driver.get_screen_size()
        except Exception:
            return

        look_dx = 0.0
        look_dy = 0.0
        if self._last_cursor is not None:
            dx = cx - self._last_cursor[0]
            dy = cy - self._last_cursor[1]
            look_dx = max(-2.5, min(2.5, dx * 0.25))
            look_dy = max(-2.5, min(2.5, dy * 0.25))
        self._last_cursor = (cx, cy)

        self._phase += 0.22
        self._tick_count += 1
        blink = (self._tick_count % 75) in (0, 1)
        bob_y = int(round(4.0 * math.sin(self._phase)))

        self.face_widget.set_state(
            self.current_mood,
            look_dx=look_dx,
            look_dy=look_dy,
            blink=blink,
            phase=self._phase,
        )

        self.adjustSize()
        ox = max(8, min(max(8, sw - self.width() - 8), cx + 24))
        oy = max(8, min(max(8, sh - self.height() - 8), cy + 24 + bob_y))
        self.move(ox, oy)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._hide_timer.stop()
        self._anim_timer.stop()
        super().closeEvent(event)


class DesktopControllerWindow(QMainWindow):
    """Window B: Operator cockpit for safe desktop automation."""

    start_requested = Signal(str, str)
    approval_requested = Signal(str, str, str)
    approval_resolved = Signal(bool)
    emergency_stopped = Signal(str)
    _history_signal = Signal(str, str, str)
    _progress_signal = Signal(int, int, str)
    _screenshot_signal = Signal(object)
    _status_signal = Signal(str)
    _completion_signal = Signal(str, str)
    _approval_signal = Signal(str, str, str)

    def __init__(
        self,
        safety: Optional[SafetyController] = None,
        driver: Optional[BaseDesktopDriver] = None,
        store: Optional[Any] = None,
        initial_speed: str = "Normal",
        initial_task: str = "",
        auto_start_demo: bool = False,
        initial_backend: str = "Local Demo / Fallback",
        initial_approval_mode: str = APPROVAL_MODE_ASK,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.safety: SafetyController = safety or SafetyController()
        self.driver: BaseDesktopDriver = driver or create_driver(self.safety)
        self.store: Optional[Any] = store
        self.auto_start_demo: bool = auto_start_demo

        self.pending_approval: bool = False
        self.approval_decision: Optional[bool] = None
        self.approval_info: dict[str, str] = {}
        self._approval_event = threading.Event()
        self._last_pixmap: Optional[QPixmap] = None
        self._engine_thread: Optional[threading.Thread] = None
        self._current_corner: str = "top_right"
        self._compact_mode: bool = False
        self._dodge_margin: int = BOT_DODGE_MARGIN_PX
        self._last_dodge_time: float = 0.0
        self._pos_anim: Optional[QPropertyAnimation] = None
        self.character_overlay = BotCharacterOverlay(driver=self.driver)

        self.setWindowTitle("Window B - Autonomous Windows Desktop Controller")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumSize(QSize(300, 108))

        self._build_ui()
        self._connect_signals()
        self._setup_shortcuts_and_polling()

        self.resize(*COMPACT_WINDOW_EXPANDED_SIZE)
        self.dock_to_corner("top_right", animate=False)

        # Apply initial speed, backend, approval mode, and task
        if initial_speed in {p.value for p in SpeedPreset}:
            self.speed_combo.setCurrentText(initial_speed)
        else:
            self.speed_combo.setCurrentText(SpeedPreset.Normal.value)
        self._on_speed_preset_changed(self.speed_combo.currentText())

        if initial_backend in BACKEND_OPTIONS:
            self.backend_combo.setCurrentText(initial_backend)
        self._on_backend_changed(self.backend_combo.currentText())

        if initial_approval_mode in APPROVAL_MODE_OPTIONS:
            self.approval_mode_combo.setCurrentText(initial_approval_mode)

        if initial_task:
            self.task_input.setText(initial_task)
        else:
            self.task_input.setText(self.preset_task_combo.currentText())

        self._refresh_safety_ui()

        if self.auto_start_demo:
            QTimer.singleShot(1000, self.on_start_clicked)

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(6)

        # Always-visible compact corner HUD header
        self._compact_header = QWidget(self)
        header_layout = QVBoxLayout(self._compact_header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)

        top_hud_row = QHBoxLayout()
        top_hud_row.setSpacing(6)
        self.bot_avatar = BotFaceWidget(self)
        self.bot_avatar.setFixedSize(40, 40)
        top_hud_row.addWidget(self.bot_avatar)

        status_col = QVBoxLayout()
        status_col.setSpacing(1)
        self.status_label = QLabel("Idle (Start Authorization Required)", self)
        self.status_label.setStyleSheet("font-weight: bold; font-size: 11px;")
        self.status_label.setMinimumWidth(0)
        self.current_action_label = QLabel("Action: None", self)
        self.current_action_label.setStyleSheet("font-size: 11px; color: #475569;")
        self.current_action_label.setMinimumWidth(0)
        status_col.addWidget(self.status_label)
        status_col.addWidget(self.current_action_label)
        top_hud_row.addLayout(status_col, stretch=1)

        self.compact_toggle_button = QPushButton("Compact", self)
        self.compact_toggle_button.setFixedWidth(64)
        top_hud_row.addWidget(self.compact_toggle_button)
        header_layout.addLayout(top_hud_row)

        hud_controls_row = QHBoxLayout()
        hud_controls_row.setSpacing(4)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(16)
        hud_controls_row.addWidget(self.progress_bar, stretch=1)

        self.pause_button = QPushButton("Pause (Esc)", self)
        hud_controls_row.addWidget(self.pause_button)

        self.emergency_stop_button = QPushButton("STOP", self)
        self.emergency_stop_button.setStyleSheet(
            "background-color: #d91e18; color: white; font-weight: bold; padding: 4px 10px;"
        )
        hud_controls_row.addWidget(self.emergency_stop_button)
        header_layout.addLayout(hud_controls_row)

        root_layout.addWidget(self._compact_header)

        # Expandable details container (hidden in compact running HUD mode)
        self._details_container = QWidget(self)
        details_layout = QVBoxLayout(self._details_container)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(6)

        task_group = QGroupBox("Task & Mode", self)
        task_layout = QVBoxLayout(task_group)
        task_layout.setContentsMargins(6, 6, 6, 6)
        task_layout.setSpacing(4)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Preset:", self))
        self.preset_task_combo = QComboBox(self)
        self.preset_task_combo.addItems(list(DEFAULT_TASK_PRESETS))
        preset_row.addWidget(self.preset_task_combo, stretch=1)
        task_layout.addLayout(preset_row)

        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("Task:", self))
        self.task_input = QLineEdit(self)
        self.task_input.setPlaceholderText("Enter natural-language desktop task...")
        input_row.addWidget(self.task_input, stretch=1)
        task_layout.addLayout(input_row)

        api_key_row = QHBoxLayout()
        api_key_row.addWidget(QLabel("Key:", self))
        self.api_key_input = QLineEdit(self)
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("Optional API Key (AIza..., sk-...)")
        api_key_row.addWidget(self.api_key_input, stretch=1)
        self.save_key_button = QPushButton("Save Key", self)
        api_key_row.addWidget(self.save_key_button)
        task_layout.addLayout(api_key_row)

        backend_row = QHBoxLayout()
        backend_row.addWidget(QLabel("Planner:", self))
        self.backend_combo = QComboBox(self)
        self.backend_combo.addItems(list(BACKEND_OPTIONS))
        backend_row.addWidget(self.backend_combo, stretch=1)
        self.backend_status_label = QLabel("Ready (Local Demo)", self)
        self.backend_status_label.setMinimumWidth(0)
        backend_row.addWidget(self.backend_status_label)
        task_layout.addLayout(backend_row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Access:", self))
        self.approval_mode_combo = QComboBox(self)
        self.approval_mode_combo.addItems(list(APPROVAL_MODE_OPTIONS))
        mode_row.addWidget(self.approval_mode_combo, stretch=1)
        task_layout.addLayout(mode_row)

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed:", self))
        self.speed_combo = QComboBox(self)
        self.speed_combo.addItems([p.value for p in SpeedPreset])
        speed_row.addWidget(self.speed_combo)

        speed_row.addWidget(QLabel("Move:", self))
        self.duration_spin = QDoubleSpinBox(self)
        self.duration_spin.setRange(0.05, 10.0)
        self.duration_spin.setSingleStep(0.05)
        self.duration_spin.setDecimals(2)
        speed_row.addWidget(self.duration_spin)

        speed_row.addWidget(QLabel("Delay:", self))
        self.delay_spin = QDoubleSpinBox(self)
        self.delay_spin.setRange(0.0, 10.0)
        self.delay_spin.setSingleStep(0.05)
        self.delay_spin.setDecimals(2)
        speed_row.addWidget(self.delay_spin)
        task_layout.addLayout(speed_row)

        details_layout.addWidget(task_group)

        # Operator controls row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        self.start_button = QPushButton("Start / Authorize", self)
        self.start_button.setStyleSheet(
            "background-color: #1f7a3e; color: white; font-weight: bold; padding: 5px 8px;"
        )
        self.resume_button = QPushButton("Resume", self)
        self.takeover_button = QPushButton("Manual Takeover", self)
        self.reset_stop_button = QPushButton("Reset Stop", self)

        for btn in (
            self.start_button,
            self.resume_button,
            self.takeover_button,
            self.reset_stop_button,
        ):
            btn_row.addWidget(btn)
        details_layout.addLayout(btn_row)

        # Middle split: Live Screenshot + Approval Panel + Action History
        splitter = QSplitter(Qt.Orientation.Vertical, self)

        preview_container = QWidget(self)
        preview_layout = QHBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(6)

        screenshot_group = QGroupBox("Observation", self)
        screenshot_layout = QVBoxLayout(screenshot_group)
        screenshot_layout.setContentsMargins(4, 4, 4, 4)
        self.screenshot_label = QLabel("No screenshot captured yet", self)
        self.screenshot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.screenshot_label.setMinimumSize(QSize(180, 96))
        self.screenshot_label.setStyleSheet(
            "background-color: #141821; color: #9aa7b8; border: 1px solid #334;"
        )
        screenshot_layout.addWidget(self.screenshot_label)
        preview_layout.addWidget(screenshot_group, stretch=1)

        self.approval_group = QGroupBox("Approval Gate", self)
        approval_layout = QVBoxLayout(self.approval_group)
        approval_layout.setContentsMargins(4, 4, 4, 4)
        self.approval_details_label = QLabel("No pending approval request.", self)
        self.approval_details_label.setWordWrap(True)
        approval_layout.addWidget(self.approval_details_label, stretch=1)

        approval_btn_row = QHBoxLayout()
        self.approve_button = QPushButton("Approve", self)
        self.approve_button.setEnabled(False)
        self.deny_button = QPushButton("Deny", self)
        self.deny_button.setEnabled(False)
        approval_btn_row.addWidget(self.approve_button)
        approval_btn_row.addWidget(self.deny_button)
        approval_layout.addLayout(approval_btn_row)
        preview_layout.addWidget(self.approval_group, stretch=1)

        splitter.addWidget(preview_container)

        history_group = QGroupBox("Action History", self)
        history_layout = QVBoxLayout(history_group)
        history_layout.setContentsMargins(4, 4, 4, 4)
        self.history_table = QTableWidget(0, 4, self)
        self.history_table.setHorizontalHeaderLabels(["Time", "Action", "Status", "Detail"])
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        history_layout.addWidget(self.history_table)
        splitter.addWidget(history_group)

        details_layout.addWidget(splitter, stretch=1)
        root_layout.addWidget(self._details_container, stretch=1)

    def _connect_signals(self) -> None:
        self.preset_task_combo.currentTextChanged.connect(self._on_preset_task_changed)
        self.speed_combo.currentTextChanged.connect(self._on_speed_preset_changed)
        self.duration_spin.valueChanged.connect(self._on_custom_spin_changed)
        self.delay_spin.valueChanged.connect(self._on_custom_spin_changed)
        self.backend_combo.currentTextChanged.connect(self._on_backend_changed)
        self.api_key_input.textChanged.connect(self._on_api_key_changed)
        self.save_key_button.clicked.connect(self.on_save_key_clicked)

        self.start_button.clicked.connect(self.on_start_clicked)
        self.pause_button.clicked.connect(self.on_pause_clicked)
        self.resume_button.clicked.connect(self.on_resume_clicked)
        self.takeover_button.clicked.connect(self.on_takeover_clicked)
        self.emergency_stop_button.clicked.connect(self.on_emergency_stop_clicked)
        self.reset_stop_button.clicked.connect(self.on_reset_stop_clicked)
        self.approve_button.clicked.connect(self.on_approve_clicked)
        self.deny_button.clicked.connect(self.on_deny_clicked)
        self.compact_toggle_button.clicked.connect(self._on_compact_toggle_clicked)
        self._history_signal.connect(self.append_history_row)
        self._progress_signal.connect(self.set_progress)
        self._screenshot_signal.connect(self.update_screenshot)
        self._status_signal.connect(self._on_status_signal)
        self._completion_signal.connect(self._on_completion_signal)
        self._approval_signal.connect(self.request_user_approval)

    def _setup_shortcuts_and_polling(self) -> None:
        self.f12_shortcut = QShortcut(QKeySequence("F12"), self)
        self.f12_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.f12_shortcut.activated.connect(self._on_f12_shortcut)

        self.esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.esc_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.esc_shortcut.activated.connect(self._on_esc_shortcut)

        self.approve_return_shortcut = QShortcut(QKeySequence("Return"), self)
        self.approve_return_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.approve_return_shortcut.activated.connect(self._on_approve_shortcut)

        self.approve_enter_shortcut = QShortcut(QKeySequence("Enter"), self)
        self.approve_enter_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.approve_enter_shortcut.activated.connect(self._on_approve_shortcut)

        self.deny_delete_shortcut = QShortcut(QKeySequence("Delete"), self)
        self.deny_delete_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.deny_delete_shortcut.activated.connect(self._on_deny_shortcut)

        self._poll_timer: Optional[QTimer] = None
        if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1":
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(50)
            self._poll_timer.timeout.connect(self._poll_global_safety)
            self._poll_timer.start()

    @staticmethod
    def _is_headless_or_test_mode() -> bool:
        return (
            os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1"
            or os.environ.get("QT_QPA_PLATFORM") == "offscreen"
        )

    def _apply_exclude_from_capture(self) -> bool:
        """Exclude Window B from desktop screenshots on Windows 10 2004+ / 11."""
        if os.name != "nt" or self._is_headless_or_test_mode():
            return False
        try:
            wda_exclude_from_capture = 0x00000011
            return bool(
                ctypes.windll.user32.SetWindowDisplayAffinity(
                    int(self.winId()),
                    wda_exclude_from_capture,
                )
            )
        except Exception:
            return False

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_exclude_from_capture()

    def is_supervised_mode(self) -> bool:
        """Return True when Supervised (Auto-Approve) mode is active."""
        return self.approval_mode_combo.currentText() == APPROVAL_MODE_SUPERVISED

    def _on_compact_toggle_clicked(self) -> None:
        self.set_compact_mode(not self._compact_mode)

    def set_compact_mode(self, compact: bool) -> None:
        """Switch Window B between the compact corner HUD and expanded panel."""
        self._compact_mode = bool(compact)
        self._details_container.setVisible(not self._compact_mode)
        self.compact_toggle_button.setText("Expand" if self._compact_mode else "Compact")
        w, h = COMPACT_WINDOW_HUD_SIZE if self._compact_mode else COMPACT_WINDOW_EXPANDED_SIZE
        self.resize(w, h)
        self.dock_to_corner(self._current_corner, animate=False)

    def _available_screen_rect(self) -> tuple[int, int, int, int]:
        if not self._is_headless_or_test_mode():
            screen = QApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                if avail.width() > 0 and avail.height() > 0:
                    return (
                        int(avail.x()),
                        int(avail.y()),
                        int(avail.width()),
                        int(avail.height()),
                    )
        sw, sh = self.driver.get_screen_size()
        return (0, 0, max(1, int(sw)), max(1, int(sh)))

    def compute_corner_positions(
        self,
        screen_rect: Optional[tuple[int, int, int, int]] = None,
    ) -> dict[str, tuple[int, int]]:
        """Compute anchored (x, y) positions for all 4 screen corners."""
        sx, sy, sw, sh = screen_rect or self._available_screen_rect()
        if self.width() > 0 and self.height() > 0:
            w, h = self.width(), self.height()
        else:
            w, h = COMPACT_WINDOW_HUD_SIZE if self._compact_mode else COMPACT_WINDOW_EXPANDED_SIZE
        m = CORNER_MARGIN_PX
        left_x = sx + m
        right_x = max(left_x, sx + sw - w - m)
        top_y = sy + m
        bottom_y = max(top_y, sy + sh - h - m)
        return {
            "top_right": (right_x, top_y),
            "top_left": (left_x, top_y),
            "bottom_left": (left_x, bottom_y),
            "bottom_right": (right_x, bottom_y),
        }

    def dock_to_corner(self, corner: str, animate: bool = True) -> tuple[int, int]:
        """Dock Window B to a named screen corner, optionally with smooth easing."""
        corners = self.compute_corner_positions()
        resolved = corner if corner in corners else "top_right"
        tx, ty = corners[resolved]
        self._current_corner = resolved

        if self._is_headless_or_test_mode() or not animate:
            if self._pos_anim is not None:
                self._pos_anim.stop()
            self.move(tx, ty)
        else:
            if self._pos_anim is not None:
                self._pos_anim.stop()
            anim = QPropertyAnimation(self, b"pos", self)
            anim.setDuration(160)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setStartValue(self.pos())
            anim.setEndValue(QPoint(tx, ty))
            self._pos_anim = anim
            anim.start()
        return tx, ty

    def check_and_dodge_bot_cursor(
        self,
        cursor_pos: Optional[tuple[int, int]] = None,
        bot_target: Optional[tuple[int, int]] = None,
    ) -> bool:
        """Move Window B to the farthest safe corner ONLY when the bot cursor approaches it."""
        if cursor_pos is None:
            cursor_pos = self.driver.get_cursor_position()
        if bot_target is None:
            bot_target = self.driver.get_bot_target_position()
        if not self.driver.is_bot_driving_cursor(cursor_pos):
            return False

        wx, wy = self.x(), self.y()
        ww, wh = max(1, self.width()), max(1, self.height())
        m = self._dodge_margin

        def _in_zone(pt: tuple[int, int], rx: int, ry: int) -> bool:
            return (rx - m) <= pt[0] <= (rx + ww + m) and (ry - m) <= pt[1] <= (ry + wh + m)

        threats = [cursor_pos]
        if bot_target is not None:
            threats.append(bot_target)

        if not any(_in_zone(pt, wx, wy) for pt in threats):
            return False

        now = time.monotonic()
        target_inside_current = bot_target is not None and _in_zone(bot_target, wx, wy)
        if not self._is_headless_or_test_mode() and not target_inside_current:
            if (now - self._last_dodge_time) < 0.25:
                return False

        corners = self.compute_corner_positions()
        best_corner: Optional[str] = None
        best_score: Optional[tuple[int, float]] = None
        for name, (cx, cy) in corners.items():
            if name == self._current_corner:
                continue
            clear = 0 if any(_in_zone(pt, cx, cy) for pt in threats) else 1
            center_x = cx + ww / 2.0
            center_y = cy + wh / 2.0
            min_dist_sq = min(
                (center_x - pt[0]) ** 2 + (center_y - pt[1]) ** 2
                for pt in threats
            )
            score = (clear, min_dist_sq)
            if best_score is None or score > best_score:
                best_score = score
                best_corner = name

        if best_corner is None:
            return False

        self.dock_to_corner(best_corner, animate=not self._is_headless_or_test_mode())
        self._last_dodge_time = now
        return True

    def _on_approve_shortcut(self) -> None:
        if self.pending_approval and self.approve_button.isEnabled():
            self.on_approve_clicked()

    def _on_deny_shortcut(self) -> None:
        if self.pending_approval and self.deny_button.isEnabled():
            self.on_deny_clicked()

    def _on_preset_task_changed(self, text: str) -> None:
        if text and text != "Custom Task":
            self.task_input.setText(text)

    def _on_speed_preset_changed(self, preset_name: str) -> None:
        try:
            cfg = SpeedConfig.from_preset(preset_name)
        except ValueError:
            return
        self.duration_spin.blockSignals(True)
        self.delay_spin.blockSignals(True)
        self.duration_spin.setValue(cfg.move_duration_sec)
        self.delay_spin.setValue(cfg.post_action_delay_sec)
        self.duration_spin.blockSignals(False)
        self.delay_spin.blockSignals(False)
        self.driver.speed_config = self.current_speed_config()

    def _on_custom_spin_changed(self, _value: float) -> None:
        if self.speed_combo.currentText() != SpeedPreset.Custom.value:
            self.speed_combo.blockSignals(True)
            self.speed_combo.setCurrentText(SpeedPreset.Custom.value)
            self.speed_combo.blockSignals(False)
        self.driver.speed_config = self.current_speed_config()

    def _on_backend_changed(self, backend: str) -> None:
        saved = get_saved_api_key_for_backend(backend)
        current = self.api_key_input.text().strip()
        if saved:
            self.api_key_input.blockSignals(True)
            self.api_key_input.setText(saved)
            self.api_key_input.blockSignals(False)
        elif (
            backend in {"Local Demo / Fallback", "Local Ollama", "Antigravity + Ollama"}
            or (backend == "Gemini API" and current.startswith("sk-"))
            or (backend == "OpenAI API" and current.startswith(("AIza", "AQ.")))
        ):
            self.api_key_input.blockSignals(True)
            self.api_key_input.clear()
            self.api_key_input.blockSignals(False)
        self._refresh_backend_status()

    def on_save_key_clicked(self) -> None:
        key = self.api_key_input.text().strip()
        if not key:
            self.backend_status_label.setText("Enter an API key first")
            return
        self._on_api_key_changed(key)
        var_name = save_api_key_to_dotenv(self.backend_combo.currentText(), key)
        self._refresh_backend_status()
        if var_name:
            self.append_history_row("api_key", "saved", f"Saved {var_name} to .env")

    def _on_api_key_changed(self, text: str) -> None:
        key = text.strip()
        if self.backend_combo.currentText() == "Local Demo / Fallback":
            if key.startswith(("AIza", "AQ.")):
                self.backend_combo.setCurrentText("Gemini API")
            elif key.startswith("sk-"):
                self.backend_combo.setCurrentText("OpenAI API")
        self._refresh_backend_status()

    def _refresh_backend_status(self) -> None:
        backend = self.backend_combo.currentText()
        inline_key = self.api_key_input.text().strip() if hasattr(self, "api_key_input") else ""
        if backend == "Local Demo / Fallback":
            self.backend_status_label.setText("Ready (Local Demo)")
        elif backend == "Gemini API":
            has_key = bool(inline_key or os.environ.get("GEMINI_API_KEY", "").strip())
            self.backend_status_label.setText("Configured (Key Present)" if has_key else "Missing GEMINI_API_KEY")
        elif backend == "OpenAI API":
            has_key = bool(inline_key or os.environ.get("OPENAI_API_KEY", "").strip())
            self.backend_status_label.setText("Configured (Key Present)" if has_key else "Missing OPENAI_API_KEY")
        elif backend == "Local Ollama":
            if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1":
                self.backend_status_label.setText("Ollama Check Skipped (Test Mode)")
            else:
                reachable = self._check_ollama_reachable()
                if reachable:
                    has_agy = bool(resolve_antigravity_executable())
                    self.backend_status_label.setText(
                        "Ollama Ready (11434) + Antigravity Brain"
                        if has_agy
                        else "Ollama Ready (11434)"
                    )
                else:
                    self.backend_status_label.setText("Ollama Unreachable")
        elif backend == "Antigravity + Ollama":
            if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1":
                self.backend_status_label.setText("Antigravity + Ollama (Test Mode)")
            else:
                has_agy = bool(resolve_antigravity_executable())
                reachable = self._check_ollama_reachable()
                if has_agy and reachable:
                    self.backend_status_label.setText("Antigravity CLI + Ollama Ready")
                elif not has_agy and not reachable:
                    self.backend_status_label.setText("Missing Antigravity CLI & Ollama")
                elif not has_agy:
                    self.backend_status_label.setText("Missing Antigravity CLI (Ollama Ready)")
                else:
                    self.backend_status_label.setText("Antigravity CLI Ready (Ollama Unreachable)")
        else:
            self.backend_status_label.setText("Unknown Backend")

    @staticmethod
    def _check_ollama_reachable() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 11434), timeout=0.15):
                return True
        except OSError:
            return False

    def current_speed_config(self) -> SpeedConfig:
        """Build a validated SpeedConfig reflecting the current GUI speed widgets."""
        preset_text = self.speed_combo.currentText() or SpeedPreset.Normal.value
        base = SpeedConfig.from_preset(preset_text)
        move_dur = max(0.05, float(self.duration_spin.value()))
        post_delay = max(0.0, float(self.delay_spin.value()))
        return SpeedConfig(
            preset=base.preset,
            move_duration_sec=move_dur,
            pixels_per_second=base.pixels_per_second,
            post_action_delay_sec=post_delay,
            interpolation_steps=base.interpolation_steps,
        )

    def update_screenshot(self, image: Image.Image) -> None:
        """Render a PIL image cleanly inside screenshot_label."""
        rgba = image.convert("RGBA")
        raw = rgba.tobytes("raw", "RGBA")
        qimage = QImage(
            raw,
            rgba.width,
            rgba.height,
            rgba.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
        pixmap = QPixmap.fromImage(qimage)
        self._last_pixmap = pixmap

        target_size = self.screenshot_label.size()
        if target_size.width() < 64 or target_size.height() < 64:
            target_size = QSize(640, 360)
        scaled = pixmap.scaled(
            target_size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.screenshot_label.setPixmap(scaled)

    def _update_bot_character(self, mood: str, status_text: str) -> None:
        self.bot_avatar.set_state(mood)
        self.character_overlay.set_bot_state(mood, status_text)

    def set_progress(self, current: int, total: int, action_text: str) -> None:
        """Update progress bar and current action status text."""
        safe_total = max(int(total), 1)
        safe_current = max(0, min(int(current), safe_total))
        self.progress_bar.setRange(0, safe_total)
        self.progress_bar.setValue(safe_current)
        self.current_action_label.setText(action_text)
        self._update_bot_character("working", f"[{safe_current}/{safe_total}] {action_text[:48]}")

    def append_history_row(
        self,
        action_type: str,
        status: str,
        detail: str,
        timestamp: str = "",
    ) -> None:
        """Append a row to the Action History table."""
        ts = timestamp or datetime.now().strftime("%H:%M:%S")
        row = self.history_table.rowCount()
        self.history_table.insertRow(row)
        self.history_table.setItem(row, 0, QTableWidgetItem(ts))
        self.history_table.setItem(row, 1, QTableWidgetItem(action_type))
        self.history_table.setItem(row, 2, QTableWidgetItem(status))
        self.history_table.setItem(row, 3, QTableWidgetItem(detail))
        self.history_table.scrollToBottom()

    def request_user_approval(
        self,
        reason: str,
        risk_category: str,
        details: str = "",
    ) -> None:
        """Highlight the approval panel and await operator Approve/Deny click."""
        self.pending_approval = True
        self.approval_decision = None
        self.approval_info = {
            "reason": reason,
            "risk_category": risk_category,
            "details": details,
        }
        self._approval_event.clear()

        detail_line = f"\nDetails: {details}" if details else ""
        self.approval_details_label.setText(
            f"Risk: {risk_category}\nReason: {reason}{detail_line}"
        )
        self.approval_group.setStyleSheet(
            "QGroupBox { border: 2px solid #e67e22; border-radius: 4px; font-weight: bold; }"
        )
        self.approve_button.setEnabled(True)
        self.deny_button.setEnabled(True)
        if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1":
            if self._compact_mode:
                self.set_compact_mode(False)
            self.raise_()
        self.status_label.setText("Awaiting User Approval")
        self._update_bot_character("approval", "Awaiting Approval")
        self.append_history_row("request_approval", "pending", f"[{risk_category}] {reason}")
        self.approval_requested.emit(reason, risk_category, details)

    def wait_for_approval_decision(self, timeout_sec: float = 300.0) -> bool:
        """Block a background worker thread until Approve or Deny is clicked."""
        signaled = self._approval_event.wait(timeout=timeout_sec)
        if not signaled:
            return False
        return bool(self.approval_decision)

    def _refresh_safety_ui(self) -> None:
        if self.safety.emergency_stopped:
            self.status_label.setText(f"EMERGENCY STOPPED ({self.safety.stop_source})")
            self.status_label.setStyleSheet("color: #d91e18; font-weight: bold;")
            self.reset_stop_button.setEnabled(True)
            self.start_button.setEnabled(False)
            self.resume_button.setEnabled(False)
        elif self.safety.manual_takeover:
            self.status_label.setText("Manual Takeover Active")
            self.status_label.setStyleSheet("color: #d35400; font-weight: bold;")
            self.takeover_button.setText("Exit Takeover")
            self.reset_stop_button.setEnabled(False)
            self.start_button.setEnabled(True)
            self.resume_button.setEnabled(False)
        elif self.safety.paused:
            self.status_label.setText("Paused")
            self.status_label.setStyleSheet("color: #b7791f; font-weight: bold;")
            self.takeover_button.setText("Manual Takeover")
            self.reset_stop_button.setEnabled(False)
            self.start_button.setEnabled(True)
            self.resume_button.setEnabled(self.safety.authorized)
        elif self.safety.authorized:
            self.status_label.setText("Authorized / Running")
            self.status_label.setStyleSheet("color: #1f7a3e; font-weight: bold;")
            self.takeover_button.setText("Manual Takeover")
            self.reset_stop_button.setEnabled(False)
            self.start_button.setEnabled(True)
            self.resume_button.setEnabled(False)
        else:
            self.status_label.setText("Idle (Start Authorization Required)")
            self.status_label.setStyleSheet("color: #2c3e50; font-weight: bold;")
            self.takeover_button.setText("Manual Takeover")
            self.reset_stop_button.setEnabled(False)
            self.start_button.setEnabled(True)
            self.resume_button.setEnabled(False)

    def _poll_global_safety(self) -> None:
        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            f12_down = bool(user32.GetAsyncKeyState(VK_F12) & 0x8000)
            ctrl_down = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
            shift_down = bool(user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
            esc_down = bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
            if f12_down or (ctrl_down and shift_down and esc_down):
                if not self.safety.emergency_stopped:
                    self._trigger_stop(StopSource.global_hotkey.value)
                return

            if esc_down and not ctrl_down and not shift_down:
                if self.safety.authorized and not self.safety.paused and not self.safety.emergency_stopped:
                    self.on_pause_clicked()
                return

            if self.safety.can_execute():
                x, y = self.driver.get_cursor_position()
                w, h = self.driver.get_screen_size()
                if self.safety.check_corner_failsafe(x, y, w, h):
                    self._after_emergency_stop(StopSource.corner_failsafe.value)
                    return
                self.check_and_dodge_bot_cursor(cursor_pos=(x, y))
        except Exception:
            pass

    def _on_f12_shortcut(self) -> None:
        self._trigger_stop(StopSource.global_hotkey.value)

    def _on_esc_shortcut(self) -> None:
        if not self.safety.paused and not self.safety.emergency_stopped:
            self.on_pause_clicked()

    def _trigger_stop(self, source: str) -> None:
        self.safety.trigger_emergency_stop(source)
        self._after_emergency_stop(source)

    def _after_emergency_stop(self, source: str) -> None:
        if self.pending_approval:
            self.pending_approval = False
            self.approval_decision = False
            self._approval_event.set()
            self.approve_button.setEnabled(False)
            self.deny_button.setEnabled(False)
            self.approval_group.setStyleSheet("")
        self._refresh_safety_ui()
        self._update_bot_character("stopped", "EMERGENCY STOP")
        self.character_overlay.hide_after_delay(1500)
        if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1":
            self.set_compact_mode(False)
            self.showNormal()
        self.append_history_row("emergency_stop", "halted", f"Source: {source}")
        self.emergency_stopped.emit(source)

    def on_start_clicked(self) -> None:
        try:
            self.safety.authorize_start()
        except EmergencyStopError as exc:
            self._refresh_safety_ui()
            self.append_history_row("start", "blocked", str(exc))
            return

        inline_key = self.api_key_input.text().strip()
        if inline_key:
            save_api_key_to_dotenv(self.backend_combo.currentText(), inline_key)

        self.driver.speed_config = self.current_speed_config()
        self._refresh_safety_ui()
        task_text = self.task_input.text().strip() or self.preset_task_combo.currentText()
        backend_name = self.backend_combo.currentText()
        self._update_bot_character("working", "Working: Starting task...")
        self.character_overlay.show_overlay()
        self.append_history_row("start", "authorized", f"Task: {task_text}")
        self.start_requested.emit(task_text, backend_name)

        # Optional M3 engine integration (only active once controller.engine exists)
        if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1":
            self.set_compact_mode(True)
            self._try_launch_engine(task_text, backend_name)

    def _on_status_signal(self, status_text: str) -> None:
        self.status_label.setText(status_text)

    def _on_completion_signal(self, status: str, summary: str) -> None:
        self.append_history_row("engine", status, summary)
        self.status_label.setText(f"Task {status.capitalize()}")
        if summary:
            self.current_action_label.setText(summary[:80])
        if status == "completed":
            self.progress_bar.setValue(self.progress_bar.maximum())
        mood = "completed" if status == "completed" else "failed"
        badge_msg = summary[:48] if summary else f"Task {status.capitalize()}"
        self._update_bot_character(mood, badge_msg)
        self.character_overlay.hide_after_delay(2000)
        if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1":
            self.set_compact_mode(False)
            self.showNormal()

    def _try_launch_engine(self, task_text: str, backend_name: str) -> None:
        if self._engine_thread is not None and self._engine_thread.is_alive():
            return
        try:
            from controller.engine import DesktopExecutionEngine, ExecutionResult
            from controller.policy import PolicyDecision
            from controller.schema import DesktopAction
        except ImportError:
            return

        def _on_progress(current: int, total: int, action_text: str) -> None:
            try:
                self._progress_signal.emit(current, total, action_text)
            except RuntimeError:
                pass

        def _on_screenshot(image: Image.Image) -> None:
            try:
                self._screenshot_signal.emit(image)
            except RuntimeError:
                pass

        def _on_history(action_type: str, status: str, detail: str) -> None:
            try:
                self._history_signal.emit(action_type, status, detail)
            except RuntimeError:
                pass

        def _on_approval(action: DesktopAction, decision: PolicyDecision) -> bool:
            try:
                if self.is_supervised_mode():
                    return True
                self._approval_event.clear()
                self._approval_signal.emit(
                    decision.reason,
                    decision.risk_category,
                    action.description,
                )
            except RuntimeError:
                return False
            return self.wait_for_approval_decision()

        def _on_completion(result: ExecutionResult) -> None:
            try:
                self._completion_signal.emit(
                    result.status,
                    result.summary or result.error or "",
                )
            except RuntimeError:
                pass

        runtime_api_key = self.api_key_input.text().strip() or None
        supervised_initial = self.is_supervised_mode()

        def _worker() -> None:
            try:
                engine = DesktopExecutionEngine(
                    driver=self.driver,
                    safety=self.safety,
                    store=self.store,
                    backend_name=backend_name,
                    api_key=runtime_api_key,
                    approval_callback=_on_approval,
                    progress_callback=_on_progress,
                    screenshot_callback=_on_screenshot,
                    history_callback=_on_history,
                    completion_callback=_on_completion,
                    auto_approve_supervised=supervised_initial,
                )
                engine.run_task(task_text)
            except Exception as exc:
                try:
                    self._history_signal.emit("engine", "error", str(exc))
                    self._status_signal.emit(f"Engine Error: {exc}")
                except RuntimeError:
                    pass

        self._engine_thread = threading.Thread(target=_worker, daemon=True)
        self._engine_thread.start()

    def on_pause_clicked(self) -> None:
        self.safety.pause()
        self._refresh_safety_ui()
        self._update_bot_character("paused", "Paused (Esc)")
        self.append_history_row("pause", "paused", "Operator paused execution")

    def on_resume_clicked(self) -> None:
        try:
            self.safety.resume()
        except (SafetyGateError, EmergencyStopError) as exc:
            self._refresh_safety_ui()
            self.append_history_row("resume", "blocked", str(exc))
            return
        self._refresh_safety_ui()
        self._update_bot_character("working", "Resumed")
        self.append_history_row("resume", "running", "Operator resumed execution")

    def on_takeover_clicked(self) -> None:
        if not self.safety.manual_takeover:
            self.safety.enter_manual_takeover()
            self._refresh_safety_ui()
            self._update_bot_character("paused", "Manual Takeover")
            self.append_history_row("manual_takeover", "active", "Operator took manual control")
        else:
            try:
                self.safety.exit_manual_takeover()
            except EmergencyStopError as exc:
                self._refresh_safety_ui()
                self.append_history_row("manual_takeover", "blocked", str(exc))
                return
            self._refresh_safety_ui()
            self._update_bot_character("working", "Automation Resumed")
            self.append_history_row("manual_takeover", "exited", "Returned control to automation")

    def on_emergency_stop_clicked(self) -> None:
        self._trigger_stop(StopSource.gui_button.value)

    def on_reset_stop_clicked(self) -> None:
        self.safety.reset_emergency_stop()
        self._refresh_safety_ui()
        self._update_bot_character("idle", "Idle (Reset)")
        self.character_overlay.hide_overlay()
        self.append_history_row("reset_stop", "cleared", "Emergency stop cleared; press Start to authorize")

    def on_approve_clicked(self) -> None:
        self.approval_decision = True
        self.pending_approval = False
        self._approval_event.set()
        self.approve_button.setEnabled(False)
        self.deny_button.setEnabled(False)
        self.approval_group.setStyleSheet("")
        self.approval_details_label.setText("Approved by operator.")
        self._refresh_safety_ui()
        self._update_bot_character("working", "Approved - Continuing...")
        self.append_history_row("approval", "approved", self.approval_info.get("reason", ""))
        self.approval_resolved.emit(True)
        if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1":
            self.set_compact_mode(True)
            self.driver.restore_last_app_focus()

    def on_deny_clicked(self) -> None:
        self.approval_decision = False
        self.pending_approval = False
        self._approval_event.set()
        self.approve_button.setEnabled(False)
        self.deny_button.setEnabled(False)
        self.approval_group.setStyleSheet("")
        self.approval_details_label.setText("Denied by operator.")
        self._refresh_safety_ui()
        self._update_bot_character("failed", "Action Denied")
        self.append_history_row("approval", "denied", self.approval_info.get("reason", ""))
        self.approval_resolved.emit(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
        if (
            self._engine_thread is not None
            and self._engine_thread.is_alive()
            and not self.safety.emergency_stopped
        ):
            self.safety.trigger_emergency_stop(StopSource.gui_button.value)
        self.character_overlay.close()
        self._approval_event.set()
        super().closeEvent(event)
