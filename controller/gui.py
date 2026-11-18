"""Window B PySide6 GUI for the Autonomous Windows Desktop Controller."""

from __future__ import annotations

import ctypes
import os
import socket
import threading
from datetime import datetime
from typing import Any, Optional

from PIL import Image
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from controller.schema import SpeedConfig, SpeedPreset

MILESTONE_4_PRESET = (
    "Milestone 4: Notepad Demo (Write, Save to Approved Workspace, Reopen & Verify)"
)

DEFAULT_TASK_PRESETS: tuple[str, ...] = (
    MILESTONE_4_PRESET,
    "Open Notepad and summarize safety rules",
    "Verify approved workspace directory contents",
    "Custom Task",
)

BACKEND_OPTIONS: tuple[str, ...] = (
    "Local Demo / Fallback",
    "Gemini API",
    "OpenAI API",
    "Local Ollama",
)


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

        self.setWindowTitle("Window B - Autonomous Windows Desktop Controller")
        self.setGeometry(970, 40, 920, 960)

        self._build_ui()
        self._connect_signals()
        self._setup_shortcuts_and_polling()

        # Apply initial speed, backend, and task
        if initial_speed in {p.value for p in SpeedPreset}:
            self.speed_combo.setCurrentText(initial_speed)
        else:
            self.speed_combo.setCurrentText(SpeedPreset.Normal.value)
        self._on_speed_preset_changed(self.speed_combo.currentText())

        if initial_backend in BACKEND_OPTIONS:
            self.backend_combo.setCurrentText(initial_backend)
        self._refresh_backend_status()

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
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        # Task & Preset row
        task_group = QGroupBox("Task Configuration", self)
        task_layout = QVBoxLayout(task_group)

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

        # Speed & Backend row
        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel("Speed:", self))
        self.speed_combo = QComboBox(self)
        self.speed_combo.addItems([p.value for p in SpeedPreset])
        controls_row.addWidget(self.speed_combo)

        controls_row.addWidget(QLabel("Move (s):", self))
        self.duration_spin = QDoubleSpinBox(self)
        self.duration_spin.setRange(0.05, 10.0)
        self.duration_spin.setSingleStep(0.05)
        self.duration_spin.setDecimals(2)
        controls_row.addWidget(self.duration_spin)

        controls_row.addWidget(QLabel("Delay (s):", self))
        self.delay_spin = QDoubleSpinBox(self)
        self.delay_spin.setRange(0.0, 10.0)
        self.delay_spin.setSingleStep(0.05)
        self.delay_spin.setDecimals(2)
        controls_row.addWidget(self.delay_spin)

        controls_row.addWidget(QLabel("Planner:", self))
        self.backend_combo = QComboBox(self)
        self.backend_combo.addItems(list(BACKEND_OPTIONS))
        controls_row.addWidget(self.backend_combo)

        self.backend_status_label = QLabel("Ready (Local Demo)", self)
        controls_row.addWidget(self.backend_status_label)
        task_layout.addLayout(controls_row)

        root_layout.addWidget(task_group)

        # Safety & Control Bar
        safety_group = QGroupBox("Safety Gate & Operator Controls (F12 / Ctrl+Shift+Esc)", self)
        safety_layout = QVBoxLayout(safety_group)

        btn_row = QHBoxLayout()
        self.start_button = QPushButton("Start / Authorize", self)
        self.start_button.setStyleSheet("background-color: #1f7a3e; color: white; font-weight: bold; padding: 6px 12px;")
        self.pause_button = QPushButton("Pause", self)
        self.resume_button = QPushButton("Resume", self)
        self.takeover_button = QPushButton("Manual Takeover", self)
        self.emergency_stop_button = QPushButton("EMERGENCY STOP", self)
        self.emergency_stop_button.setStyleSheet(
            "background-color: #d91e18; color: white; font-weight: bold; padding: 6px 14px;"
        )
        self.reset_stop_button = QPushButton("Reset Stop", self)

        for btn in (
            self.start_button,
            self.pause_button,
            self.resume_button,
            self.takeover_button,
            self.emergency_stop_button,
            self.reset_stop_button,
        ):
            btn_row.addWidget(btn)
        safety_layout.addLayout(btn_row)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Idle (Start Authorization Required)", self)
        self.status_label.setStyleSheet("font-weight: bold;")
        self.current_action_label = QLabel("Action: None", self)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        status_row.addWidget(self.status_label, stretch=1)
        status_row.addWidget(self.current_action_label, stretch=1)
        status_row.addWidget(self.progress_bar, stretch=1)
        safety_layout.addLayout(status_row)

        root_layout.addWidget(safety_group)

        # Middle split: Live Screenshot + Approval Panel
        splitter = QSplitter(Qt.Orientation.Vertical, self)

        preview_container = QWidget(self)
        preview_layout = QHBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)

        screenshot_group = QGroupBox("Live Desktop Observation", self)
        screenshot_layout = QVBoxLayout(screenshot_group)
        self.screenshot_label = QLabel("No screenshot captured yet", self)
        self.screenshot_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.screenshot_label.setMinimumSize(QSize(420, 220))
        self.screenshot_label.setStyleSheet("background-color: #141821; color: #9aa7b8; border: 1px solid #334;")
        screenshot_layout.addWidget(self.screenshot_label)
        preview_layout.addWidget(screenshot_group, stretch=2)

        self.approval_group = QGroupBox("Policy & Risk Approval Gate", self)
        approval_layout = QVBoxLayout(self.approval_group)
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

        # Action History table
        history_group = QGroupBox("Action History", self)
        history_layout = QVBoxLayout(history_group)
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

        root_layout.addWidget(splitter, stretch=1)

    def _connect_signals(self) -> None:
        self.preset_task_combo.currentTextChanged.connect(self._on_preset_task_changed)
        self.speed_combo.currentTextChanged.connect(self._on_speed_preset_changed)
        self.duration_spin.valueChanged.connect(self._on_custom_spin_changed)
        self.delay_spin.valueChanged.connect(self._on_custom_spin_changed)
        self.backend_combo.currentTextChanged.connect(lambda _: self._refresh_backend_status())

        self.start_button.clicked.connect(self.on_start_clicked)
        self.pause_button.clicked.connect(self.on_pause_clicked)
        self.resume_button.clicked.connect(self.on_resume_clicked)
        self.takeover_button.clicked.connect(self.on_takeover_clicked)
        self.emergency_stop_button.clicked.connect(self.on_emergency_stop_clicked)
        self.reset_stop_button.clicked.connect(self.on_reset_stop_clicked)
        self.approve_button.clicked.connect(self.on_approve_clicked)
        self.deny_button.clicked.connect(self.on_deny_clicked)
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

        self._poll_timer: Optional[QTimer] = None
        if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1":
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(80)
            self._poll_timer.timeout.connect(self._poll_global_safety)
            self._poll_timer.start()

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

    def _refresh_backend_status(self) -> None:
        backend = self.backend_combo.currentText()
        if backend == "Local Demo / Fallback":
            self.backend_status_label.setText("Ready (Local Demo)")
        elif backend == "Gemini API":
            has_key = bool(os.environ.get("GEMINI_API_KEY", "").strip())
            self.backend_status_label.setText("Configured (Key Present)" if has_key else "Missing GEMINI_API_KEY")
        elif backend == "OpenAI API":
            has_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
            self.backend_status_label.setText("Configured (Key Present)" if has_key else "Missing OPENAI_API_KEY")
        elif backend == "Local Ollama":
            if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1":
                self.backend_status_label.setText("Ollama Check Skipped (Test Mode)")
            else:
                reachable = self._check_ollama_reachable()
                self.backend_status_label.setText("Ollama Ready (11434)" if reachable else "Ollama Unreachable")
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

    def set_progress(self, current: int, total: int, action_text: str) -> None:
        """Update progress bar and current action status text."""
        safe_total = max(int(total), 1)
        safe_current = max(0, min(int(current), safe_total))
        self.progress_bar.setRange(0, safe_total)
        self.progress_bar.setValue(safe_current)
        self.current_action_label.setText(action_text)

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
        self.status_label.setText("Awaiting User Approval")
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

            if self.safety.can_execute():
                x, y = self.driver.get_cursor_position()
                w, h = self.driver.get_screen_size()
                if self.safety.check_corner_failsafe(x, y, w, h):
                    self._after_emergency_stop(StopSource.corner_failsafe.value)
        except Exception:
            pass

    def _on_f12_shortcut(self) -> None:
        self._trigger_stop(StopSource.global_hotkey.value)

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
        self.append_history_row("emergency_stop", "halted", f"Source: {source}")
        self.emergency_stopped.emit(source)

    def on_start_clicked(self) -> None:
        try:
            self.safety.authorize_start()
        except EmergencyStopError as exc:
            self._refresh_safety_ui()
            self.append_history_row("start", "blocked", str(exc))
            return

        self.driver.speed_config = self.current_speed_config()
        self._refresh_safety_ui()
        task_text = self.task_input.text().strip() or self.preset_task_combo.currentText()
        backend_name = self.backend_combo.currentText()
        self.append_history_row("start", "authorized", f"Task: {task_text}")
        self.start_requested.emit(task_text, backend_name)

        # Optional M3 engine integration (only active once controller.engine exists)
        if os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1":
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
            self._progress_signal.emit(current, total, action_text)

        def _on_screenshot(image: Image.Image) -> None:
            self._screenshot_signal.emit(image)

        def _on_history(action_type: str, status: str, detail: str) -> None:
            self._history_signal.emit(action_type, status, detail)

        def _on_approval(action: DesktopAction, decision: PolicyDecision) -> bool:
            self._approval_event.clear()
            self._approval_signal.emit(
                decision.reason,
                decision.risk_category,
                action.description,
            )
            return self.wait_for_approval_decision()

        def _on_completion(result: ExecutionResult) -> None:
            self._completion_signal.emit(
                result.status,
                result.summary or result.error or "",
            )

        def _worker() -> None:
            try:
                engine = DesktopExecutionEngine(
                    driver=self.driver,
                    safety=self.safety,
                    store=self.store,
                    backend_name=backend_name,
                    approval_callback=_on_approval,
                    progress_callback=_on_progress,
                    screenshot_callback=_on_screenshot,
                    history_callback=_on_history,
                    completion_callback=_on_completion,
                )
                engine.run_task(task_text)
            except Exception as exc:
                self._history_signal.emit("engine", "error", str(exc))
                self._status_signal.emit(f"Engine Error: {exc}")

        self._engine_thread = threading.Thread(target=_worker, daemon=True)
        self._engine_thread.start()

    def on_pause_clicked(self) -> None:
        self.safety.pause()
        self._refresh_safety_ui()
        self.append_history_row("pause", "paused", "Operator paused execution")

    def on_resume_clicked(self) -> None:
        try:
            self.safety.resume()
        except (SafetyGateError, EmergencyStopError) as exc:
            self._refresh_safety_ui()
            self.append_history_row("resume", "blocked", str(exc))
            return
        self._refresh_safety_ui()
        self.append_history_row("resume", "running", "Operator resumed execution")

    def on_takeover_clicked(self) -> None:
        if not self.safety.manual_takeover:
            self.safety.enter_manual_takeover()
            self._refresh_safety_ui()
            self.append_history_row("manual_takeover", "active", "Operator took manual control")
        else:
            try:
                self.safety.exit_manual_takeover()
            except EmergencyStopError as exc:
                self._refresh_safety_ui()
                self.append_history_row("manual_takeover", "blocked", str(exc))
                return
            self._refresh_safety_ui()
            self.append_history_row("manual_takeover", "exited", "Returned control to automation")

    def on_emergency_stop_clicked(self) -> None:
        self._trigger_stop(StopSource.gui_button.value)

    def on_reset_stop_clicked(self) -> None:
        self.safety.reset_emergency_stop()
        self._refresh_safety_ui()
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
        self.append_history_row("approval", "approved", self.approval_info.get("reason", ""))
        self.approval_resolved.emit(True)

    def on_deny_clicked(self) -> None:
        self.approval_decision = False
        self.pending_approval = False
        self._approval_event.set()
        self.approve_button.setEnabled(False)
        self.deny_button.setEnabled(False)
        self.approval_group.setStyleSheet("")
        self.approval_details_label.setText("Denied by operator.")
        self._refresh_safety_ui()
        self.append_history_row("approval", "denied", self.approval_info.get("reason", ""))
        self.approval_resolved.emit(False)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self._approval_event.set()
        super().closeEvent(event)
