import sys
import os

__version__ = "1.3.0"  # single source of truth; read by pyproject.toml

# --- Code vs data -----------------------------------------------------------
# Data (exports, backups, logs) lives outside the code. The app runs with the data
# folder as its working directory, so relative paths like "Data/" land there.
# Override with UAKRON_APP_DATA_DIR (this app) or UAKRON_DATA_DIR (data root).
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.argv[0] = os.path.abspath(sys.argv[0])  # keeps "Restart" working after chdir

DATA_FOLDER_NAME = "Electrical Conductivity Meter"


def default_data_dir(home=None):
    """The lab's SyncThing folder when it exists on this computer, else Documents/Apera Meter Data."""
    home = home or os.path.expanduser("~")
    synced_root = os.path.join(home, "SyncThing", "UAkron Playground")
    if os.path.isdir(synced_root):
        return os.path.join(synced_root, DATA_FOLDER_NAME)
    return os.path.join(home, "Documents", "Apera Meter Data")


def resolve_data_dir(environ=None, home=None):
    environ = os.environ if environ is None else environ
    if environ.get("UAKRON_APP_DATA_DIR"):
        return environ["UAKRON_APP_DATA_DIR"]
    if environ.get("UAKRON_DATA_DIR"):
        return os.path.join(environ["UAKRON_DATA_DIR"], DATA_FOLDER_NAME)
    return default_data_dir(home)


DATA_DIR = resolve_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
os.chdir(DATA_DIR)
# Settings (config, secrets, ROIs) live in the app folder by default, gitignored.
# The "Settings Folder" option (settings_location.py) can point them at a synced
# folder instead, e.g. "Settings" inside the data folder.
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
import settings_location
import crash_log
import diagnostics
import single_instance

APP_NAME = "Apera Meter GUI"
APP_DIR = settings_location.app_folder(script_dir)
CONFIG_DIR = settings_location.resolve(APP_DIR, DATA_DIR)
SETTINGS_FILES = ["config.json", "global_rois.json"]

import serial
import serial.tools.list_ports
import threading
import time
import math
import requests
import datetime
import csv
import json
import openpyxl
from openpyxl.styles import Font, Alignment
import pandas as pd
import serial
import serial.tools.list_ports
import struct

# Import torch/ocr before QApplication is created to prevent OpenMP/MKL deadlocks on Windows
try:
    import torch  # noqa: F401 - imported early on purpose, see comment above
    from ocr_pipeline import MeterOCR  # noqa: F401
except ImportError:
    pass

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTabWidget,
    QSplitter,
    QScrollArea,
    QLabel,
    QPushButton,
    QCheckBox,
    QLineEdit,
    QComboBox,
    QTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
    QFileDialog,
    QInputDialog,
    QFormLayout,
    QFrame,
    QDialog,
    QGridLayout,
    QListWidget,
    QStackedWidget,
    QButtonGroup,
    QRadioButton,
    QSpinBox,
    QDoubleSpinBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont

import qdarktheme

import matplotlib

matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

scan_lock = threading.Lock()


class NumericTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        if isinstance(other, QTableWidgetItem):
            try:
                return float(self.text()) < float(other.text())
            except ValueError:
                return self.text() < other.text()
        return super().__lt__(other)


class WorkerSignals(QObject):
    update_tree = pyqtSignal(object, tuple)
    update_status = pyqtSignal(str, str)
    update_device_info = pyqtSignal(str, str)
    reset_manual_snap = pyqtSignal()


class CollapsibleBox(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        title = title.replace("&", "&&")  # "&" would otherwise become a keyboard-shortcut underline
        self.btn_toggle = QPushButton(f"▼ {title}")
        self.btn_toggle.setStyleSheet(
            "text-align: left; font-weight: bold; padding: 5px; background-color: transparent; border: none;"
        )
        self.btn_toggle.setCheckable(True)
        self.btn_toggle.setChecked(False)  # False = expanded
        self.btn_toggle.toggled.connect(self.on_toggle)
        self.layout.addWidget(self.btn_toggle)

        self.content_widget = QWidget()
        self.layout.addWidget(self.content_widget)

        self.title_text = title

    def setLayout(self, layout):
        layout.setContentsMargins(5, 5, 5, 5)
        self.content_widget.setLayout(layout)

    def on_toggle(self, checked):
        self.btn_toggle.setText(f"▶ {self.title_text}" if checked else f"▼ {self.title_text}")
        self.content_widget.setVisible(not checked)

    def set_collapsed(self, collapsed=True):
        self.btn_toggle.setChecked(collapsed)


class MeterController(QWidget):
    def __init__(self, meter_id=1, parent_app=None):
        super().__init__()
        self.meter_id = meter_id
        self.parent_app = parent_app
        self.app_controller = parent_app

        self.ser = None
        self.is_connected = False
        self.unsaved_changes = False
        self.is_recording = False
        self.received_timestamps = set()
        self.baseline_log_cond = None
        self.last_alert_time = 0
        self.alert_start_time = None

        self.plot_dates = []
        self.plot_conds = []
        self.plot_temps = []

        self.last_auto_dump_time = time.time()
        self.last_data_time = 0

        # Signals for thread-safe UI updates
        self.signals = WorkerSignals()
        self.signals.update_tree.connect(self.log_point_ui)
        self.signals.update_status.connect(self.set_status)
        self.signals.update_device_info.connect(self.set_device_info)
        self.signals.reset_manual_snap.connect(self._reset_manual_snap_ui)

        self.config_path = os.path.join(CONFIG_DIR, "config.json")

        if self.app_controller and not hasattr(self.app_controller, "shared_config"):
            self.app_controller.shared_config = {}
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path) as f:
                        self.app_controller.shared_config = json.load(f)
                except Exception:
                    pass

        self.config = self.app_controller.shared_config if self.app_controller else {}

        # Set defaults
        self.config.setdefault(
            f"default_com_port_{meter_id}", "COM4" if meter_id == 1 else "Auto-Detect Apera Meter"
        )
        self.config.setdefault(f"default_auto_dump_hrs_{meter_id}", 2.0)
        self.config.setdefault(f"default_save_location_{meter_id}", "")
        self.config.setdefault(f"meter_notes_{meter_id}", "")
        self.config.setdefault(f"keep_alive_{meter_id}", True)
        self.config.setdefault(f"auto_time_{meter_id}", True)
        self.config.setdefault(f"auto_cond_{meter_id}", True)
        self.config.setdefault(f"auto_temp_{meter_id}", True)
        self.config.setdefault(f"auto_dump_enabled_{meter_id}", False)
        self.config.setdefault(f"autosave_mode_{meter_id}", "Off")
        self.config.setdefault(f"autosave_min_{meter_id}", "5.0")
        self.config.setdefault(f"save_template_{meter_id}", f"Meter_<{meter_id}>_<Date>")

        self.columns = (
            "DateTime",
            "Duration (min)",
            "Mode",
            "Cond (uS/cm)",
            "Cond (mS/cm)",
            "Temp (°C)",
            "OCR Conf (%)",
        )
        self.setup_backup_path()
        self.init_ui()

        self.refresh_ports()
        target_com = self.config.get(f"default_com_port_{self.meter_id}", "Auto-Detect Apera Meter")
        idx = self.port_combo.findText(target_com)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)

        if target_com != "Auto-Detect Apera Meter":
            # Wait 2 seconds before connecting to avoid COM port locks
            QTimer.singleShot(2000, self.auto_connect_if_valid)

    def auto_connect_if_valid(self):
        if not hasattr(self, "serial_worker") or not self.serial_worker:
            self.toggle_connection()

    def log_event(self, msg):
        import datetime

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_msg = f"[{now_str}] {msg}"
        current_text = self.txt_system_log.toPlainText()
        if current_text:
            self.txt_system_log.setPlainText(full_msg + "\n" + current_text)
        else:
            self.txt_system_log.setPlainText(full_msg)

    def init_ui(self):
        main_layout = QVBoxLayout(self)

        self.top_btn_layout = QHBoxLayout()
        main_layout.addLayout(self.top_btn_layout)
        self.btn_sync = QPushButton("⏱️ Sync Meter Time")
        self.btn_sync.setEnabled(False)
        self.btn_sync.clicked.connect(self.sync_time)
        self.top_btn_layout.addWidget(self.btn_sync)

        self.btn_download = QPushButton("📥 Download Raw Memory")
        self.btn_download.setToolTip("Download saved memory records from the meter")
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self.download_memory)
        self.top_btn_layout.addWidget(self.btn_download)

        self.btn_export = QPushButton("💾 Save Log to Excel")
        self.btn_export.setToolTip("Export the current table data and metadata to an Excel workbook")
        self.btn_export.setStyleSheet("background-color: #27ae60; color: white;")
        self.btn_export.clicked.connect(lambda checked=False: self.save_excel(auto=False))
        self.top_btn_layout.addWidget(self.btn_export)

        self.btn_quick_save_top = QPushButton("⚡ Quick Save")
        self.btn_quick_save_top.setStyleSheet("background-color: #2980b9; color: white;")
        self.btn_quick_save_top.setToolTip("Instantly overwrite the target Auto-Save file without prompting.")
        self.btn_quick_save_top.clicked.connect(lambda checked=False: self.save_excel(auto=True))
        self.top_btn_layout.addWidget(self.btn_quick_save_top)

        self.btn_save_graph = QPushButton("🖼️ Save Graph PNG")
        self.btn_save_graph.clicked.connect(self.save_graph)
        self.top_btn_layout.addWidget(self.btn_save_graph)

        self.btn_recover = QPushButton("♻️ Recover Session")
        self.btn_recover.clicked.connect(self.prompt_session_recovery)
        self.top_btn_layout.addWidget(self.btn_recover)

        self.btn_clear = QPushButton("🗑️ Clear Data")
        self.btn_clear.setStyleSheet("background-color: #c0392b; color: white;")
        self.btn_clear.clicked.connect(self.clear_log)
        self.top_btn_layout.addWidget(self.btn_clear)

        self.top_btn_layout.addStretch()
        self.btn_manual_snap = QPushButton("📷 Manual Snap")
        self.btn_calibration_burst = QPushButton("🎯 Capture Calibration Burst")
        self.mode_combo = QComboBox()

        # We don't add the global app buttons here anymore to avoid the widget-parenting bug.
        btn_layout = QHBoxLayout()
        # You can add meter-specific buttons here if needed in the future
        btn_layout.addStretch()
        main_layout.addLayout(btn_layout)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(self.main_splitter)

        # Left Panel (Settings)
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(10, 10, 10, 10)

        lbl_title = QLabel(f"Meter {self.meter_id}")
        lbl_title.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        left_layout.addWidget(lbl_title)

        self.gb_rec = CollapsibleBox("Recording Settings")
        l_rec = QVBoxLayout()

        self.btn_record = QPushButton("▶️ Start Recording")
        self.btn_record.setStyleSheet("background-color: #2ecc71; color: white;")
        self.btn_record.clicked.connect(self.toggle_record)
        l_rec.addWidget(self.btn_record)

        lag_layout = QHBoxLayout()
        lag_layout.addWidget(QLabel("Hardware Lag (ms):"))
        self.spin_lag = QDoubleSpinBox()
        self.spin_lag.setRange(-2000.0, 2000.0)
        self.spin_lag.setDecimals(1)
        self.spin_lag.setValue(float(self.config.get(f"hardware_lag_ms_{self.meter_id}", 300.0)))
        self.spin_lag.valueChanged.connect(self._save_hardware_lag)
        lag_layout.addWidget(self.spin_lag)
        l_rec.addLayout(lag_layout)

        self.gb_rec.setLayout(l_rec)
        left_layout.addWidget(self.gb_rec)

        line1 = QFrame()
        line1.setFrameShape(QFrame.Shape.HLine)
        left_layout.addWidget(line1)

        self.gb_axis = CollapsibleBox("Axis Limits")
        l_axis = QVBoxLayout()

        self.auto_time_chk = QCheckBox("Auto Time")
        self.auto_time_chk.setChecked(self.config.get(f"auto_time_{self.meter_id}", True))
        self.auto_time_chk.stateChanged.connect(self._auto_chk_changed)
        l_axis.addWidget(self.auto_time_chk)

        self.auto_cond_chk = QCheckBox("Auto Cond")
        self.auto_cond_chk.setChecked(self.config.get(f"auto_cond_{self.meter_id}", True))
        self.auto_cond_chk.stateChanged.connect(self._auto_chk_changed)
        l_axis.addWidget(self.auto_cond_chk)

        self.auto_temp_chk = QCheckBox("Auto Temp")
        self.auto_temp_chk.setChecked(self.config.get(f"auto_temp_{self.meter_id}", True))
        self.auto_temp_chk.stateChanged.connect(self._auto_chk_changed)
        l_axis.addWidget(self.auto_temp_chk)

        self.auto_deriv_chk = QCheckBox("Auto Deriv")
        self.auto_deriv_chk.setChecked(self.config.get(f"auto_deriv_{self.meter_id}", True))
        self.auto_deriv_chk.stateChanged.connect(self._auto_chk_changed)
        l_axis.addWidget(self.auto_deriv_chk)

        form_layout = QFormLayout()
        self.ent_time_win = QLineEdit(str(self.config.get(f"time_win_{self.meter_id}", "10.0")))
        self.ent_cond_min = QLineEdit(str(self.config.get(f"cond_min_{self.meter_id}", "0")))
        self.ent_cond_max = QLineEdit(str(self.config.get(f"cond_max_{self.meter_id}", "5000")))
        self.ent_temp_min = QLineEdit(str(self.config.get(f"temp_min_{self.meter_id}", "15")))
        self.ent_temp_max = QLineEdit(str(self.config.get(f"temp_max_{self.meter_id}", "35")))
        self.ent_deriv_min = QLineEdit(str(self.config.get(f"deriv_min_{self.meter_id}", "-1.0")))
        self.ent_deriv_max = QLineEdit(str(self.config.get(f"deriv_max_{self.meter_id}", "1.0")))

        self.spin_deriv_smooth = QSpinBox()
        self.spin_deriv_smooth.setRange(3, 99)
        self.spin_deriv_smooth.setSingleStep(2)
        self.spin_deriv_smooth.setValue(int(self.config.get(f"deriv_smooth_{self.meter_id}", 5)))
        # Force odd values
        self.spin_deriv_smooth.valueChanged.connect(
            lambda v: self.spin_deriv_smooth.setValue(v if v % 2 != 0 else v + 1)
        )

        form_layout.addRow("Time Window (min):", self.ent_time_win)
        form_layout.addRow("Cond Min:", self.ent_cond_min)
        form_layout.addRow("Cond Max:", self.ent_cond_max)
        form_layout.addRow("Temp Min (°C):", self.ent_temp_min)
        form_layout.addRow("Temp Max (°C):", self.ent_temp_max)
        form_layout.addRow("Deriv Min:", self.ent_deriv_min)
        form_layout.addRow("Deriv Max:", self.ent_deriv_max)
        form_layout.addRow("Deriv Smooth (Odd int):", self.spin_deriv_smooth)

        self.xaxis_combo = QComboBox()
        self.xaxis_combo.addItems(["X-Axis: DateTime", "X-Axis: Duration (min)"])
        self.xaxis_combo.setCurrentText(self.config.get(f"xaxis_{self.meter_id}", "X-Axis: DateTime"))
        self.xaxis_combo.currentTextChanged.connect(lambda _: self.apply_limits())
        form_layout.addRow(self.xaxis_combo)  # the items say what they are; a label clipped them

        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["Graph Unit: uS/cm", "Graph Unit: mS/cm"])
        self.unit_combo.setCurrentText(self.config.get(f"graph_unit_{self.meter_id}", "Graph Unit: uS/cm"))
        self.unit_combo.currentTextChanged.connect(self.on_unit_changed)
        form_layout.addRow(self.unit_combo)

        l_axis.addLayout(form_layout)

        self.btn_apply_lims = QPushButton("📊 Apply Limits")
        self.btn_apply_lims.clicked.connect(self.apply_limits)
        l_axis.addWidget(self.btn_apply_lims)

        self.gb_axis.setLayout(l_axis)
        left_layout.addWidget(self.gb_axis)

        self.gb_syslog = CollapsibleBox("System Log Events")
        l_syslog = QVBoxLayout()
        self.txt_system_log = QTextEdit()
        self.txt_system_log.setMinimumHeight(150)
        self.txt_system_log.setReadOnly(True)
        self.txt_system_log.setStyleSheet(
            "background-color: #1e1e1e; color: #00ff00; font-family: Consolas; font-size: 11px;"
        )
        l_syslog.addWidget(self.txt_system_log)
        self.gb_syslog.setLayout(l_syslog)
        left_layout.addWidget(self.gb_syslog)

        left_layout.addStretch()

        left_scroll = QScrollArea()
        left_scroll.setWidget(left_widget)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )  # side panels scroll vertically only

        # Middle Panel (Plot & Table)
        self.mid_splitter = QSplitter(Qt.Orientation.Vertical)

        self.fig, (self.ax, self.ax_temp) = plt.subplots(
            2, 1, figsize=(5, 5), gridspec_kw={"height_ratios": [3, 1]}
        )
        self.ax.set_title(f"Live Conductivity (Meter {self.meter_id})")
        self.ax.set_ylabel("Conductivity", color="#3498db")
        self.ax.grid(True, linestyle="--", alpha=0.3)
        self.ax2 = self.ax.twinx()
        self.ax2.set_ylabel("d(log C)/dt", color="#9b59b6")

        self.ax_temp.set_xlabel("Time")
        self.ax_temp.set_ylabel("Temp (°C)", color="#e74c3c")
        self.ax_temp.grid(True, linestyle="--", alpha=0.3)
        self.fig.tight_layout()

        self.canvas = FigureCanvasQTAgg(self.fig)
        self.mid_splitter.addWidget(self.canvas)

        self.table = QTableWidget()
        self.table.setColumnCount(len(self.columns))
        self.table.setHorizontalHeaderLabels(self.columns)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 160)  # room for "YYYY-MM-DD HH:MM:SS"; saved widths still apply
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.mid_splitter.addWidget(self.table)

        # Right Panel (Connection & Controls)
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(10, 10, 10, 10)

        self.gb_connection = CollapsibleBox("Connection Details")
        l_conn = QVBoxLayout()

        self.sn_label = QLabel("SN: Unknown")
        self.model_label = QLabel("Model: Unknown")
        self.sn_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        self.model_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        l_conn.addWidget(self.sn_label)
        l_conn.addWidget(self.model_label)

        self.port_combo = QComboBox()
        l_conn.addWidget(self.port_combo)

        self.btn_refresh = QPushButton("🔄 Refresh Ports")
        self.btn_refresh.clicked.connect(self.refresh_ports)
        l_conn.addWidget(self.btn_refresh)

        self.btn_connect = QPushButton("🔌 Connect")
        self.btn_connect.clicked.connect(self.toggle_connection)
        l_conn.addWidget(self.btn_connect)

        self.gb_connection.setLayout(l_conn)
        right_layout.addWidget(self.gb_connection)

        # Metadata Tokens
        self.gb_tokens = CollapsibleBox("Metadata Tokens")
        self.tokens_outer_layout = QVBoxLayout()
        self.l_tokens = QFormLayout()
        self.tokens_outer_layout.addLayout(self.l_tokens)

        self.btn_add_tab_token = QPushButton("+ Add Custom Token")
        self.btn_add_tab_token.clicked.connect(self.request_add_custom_token)
        self.tokens_outer_layout.addWidget(self.btn_add_tab_token)
        self.token_inputs = {}
        self.gb_tokens.setLayout(self.tokens_outer_layout)
        right_layout.addWidget(self.gb_tokens)

        # --- Data Recording & Export ---
        gb_save = CollapsibleBox("Data Recording & Export")
        l_save = QFormLayout()

        self.chk_use_global_template = QCheckBox("Use Global Save Settings")
        self.chk_use_global_template.setChecked(self.config.get(f"use_global_{self.meter_id}", True))
        self.chk_use_global_template.toggled.connect(self.toggle_global_template)
        l_save.addRow(self.chk_use_global_template)

        self.ent_save_template = QLineEdit(self.config.get(f"save_template_{self.meter_id}", ""))
        self.ent_save_template.setMinimumWidth(50)
        l_save.addRow("Filename Template:", self.ent_save_template)

        self.combo_autosave = QComboBox()
        self.combo_autosave.addItems(["Off", "Every X Minutes"])
        self.combo_autosave.setCurrentText(self.config.get(f"autosave_mode_{self.meter_id}", "Off"))
        l_save.addRow("Auto-Save:", self.combo_autosave)

        self.ent_autosave_min = QLineEdit(self.config.get(f"autosave_min_{self.meter_id}", "5.0"))
        self.ent_autosave_min.setPlaceholderText("Minutes")
        self.ent_autosave_min.setMinimumWidth(50)
        l_save.addRow("Interval (min):", self.ent_autosave_min)

        self.combo_autostop = QComboBox()
        self.combo_autostop.addItems(["Off", "Stop Meter", "Stop OCR Log", "Stop Both"])
        saved_mode = self.config.get(f"autostop_mode_{self.meter_id}", "Off")
        if saved_mode == "Stop Camera":
            saved_mode = "Stop OCR Log"
        self.combo_autostop.setCurrentText(saved_mode)
        l_save.addRow("Auto-Stop:", self.combo_autostop)

        self.ent_autostop_min = QLineEdit(self.config.get(f"autostop_min_{self.meter_id}", "60.0"))
        self.ent_autostop_min.setPlaceholderText("Minutes")
        self.ent_autostop_min.setMinimumWidth(50)
        l_save.addRow("Stop After (min):", self.ent_autostop_min)

        btn_layout = QHBoxLayout()
        btn_export = QPushButton("💾 Export As...")
        btn_export.setStyleSheet("background-color: #27ae60; color: white;")
        btn_export.clicked.connect(lambda checked=False: self.save_excel(auto=False))

        btn_quick_save = QPushButton("⚡ Quick Save")
        btn_quick_save.setStyleSheet("background-color: #2980b9; color: white;")
        btn_quick_save.setToolTip("Instantly overwrite the target Auto-Save file without prompting.")
        btn_quick_save.clicked.connect(lambda checked=False: self.save_excel(auto=True))

        btn_layout.addWidget(btn_export)
        btn_layout.addWidget(btn_quick_save)
        l_save.addRow(btn_layout)

        self.combo_autosave.currentIndexChanged.connect(self.toggle_autosave_interval)
        self.combo_autosave.currentIndexChanged.connect(self._save_autosave_config)
        self.ent_autosave_min.textChanged.connect(self._save_autosave_config)

        self.combo_autostop.currentIndexChanged.connect(self.toggle_autosave_interval)
        self.combo_autostop.currentIndexChanged.connect(self._save_autosave_config)
        self.ent_autostop_min.textChanged.connect(self._save_autosave_config)

        self.ent_save_template.textChanged.connect(self._save_autosave_config)
        self.toggle_autosave_interval()

        gb_save.setLayout(l_save)
        right_layout.addWidget(gb_save)

        self.refresh_tokens()
        self.toggle_global_template()

        # Status label removed, using global status bar

        line3 = QFrame()
        line3.setFrameShape(QFrame.Shape.HLine)
        right_layout.addWidget(line3)

        lbl_notes = QLabel("Experiment Notes")
        lbl_notes.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        right_layout.addWidget(lbl_notes)
        self.txt_notes = QTextEdit()
        self.txt_notes.setMinimumHeight(200)
        self.txt_notes.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.txt_notes.setText(self.config.get(f"meter_notes_{self.meter_id}", ""))
        self.txt_notes.textChanged.connect(self.save_notes)
        right_layout.addWidget(self.txt_notes)

        right_layout.addStretch()

        right_scroll = QScrollArea()
        right_scroll.setWidget(right_widget)
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        left_scroll.setMinimumWidth(270)
        right_scroll.setMinimumWidth(360)

        # Add to main splitter
        self.main_splitter.addWidget(left_scroll)
        self.main_splitter.addWidget(self.mid_splitter)
        self.main_splitter.addWidget(right_scroll)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 3)
        self.main_splitter.setStretchFactor(2, 1)

        self.update_button_styles(self.config.get("button_style", "Icon + Text"))

    def load_config(self):
        # We handle loading during initialization in the shared config logic now.
        pass

    def save_config(self):
        try:
            with open(self.config_path, "w") as f:
                json.dump(self.config, f, indent=4)
        except Exception:
            pass

    def save_notes(self):
        self.config[f"meter_notes_{self.meter_id}"] = self.txt_notes.toPlainText().strip()
        self.save_config()

    def get_ports(self):
        ports = serial.tools.list_ports.comports()
        return ["Auto-Detect Apera Meter"] + [p.device for p in ports]

    def refresh_ports(self, checked=False):
        ports = self.get_ports()
        self.port_combo.clear()
        self.port_combo.addItems(ports)

    def _save_autosave_config(self):
        self.config[f"autosave_mode_{self.meter_id}"] = self.combo_autosave.currentText()
        self.config[f"autosave_min_{self.meter_id}"] = self.ent_autosave_min.text()
        self.config[f"autostop_mode_{self.meter_id}"] = self.combo_autostop.currentText()
        self.config[f"autostop_min_{self.meter_id}"] = self.ent_autostop_min.text()
        self.config[f"save_template_{self.meter_id}"] = self.ent_save_template.text()
        self.config[f"save_on_pause_{self.meter_id}"] = getattr(
            self, "chk_save_on_pause", QCheckBox()
        ).isChecked()
        self.save_config()

    def toggle_autosave_interval(self):
        self.ent_autosave_min.setEnabled(self.combo_autosave.currentText() == "Every X Minutes")
        self.ent_autostop_min.setEnabled(self.combo_autostop.currentText() != "Off")

    def refresh_tokens(self):
        while self.l_tokens.rowCount() > 0:
            self.l_tokens.removeRow(0)

        old_values = {k: v.text() for k, v in getattr(self, "token_inputs", {}).items()}
        self.token_inputs = {}

        default_tokens = [
            {"name": "Column #/Name", "value": ""},
            {"name": "Material", "value": ""},
            {"name": "Flow rate", "value": ""},
            {"name": "Bed length", "value": ""},
            {"name": "Run #", "value": ""},
            {"name": "(De)Sorption", "value": ""},
        ]

        global_tokens = getattr(self, "app_controller", None) and self.app_controller.shared_config.get(
            "global_tokens", None
        )
        if not global_tokens:
            global_tokens = default_tokens
        existing_names = [t.get("name") for t in global_tokens if t.get("name")]
        if "Column Name" in existing_names:
            for t in global_tokens:
                if t.get("name") == "Column Name":
                    t["name"] = "Column #/Name"
        if "(De)Sorption" not in existing_names:
            global_tokens.append({"name": "(De)Sorption", "value": ""})

        for t in global_tokens:
            name = t.get("name", "")
            val = t.get("value", "")
            if name:
                saved_val = self.config.get(f"token_{name}_{self.meter_id}", val)
                ent = QLineEdit(old_values.get(name, saved_val))
                ent.textChanged.connect(lambda text, n=name: self.save_token(n, text))
                self.l_tokens.addRow(name + ":", ent)
                self.token_inputs[name] = ent

        # load saved custom tokens on startup if old_values is empty (initial load)
        if not old_values:
            saved_customs = self.config.get(f"custom_tokens_{self.meter_id}", [])
            for name in saved_customs:
                if name not in self.token_inputs:
                    old_values[name] = self.config.get(f"token_{name}_{self.meter_id}", "")

        # restore custom tokens
        for name, val in old_values.items():
            if name not in self.token_inputs:
                self.add_custom_token_ui(name, val)

    def request_add_custom_token(self, checked=False):
        name, ok = QInputDialog.getText(self, "Add Custom Token", "Token Name:")
        if ok and name:
            name = name.strip()
            if name in self.token_inputs:
                QMessageBox.warning(self, "Warning", "Token already exists!")
                return
            self.add_custom_token_ui(name, "")
            self.save_custom_tokens()

    def add_custom_token_ui(self, name, value):
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)

        saved_val = self.config.get(f"token_{name}_{self.meter_id}", value)
        if value:
            saved_val = value  # prefer in-memory value if passed
        ent = QLineEdit(saved_val)
        ent.setMinimumWidth(50)
        ent.textChanged.connect(lambda text, n=name: self.save_token(n, text))
        btn_del = QPushButton("X")
        btn_del.setFixedWidth(25)
        btn_del.setStyleSheet("background-color: #e74c3c; color: white;")
        btn_del.clicked.connect(lambda _, n=name: self.remove_custom_token(n))

        row_layout.addWidget(ent)
        row_layout.addWidget(btn_del)

        self.l_tokens.addRow(name + ":", row_widget)
        self.token_inputs[name] = ent

    def save_token(self, name, text):
        self.config[f"token_{name}_{self.meter_id}"] = text
        self.save_config()

    def refresh_local_token_buttons(self):
        while self.local_token_grid.count():
            item = self.local_token_grid.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().setParent(None)
                item.widget().deleteLater()

        base_tokens = ["Date", "Time", "Start Date", "Start Time", "Meter ID"]
        all_tokens = base_tokens.copy()

        global_tokens = getattr(self, "app_controller", None) and self.app_controller.shared_config.get(
            "global_tokens", None
        )
        if not global_tokens:
            global_tokens = [
                {"name": "Column #/Name"},
                {"name": "Material"},
                {"name": "Flow rate"},
                {"name": "Bed length"},
                {"name": "Run #"},
                {"name": "(De)Sorption"},
            ]
        for t in global_tokens:
            n = t.get("name")
            if n and n not in all_tokens:
                all_tokens.append(n)

        for name in getattr(self, "token_inputs", {}):
            if name not in all_tokens:
                all_tokens.append(name)

        row = 0
        col = 0
        for tok in all_tokens:
            btn = QPushButton(f"<{tok}>")
            btn.clicked.connect(lambda checked, t=tok: self.ent_save_template.insert(f"<{t}>"))
            self.local_token_grid.addWidget(btn, row, col)
            col += 1
            if col > 3:
                col = 0
                row += 1

    def save_custom_tokens(self):
        customs = []
        default_names = ["Column Name", "Material", "Flow rate", "Bed length", "Run #"]
        global_tokens = getattr(self, "app_controller", None) and self.app_controller.shared_config.get(
            "global_tokens", None
        )
        if global_tokens:
            default_names = [t.get("name", "") for t in global_tokens if t.get("name")]

        if hasattr(self, "token_inputs"):
            for name in self.token_inputs:
                if name not in default_names:
                    customs.append(name)
        self.config[f"custom_tokens_{self.meter_id}"] = customs
        self.save_config()

    def remove_custom_token(self, name):
        if name in self.token_inputs:
            self.token_inputs.pop(name)
            self.save_custom_tokens()
            self.refresh_tokens()

    def toggle_global_template(self):
        use_global = self.chk_use_global_template.isChecked()
        self.ent_save_template.setEnabled(not use_global)
        if getattr(self, "app_controller", None):
            self.app_controller.shared_config[f"use_global_{self.meter_id}"] = use_global
            self.save_config()
            if use_global:
                global_template = self.app_controller.shared_config.get(
                    "global_filename_template", "<Meter ID>_<Date>_<Time>"
                )
                self.ent_save_template.setText(global_template)

    def get_resolved_string(self, template):
        import datetime
        import re

        s = template
        if hasattr(self, "token_inputs"):
            for name, ent in self.token_inputs.items():
                s = s.replace(f"<{name}>", ent.text())
        s = s.replace("<Meter ID>", str(self.meter_id))

        now = datetime.datetime.now()
        start_dt = getattr(self, "session_start_time", None) or now

        s = s.replace("<Start Date>", start_dt.strftime("%Y-%m-%d"))
        s = s.replace("<Start Time>", start_dt.strftime("%H%M%S"))
        s = s.replace("<Date>", now.strftime("%Y-%m-%d"))
        s = s.replace("<Time>", now.strftime("%H%M%S"))

        # Strip remaining unresolved tokens
        s = re.sub(r"<[^>]+>", "", s)
        # Sanitize invalid Windows filename characters
        s = re.sub(r'[<>:"/\\|?*]', "_", s)
        return s

    def toggle_record(self, checked=False):

        if not self.is_recording:
            self.is_recording = True
            self.session_start_time = None
            self.last_autosave_min = 0.0
            self.btn_record.setText("⏸ Pause Recording")
            self.btn_record.setStyleSheet("background-color: #e74c3c; color: white;")
            self.set_status("Recording...", "gray")
            self.unsaved_changes = True
            self.log_event("🔴 Recording Session Started.")
            if hasattr(self, "parent_app") and hasattr(self.parent_app, "_on_record_toggled"):
                self.parent_app._on_record_toggled()
        else:
            self.is_recording = False
            self.btn_record.setText("▶ Start Recording")
            self.btn_record.setStyleSheet("background-color: #2ecc71; color: white;")
            self.set_status("Paused", "gray")
            self.log_event("⏸ Recording Session Paused.")

            if self.combo_autosave.currentText() == "Every X Minutes":
                self.save_excel(auto=True, is_background_autosave=True)
                self.log_event("[Auto-Save] Data saved to directory automatically upon pause.")
            self.ax.set_title(f"Live (Not Recording): Conductivity (Meter {self.meter_id})")
        self.canvas.draw_idle()

        # Sync with global checkboxes
        if hasattr(self, "parent_app"):
            if self.meter_id == 1 and hasattr(self.parent_app, "chk_record_m1"):
                self.parent_app.chk_record_m1.setChecked(self.is_recording)
                self.parent_app.chk_record_m1.setStyleSheet(
                    "font-weight: bold; margin-left: 10px; "
                    f"color: {'#2ecc71' if self.is_recording else '#e74c3c'};"
                )
            elif self.meter_id == 2 and hasattr(self.parent_app, "chk_record_m2"):
                self.parent_app.chk_record_m2.setChecked(self.is_recording)
                self.parent_app.chk_record_m2.setStyleSheet(
                    "font-weight: bold; margin-left: 10px; "
                    f"color: {'#2ecc71' if self.is_recording else '#e74c3c'};"
                )

    def _save_hardware_lag(self, val):
        self.config[f"hardware_lag_ms_{self.meter_id}"] = float(val)
        self.save_config()

    def _auto_chk_changed(self):
        try:
            self.config[f"auto_time_{self.meter_id}"] = self.auto_time_chk.isChecked()
            self.config[f"auto_cond_{self.meter_id}"] = self.auto_cond_chk.isChecked()
            self.config[f"auto_temp_{self.meter_id}"] = self.auto_temp_chk.isChecked()
            self.config[f"auto_deriv_{self.meter_id}"] = self.auto_deriv_chk.isChecked()
            self.save_config()
        except Exception:
            pass
        self.plot_data()

    def on_unit_changed(self, new_text):
        old_unit = self.config.get(f"graph_unit_{self.meter_id}", "Graph Unit: uS/cm")
        if old_unit != new_text:
            try:
                cmin = float(self.ent_cond_min.text())
                cmax = float(self.ent_cond_max.text())
                if new_text == "Graph Unit: uS/cm" and old_unit == "Graph Unit: mS/cm":
                    self.ent_cond_min.setText(str(cmin * 1000.0))
                    self.ent_cond_max.setText(str(cmax * 1000.0))
                elif new_text == "Graph Unit: mS/cm" and old_unit == "Graph Unit: uS/cm":
                    self.ent_cond_min.setText(str(cmin / 1000.0))
                    self.ent_cond_max.setText(str(cmax / 1000.0))
            except Exception:
                pass
            self.config[f"graph_unit_{self.meter_id}"] = new_text
            self.apply_limits()

    def apply_limits(self, checked=False):
        try:
            self.config[f"time_win_{self.meter_id}"] = self.ent_time_win.text()
            self.config[f"cond_min_{self.meter_id}"] = self.ent_cond_min.text()
            self.config[f"cond_max_{self.meter_id}"] = self.ent_cond_max.text()
            self.config[f"temp_min_{self.meter_id}"] = self.ent_temp_min.text()
            self.config[f"temp_max_{self.meter_id}"] = self.ent_temp_max.text()
            self.config[f"deriv_min_{self.meter_id}"] = self.ent_deriv_min.text()
            self.config[f"deriv_max_{self.meter_id}"] = self.ent_deriv_max.text()
            self.config[f"deriv_smooth_{self.meter_id}"] = self.spin_deriv_smooth.value()
            self.config[f"xaxis_{self.meter_id}"] = self.xaxis_combo.currentText()
            self.config[f"graph_unit_{self.meter_id}"] = self.unit_combo.currentText()
            self.config[f"auto_time_{self.meter_id}"] = self.auto_time_chk.isChecked()
            self.config[f"auto_cond_{self.meter_id}"] = self.auto_cond_chk.isChecked()
            self.config[f"auto_temp_{self.meter_id}"] = self.auto_temp_chk.isChecked()
            self.config[f"auto_deriv_{self.meter_id}"] = self.auto_deriv_chk.isChecked()
            self.save_config()
        except Exception:
            pass

        self.plot_data()

    def save_graph(self, checked=False):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save Graph", "", "PNG (*.png)")
        if filepath:
            try:
                self.fig.savefig(filepath, dpi=300, bbox_inches="tight")
                QMessageBox.information(self, "Success", "Graph saved!")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def update_button_styles(self, style):
        if style == "Icon Only":
            self.btn_sync.setText("⏱️")
            self.btn_download.setText("📥")
            self.btn_export.setText("💾")
            self.btn_save_graph.setText("🖼️")
            self.btn_recover.setText("♻️")
            self.btn_clear.setText("🗑️")
        else:
            self.btn_sync.setText("⏱️ Sync Meter Time")
            self.btn_download.setText("📥 Download Raw Memory")
            self.btn_export.setText("💾 Save Log to Excel")
            self.btn_save_graph.setText("🖼️ Save Graph PNG")
            self.btn_recover.setText("♻️ Recover Session")
            self.btn_clear.setText("🗑️ Clear Data")

    def clear_log(self, init_file=False):
        self.table.setRowCount(0)
        self.last_auto_filepath = None
        self.received_timestamps.clear()
        self.plot_dates.clear()
        self.plot_conds.clear()
        self.plot_temps.clear()
        self.session_start_time = None
        self.last_auto_dump_time = time.time()
        self.plot_data()

    def setup_backup_path(self):
        backup_dir = os.path.join(os.getcwd(), "Data Backups")
        os.makedirs(backup_dir, exist_ok=True)
        self.backup_path = os.path.join(backup_dir, f"Apera_Meter{self.meter_id}_Backup.csv")

    def scan_backup_sessions(self, gap_minutes=30.0):
        if not os.path.exists(self.backup_path):
            return []
        sessions = []
        try:
            with open(self.backup_path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None)  # skip the header row
                rows = list(reader)

            current_session = None
            last_ts_obj = None
            import datetime

            for idx, row in enumerate(rows):
                if len(row) >= 6:
                    try:
                        ts_str = row[0]
                        ts_obj = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")

                        is_new_session = False
                        if current_session is None:
                            is_new_session = True
                        elif last_ts_obj is not None:
                            diff_min = (ts_obj - last_ts_obj).total_seconds() / 60.0
                            if diff_min >= gap_minutes:
                                is_new_session = True

                        if is_new_session:
                            if current_session:
                                current_session["end_idx"] = idx - 1
                                sessions.append(current_session)
                            current_session = {
                                "start_idx": idx,
                                "start_ts": ts_str,
                                "end_ts": ts_str,
                                "points": 0,
                            }

                        current_session["end_ts"] = ts_str
                        current_session["points"] += 1
                        last_ts_obj = ts_obj
                    except Exception:
                        pass

            if current_session:
                current_session["end_idx"] = len(rows) - 1
                sessions.append(current_session)
        except Exception:
            pass
        return sessions

    def prompt_session_recovery(self, checked=False):
        dialog = QDialog(self)
        dialog.setWindowTitle("Restore Session")
        dialog.setMinimumSize(450, 350)
        layout = QVBoxLayout(dialog)

        control_layout = QHBoxLayout()
        control_layout.addWidget(QLabel("Time Gap to Split Sessions (minutes):"))
        spin_gap = QDoubleSpinBox()
        spin_gap.setRange(1.0, 1440.0)
        spin_gap.setValue(float(self.config.get(f"recovery_gap_{self.meter_id}", 30.0)))
        control_layout.addWidget(spin_gap)

        btn_apply_gap = QPushButton("Apply")
        control_layout.addWidget(btn_apply_gap)

        layout.addLayout(control_layout)

        layout.addWidget(
            QLabel("Multiple recording sessions were found. Please select which one to restore:")
        )

        scroll = QScrollArea()
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_widget.setLayout(scroll_layout)
        scroll.setWidget(scroll_widget)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll)

        btn_layout = QHBoxLayout()
        btn_restore = QPushButton("♻️ Restore Selected")
        btn_delete = QPushButton("🗑️ Delete Selected")
        btn_ignore = QPushButton("Cancel")
        btn_layout.addWidget(btn_restore)
        btn_layout.addWidget(btn_delete)
        btn_layout.addWidget(btn_ignore)
        layout.addLayout(btn_layout)

        sessions = []
        bg = QButtonGroup(dialog)

        def refresh_sessions():
            nonlocal sessions
            for i in reversed(range(scroll_layout.count())):
                widget = scroll_layout.itemAt(i).widget()
                if widget is not None:
                    widget.deleteLater()

            # Recreate button group to clear old references
            for btn in bg.buttons():
                bg.removeButton(btn)

            gap = spin_gap.value()
            self.config[f"recovery_gap_{self.meter_id}"] = gap
            self.save_config()
            sessions = self.scan_backup_sessions(gap_minutes=gap)

            if not sessions:
                scroll_layout.addWidget(QLabel("No recovery sessions found with current settings."))
                btn_restore.setEnabled(False)
                btn_delete.setEnabled(False)
                return

            btn_restore.setEnabled(True)
            btn_delete.setEnabled(True)

            for i, s in enumerate(sessions):
                rb = QRadioButton(f"Session {i + 1} | {s['start_ts']} -> {s['end_ts']} | {s['points']} pts")
                if i == len(sessions) - 1:
                    rb.setChecked(True)
                bg.addButton(rb, i)
                scroll_layout.addWidget(rb)

        btn_apply_gap.clicked.connect(lambda checked=False: refresh_sessions())
        refresh_sessions()

        if not sessions:
            QMessageBox.information(self, "Recovery", "No recovery sessions found.")
            return

        def do_restore(checked=False):
            idx = bg.checkedId()
            if idx >= 0:
                s = sessions[idx]
                try:
                    with open(self.backup_path, newline="", encoding="utf-8") as f:
                        reader = csv.reader(f)
                        next(reader, None)  # skip the header row
                        rows = list(reader)

                    self.clear_log(init_file=False)
                    self.table.setSortingEnabled(False)

                    for r_idx in range(s["start_idx"], s["end_idx"] + 1):
                        row = rows[r_idx]
                        if len(row) == 6:
                            dur_min = "0.00"
                            try:
                                dur_min = f"{float(row[1]) * 60:.2f}"
                            except Exception:
                                pass
                            row = [row[0], dur_min, "Cond", row[2], row[3], row[4], row[5]]

                        if len(row) >= 7:
                            self.received_timestamps.add(row[0])
                            r = self.table.rowCount()
                            self.table.insertRow(r)
                            for col, val in enumerate(row):
                                self.table.setItem(r, col, NumericTableWidgetItem(str(val)))

                            try:
                                dt = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                                if not getattr(self, "session_start_time", None):
                                    self.session_start_time = dt
                                self.plot_dates.append(dt)
                                try:
                                    self.plot_conds.append(float(row[4]))
                                except ValueError:
                                    self.plot_conds.append(0.0)

                                try:
                                    self.plot_temps.append(float(row[6]))
                                except ValueError:
                                    self.plot_temps.append(0.0)
                            except Exception:
                                pass

                    self.table.setSortingEnabled(True)
                    self.plot_data()
                    QMessageBox.information(self, "Recover", f"Recovered {s['points']} points.")
                except serial.SerialException as e:
                    self.parent_app.update_meter_status(self.meter_id, False)
                    self.is_connected = False
                    QMessageBox.critical(self, "Error", f"Recovery failed: {e}")
            dialog.accept()

        def do_delete(checked=False):
            idx = bg.checkedId()
            if idx >= 0:
                s = sessions[idx]
                try:
                    with open(self.backup_path, newline="", encoding="utf-8") as f:
                        reader = csv.reader(f)
                        header = next(reader, None)
                        rows = list(reader)

                    del rows[s["start_idx"] : s["end_idx"] + 1]

                    with open(self.backup_path, "w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        if header:
                            writer.writerow(header)
                        writer.writerows(rows)

                    QMessageBox.information(self, "Deleted", "Session permanently deleted from backup.")
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Deletion failed: {e}")
            dialog.accept()

        btn_restore.clicked.connect(do_restore)
        btn_delete.clicked.connect(do_delete)
        btn_ignore.clicked.connect(dialog.reject)
        dialog.exec()

    def toggle_connection(self, checked=False):
        if self.is_connected:
            self.disconnect_meter()
        else:
            self.connect_meter()

    def find_apera_meter(self):
        with scan_lock:
            ports = [p.device for p in serial.tools.list_ports.comports()]
            used_ports = self.app_controller.get_used_ports() if self.app_controller else []
            for p in ports:
                if p in used_ports:
                    continue
                try:
                    ts = serial.Serial(p, 115200, timeout=0.5)
                    ts.setDTR(False)
                    ts.setRTS(False)
                    ts.write(b"&OK#\r\n")
                    time.sleep(0.3)
                    resp = ts.read(ts.in_waiting)
                    ts.close()
                    time.sleep(1.0)  # Let Windows fully release the COM port handle
                    if b"<SH" in resp or b"<SL" in resp or b"#>" in resp:
                        return p
                except Exception:
                    pass
            return None

    def connect_meter(self):
        port = self.port_combo.currentText()
        if port == "Auto-Detect Apera Meter":
            if not getattr(self, "is_scanning", False):
                self.set_status("Scanning COM ports...", "gray")
                self.btn_connect.setEnabled(False)
                self.is_scanning = True

                self.scan_id = getattr(self, "scan_id", 0) + 1
            current_id = self.scan_id

            def timeout_scan():
                if getattr(self, "scan_id", None) == current_id:
                    self.ser = None
                    self.parent_app.update_meter_status(self.meter_id, False)
                    self.btn_connect.setText("Connect")
                    self.btn_connect.setEnabled(True)
                    self.set_status("Auto-Detect Timeout", "gray")
                    self.is_scanning = False

            QTimer.singleShot(7000, timeout_scan)

            threading.Thread(target=self._scan_thread_task, args=(current_id,), daemon=True).start()
            return

        self.scan_id = None
        self.btn_connect.setEnabled(False)
        self.btn_connect.setText("⏳ Connecting...")
        # Add a 1.5s delay to ensure previous handles are completely freed
        QTimer.singleShot(1500, lambda: self._finish_connect(port))

    def _scan_thread_task(self, scan_id=None):
        found_port = self.find_apera_meter()
        if found_port:
            time.sleep(1.5)  # Extra buffer time after scanner closes the port
        QTimer.singleShot(0, lambda: self._finish_connect(found_port, scan_id))

    def _finish_connect(self, port, scan_id=None):
        if scan_id is not None and getattr(self, "scan_id", None) != scan_id:
            return
        self.is_scanning = False
        self.btn_connect.setEnabled(True)
        if not port:
            self.set_status("Auto-Detect Failed", "red")
            QMessageBox.critical(
                self,
                "Connection Error",
                f"Could not auto-detect Apera Meter {self.meter_id} on any COM port.",
            )
            return

        try:
            idx = self.port_combo.findText(port)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)

            self.ser = serial.Serial(port, 115200, timeout=0.1)
            self.ser.setDTR(False)
            self.ser.setRTS(False)
            self.is_connected = True
            self.parent_app.update_meter_status(self.meter_id, True, self.ser.port)
            self.btn_connect.setText("🔌 Disconnect")
            self.btn_download.setEnabled(True)
            self.btn_sync.setEnabled(True)
            self.set_status(f"Connected ({port})", "#27ae60")

            self.config[f"default_com_port_{self.meter_id}"] = port
            self.save_config()

            self.log_event(f"🔌 Connected to {port}.")

            self.ser.write(b"&OK#\r\n")
            if self.app_controller and self.app_controller.shared_config.get("global_auto_sync", False):
                QTimer.singleShot(500, self.sync_time)

            threading.Thread(target=self.read_serial_loop, daemon=True).start()
            threading.Thread(target=self.keep_alive_loop, daemon=True).start()
        except Exception as e:
            self.set_status(f"Failed to connect: {e}", "red")
            self.btn_connect.setText("🔌 Connect")

    def disconnect_meter(self):
        self.is_connected = False

        # Defer ser.close() to a background thread because PySerial close() can block
        # on Windows if another thread is currently polling it.
        def async_close(ser):
            time.sleep(0.1)  # Let the read loops exit first
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass

        threading.Thread(target=async_close, args=(self.ser,), daemon=True).start()

        self.btn_connect.setText("🔌 Connect")
        self.set_status("Disconnected", "gray")
        self.btn_download.setEnabled(False)
        self.btn_sync.setEnabled(False)
        self.sn_label.setText("SN: Unknown")
        self.model_label.setText("Model: Unknown")
        if self.parent_app:
            self.parent_app.update_meter_status(self.meter_id, False)

        self.log_event("❌ Disconnected.")

    def read_serial_loop(self):
        buffer = b""
        self.last_data_time = 0
        while self.is_connected:
            try:
                if self.ser.in_waiting > 0:
                    data = self.ser.read(self.ser.in_waiting)
                    buffer += data
                    buffer = buffer.replace(b"#\r\n", b"")
                    while b">" in buffer:
                        idx = buffer.find(b">")
                        packet = buffer[: idx + 1]
                        buffer = buffer[idx + 1 :]
                        try:
                            self.process_packet(packet)
                        except Exception as e:
                            print(f"Meter {self.meter_id} Packet Parse Error: {e}")
            except Exception as e:
                print(f"Meter {self.meter_id} Serial Error: {e}")
                self.signals.update_status.emit(f"Disconnected ({type(e).__name__})", "red")
                self.is_connected = False
                break
            time.sleep(0.05)

    def keep_alive_loop(self):
        while self.is_connected:
            if self.config.get(f"keep_alive_{self.meter_id}", True):
                if time.time() - self.last_data_time > 2.0:
                    try:
                        if self.ser and self.ser.is_open:
                            self.ser.write(b"&OK#\r\n")
                    except Exception:
                        pass
            time.sleep(1.5)

    def process_packet(self, packet):
        if not packet:
            return
        packet = packet.strip(b"\r\n")
        p_len = len(packet)
        if p_len < 3 or packet == b">" or packet == b"#>":
            return

        if (packet.startswith(b"<DS") or packet.startswith(b"<DN")) and p_len >= 12:
            self.last_data_time = time.time()
            try:
                payload = packet[3:11]
                w1, w2, w3, w4 = struct.unpack(">HHHH", payload[0:8])

                mode = w1 // 4096
                w1_rem = w1 % 4096
                mm = w1_rem // 64
                ss = w1_rem % 64

                yy = w2 // 512
                w2_rem = w2 % 512
                mt = w2_rem // 32
                dd = w2_rem % 32

                hh = w3 // 2048
                w3_rem = w3 % 2048
                MTC = w3_rem // 1024
                NTC = w3_rem % 1024

                b = w4 // 8192
                w4_rem = w4 % 8192
                dot = w4_rem // 2048
                DP = w4_rem % 2048

                temp_c = NTC / 10.0
                self.last_serial_temp = f"{temp_c:.1f}"
                mode_str = "Cond" if mode == 2 else f"Mode({mode})"

                val_float = DP
                if dot == 1:
                    val_float = DP / 10.0
                elif dot == 2:
                    val_float = DP / 100.0
                elif dot == 3:
                    val_float = DP / 1000.0

                screen_str = f"{val_float:.{dot}f}"

                cond_us, cond_ms = "", ""
                unit_str = "-"
                cond_us_val = val_float
                if mode == 2:
                    u_val = b + 10
                    if u_val == 10:
                        cond_ms = f"{val_float:.3f}"
                        cond_us = f"{val_float * 1000:.1f}"
                        cond_us_val = val_float * 1000
                        unit_str = "mS/cm"
                    elif u_val == 11:
                        cond_ms = f"{val_float / 1000:.3f}"
                        cond_us = f"{val_float:.1f}"
                        cond_us_val = val_float
                        unit_str = "uS/cm"
                    val_changed = False
                    try:
                        new_ms = float(cond_ms)
                        if getattr(self, "first_serial_time", None) is None:
                            self.first_serial_time = time.time()

                        if getattr(self, "last_serial_cond_ms", None) != new_ms:
                            self.last_serial_cond_ms = new_ms
                            if hasattr(self.app_controller, "ocr_pipeline"):
                                if not hasattr(self.app_controller.ocr_pipeline, "latest_serial_ms"):
                                    self.app_controller.ocr_pipeline.latest_serial_ms = {}
                                self.app_controller.ocr_pipeline.latest_serial_ms[
                                    f"m{self.meter_id}_cond"
                                ] = new_ms
                            val_changed = True
                        self.last_serial_cond_ms = new_ms
                    except Exception:
                        pass
                else:
                    cond_us = str(val_float)

                if cond_us_val > 0:
                    curr_log = math.log10(cond_us_val)
                    if self.config.get(f"alert_enabled_{self.meter_id}", False):
                        webhook_url = self.config.get(f"alert_webhook_{self.meter_id}", "")
                        try:
                            threshold = float(self.config.get(f"alert_threshold_{self.meter_id}", 0.5))
                        except Exception:
                            threshold = 0.5
                        try:
                            cooldown_min = float(self.config.get(f"alert_cooldown_{self.meter_id}", 5.0))
                        except Exception:
                            cooldown_min = 5.0
                        try:
                            delay_min = float(self.config.get(f"alert_delay_{self.meter_id}", 5.0))
                        except Exception:
                            delay_min = 5.0

                        if self.alert_start_time is None:
                            self.alert_start_time = time.time()

                        if time.time() - self.alert_start_time < delay_min * 60:
                            self.baseline_log_cond = curr_log
                        else:
                            if self.baseline_log_cond is None:
                                self.baseline_log_cond = curr_log

                            if abs(curr_log - self.baseline_log_cond) >= threshold:
                                if time.time() - self.last_alert_time > cooldown_min * 60:
                                    self.last_alert_time = time.time()
                                    msg = (
                                        f"🔔 **Meter {self.meter_id} Alert**\n"
                                        "Conductivity changed significantly!\n"
                                        f"Previous baseline log: {self.baseline_log_cond:.3f}\n"
                                        f"Current log: {curr_log:.3f}\n"
                                        f"Current value: {cond_us_val:.1f} µS/cm"
                                    )
                                    self.baseline_log_cond = curr_log
                                    self.send_discord_alert(webhook_url, msg)

                dt_str = f"20{yy:02d}-{mt:02d}-{dd:02d} {hh:02d}:{mm:02d}:{ss:02d}"
                dt_obj = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")

                is_dn_packet = packet.startswith(b"<DN")

                if dt_str in self.received_timestamps:
                    return
                self.received_timestamps.add(dt_str)

                row_data = (dt_str, mode_str, cond_us, cond_ms, f"{temp_c:.1f}")

                app = self.parent_app
                is_continuous = getattr(app, "shared_config", {}).get("dataset_continuous", False)
                is_manual = getattr(app, "manual_snap_requested", False)

                if self.is_recording:
                    if getattr(self, "ocr_active", False):
                        # Throttle OCR visual logging to 10s if we wanted to
                        pass
                    self.signals.update_tree.emit(dt_obj, row_data)

                # --- SMART DATASET RECORDING LOGIC ---
                if is_manual and not getattr(app, "dataset_session_dir", None):
                    timestamp = int(time.time())
                    app.dataset_session_dir = os.path.join(
                        os.getcwd(), "Sessions", f"dataset_session_{timestamp}"
                    )
                    frames_dir = os.path.join(app.dataset_session_dir, "frames")
                    os.makedirs(frames_dir, exist_ok=True)
                    app.dataset_csv_path = os.path.join(app.dataset_session_dir, "readings.csv")
                    with open(app.dataset_csv_path, "w", encoding="utf-8") as f:
                        f.write("timestamp,meter_id,screen_val,unit,raw_cond\n")
                    rois_path = os.path.join(app.dataset_session_dir, "rois.json")
                    with open(rois_path, "w") as f:
                        json.dump(app.ocr_pipeline.rois, f)

                if (is_continuous or is_manual) and getattr(app, "dataset_session_dir", None):
                    # The physical LCD updates ~Xms AFTER the serial packet is sent.
                    from PyQt6.QtCore import QTimer

                    ts = time.time()

                    if is_manual:
                        app.manual_snap_requested = False

                    # Retrieve dynamic hardware lag
                    hw_lag = self.config.get(f"hardware_lag_ms_{self.meter_id}", 300)
                    if not getattr(app, "shared_config", {}).get("apply_hardware_lag", True):
                        hw_lag = 0.0

                    if is_manual and getattr(app, "shared_config", {}).get("burst_on_snap", False):
                        app.calibration_burst_requested = True

                    def capture_delayed_frame(
                        cap_ts=ts,
                        c_ms=cond_ms,
                        s_val=screen_str,
                        r_cond=str(w4),
                        m_id=self.meter_id,
                        m_flag=is_manual,
                        h_lag=hw_lag,
                    ):
                        import cv2
                        import os
                        import time

                        time.sleep(h_lag / 1000.0)

                        frame = app.ocr_feed_tab.last_frame
                        if frame is not None:
                            frame_path = os.path.join(
                                app.dataset_session_dir, "frames", f"frame_{cap_ts}.jpg"
                            )
                            cv2.imwrite(frame_path, frame)
                            with open(app.dataset_csv_path, "a", encoding="utf-8") as f:
                                f.write(f"{cap_ts},{m_id},{c_ms},mS,{r_cond}\n")

                            roi_imgs, _ = app.ocr_pipeline.extract_crops(frame)
                            roi_name = f"m{m_id}_cond"
                            roi_img = roi_imgs.get(roi_name)
                            if roi_img is not None and roi_img.size > 0:
                                crop_dir = os.path.join(app.dataset_session_dir, "extracted_crops")
                                os.makedirs(crop_dir, exist_ok=True)
                                cnn_img = cv2.resize(roi_img, (128, 128))
                                actual_frame_ts = time.time()
                                filename = f"{s_val}_{cap_ts:.3f}_{actual_frame_ts:.3f}_{m_id}.jpg"
                                out_path = os.path.join(crop_dir, filename)
                                cv2.imwrite(out_path, cnn_img)

                        if m_flag:
                            try:
                                self.signals.reset_manual_snap.emit()
                            except Exception:
                                pass

                    import threading

                    threading.Thread(target=capture_delayed_frame, daemon=True).start()

                # --- PHASE 1: CALIBRATION BURST LOGIC ---
                if getattr(app, "calibration_burst_requested", False):
                    # Only consume the calibration burst if the meter's value actually changed,
                    # or if the user triggered a physical manual snap for this specific meter!
                    if is_manual or locals().get("val_changed", False):
                        app.calibration_burst_requested = False
                        if hasattr(app, "bursts_remaining"):
                            app.bursts_remaining -= 1
                        app.calibration_burst_meter = self.meter_id
                        app.calibration_burst_serial_ts = time.time()
                        app.calibration_burst_screen_val = screen_str
                        app.calibration_burst_count = 15  # 15 frames @ 30 FPS = ~500ms burst
                        try:
                            from PyQt6.QtCore import QTimer

                            QTimer.singleShot(
                                0, lambda: app.btn_calibration_burst.setText("Recording Burst...")
                            )
                            QTimer.singleShot(0, lambda: app.btn_calibration_burst.setEnabled(False))
                        except Exception:
                            pass

            except Exception as e:
                print(f"Packet error: {e}")

        elif packet.startswith(b"<SH") and p_len >= 12:
            payload = packet[3:11]
            hex_str = "-".join([f"{b:02X}" for b in payload])
            self.signals.update_device_info.emit("SN", hex_str)
        elif packet.startswith(b"<P2") and p_len >= 12:
            payload = packet[3:11]
            hex_str = "-".join([f"{b:02X}" for b in payload])
            self.signals.update_device_info.emit("Model", hex_str)

    def _reset_manual_snap_ui(self):
        if hasattr(self.app_controller, "btn_manual_snap"):
            self.app_controller.btn_manual_snap.setText("🎯 Arm Capture (Press M+ on Meter)")
            self.app_controller.btn_manual_snap.setEnabled(True)

    def log_point_ui(self, dt, row_data):
        if not getattr(self, "session_start_time", None):
            self.session_start_time = dt

        duration_min = (dt - self.session_start_time).total_seconds() / 60.0

        if len(row_data) == 5:
            full_row = (
                row_data[0],
                f"{duration_min:.2f}",
                row_data[1],
                row_data[2],
                row_data[3],
                row_data[4],
                "-",
            )
        elif len(row_data) == 6:
            full_row = (
                row_data[0],
                f"{duration_min:.2f}",
                row_data[1],
                row_data[2],
                row_data[3],
                row_data[4],
                row_data[5],
            )
        else:
            full_row = row_data

        self.plot_dates.append(dt)
        try:
            self.plot_conds.append(float(full_row[4]))
        except ValueError:
            self.plot_conds.append(0.0)

        try:
            self.plot_temps.append(float(full_row[5]))
        except ValueError:
            self.plot_temps.append(float("nan"))

        self.table.setSortingEnabled(False)
        row_idx = self.table.rowCount()
        self.table.insertRow(row_idx)
        for col, text in enumerate(full_row):
            self.table.setItem(row_idx, col, NumericTableWidgetItem(str(text)))
        self.table.setSortingEnabled(True)
        self.table.scrollToBottom()
        self.unsaved_changes = True

        try:
            is_new = not os.path.exists(self.backup_path)
            with open(self.backup_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if is_new:
                    writer.writerow(self.columns)
                writer.writerow(full_row)
        except Exception:
            pass

        self.schedule_plot()

    def set_status(self, text, color):
        if self.app_controller:
            status_lbl = (
                self.app_controller.status_m1 if self.meter_id == 1 else self.app_controller.status_m2
            )
            status_lbl.setText(f"M{self.meter_id}: {text}")
            status_lbl.setStyleSheet(f"color: {color}; font-weight: bold; margin-right: 20px;")

        if text.endswith("(Error)"):
            self.btn_connect.setText("🔌 Connect")
            self.btn_download.setEnabled(False)
            self.btn_sync.setEnabled(False)

    def set_device_info(self, typ, val):
        if typ == "SN":
            self.sn_label.setText(f"SN: {val}")
        elif typ == "Model":
            self.model_label.setText(f"Model: {val}")

    def send_discord_alert(self, url, msg):
        if not url:
            return

        def worker():
            try:
                requests.post(url, json={"content": msg}, timeout=5)
            except Exception as e:
                print("Webhook error:", e)

        threading.Thread(target=worker, daemon=True).start()

    def test_discord_alert(self, checked=False):
        url = self.config.get(f"alert_webhook_{self.meter_id}", "")
        if url:
            self.send_discord_alert(
                url,
                f"🧪 **Test Alert from Meter {self.meter_id}**\nThis is a test to confirm the webhook works!",
            )
            QMessageBox.information(self, "Test Sent", "Test alert dispatched to Discord.")
        else:
            QMessageBox.warning(self, "No Webhook", "Please save a Discord Webhook URL first.")

    def schedule_plot(self):
        if not getattr(self, "_plot_scheduled", False):
            self._plot_scheduled = True
            QTimer.singleShot(500, self.plot_data)

    def plot_data(self):
        self._plot_scheduled = False
        self.ax.clear()
        self.ax2.clear()
        self.ax_temp.clear()

        self.ax.set_yscale("log")
        if self.is_recording:
            self.ax.set_title(f"Recording: Conductivity (Meter {self.meter_id})", color="white")
        else:
            self.ax.set_title(f"Live: Conductivity (Meter {self.meter_id})", color="white")

        self.ax.set_xlabel("Time", color="white")

        is_us = self.unit_combo.currentText() == "Graph Unit: uS/cm"
        if is_us:
            self.ax.set_ylabel("Conductivity (uS/cm)", color="#3498db")
        else:
            self.ax.set_ylabel("Conductivity (mS/cm)", color="#3498db")

        self.ax.grid(True, linestyle="--", alpha=0.3)
        self.ax.tick_params(colors="white")

        self.ax2.set_ylabel("d(log C)/dt", color="#9b59b6")
        self.ax2.yaxis.set_label_position("right")  # clear() moves it left, on top of the cond. ticks
        self.ax2.tick_params(colors="white")

        self.ax_temp.set_ylabel("Temp (°C)", color="#e74c3c")
        self.ax_temp.tick_params(colors="white")
        self.ax_temp.grid(True, linestyle="--", alpha=0.3)
        self.ax_temp.set_facecolor("#1e1e2e")

        # Style adjustments for dark theme integration
        self.fig.patch.set_facecolor("#1e1e2e")
        self.ax.set_facecolor("#1e1e2e")

        if self.plot_dates:
            import matplotlib.ticker as ticker

            # Sort all arrays by date to prevent zig-zag lines from async threads
            points = zip(self.plot_dates, self.plot_conds, self.plot_temps, strict=False)
            sorted_data = sorted(points, key=lambda x: x[0])
            columns = [list(x) for x in zip(*sorted_data, strict=False)]
            self.plot_dates, self.plot_conds, self.plot_temps = columns

            is_duration = self.xaxis_combo.currentText() == "X-Axis: Duration (min)"

            if not getattr(self, "session_start_time", None):
                self.session_start_time = self.plot_dates[0]

            durations = [(d - self.session_start_time).total_seconds() / 60.0 for d in self.plot_dates]

            if self.is_recording and self.combo_autosave.currentText() == "Every X Minutes":
                try:
                    autosave_interval = float(self.ent_autosave_min.text())
                except Exception:
                    autosave_interval = 5.0

                if len(durations) > 0:
                    elapsed_min = durations[-1]
                    last_autosave = getattr(self, "last_autosave_min", 0.0)
                    if (elapsed_min - last_autosave) >= autosave_interval:
                        self.save_excel(auto=True, is_background_autosave=True)
                        self.last_autosave_min = elapsed_min

            if (
                self.is_recording
                and hasattr(self, "combo_autostop")
                and self.combo_autostop.currentText() != "Off"
            ):
                mode = self.combo_autostop.currentText()
                try:
                    autostop_interval = float(self.ent_autostop_min.text())
                except Exception:
                    autostop_interval = 60.0

                if len(durations) > 0:
                    elapsed_min = durations[-1]
                    if elapsed_min >= autostop_interval:
                        import datetime

                        if mode in ["Stop Meter", "Stop Both"] and getattr(self, "is_recording", False):
                            self.toggle_record()
                            self.log_event(f"🛑 Auto-Stop triggered for Meter at {elapsed_min:.1f} minutes.")

                        if (
                            mode in ["Stop OCR Log", "Stop Both", "Stop Camera"]
                            and hasattr(self.parent_app, "ocr_autolog_checkbox")
                            and self.parent_app.ocr_autolog_checkbox.isChecked()
                        ):
                            self.parent_app.ocr_autolog_checkbox.setChecked(False)
                            self.log_event(
                                f"🔴 Auto-Stop triggered for OCR Logging at {elapsed_min:.1f} minutes."
                            )

            if is_duration:
                x_data = durations
                self.ax_temp.set_xlabel("Duration (min)", color="white")
            else:
                x_data = self.plot_dates
                self.ax_temp.set_xlabel("Time", color="white")

            if is_us:
                plot_y = [y * 1000.0 for y in self.plot_conds]
            else:
                plot_y = self.plot_conds

            self.ax.plot(x_data, plot_y, marker="o", linestyle="-", color="#3498db", label="Cond")

            if not self.auto_time_chk.isChecked():
                try:
                    win_min = float(self.ent_time_win.text())
                    xmax = x_data[-1]
                    if is_duration:
                        xmin = xmax - win_min
                    else:
                        xmin = xmax - datetime.timedelta(minutes=win_min)
                    self.ax.set_xlim(xmin, xmax)
                except Exception:
                    pass

            if not self.auto_cond_chk.isChecked():
                try:
                    cmin = float(self.ent_cond_min.text())
                    cmax = float(self.ent_cond_max.text())
                    self.ax.set_ylim(cmin, cmax)
                except Exception:
                    pass

            import math

            valid_x = [x for x, y in zip(x_data, self.plot_temps, strict=False) if not math.isnan(y)]
            valid_y = [y for y in self.plot_temps if not math.isnan(y)]
            self.ax_temp.plot(valid_x, valid_y, marker="s", linestyle="-", color="#e74c3c", label="Temp")

            # Calculate and plot derivative
            if len(plot_y) > 1:
                import numpy as np

                y_arr = np.array(plot_y, dtype=float)
                y_arr[y_arr <= 0] = 1e-9
                log_y = np.log10(y_arr)
                deriv = None

                win_len = self.spin_deriv_smooth.value()
                if len(log_y) > win_len:
                    try:
                        import scipy.signal

                        deriv = scipy.signal.savgol_filter(log_y, window_length=win_len, polyorder=2, deriv=1)
                    except Exception:
                        pass
                if deriv is None:
                    deriv = np.gradient(log_y)
                self.ax2.plot(x_data, deriv, color="#9b59b6", linestyle="-", alpha=0.6, label="d(log)/dt")

                if not self.auto_deriv_chk.isChecked():
                    try:
                        dmin = float(self.ent_deriv_min.text())
                        dmax = float(self.ent_deriv_max.text())
                        self.ax2.set_ylim(dmin, dmax)
                    except Exception:
                        pass

            if not self.auto_temp_chk.isChecked():
                try:
                    tmin = float(self.ent_temp_min.text())
                    tmax = float(self.ent_temp_max.text())
                    self.ax_temp.set_ylim(tmin, tmax)
                except Exception:
                    pass

            if is_duration:
                self.ax_temp.xaxis.set_major_formatter(ticker.ScalarFormatter())
                self.fig.autofmt_xdate(rotation=0, ha="center")
            else:
                self.ax_temp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                self.fig.autofmt_xdate()

            # Hide x-axis labels on top plot
            plt.setp(self.ax.get_xticklabels(), visible=False)
            self.ax.set_xlabel("")

        self.fig.tight_layout()
        self.canvas.draw_idle()

    def sync_time(self, checked=False):
        if self.is_connected and self.ser:
            now = datetime.datetime.now()
            try:
                packet = bytearray(
                    [
                        ord("&"),
                        ord("D"),
                        ord("T"),
                        now.year - 2000,
                        now.month,
                        now.day,
                        now.hour,
                        now.minute,
                        now.second,
                        ord("#"),
                    ]
                )
                packet.extend(b"\r\n")
                self.ser.write(packet)
            except Exception:
                pass

    def download_memory(self, checked=False):
        if not self.ser:
            return
        self.set_status("Downloading Memory...", "gray")
        self.btn_download.setEnabled(False)
        self.ser.write(b"&WA#\r\n")

    def save_excel(self, auto=False, is_background_autosave=False):
        if self.table.rowCount() == 0:
            if not auto:
                QMessageBox.information(self, "Export", "No data to export.")
            return

        use_global = self.chk_use_global_template.isChecked()
        if use_global and self.app_controller:
            template = self.app_controller.shared_config.get(
                "global_filename_template", "<Meter ID>_<Date>_<Time>"
            )
            docs = self.app_controller.shared_config.get("global_default_save_folder", "Data")
        else:
            template = self.ent_save_template.text()
            docs = self.config.get(f"default_save_location_{self.meter_id}", "")

        if not template.strip():
            template = "<Meter ID>_<Date>_<Time>"
        filename = self.get_resolved_string(template)
        if not filename.endswith(".xlsx"):
            filename += ".xlsx"

        if not docs or not os.path.isdir(docs):
            docs = os.path.expanduser("~/Documents")
        filepath = os.path.join(docs, filename)

        if not auto:
            filepath, _ = QFileDialog.getSaveFileName(
                self, "Export Excel", filepath, "Excel files (*.xlsx);;All files (*.*)"
            )
            if not filepath:
                return

        try:
            from openpyxl.utils.dataframe import dataframe_to_rows

            data = []
            for row in range(self.table.rowCount()):
                row_data = []
                for col in range(self.table.columnCount()):
                    item = self.table.item(row, col)
                    if item:
                        text = item.text()

                        # Convert to number so Excel recognizes it
                        try:
                            if "." in text:
                                val = float(text)
                            else:
                                val = int(text)
                            row_data.append(val)
                        except ValueError:
                            row_data.append(text)
                    else:
                        row_data.append("")
                data.append(row_data)

            df = pd.DataFrame(data, columns=self.columns)

            wb = openpyxl.Workbook()

            data_ws = wb.active
            data_ws.title = "Data"
            for r in dataframe_to_rows(df, index=False, header=True):
                data_ws.append(r)

            doc_ws = wb.create_sheet(title="Documentation")
            doc_ws["A1"] = f"{self.meter_id} - Experiment Documentation"
            doc_ws["A1"].font = Font(size=14, bold=True)
            doc_ws["A3"] = "Export Date:"
            doc_ws["B3"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            doc_ws["A4"] = "Meter SN:"
            doc_ws["B4"] = self.sn_label.text().replace("SN: ", "")

            current_row = 6
            doc_ws.cell(row=current_row, column=1, value="Metadata Tokens").font = Font(bold=True)
            current_row += 1
            doc_ws.cell(row=current_row, column=1, value="Token Name").font = Font(bold=True, italic=True)
            doc_ws.cell(row=current_row, column=2, value="Token Value").font = Font(bold=True, italic=True)
            current_row += 1
            if hasattr(self, "token_inputs"):
                for name, ent in self.token_inputs.items():
                    doc_ws.cell(row=current_row, column=1, value=name)
                    token_text = ent.text().strip()
                    try:
                        if "." in token_text:
                            token_val = float(token_text)
                        else:
                            token_val = int(token_text)
                    except ValueError:
                        token_val = token_text
                    doc_ws.cell(row=current_row, column=2, value=token_val)
                    current_row += 1

            current_row += 1
            doc_ws.cell(row=current_row, column=1, value="Experiment Notes:").font = Font(bold=True)
            current_row += 1
            doc_ws.cell(row=current_row, column=1, value=self.txt_notes.toPlainText()).alignment = Alignment(
                wrap_text=True, vertical="top"
            )
            doc_ws.row_dimensions[current_row].height = 100

            doc_ws.column_dimensions["A"].width = 25
            doc_ws.column_dimensions["B"].width = 35

            try:
                from openpyxl.chart import ScatterChart, Reference, Series
                from openpyxl.chart.axis import ChartLines

                graph_ws = wb.create_sheet(title="Graph")

                chart = ScatterChart()
                chart.title = f"Meter {self.meter_id} Conductivity Over Time"
                chart.style = 13
                chart.x_axis.title = "Duration (min)"
                chart.y_axis.title = "Conductivity (mS/cm)"
                chart.y_axis.scaling.logBase = 10
                chart.x_axis.majorGridlines = ChartLines()
                chart.y_axis.majorGridlines = ChartLines()
                chart.width = 22
                chart.height = 12

                max_row = len(data) + 1
                if max_row > 1:
                    xvalues = Reference(data_ws, min_col=2, min_row=2, max_row=max_row)
                    values = Reference(data_ws, min_col=5, min_row=1, max_row=max_row)
                    series = Series(values, xvalues, title_from_data=True)
                    chart.series.append(series)
                    graph_ws.add_chart(chart, "A1")
            except Exception as e:
                print(f"Failed to generate graph sheet: {e}")

            wb.save(filepath)
            self.set_status("Exported to Excel!", "#27ae60")
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(
                3000,
                lambda: self.set_status(
                    "Receiving Data..." if getattr(self, "is_recording", False) else "Connected", "#27ae60"
                ),
            )
            self.unsaved_changes = False

            # Show success prompt as requested by user
            if not is_background_autosave:
                QMessageBox.information(self, "Save Successful", f"File successfully saved to:\n{filepath}")
        except Exception as e:
            try:
                with open(os.path.expanduser("~/autosave_error.log"), "a") as f:
                    import traceback

                    f.write(f"{datetime.datetime.now()}: {str(e)}\n{traceback.format_exc()}\n")
            except Exception:
                pass
            if not auto:
                QMessageBox.critical(self, "Error", f"Export failed! {e}")


class SettingsTab(QWidget):
    def __init__(self, parent_app):
        super().__init__()
        self.parent_app = parent_app

        main_layout = QHBoxLayout(self)

        # Sidebar
        self.sidebar = QListWidget()
        self.sidebar.setMaximumWidth(220)
        self.sidebar.setStyleSheet("QListWidget { font-size: 14px; } QListWidget::item { padding: 10px; }")

        # Stack
        self.stack = QStackedWidget()

        main_layout.addWidget(self.sidebar)

        # Right side content area
        right_layout = QVBoxLayout()
        right_layout.addWidget(self.stack)

        # 1. General (global & application settings)
        page_global = QWidget()
        l_global = QVBoxLayout(page_global)

        # --- App Preferences ---
        gb_app = CollapsibleBox("Application Preferences")
        gb_app.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        l_app_inner = QVBoxLayout()
        gb_app.setLayout(l_app_inner)

        style_layout = QHBoxLayout()
        style_layout.addWidget(QLabel("Action Button Style:"))
        self.combo_style = QComboBox()
        self.combo_style.addItems(["Icon + Text", "Icon Only"])
        self.combo_style.setCurrentText(self.parent_app.meter1.config.get("button_style", "Icon + Text"))
        style_layout.addWidget(self.combo_style)
        l_app_inner.addLayout(style_layout)

        l_app_inner.addWidget(
            settings_location.settings_folder_box(
                APP_DIR,
                DATA_DIR,
                SETTINGS_FILES,
                save_cb=self.parent_app.meter1.save_config,
                restart_cb=self.parent_app.restart_app,
            )
        )
        update_row = QHBoxLayout()
        self.lbl_version = QLabel(f"Version {__version__}")
        self.lbl_version.setStyleSheet("font-weight: bold;")
        update_row.addWidget(self.lbl_version)
        self.lbl_update_status = QLabel("")
        self.lbl_update_status.setStyleSheet("color: #9aa0a6;")
        update_row.addWidget(self.lbl_update_status, 1)
        self.btn_update = QPushButton("⬆️ Check for Updates")
        self.btn_update.setToolTip("Look for a newer release on GitHub and install it")
        self.btn_update.setStyleSheet(
            "background-color: #8e44ad; color: white; padding: 8px 16px; font-weight: bold;"
        )
        self.btn_update.clicked.connect(self.parent_app.check_for_updates)
        self.btn_shortcut = QPushButton("🖥️ Create Desktop Shortcut")
        self.btn_shortcut.setToolTip("Put a shortcut to this app on the desktop")
        self.btn_shortcut.setStyleSheet("padding: 8px 16px; font-weight: bold;")
        self.btn_shortcut.clicked.connect(self.parent_app.create_desktop_shortcut)
        update_row.addWidget(self.btn_shortcut)
        update_row.addWidget(self.btn_update)
        l_app_inner.addLayout(update_row)

        self.chk_update_on_start = QCheckBox("Check for updates when the app starts")
        self.chk_update_on_start.setToolTip(
            "Asks GitHub for the latest release a few seconds after start and once a day while the app "
            "runs. If there is one, the version number turns amber. Nothing about you is sent."
        )
        self.chk_update_on_start.setChecked(self.parent_app.update_check_on_start())
        self.chk_update_on_start.toggled.connect(self.parent_app.set_update_check_on_start)
        l_app_inner.addWidget(self.chk_update_on_start)

        help_row = QHBoxLayout()
        self.btn_open_logs = QPushButton("📂 Open Log Folder")
        self.btn_open_logs.setToolTip("Show the folder with the app's error log")
        self.btn_open_logs.setStyleSheet("padding: 8px 16px; font-weight: bold;")
        self.btn_open_logs.clicked.connect(self.parent_app.open_log_folder)
        self.btn_copy_diagnostics = QPushButton("📋 Copy Diagnostics")
        self.btn_copy_diagnostics.setToolTip(
            "Copy the app version, folders, meter connections and the end of the error log, "
            "for a bug report. Home folders and passwords are left out."
        )
        self.btn_copy_diagnostics.setStyleSheet("padding: 8px 16px; font-weight: bold;")
        self.btn_copy_diagnostics.clicked.connect(self.parent_app.copy_diagnostics)
        self.lbl_diagnostics = QLabel("")
        self.lbl_diagnostics.setStyleSheet("color: #9aa0a6;")
        self.lbl_diagnostics.setWordWrap(True)
        help_row.addWidget(self.btn_open_logs)
        help_row.addWidget(self.btn_copy_diagnostics)
        help_row.addWidget(self.lbl_diagnostics, 1)
        l_app_inner.addLayout(help_row)
        l_global.addWidget(gb_app)

        # --- Global Saving Settings ---
        gb_save = CollapsibleBox("Global Saving Settings")
        gb_save.setFont(QFont("Arial", 11, QFont.Weight.Bold))
        l_save_inner = QVBoxLayout()
        gb_save.setLayout(l_save_inner)

        dir_layout = QHBoxLayout()
        self.lbl_folder = QLabel(self.parent_app.meter1.config.get("global_default_save_folder", "Data"))
        btn_folder = QPushButton("Browse...")
        btn_folder.clicked.connect(self.choose_global_dir)
        dir_layout.addWidget(QLabel("Default save folder:"))
        dir_layout.addWidget(self.lbl_folder, 1)
        dir_layout.addWidget(btn_folder)
        l_save_inner.addLayout(dir_layout)

        l_save_inner.addWidget(QLabel("Filename Template:"))
        self.ent_template = QLineEdit(
            self.parent_app.meter1.config.get("global_filename_template", "<Meter ID>_<Date>_<Time>")
        )
        l_save_inner.addWidget(self.ent_template)

        l_save_inner.addWidget(QLabel("Click to insert token:"))
        grid_buttons = QGridLayout()

        base_tokens = ["Date", "Time", "Start Date", "Start Time", "Meter ID"]
        global_tokens = getattr(self.parent_app, "meter1", self.parent_app).config.get("global_tokens", [])
        custom_names = [t.get("name") for t in global_tokens if t.get("name")]
        if not custom_names:
            custom_names = ["Column #/Name", "Material", "Flow rate", "Bed length", "Run #", "(De)Sorption"]

        tokens = base_tokens + [n for n in custom_names if n not in base_tokens]
        for i, tok in enumerate(tokens):
            btn = QPushButton(f"<{tok}>")
            btn.clicked.connect(lambda checked, t=tok: self.ent_template.insert(f"<{t}>"))
            grid_buttons.addWidget(btn, i // 4, i % 4)
        l_save_inner.addLayout(grid_buttons)

        self.auto_sync_chk = QCheckBox("Auto-Sync Time on Connect (All Meters)")
        self.auto_sync_chk.setChecked(self.parent_app.meter1.config.get("global_auto_sync", False))
        l_save_inner.addWidget(self.auto_sync_chk)
        l_global.addWidget(gb_save)

        l_global.addStretch()

        self.sidebar.addItem("🌍 General")
        self.stack.addWidget(page_global)

        # 2. Meter 1 Settings
        page_m1 = QWidget()
        l_m1 = QFormLayout(page_m1)
        self.lbl_dir1 = QLabel(self.parent_app.meter1.config.get("default_save_location_1", "Not Set"))
        btn_dir1 = QPushButton("Browse...")
        btn_dir1.clicked.connect(lambda checked=False: self.choose_meter_dir(1, self.lbl_dir1))
        d1 = QHBoxLayout()
        d1.addWidget(self.lbl_dir1)
        d1.addWidget(btn_dir1)
        l_m1.addRow("Meter 1 Save Directory:", d1)
        self.ka1_chk = QCheckBox("Enable Keep-Alive (Ping every 2s)")
        self.ka1_chk.setChecked(self.parent_app.meter1.config.get("keep_alive_1", True))
        l_m1.addRow("", self.ka1_chk)

        self.al1_chk = QCheckBox("Enable Discord Alerts")
        self.al1_chk.setChecked(self.parent_app.meter1.config.get("alert_enabled_1", False))
        self.al1_web = QLineEdit(self.parent_app.meter1.config.get("alert_webhook_1", ""))
        self.al1_thr = QLineEdit(str(self.parent_app.meter1.config.get("alert_threshold_1", "0.5")))
        self.al1_cool = QLineEdit(str(self.parent_app.meter1.config.get("alert_cooldown_1", "5.0")))
        self.al1_del = QLineEdit(str(self.parent_app.meter1.config.get("alert_delay_1", "5.0")))
        btn_test1 = QPushButton("Test Webhook")
        btn_test1.clicked.connect(self.parent_app.meter1.test_discord_alert)
        l_m1.addRow("", self.al1_chk)
        l_m1.addRow("Discord Webhook URL:", self.al1_web)
        l_m1.addRow("Log Change Threshold:", self.al1_thr)
        l_m1.addRow("Baseline Delay (Minutes):", self.al1_del)
        l_m1.addRow("Cooldown (Minutes):", self.al1_cool)
        l_m1.addRow("", btn_test1)

        self.sidebar.addItem("📈 Meter 1 Settings")
        self.stack.addWidget(page_m1)

        # 3. Meter 2 Settings
        page_m2 = QWidget()
        l_m2 = QFormLayout(page_m2)
        self.lbl_dir2 = QLabel(self.parent_app.meter2.config.get("default_save_location_2", "Not Set"))
        btn_dir2 = QPushButton("Browse...")
        btn_dir2.clicked.connect(lambda checked=False: self.choose_meter_dir(2, self.lbl_dir2))
        d2 = QHBoxLayout()
        d2.addWidget(self.lbl_dir2)
        d2.addWidget(btn_dir2)
        l_m2.addRow("Meter 2 Save Directory:", d2)
        self.ka2_chk = QCheckBox("Enable Keep-Alive (Ping every 2s)")
        self.ka2_chk.setChecked(self.parent_app.meter2.config.get("keep_alive_2", True))
        l_m2.addRow("", self.ka2_chk)

        self.al2_chk = QCheckBox("Enable Discord Alerts")
        self.al2_chk.setChecked(self.parent_app.meter2.config.get("alert_enabled_2", False))
        self.al2_web = QLineEdit(self.parent_app.meter2.config.get("alert_webhook_2", ""))
        self.al2_thr = QLineEdit(str(self.parent_app.meter2.config.get("alert_threshold_2", "0.5")))
        self.al2_cool = QLineEdit(str(self.parent_app.meter2.config.get("alert_cooldown_2", "5.0")))
        self.al2_del = QLineEdit(str(self.parent_app.meter2.config.get("alert_delay_2", "5.0")))
        btn_test2 = QPushButton("Test Webhook")
        btn_test2.clicked.connect(self.parent_app.meter2.test_discord_alert)
        l_m2.addRow("", self.al2_chk)
        l_m2.addRow("Discord Webhook URL:", self.al2_web)
        l_m2.addRow("Log Change Threshold:", self.al2_thr)
        l_m2.addRow("Baseline Delay (Minutes):", self.al2_del)
        l_m2.addRow("Cooldown (Minutes):", self.al2_cool)
        l_m2.addRow("", btn_test2)

        self.sidebar.addItem("📉 Meter 2 Settings")
        self.stack.addWidget(page_m2)

        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)

        # Save Button at the bottom of the right side layout
        btn_save = QPushButton("💾 Save All Settings")
        btn_save.setStyleSheet(
            "background-color: #3498db; color: white; padding: 12px; font-size: 14px; font-weight: bold;"
        )
        btn_save.clicked.connect(self.save_all)
        right_layout.addWidget(btn_save)

        main_layout.addLayout(right_layout)

    def choose_global_dir(self, checked=False):
        d = QFileDialog.getExistingDirectory(self, "Select Global Directory")
        if d:
            self.lbl_folder.setText(d)

    def choose_meter_dir(self, meter_id, lbl):
        d = QFileDialog.getExistingDirectory(self, f"Select Directory for Meter {meter_id}")
        if d:
            lbl.setText(d)

    def save_all(self, checked=False):
        # Meter 1 config
        c1 = self.parent_app.meter1.config
        c1["global_default_save_folder"] = self.lbl_folder.text()
        c1["global_filename_template"] = self.ent_template.text()
        c1["global_auto_sync"] = self.auto_sync_chk.isChecked()
        c1["default_save_location_1"] = self.lbl_dir1.text()
        c1["keep_alive_1"] = self.ka1_chk.isChecked()
        c1["alert_enabled_1"] = self.al1_chk.isChecked()
        c1["alert_webhook_1"] = self.al1_web.text()
        c1["alert_threshold_1"] = self.al1_thr.text()
        c1["alert_delay_1"] = self.al1_del.text()
        c1["alert_cooldown_1"] = self.al1_cool.text()

        # Meter 2 config
        c2 = self.parent_app.meter2.config
        c2["global_default_save_folder"] = self.lbl_folder.text()
        c2["global_filename_template"] = self.ent_template.text()
        c2["global_auto_sync"] = self.auto_sync_chk.isChecked()
        c2["default_save_location_2"] = self.lbl_dir2.text()
        c2["keep_alive_2"] = self.ka2_chk.isChecked()
        c2["alert_enabled_2"] = self.al2_chk.isChecked()
        c2["alert_webhook_2"] = self.al2_web.text()
        c2["alert_threshold_2"] = self.al2_thr.text()
        c2["alert_delay_2"] = self.al2_del.text()
        c2["alert_cooldown_2"] = self.al2_cool.text()

        style = self.combo_style.currentText()
        c1["button_style"] = style
        c2["button_style"] = style
        self.parent_app.meter1.update_button_styles(style)
        self.parent_app.meter2.update_button_styles(style)

        # Sync UI template
        if self.parent_app.meter1.chk_use_global_template.isChecked():
            self.parent_app.meter1.ent_save_template.setText(self.ent_template.text())
        if self.parent_app.meter2.chk_use_global_template.isChecked():
            self.parent_app.meter2.ent_save_template.setText(self.ent_template.text())

        self.parent_app.meter1.save_config()
        self.parent_app.meter2.save_config()

        QMessageBox.information(self, "Success", "All settings saved successfully.")


from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
import cv2

from PyQt6.QtWidgets import QRubberBand, QSlider
from PyQt6.QtCore import QPoint, QRect, pyqtSignal


class InteractiveImageLabel(QLabel):
    roi_selected = pyqtSignal(QRect)

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self.rubberBand = None
        self.origin = QPoint()
        self.current_rect = None
        self.drawing_enabled = False

    def mousePressEvent(self, event):
        if not self.drawing_enabled:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.pos()
            if not self.rubberBand:
                self.rubberBand = QRubberBand(QRubberBand.Shape.Rectangle, self)
            self.rubberBand.setGeometry(QRect(self.origin, self.origin))
            self.rubberBand.show()

    def mouseMoveEvent(self, event):
        if not self.drawing_enabled:
            return
        if self.rubberBand:
            self.rubberBand.setGeometry(QRect(self.origin, event.pos()).normalized())

    def mouseReleaseEvent(self, event):
        if not self.drawing_enabled:
            return
        if event.button() == Qt.MouseButton.LeftButton and self.rubberBand:
            self.current_rect = self.rubberBand.geometry()
            self.roi_selected.emit(self.current_rect)


class OCRFeedTab(QWidget):
    image_ready = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.app = parent
        main_layout = QHBoxLayout()
        self.setLayout(main_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # --- Left Panel ---
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(left_panel)
        scroll_area.setFixedWidth(350)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        splitter.addWidget(scroll_area)

        def make_collapsible(title, parent_layout, default_collapsed=False):
            title = title.replace("&", "&&")  # show "&" literally, not as a shortcut underline
            btn_toggle = QPushButton(f"▼ {title}")
            btn_toggle.setCheckable(True)
            btn_toggle.setStyleSheet(
                "text-align: left; font-weight: bold; padding: 5px; background-color: #34495e; color: white;"
            )
            content_widget = QWidget()
            lay = QVBoxLayout(content_widget)
            lay.setContentsMargins(0, 0, 0, 0)
            parent_layout.addWidget(btn_toggle)
            parent_layout.addWidget(content_widget)
            btn_toggle.toggled.connect(
                lambda checked, w=content_widget, b=btn_toggle, t=title: (
                    w.setVisible(not checked),
                    b.setText(f"▶ {t}" if checked else f"▼ {t}"),
                )
            )
            btn_toggle.setChecked(default_collapsed)
            if not default_collapsed:
                content_widget.setVisible(True)
                btn_toggle.setText(f"▼ {title}")
            return lay

        # 1. Camera Settings
        l_camera = make_collapsible("Camera Settings", left_layout)
        l_camera.addWidget(self.app.ocr_checkbox)
        l_camera.addWidget(self.app.ocr_autostart_chk)

        l_camera.addWidget(QLabel("Camera Index (0=default, 1=external):"))
        self.camera_index_spin = QSpinBox()
        self.camera_index_spin.setRange(0, 10)

        default_cam = self.app.shared_config.get("camera_index", 0) if self.app else 0
        if self.app and hasattr(self.app, "ocr_pipeline"):
            self.app.ocr_pipeline.camera_index = default_cam
        self.camera_index_spin.setValue(default_cam)

        self.camera_index_spin.valueChanged.connect(self.on_camera_index_changed)
        l_camera.addWidget(self.camera_index_spin)

        l_camera.addWidget(QLabel("Camera Focus:"))
        self.focus_slider = QSlider(Qt.Orientation.Horizontal)
        self.focus_slider.setRange(-1, 255)
        self.focus_slider.setValue(-1)
        self.focus_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.focus_slider.setTickInterval(10)
        self.focus_label = QLabel("Focus: Auto")
        self.focus_slider.valueChanged.connect(self.on_focus_changed)
        l_camera.addWidget(self.focus_slider)
        l_camera.addWidget(self.focus_label)

        # 2. Data Insertion
        l_data = make_collapsible("Data Logging & Graphing", left_layout)
        l_data.addWidget(self.app.ocr_autolog_checkbox)
        l_data.addWidget(self.app.chk_apply_lag)

        # 3. Training & Calibration
        l_train = make_collapsible("Training & Calibration", left_layout, default_collapsed=True)

        lbl_phase1 = QLabel("Mode: Hardware Calibration")
        lbl_phase1.setStyleSheet("font-weight: bold; font-size: 13px; padding-top: 10px; color: #E67E22;")
        l_train.addWidget(lbl_phase1)

        burst_layout = QHBoxLayout()
        burst_layout.addWidget(self.app.btn_calibration_burst)
        burst_layout.addWidget(self.app.spin_burst_count)
        l_train.addLayout(burst_layout)

        lbl_phase2 = QLabel("Mode: OCR Training Dataset Builder")
        lbl_phase2.setStyleSheet("font-weight: bold; font-size: 13px; padding-top: 10px; color: #E67E22;")
        l_train.addWidget(lbl_phase2)

        l_train.addWidget(self.app.dataset_checkbox)
        l_train.addWidget(self.app.btn_manual_snap)
        l_train.addWidget(self.app.chk_burst_on_snap)

        # 4. OCR Filters & Sanity Checks
        l_filters = make_collapsible("OCR Filters & Sanity Checks", left_layout)
        l_filters.addWidget(self.app.ocr_dual_mode_chk)
        l_filters.addWidget(self.app.ocr_warn_streams_chk)

        dev_layout = QHBoxLayout()
        dev_layout.addWidget(self.app.ocr_enforce_dev_chk)
        dev_layout.addWidget(self.app.ocr_max_dev_spin)
        l_filters.addLayout(dev_layout)

        # Jump filter removed because it was duplicative of max deviation filter

        unit_layout = QHBoxLayout()
        unit_layout.addWidget(QLabel("Unit Logic:"))
        unit_layout.addWidget(self.app.ocr_unit_combo)
        l_filters.addLayout(unit_layout)

        l_filters.addWidget(self.app.ocr_discard_chk)
        l_filters.addWidget(self.app.ocr_midpoint_chk)

        conf_layout = QHBoxLayout()
        conf_layout.addWidget(QLabel("CNN Min Confidence (0=Off):"))
        conf_layout.addWidget(self.app.ocr_conf_spin)
        l_filters.addLayout(conf_layout)

        phys_max_layout = QHBoxLayout()
        phys_max_layout.addWidget(self.app.ocr_phys_max_chk)
        phys_max_layout.addWidget(self.app.ocr_phys_max_spin)
        l_filters.addLayout(phys_max_layout)

        max_layout = QHBoxLayout()
        max_layout.addWidget(self.app.ocr_enforce_max_chk)
        max_layout.addWidget(self.app.ocr_max_val_spin)
        l_filters.addLayout(max_layout)

        # Draw control
        self.btn_draw = QPushButton("🖌️ Draw ROI")
        self.btn_draw.setCheckable(True)
        self.btn_draw.toggled.connect(self.toggle_drawing)
        left_layout.addWidget(self.btn_draw)

        # Clear All
        self.btn_clear_roi = QPushButton("🗑️ Clear All ROIs")
        self.btn_clear_roi.clicked.connect(self.clear_roi)
        left_layout.addWidget(self.btn_clear_roi)

        self.roi_spinboxes = {}
        # ROI Groups
        for name, title in [("m1_cond", "M1 Cond"), ("m2_cond", "M2 Cond")]:
            btn_toggle = QPushButton(f"▼ {title}")
            btn_toggle.setCheckable(True)
            btn_toggle.setStyleSheet(
                "text-align: left; font-weight: bold; padding: 5px; background-color: #34495e; color: white;"
            )

            content_widget = QWidget()
            lay = QVBoxLayout(content_widget)
            lay.setContentsMargins(0, 0, 0, 0)

            btn_set = QPushButton(f"✅ Assign {title} to Drawn Box")
            btn_set.clicked.connect(lambda checked, n=name: self.set_specific_roi(n))
            lay.addWidget(btn_set)

            grid = QGridLayout()
            spins = {}
            for i, prop in enumerate(["X", "Y", "W", "H", "Angle"]):
                grid.addWidget(QLabel(prop), i, 0)
                spin = QDoubleSpinBox()
                if prop == "Angle":
                    spin.setRange(-180.0, 180.0)
                else:
                    spin.setRange(0.0, 100.0)  # Percentage
                spin.setDecimals(1)
                spin.setSingleStep(0.1)
                spin.valueChanged.connect(lambda val, n=name, p=prop: self.on_spinbox_changed(n, p, val))
                grid.addWidget(spin, i, 1)
                spins[prop] = spin

            self.roi_spinboxes[name] = spins
            lay.addLayout(grid)

            btn_clear_single = QPushButton("🗑️ Clear Single")
            btn_clear_single.clicked.connect(lambda checked, n=name: self.clear_single_roi(n))
            lay.addWidget(btn_clear_single)

            left_layout.addWidget(btn_toggle)
            left_layout.addWidget(content_widget)

            btn_toggle.toggled.connect(
                lambda checked, w=content_widget, b=btn_toggle, t=title: (
                    w.setVisible(not checked),
                    b.setText(f"▶ {t}" if checked else f"▼ {t}"),
                )
            )
            btn_toggle.setChecked(True)  # Start collapsed

        left_layout.addStretch()

        # --- Image Label ---
        self.image_label = InteractiveImageLabel("Waiting for OCR Camera Feed...")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: black; color: white; font-size: 18px;")
        self.image_label.setMinimumSize(400, 300)
        from PyQt6.QtWidgets import QSizePolicy

        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.image_label.roi_selected.connect(self.on_roi_selected)
        splitter.addWidget(self.image_label)

        # Set splitter sizes so the left panel takes up its minimum width and the image label takes the rest
        splitter.setSizes([350, 1000])

        self.image_ready.connect(self.set_image)
        self.last_frame = None
        self.current_drawn_rect = None
        import collections

        self.frame_buffer = collections.deque(maxlen=30)

    def toggle_drawing(self, checked):
        self.image_label.drawing_enabled = checked
        if not checked and self.image_label.rubberBand:
            self.image_label.rubberBand.hide()

    def on_focus_changed(self, val):
        if val == -1:
            self.focus_label.setText("Focus: Auto")
        else:
            self.focus_label.setText(f"Focus: {val}")
        if hasattr(self.app, "ocr_pipeline") and self.app.ocr_pipeline:
            self.app.ocr_pipeline.set_focus(val)

    def on_camera_index_changed(self, val):
        if self.app and hasattr(self.app, "ocr_pipeline"):
            self.app.ocr_pipeline.camera_index = val
            if hasattr(self.app, "_save_ocr_config"):
                self.app.shared_config["camera_index"] = val
                self.app._save_ocr_config()
            if self.app.ocr_pipeline.is_running:
                self.app.ocr_pipeline.stop()
                import time

                time.sleep(0.5)
                if not self.app.ocr_pipeline.start():
                    self.app.ocr_checkbox.setChecked(False)
                    self.app.set_ocr_status("🎥 Camera: Not found", "#e67e22")

    def on_ocr_interval_changed(self, val):
        if self.app and hasattr(self.app, "ocr_pipeline"):
            self.app.ocr_pipeline.ocr_interval = val
            if hasattr(self.app, "_save_ocr_config"):
                self.app.shared_config["ocr_interval"] = val
                self.app._save_ocr_config()

    def on_roi_selected(self, rect):
        self.current_drawn_rect = rect

    def set_specific_roi(self, name):
        if not self.current_drawn_rect or not self.last_frame is not None:
            return

        # Calculate normalized coordinates
        # We need the displayed pixmap size vs the original image size
        if not self.image_label.pixmap():
            return

        pm_w = self.image_label.pixmap().width()
        pm_h = self.image_label.pixmap().height()

        lbl_w = self.image_label.width()
        lbl_h = self.image_label.height()

        # Offset for centered image
        x_off = (lbl_w - pm_w) / 2
        y_off = (lbl_h - pm_h) / 2

        rect = self.current_drawn_rect

        # Adjust rect to pixmap coordinates
        rx = max(0, rect.x() - x_off)
        ry = max(0, rect.y() - y_off)
        rw = min(rect.width(), pm_w - rx)
        rh = min(rect.height(), pm_h - ry)

        # Normalize
        norm_x = rx / pm_w
        norm_y = ry / pm_h
        norm_w = rw / pm_w
        norm_h = rh / pm_h

        self.app.ocr_pipeline.rois[name] = [norm_x, norm_y, norm_w, norm_h, 0.0]
        self.app.ocr_pipeline.save_rois()
        self.refresh_spinboxes()

        # Hide rubberband
        if self.image_label.rubberBand:
            self.image_label.rubberBand.hide()
        self.current_drawn_rect = None

        QMessageBox.information(self, "ROI Set", f"Successfully set ROI for {name}.")

    def clear_roi(self):
        self.app.ocr_pipeline.rois = {"m1_cond": None, "m2_cond": None}
        self.app.ocr_pipeline.save_rois()
        self.refresh_spinboxes()
        if self.image_label.rubberBand:
            self.image_label.rubberBand.hide()
        QMessageBox.information(self, "ROI Cleared", "All ROIs have been reset to defaults.")

    def on_spinbox_changed(self, name, prop, val):
        if not hasattr(self.app, "ocr_pipeline") or not self.app.ocr_pipeline:
            return
        roi = self.app.ocr_pipeline.rois.get(name)
        if not roi or len(roi) < 5:
            roi = [0.0, 0.0, 0.0, 0.0, 0.0]
        else:
            roi = list(roi)

        if prop == "X":
            roi[0] = val / 100.0
        elif prop == "Y":
            roi[1] = val / 100.0
        elif prop == "W":
            roi[2] = val / 100.0
        elif prop == "H":
            roi[3] = val / 100.0
        elif prop == "Angle":
            roi[4] = val

        self.app.ocr_pipeline.rois[name] = roi
        self.app.ocr_pipeline.save_rois()

    def clear_single_roi(self, name):
        self.app.ocr_pipeline.rois[name] = None
        self.app.ocr_pipeline.save_rois()
        self.refresh_spinboxes()
        QMessageBox.information(self, "ROI Cleared", f"{name} ROI cleared.")

    def refresh_spinboxes(self):
        if not hasattr(self.app, "ocr_pipeline") or not self.app.ocr_pipeline:
            return
        for name, spins in self.roi_spinboxes.items():
            roi = self.app.ocr_pipeline.rois.get(name)
            for spin in spins.values():
                spin.blockSignals(True)
            if roi and len(roi) >= 4:
                spins["X"].setValue(roi[0] * 100.0)
                spins["Y"].setValue(roi[1] * 100.0)
                spins["W"].setValue(roi[2] * 100.0)
                spins["H"].setValue(roi[3] * 100.0)
                spins["Angle"].setValue(roi[4] if len(roi) > 4 else 0.0)
            else:
                for spin in spins.values():
                    spin.setValue(0.0)
            for spin in spins.values():
                spin.blockSignals(False)

    def receive_frame_threadsafe(self, frame, roi_imgs=None):
        import time
        import os
        import cv2

        self.last_frame = frame.copy()

        frame_ts = time.time()

        if getattr(self.app, "chk_burst_on_snap", None) and self.app.chk_burst_on_snap.isChecked():
            if roi_imgs is not None:
                safe_crops = {k: v.copy() for k, v in roi_imgs.items() if v is not None}
                self.frame_buffer.append((frame_ts, safe_crops))
        else:
            self.frame_buffer.clear()

        # Phase 1: Intercept Calibration Burst
        if getattr(self.app, "calibration_burst_count", 0) > 0:
            self.app.calibration_burst_count -= 1

            if self.app.calibration_burst_count == 0:
                meter_id = getattr(self.app, "calibration_burst_meter", 1)
                serial_ts = getattr(self.app, "calibration_burst_serial_ts", 0)
                screen_val = getattr(self.app, "calibration_burst_screen_val", "0.0")

                sess_dir = getattr(self.app, "calibration_session_dir", None)
                if not sess_dir:
                    sess_dir = os.path.join(os.getcwd(), "Sessions", f"calibration_session_{int(serial_ts)}")
                    self.app.calibration_session_dir = sess_dir
                    os.makedirs(os.path.join(sess_dir, "extracted_crops"), exist_ok=True)

                for f_ts, buffered_crops in list(self.frame_buffer):
                    roi_name = f"m{meter_id}_cond"
                    roi_img = buffered_crops.get(roi_name)

                    if roi_img is not None and roi_img.size > 0:
                        cnn_img = cv2.resize(roi_img, (128, 128))
                        # Filename: screenval_serialts_framets_meterid.jpg
                        filename = f"{screen_val}_{serial_ts:.3f}_{f_ts:.3f}_{meter_id}.jpg"
                        out_path = os.path.join(sess_dir, "extracted_crops", filename)
                        cv2.imwrite(out_path, cnn_img)

                try:
                    from PyQt6.QtCore import QTimer

                    QTimer.singleShot(
                        0, lambda: self.app.btn_calibration_burst.setText("🎯 Capture Calibration Burst")
                    )
                    QTimer.singleShot(0, lambda: self.app.btn_calibration_burst.setEnabled(True))
                except Exception:
                    pass

                self.app.calibration_session_dir = None

                bursts_rem = getattr(self.app, "bursts_remaining", 0)
                if bursts_rem > 0:
                    self.app.calibration_burst_requested = True
                    try:
                        from PyQt6.QtCore import QTimer

                        QTimer.singleShot(
                            0,
                            lambda: self.app.btn_calibration_burst.setText(
                                f"⏳ Waiting for Serial ({bursts_rem} left)..."
                            ),
                        )
                        QTimer.singleShot(0, lambda: self.app.btn_calibration_burst.setEnabled(False))
                    except Exception:
                        pass
        try:
            h, w = frame.shape[:2]
            if w > 1920:
                small_frame = cv2.resize(frame, (1280, 720))
            else:
                small_frame = frame
            self.image_ready.emit(small_frame)
        except Exception:
            self.image_ready.emit(frame)

    def set_image(self, frame):
        rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        q_img = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()

        pixmap = QPixmap.fromImage(q_img)
        scaled_pixmap = pixmap.scaled(
            self.image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled_pixmap)


class MeterApp(QMainWindow):
    ocr_readings_signal = pyqtSignal(list)
    ocr_frame_signal = pyqtSignal(object, object)
    update_check_finished = pyqtSignal(object, object)  # (Release or None, error message or None)
    background_update_checked = pyqtSignal(object, object)  # same, from the silent check at start
    update_install_finished = pyqtSignal(object, str)  # (error message or None, log text)

    def __init__(self):
        super().__init__()
        import os
        import json

        self.setWindowTitle(f"Apera Meter GUI {__version__}")
        self.resize(1400, 800)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("QTabBar::tab { padding: 6px 16px; }")

        self.config_path = os.path.join(CONFIG_DIR, "config.json")
        self.shared_config = {}
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path) as f:
                    self.shared_config = json.load(f)
            except Exception:
                pass

        # Instantiate buttons without placing them in the global toolbar
        self.ocr_checkbox = QPushButton("🎥 Turn Camera On")
        self.ocr_checkbox.setCheckable(True)
        self.ocr_checkbox.setStyleSheet("font-weight: bold; font-size: 14px; padding: 5px;")
        self.ocr_checkbox.toggled.connect(self.toggle_ocr_mode)

        self.ocr_autostart_chk = QCheckBox("Auto-Start Camera on Launch")
        self.ocr_autostart_chk.setChecked(self.shared_config.get("ocr_auto_start", True))
        self.ocr_autostart_chk.setStyleSheet("margin-left: 10px; color: gray;")
        self.ocr_autostart_chk.stateChanged.connect(self._save_ocr_config)

        self.dataset_checkbox = QCheckBox("Auto-Record OCR Training Dataset")
        self.dataset_checkbox.setStyleSheet(
            "font-weight: bold; margin-left: 10px; margin-right: 10px; color: #2980b9;"
        )
        self.dataset_checkbox.stateChanged.connect(self.toggle_dataset_mode)

        self.ocr_autolog_checkbox = QCheckBox("Auto-Log OCR to Graphs/Tables")
        self.ocr_autolog_checkbox.setChecked(self.shared_config.get("ocr_autolog", True))
        self.ocr_autolog_checkbox.setStyleSheet(
            "font-weight: bold; margin-left: 10px; margin-right: 10px; color: #27ae60;"
        )
        self.ocr_autolog_checkbox.stateChanged.connect(self._save_ocr_config)

        self.ocr_warn_streams_chk = QCheckBox("Warn if missing streams on run start")
        self.ocr_warn_streams_chk.setChecked(self.shared_config.get("ocr_warn_streams", True))
        self.ocr_warn_streams_chk.stateChanged.connect(self._save_ocr_config)

        self.ocr_dual_mode_chk = QCheckBox("Require CNN && CV Consensus (Dual-Mode)")
        self.ocr_dual_mode_chk.setChecked(self.shared_config.get("ocr_dual_mode", True))
        self.ocr_dual_mode_chk.setToolTip("If enabled, requires both CNN and Classic algorithms to agree.")
        self.ocr_dual_mode_chk.stateChanged.connect(self._save_ocr_config)

        self.ocr_discard_chk = QCheckBox("Discard Bad Reads (Return 0.0)")
        self.ocr_discard_chk.setChecked(self.shared_config.get("ocr_discard", False))
        self.ocr_discard_chk.stateChanged.connect(self._save_ocr_config)

        self.ocr_midpoint_chk = QCheckBox("Smooth Sub-interval Reads (Midpoint Mean)")
        self.ocr_midpoint_chk.setChecked(self.shared_config.get("ocr_midpoint", True))
        self.ocr_midpoint_chk.stateChanged.connect(self._save_ocr_config)

        self.ocr_conf_spin = QDoubleSpinBox()
        self.ocr_conf_spin.setRange(0.0, 1.0)
        self.ocr_conf_spin.setSingleStep(0.05)
        self.ocr_conf_spin.setDecimals(2)
        self.ocr_conf_spin.setValue(self.shared_config.get("ocr_conf", 0.0))
        self.ocr_conf_spin.valueChanged.connect(self._save_ocr_config)

        self.ocr_enforce_max_chk = QCheckBox("Clamp LCD Overflow (Max Value):")
        self.ocr_enforce_max_chk.setChecked(self.shared_config.get("ocr_enforce_max", True))
        self.ocr_enforce_max_chk.stateChanged.connect(self._save_ocr_config)

        self.ocr_max_val_spin = QSpinBox()
        self.ocr_max_val_spin.setRange(10, 999999)
        self.ocr_max_val_spin.setValue(self.shared_config.get("ocr_max_val", 1999))
        self.ocr_max_val_spin.valueChanged.connect(self._save_ocr_config)

        self.ocr_phys_max_chk = QCheckBox("Enforce Absolute Max (mS/cm):")
        self.ocr_phys_max_chk.setChecked(self.shared_config.get("ocr_phys_max_chk", True))
        self.ocr_phys_max_chk.stateChanged.connect(self._save_ocr_config)

        self.ocr_phys_max_spin = QDoubleSpinBox()
        self.ocr_phys_max_spin.setRange(0.0, 9999.0)
        self.ocr_phys_max_spin.setDecimals(1)
        self.ocr_phys_max_spin.setValue(self.shared_config.get("ocr_phys_max_spin", 199.9))
        self.ocr_phys_max_spin.valueChanged.connect(self._save_ocr_config)

        self.ocr_enforce_dev_chk = QCheckBox("Limit Max Frame-to-Frame Dev (%):")
        self.ocr_enforce_dev_chk.setChecked(self.shared_config.get("ocr_enforce_dev_chk", False))
        self.ocr_enforce_dev_chk.stateChanged.connect(self._save_ocr_config)

        self.ocr_max_dev_spin = QSpinBox()
        self.ocr_max_dev_spin.setRange(1, 99999)
        self.ocr_max_dev_spin.setValue(self.shared_config.get("ocr_max_dev", 50))
        self.ocr_max_dev_spin.valueChanged.connect(self._save_ocr_config)

        # Jump filter removed

        self.ocr_unit_combo = QComboBox()
        self.ocr_unit_combo.addItems(
            ["Auto-detect (Closest Ratio)", "Auto-detect (Assume Spike)", "Auto-detect (Assume Drop)"]
        )
        self.ocr_unit_combo.setCurrentText(
            self.shared_config.get("ocr_unit_force", "Auto-detect (Closest Ratio)")
        )
        self.ocr_unit_combo.currentTextChanged.connect(self._save_ocr_config)

        self.spin_burst_count = QSpinBox()
        self.spin_burst_count.setRange(1, 100)
        self.spin_burst_count.setValue(self.shared_config.get("ocr_burst_count", 1))
        self.spin_burst_count.valueChanged.connect(self._save_ocr_config)
        self.spin_burst_count.setPrefix("Bursts: ")
        self.spin_burst_count.setStyleSheet("font-weight: bold; font-size: 13px; margin-left: 20px;")

        self.bursts_remaining = 0
        self.calibration_burst_requested = False
        self.calibration_burst_count = 0
        self.btn_calibration_burst = QPushButton("🎯 Capture Calibration Burst")
        self.btn_calibration_burst.setStyleSheet(
            "background-color: #8E44AD; color: white; font-weight: bold;"
        )
        self.btn_calibration_burst.clicked.connect(self.request_calibration_burst)

        self.manual_snap_requested = False
        self.btn_manual_snap = QPushButton("🎯 Arm Capture (Press M+ on Meter)")
        self.btn_manual_snap.setStyleSheet("background-color: #2980B9; color: white; font-weight: bold;")
        self.btn_manual_snap.clicked.connect(self.request_manual_snap)

        self.chk_apply_lag = QCheckBox("Apply Hardware Lag to Armed Capture")
        self.chk_apply_lag.setChecked(self.shared_config.get("apply_hardware_lag", True))
        self.chk_apply_lag.setStyleSheet("color: #34495E; font-size: 13px; margin-left: 20px;")
        self.chk_apply_lag.stateChanged.connect(
            lambda s: self.shared_config.update({"apply_hardware_lag": bool(s)})
        )

        self.chk_burst_on_snap = QCheckBox("Capture 30-Frame Burst on Armed Capture")
        self.chk_burst_on_snap.setChecked(self.shared_config.get("burst_on_snap", False))
        self.chk_burst_on_snap.setStyleSheet("color: #8E44AD; font-size: 13px; margin-left: 20px;")
        self.chk_burst_on_snap.stateChanged.connect(
            lambda s: self.shared_config.update({"burst_on_snap": bool(s)})
        )

        from ocr_pipeline import MeterOCR

        self.ocr_pipeline = MeterOCR()

        self.ocr_readings_signal.connect(self.on_ocr_readings)
        self.ocr_pipeline.add_callback(lambda readings: self.ocr_readings_signal.emit(readings))

        self.ocr_feed_tab = OCRFeedTab(self)

        # Setup polling for camera frames to prevent Memory Queue leaks
        self._latest_camera_frame = None
        self._latest_camera_rois = None

        def _store_frame(frame, rois):
            self._latest_camera_frame = frame
            self._latest_camera_rois = rois

        self.ocr_pipeline.add_frame_callback(_store_frame)

        self.camera_poll_timer = QTimer(self)
        self.camera_poll_timer.timeout.connect(self._poll_camera_frame)
        self.camera_poll_timer.start(100)  # ~10 FPS

        # Populate initial values
        self.ocr_feed_tab.refresh_spinboxes()

        self.dataset_session_dir = None
        self.dataset_csv_path = None

        self.setCentralWidget(self.tabs)

        # Add Global Status Bar
        self.status_m1 = QPushButton("⚠️ M1: NO SERIAL (DISCONNECTED)")
        self.status_m2 = QPushButton("⚠️ M2: NO SERIAL (DISCONNECTED)")
        self.status_cam = QPushButton("🎥 Camera: Off")

        # Style status labels
        self.status_m1.setStyleSheet(
            "color: #e74c3c; font-weight: bold; margin-right: 5px; "
            "border: 1px solid #e74c3c; border-radius: 3px; padding: 2px;"
        )
        self.status_m2.setStyleSheet(
            "color: #e74c3c; font-weight: bold; margin-right: 20px; "
            "border: 1px solid #e74c3c; border-radius: 3px; padding: 2px;"
        )
        self.status_cam.setStyleSheet("color: gray; font-weight: bold; margin-right: 20px; padding: 3px;")

        self.statusBar().addWidget(self.status_m1)
        self.statusBar().addWidget(self.status_m2)
        self.statusBar().addWidget(self.status_cam)
        self.status_cam.clicked.connect(self.ocr_checkbox.toggle)

        # Add OCR controls to status bar
        self.statusBar().addPermanentWidget(self.ocr_autolog_checkbox)

        self.chk_record_m1 = QCheckBox("Rec M1")
        self.chk_record_m1.setStyleSheet("font-weight: bold; margin-left: 10px; color: #e74c3c;")
        self.chk_record_m1.clicked.connect(
            lambda: self.meter1.toggle_record() if hasattr(self, "meter1") else None
        )
        self.chk_record_m1.clicked.connect(self._on_record_toggled)
        self.statusBar().addPermanentWidget(self.chk_record_m1)

        self.chk_record_m2 = QCheckBox("Rec M2")
        self.chk_record_m2.setStyleSheet("font-weight: bold; margin-left: 10px; color: #e74c3c;")
        self.chk_record_m2.clicked.connect(
            lambda: self.meter2.toggle_record() if hasattr(self, "meter2") else None
        )
        self.chk_record_m2.clicked.connect(self._on_record_toggled)
        self.statusBar().addPermanentWidget(self.chk_record_m2)

        lbl_rate = QLabel(" OCR Sampling Rate (s): ")
        lbl_rate.setStyleSheet("font-weight: bold; margin-left: 20px;")
        self.statusBar().addPermanentWidget(lbl_rate)

        self.ocr_interval_spin = QDoubleSpinBox()
        self.ocr_interval_spin.setRange(1.0, 60.0)
        self.ocr_interval_spin.setDecimals(2)
        self.ocr_interval_spin.setSingleStep(0.1)

        default_interval = self.shared_config.get("ocr_interval", 1.0)
        if hasattr(self, "ocr_pipeline"):
            self.ocr_pipeline.ocr_interval = default_interval
        self.ocr_interval_spin.setValue(default_interval)

        def on_ocr_interval_changed(val):
            if hasattr(self, "ocr_pipeline"):
                self.ocr_pipeline.ocr_interval = val
            self.shared_config["ocr_interval"] = val
            self._save_ocr_config()

        self.ocr_interval_spin.valueChanged.connect(on_ocr_interval_changed)
        self.statusBar().addPermanentWidget(self.ocr_interval_spin)

        self.lbl_ocr_status = QLabel("OCR: Idle")
        self.lbl_ocr_status.setStyleSheet("font-weight: bold; margin-left: 20px; color: gray;")
        self.statusBar().addWidget(self.lbl_ocr_status)

        # Version, bottom right. Turns amber when GitHub has a newer release (see _start_background_checks).
        self.btn_version = QPushButton(f"v{__version__}")
        self.btn_version.setFlat(True)
        self.btn_version.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_version.setToolTip("Version of this app. Click to check for updates.")
        self.btn_version.clicked.connect(self.check_for_updates)
        self._set_version_badge("unknown")
        self.statusBar().addPermanentWidget(self.btn_version)

        self.meter1 = MeterController(meter_id=1, parent_app=self)
        self.meter2 = MeterController(meter_id=2, parent_app=self)

        self.status_m1.clicked.connect(self.meter1.toggle_connection)
        self.status_m2.clicked.connect(self.meter2.toggle_connection)

        # Synchronize splitter positions between both meters so UI layouts match
        def sync_splitters(pos, index, target_splitter):
            target_splitter.blockSignals(True)
            target_splitter.moveSplitter(pos, index)
            target_splitter.blockSignals(False)

        self.meter1.main_splitter.splitterMoved.connect(
            lambda pos, index: sync_splitters(pos, index, self.meter2.main_splitter)
        )
        self.meter2.main_splitter.splitterMoved.connect(
            lambda pos, index: sync_splitters(pos, index, self.meter1.main_splitter)
        )

        self.meter1.mid_splitter.splitterMoved.connect(
            lambda pos, index: sync_splitters(pos, index, self.meter2.mid_splitter)
        )
        self.meter2.mid_splitter.splitterMoved.connect(
            lambda pos, index: sync_splitters(pos, index, self.meter1.mid_splitter)
        )
        self.settings_tab = SettingsTab(self)

        from dataset_curation_tab import DatasetCurationTab

        self.dataset_curation_tab = DatasetCurationTab(self)

        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.addTab(self.meter1, "Meter 1")
        self.tabs.addTab(self.meter2, "Meter 2")
        self.tabs.addTab(self.ocr_feed_tab, "Live OCR Feed")
        self.tabs.addTab(self.dataset_curation_tab, "Dataset Curation")

        if self.ocr_autostart_chk.isChecked():
            QTimer.singleShot(2500, lambda: self.ocr_checkbox.setChecked(True))

        try:
            if "app_geometry" in self.meter1.config:
                from PyQt6.QtCore import QByteArray

                self.restoreGeometry(QByteArray.fromHex(self.meter1.config["app_geometry"].encode()))

            if "app_tab" in self.meter1.config:
                self.tabs.setCurrentIndex(self.meter1.config["app_tab"])

            widths1 = self.meter1.config.get(f"col_widths_{self.meter1.meter_id}")
            if widths1:
                for i, w in enumerate(widths1):
                    self.meter1.table.setColumnWidth(i, w)

            widths2 = self.meter1.config.get(f"col_widths_{self.meter2.meter_id}")
            if widths2:
                for i, w in enumerate(widths2):
                    self.meter2.table.setColumnWidth(i, w)

            state1_main = self.meter1.config.get(f"main_splitter_{self.meter1.meter_id}")
            if state1_main:
                self.meter1.main_splitter.restoreState(QByteArray.fromHex(state1_main.encode()))
            state1_mid = self.meter1.config.get(f"mid_splitter_{self.meter1.meter_id}")
            if state1_mid:
                self.meter1.mid_splitter.restoreState(QByteArray.fromHex(state1_mid.encode()))

            state2_main = self.meter1.config.get(f"main_splitter_{self.meter2.meter_id}")
            if state2_main:
                self.meter2.main_splitter.restoreState(QByteArray.fromHex(state2_main.encode()))
            state2_mid = self.meter1.config.get(f"mid_splitter_{self.meter2.meter_id}")
            if state2_mid:
                self.meter2.mid_splitter.restoreState(QByteArray.fromHex(state2_mid.encode()))
        except Exception:
            pass

    # --- Desktop shortcut ----------------------------------------------------
    def create_desktop_shortcut(self, checked=False):
        import shortcuts

        try:
            path = shortcuts.create_desktop_shortcut(script_dir)
        except shortcuts.ShortcutError as e:
            QMessageBox.warning(self, "Create Desktop Shortcut", str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, "Create Desktop Shortcut", f"Unexpected error: {e}")
            return
        QMessageBox.information(
            self,
            "Create Desktop Shortcut",
            f"Shortcut created on the desktop:\n{path}\n\nIt keeps working after updates.",
        )

    # --- Version badge and background checks -----------------------------------
    def _set_version_badge(self, state, latest=None):
        """state: 'unknown' (not checked / offline), 'current', or 'outdated'."""
        if state == "outdated":
            self.btn_version.setText(f"⬆️ v{__version__} – {latest} available")
            self.btn_version.setToolTip(
                f"A newer version ({latest}) is available. Click to see what changed and update."
            )
            self.btn_version.setStyleSheet(
                "QPushButton { color: #1e1e1e; background-color: #f39c12; border-radius: 3px;"
                " padding: 2px 8px; font-weight: bold; }"
            )
        else:
            self.btn_version.setText(f"v{__version__}")
            tip = (
                f"Up to date (latest release is {latest}). Click to check again."
                if state == "current"
                else "Version of this app. Click to check for updates."
            )
            self.btn_version.setToolTip(tip)
            self.btn_version.setStyleSheet("QPushButton { color: #9aa0a6; border: none; padding: 2px 8px; }")

    def _start_background_checks(self):
        """After start-up: quietly look for a newer release (and again once a day while running),
        and refresh an existing desktop shortcut so its icon and target stay current."""
        if os.environ.get("APERA_NO_BACKGROUND_CHECKS") == "1":
            return
        self.background_update_checked.connect(self._on_background_update_checked)
        QTimer.singleShot(3000, self._automatic_update_check)
        self._daily_update_timer = QTimer(self)
        self._daily_update_timer.timeout.connect(self._automatic_update_check)
        self._daily_update_timer.start(24 * 60 * 60 * 1000)

        def refresh_shortcut():
            try:
                import shortcuts

                shortcuts.refresh_existing_shortcut(script_dir)
            except Exception as e:
                print(f"Couldn't refresh the desktop shortcut: {e}")

        threading.Thread(target=refresh_shortcut, daemon=True).start()

    def update_check_on_start(self):
        """Settings → "Check for updates when the app starts" (on unless the user turned it off)."""
        return bool(self.meter1.config.get("check_updates_on_start", True))

    def set_update_check_on_start(self, on):
        self.meter1.config["check_updates_on_start"] = bool(on)
        self.meter1.save_config()

    def _automatic_update_check(self):
        """The quiet check a few seconds after start and once a day; skipped when turned off."""
        if self.update_check_on_start():
            self._background_update_check()

    def _background_update_check(self):
        import updater

        def worker():
            try:
                self.background_update_checked.emit(updater.fetch_latest_release(), None)
            except Exception as e:  # offline, rate-limited... stay quiet
                self.background_update_checked.emit(None, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_background_update_checked(self, release, error):
        import updater

        if error or release is None:
            self._set_version_badge("unknown")
            self.btn_version.setToolTip("Couldn't check for updates (offline?). Click to try again.")
            return
        if updater.is_newer(release.tag, __version__):
            self._set_version_badge("outdated", release.tag)
            self.settings_tab.lbl_update_status.setText(f"{release.tag} is available.")
        else:
            self._set_version_badge("current", release.tag)

    # --- Updates -------------------------------------------------------------
    def check_for_updates(self, checked=False):
        """Ask GitHub for the newest release (in the background) and offer to install it."""
        import updater

        tab = self.settings_tab
        tab.btn_update.setEnabled(False)
        tab.lbl_update_status.setText("Checking GitHub…")

        def worker():
            try:
                self.update_check_finished.emit(updater.fetch_latest_release(), None)
            except updater.UpdateError as e:
                self.update_check_finished.emit(None, str(e))
            except Exception as e:
                self.update_check_finished.emit(None, f"Unexpected error while checking: {e}")

        if not getattr(self, "_update_signals_connected", False):
            self.update_check_finished.connect(self._on_update_checked)
            self.update_install_finished.connect(self._on_update_installed)
            self._update_signals_connected = True
        threading.Thread(target=worker, daemon=True).start()

    def _on_update_checked(self, release, error):
        import updater

        tab = self.settings_tab
        tab.btn_update.setEnabled(True)
        if error:
            tab.lbl_update_status.setText("Couldn't check for updates.")
            QMessageBox.warning(self, "Check for Updates", error)
            return
        if not updater.is_newer(release.tag, __version__):
            self._set_version_badge("current", release.tag)
            tab.lbl_update_status.setText(f"Up to date (latest release is {release.tag}).")
            QMessageBox.information(
                self, "Check for Updates", f"You have the latest version ({__version__})."
            )
            return

        tab.lbl_update_status.setText(f"{release.tag} is available.")
        self._set_version_badge("outdated", release.tag)
        kind = updater.install_kind(script_dir)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Update available: {release.name}")
        dialog.resize(640, 480)
        lay = QVBoxLayout(dialog)
        lay.addWidget(QLabel(f"<b>{release.name}</b> is available. You have version {__version__}."))
        notes = QTextEdit()
        notes.setReadOnly(True)
        notes.setMarkdown(release.notes or "No release notes.")
        lay.addWidget(notes, 1)
        if kind == "git":
            hint = (
                "Updating downloads the new version, reinstalls its Python packages and restarts "
                "the app. Your data and settings are not touched."
            )
            action = "⬆️ Update and Restart"
        else:
            hint = "Download the new version from the release page, then replace this copy with it."
            action = "🌐 Open Download Page"
        info = QLabel(hint)
        info.setWordWrap(True)
        lay.addWidget(info)
        row = QHBoxLayout()
        row.addStretch(1)
        btn_later = QPushButton("Later")
        btn_go = QPushButton(action)
        btn_go.setDefault(True)
        btn_go.setStyleSheet("background-color: #8e44ad; color: white; padding: 6px 14px; font-weight: bold;")
        btn_later.clicked.connect(dialog.reject)
        btn_go.clicked.connect(dialog.accept)
        row.addWidget(btn_later)
        row.addWidget(btn_go)
        lay.addLayout(row)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        if kind != "git":
            from PyQt6.QtCore import QUrl
            from PyQt6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(release.url))
            return

        if (self.meter1.is_recording or self.meter2.is_recording) and QMessageBox.question(
            self,
            "Recording in progress",
            "A meter is still recording. Updating restarts the app and stops the recording.\n\n"
            "Update anyway?",
        ) != QMessageBox.StandardButton.Yes:
            return
        self._install_update(release)

    def _install_update(self, release):
        import updater

        from PyQt6.QtWidgets import QProgressDialog

        self._update_progress = QProgressDialog(f"Installing {release.tag}…", None, 0, 0, self)
        self._update_progress.setWindowTitle("Updating")
        self._update_progress.setMinimumDuration(0)
        self._update_progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._update_progress.show()
        self._pending_release = release

        def worker():
            lines = []
            try:
                updater.update_git_clone(script_dir, release.tag, log=lines.append)
                self.update_install_finished.emit(None, "\n".join(lines))
            except updater.UpdateError as e:
                self.update_install_finished.emit(str(e), "\n".join(lines))
            except Exception as e:
                self.update_install_finished.emit(f"Unexpected error while updating: {e}", "\n".join(lines))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_installed(self, error, log_text):
        self._update_progress.close()
        tag = self._pending_release.tag
        if error:
            box = QMessageBox(QMessageBox.Icon.Warning, "Update failed", error, parent=self)
            box.setDetailedText(log_text)
            box.exec()
            return
        self.settings_tab.lbl_update_status.setText(f"Updated to {tag}; restarting…")
        QMessageBox.information(self, "Update installed", f"Updated to {tag}. The app will now restart.")
        self.restart_app()

    def restart_app(self, checked=False):
        import sys
        import subprocess

        # If self.close() returns False, the user canceled the closing (e.g. unsaved data)
        if not self.close():
            return

        if getattr(sys, "frozen", False):
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable] + sys.argv

        single_instance.release()  # otherwise the new copy finds this one still running and quits
        subprocess.Popen(args)  # noqa: S603 - relaunches this same program
        QApplication.quit()

    # --- Diagnostics (Settings → Open Log Folder / Copy Diagnostics) ----------
    def open_log_folder(self, checked=False):
        try:
            diagnostics.open_folder(diagnostics.log_folder(crash_log.log_path()))
        except OSError as e:
            QMessageBox.warning(self, "Open Log Folder", f"Couldn't open the log folder.\n\n{e}")

    def _diagnostics_extra(self):
        """App-specific lines for the report: meter connections and the OCR model (nothing secret)."""
        extra = {}
        for meter in (self.meter1, self.meter2):
            ser = meter.ser
            if meter.is_connected and ser is not None:
                state = f"connected on {ser.port}"
            else:
                state = f"not connected (port: {meter.port_combo.currentText() or 'none'})"
            model = meter.model_label.text().removeprefix("Model: ")
            if model and model != "Unknown":
                state += f", model {model}"
            if meter.is_recording:
                state += ", recording"
            extra[f"Meter {meter.meter_id}"] = state
        ocr = getattr(self, "ocr_pipeline", None)
        extra["OCR model"] = "loaded" if getattr(ocr, "model", None) is not None else "not loaded"
        output_log = os.path.join(DATA_DIR, "app.log")
        if os.path.exists(output_log):
            extra["Output log"] = output_log
        return extra

    def diagnostics_text(self):
        return diagnostics.report(
            APP_NAME,
            __version__,
            APP_DIR,
            settings_dir=CONFIG_DIR,
            data_dir=DATA_DIR,
            log_path=crash_log.log_path(),
            extra=self._diagnostics_extra(),
        )

    def copy_diagnostics(self, checked=False):
        QApplication.clipboard().setText(self.diagnostics_text())
        self.settings_tab.lbl_diagnostics.setText(
            "Diagnostics copied. Paste them into a GitHub issue or an email."
        )

    def _poll_camera_frame(self):
        if self._latest_camera_frame is not None:
            # Pop the reference off so we only process it once
            frame = self._latest_camera_frame
            rois = self._latest_camera_rois
            self._latest_camera_frame = None
            self.ocr_feed_tab.receive_frame_threadsafe(frame, rois)

    def _save_ocr_config(self):
        import json

        self.shared_config["ocr_autolog"] = self.ocr_autolog_checkbox.isChecked()
        self.shared_config["ocr_warn_streams"] = self.ocr_warn_streams_chk.isChecked()
        self.shared_config["ocr_auto_start"] = self.ocr_autostart_chk.isChecked()
        self.shared_config["ocr_dual_mode"] = self.ocr_dual_mode_chk.isChecked()
        self.shared_config["ocr_discard"] = self.ocr_discard_chk.isChecked()
        self.shared_config["ocr_midpoint"] = self.ocr_midpoint_chk.isChecked()
        self.shared_config["ocr_conf"] = self.ocr_conf_spin.value()
        self.shared_config["ocr_enforce_max"] = self.ocr_enforce_max_chk.isChecked()
        self.shared_config["ocr_max_val"] = self.ocr_max_val_spin.value()
        self.shared_config["ocr_max_dev"] = self.ocr_max_dev_spin.value()
        self.shared_config["ocr_unit_force"] = self.ocr_unit_combo.currentText()
        self.shared_config["ocr_burst_count"] = self.spin_burst_count.value()
        self.shared_config["ocr_phys_max_chk"] = self.ocr_phys_max_chk.isChecked()
        self.shared_config["ocr_phys_max_spin"] = self.ocr_phys_max_spin.value()

        if hasattr(self, "ocr_pipeline") and self.ocr_pipeline is not None:
            self.ocr_pipeline.use_midpoint_filter = self.ocr_midpoint_chk.isChecked()
            self.ocr_pipeline.min_confidence = self.ocr_conf_spin.value()
        try:
            with open(self.config_path, "w") as f:
                json.dump(self.shared_config, f, indent=4)
        except Exception:
            pass

    def _on_record_toggled(self):
        if not getattr(self, "ocr_warn_streams_chk", None) or not self.ocr_warn_streams_chk.isChecked():
            return

        m1_rec = self.meter1.is_recording if hasattr(self, "meter1") else False
        m2_rec = self.meter2.is_recording if hasattr(self, "meter2") else False

        # If any is recording, start a 15-second timer
        if m1_rec or m2_rec:
            # Prevent multiple timers
            if getattr(self, "_warning_timer_active", False):
                return
            self._warning_timer_active = True
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(15000, self._check_streams_active)
        else:
            self._warning_timer_active = False

    def _check_streams_active(self):
        self._warning_timer_active = False
        m1_rec = self.meter1.is_recording if hasattr(self, "meter1") else False
        m2_rec = self.meter2.is_recording if hasattr(self, "meter2") else False

        if not m1_rec and not m2_rec:
            return  # User stopped recording before timer fired

        missing = []
        if not self.ocr_checkbox.isChecked():
            missing.append("OCR Camera (Turn Camera On)")

        m1_conn = self.meter1.is_connected if hasattr(self, "meter1") else False
        m2_conn = self.meter2.is_connected if hasattr(self, "meter2") else False

        if not m1_conn:
            missing.append("Meter 1 Serial Connection (Disconnected)")
        if not m1_rec:
            missing.append("Meter 1 Recording Toggle (Rec M1)")

        if not m2_conn:
            missing.append("Meter 2 Serial Connection (Disconnected)")
        if not m2_rec:
            missing.append("Meter 2 Recording Toggle (Rec M2)")

        if missing:
            from PyQt6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self,
                "Missing Data Streams Warning",
                "You have started recording a run, but the following streams are offline "
                "or not recording:\n\n"
                + "\n".join([f"- {m}" for m in missing])
                + "\n\nIf you intended to record all of them, please enable them now "
                "to prevent unit logic lock-outs.",
            )

    def set_ocr_status(self, text, color):
        if hasattr(self, "status_cam"):
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(0, lambda: self.status_cam.setText(text))
            QTimer.singleShot(
                0,
                lambda: self.status_cam.setStyleSheet(
                    f"color: {color}; font-weight: bold; margin-right: 20px; padding: 3px;"
                ),
            )

    def update_meter_status(self, meter_id, is_connected, port=""):
        status_label = self.status_m1 if meter_id == 1 else self.status_m2
        if is_connected:
            status_label.setText(f"M{meter_id}: Connected ({port})")
            status_label.setStyleSheet("color: #27ae60; font-weight: bold; margin-right: 20px;")
        else:
            status_label.setText(f"⚠️ M{meter_id}: NO SERIAL (DISCONNECTED)")
            status_label.setStyleSheet(
                "color: #e74c3c; font-weight: bold; margin-right: 20px; "
                "border: 1px solid #e74c3c; border-radius: 3px; padding: 2px;"
            )

    def toggle_ocr_mode(self, state):
        import time

        if state:
            if not self.ocr_pipeline.start():
                # No camera at this index: say so instead of claiming the feed is active.
                self.ocr_checkbox.blockSignals(True)
                self.ocr_checkbox.setChecked(False)
                self.ocr_checkbox.blockSignals(False)
                self.ocr_checkbox.setText("🎥 Turn Camera On")
                self.set_ocr_status("🎥 Camera: Not found", "#e67e22")
                return
            self.ocr_checkbox.setText("🎥 Turn Camera Off")
            self.set_ocr_status("🎥 Camera: Feed Active", "#27ae60")
            self.meter1.ocr_active = True
            self.meter2.ocr_active = True
            self.ocr_start_time = time.time()
        else:
            self.ocr_checkbox.setText("🎥 Turn Camera On")
            self.ocr_pipeline.stop()
            self.set_ocr_status("🎥 Camera: Off", "gray")
            self.meter1.ocr_active = False
            self.meter2.ocr_active = False

    def toggle_dataset_mode(self, state):
        import os
        import time

        self.shared_config["dataset_continuous"] = state == 2
        if state:
            # Create session folder
            timestamp = int(time.time())
            self.dataset_session_dir = os.path.join(os.getcwd(), "Sessions", f"dataset_session_{timestamp}")
            frames_dir = os.path.join(self.dataset_session_dir, "frames")
            os.makedirs(frames_dir, exist_ok=True)
            self.dataset_csv_path = os.path.join(self.dataset_session_dir, "readings.csv")
            with open(self.dataset_csv_path, "w", encoding="utf-8") as f:
                f.write("timestamp,meter_id,screen_val,unit,raw_cond\n")

            # Save ROIs immediately
            import json

            rois_path = os.path.join(self.dataset_session_dir, "rois.json")
            with open(rois_path, "w") as f:
                json.dump(self.ocr_pipeline.rois, f)
        else:
            self.dataset_session_dir = None
            self.dataset_csv_path = None

    def request_manual_snap(self):
        import os
        import time
        import json

        if not getattr(self, "dataset_session_dir", None):
            # Create session folder if it doesn't exist
            timestamp = int(time.time())
            self.dataset_session_dir = os.path.join(os.getcwd(), "Sessions", f"dataset_session_{timestamp}")
            frames_dir = os.path.join(self.dataset_session_dir, "frames")
            os.makedirs(frames_dir, exist_ok=True)
            self.dataset_csv_path = os.path.join(self.dataset_session_dir, "readings.csv")
            with open(self.dataset_csv_path, "w", encoding="utf-8") as f:
                f.write("timestamp,meter_id,screen_val,unit,raw_cond\n")
            rois_path = os.path.join(self.dataset_session_dir, "rois.json")
            with open(rois_path, "w") as f:
                json.dump(self.ocr_pipeline.rois, f)

        self.manual_snap_requested = True
        self.btn_manual_snap.setText("⏳ Waiting for Serial...")
        self.btn_manual_snap.setEnabled(False)

    def request_calibration_burst(self):
        if not self.ocr_checkbox.isChecked():
            QMessageBox.warning(
                self, "Camera Off", "Please turn on the camera first so we can capture burst crops."
            )
            return

        self.bursts_remaining = self.spin_burst_count.value()
        self.calibration_burst_requested = True
        self.btn_calibration_burst.setText(f"⏳ Waiting for Serial ({self.bursts_remaining} left)...")
        self.btn_calibration_burst.setEnabled(False)

    def _apply_ocr_consensus(self, val_cnn, val_cv, last_serial_ms):
        def _get_best_permutation(ocr_val, apply_dev_filter=True):
            if ocr_val == 0.0 or last_serial_ms == 0.0 or last_serial_ms is None:
                return ocr_val

            # Python's float() adds an artificial '.0' to whole numbers (e.g., 530 -> 530.0).
            # If we blindly strip the decimal, 530.0 becomes 5300, which is 10x larger and
            # falsely triggers the max value limit (1999), causing the correct CNN reading to be thrown out!
            ocr_str = str(ocr_val)
            if ocr_str.endswith(".0"):
                ocr_str = ocr_str[:-2]

            raw_str = ocr_str.replace(".", "")
            try:
                raw_int = int(raw_str)
            except Exception:
                return 0.0
            if raw_int == 0:
                return 0.0

            if getattr(self, "ocr_enforce_max_chk", None) and self.ocr_enforce_max_chk.isChecked():
                limit_spin = getattr(self, "ocr_max_val_spin", None)
                limit_val = limit_spin.value() if limit_spin else 1999
                if raw_int > limit_val:
                    return 0.0

            # Since the CNN perfectly predicts the decimal point, we no longer need to
            # generate all decimal permutations.
            # The only ambiguity is the UNIT on the LCD screen (mS vs uS).
            # The true conductivity in mS is either the literal value (if LCD is showing mS)
            # or value / 1000 (if LCD is showing uS).
            possible_ms_values = [ocr_val, ocr_val / 1000.0]

            unit_force = getattr(self, "ocr_unit_combo", None)
            unit_force_text = unit_force.currentText() if unit_force else "Auto-detect (Closest Ratio)"

            def _get_ratio(x):
                if x == 0.0 or last_serial_ms == 0.0:
                    return float("inf")
                return x / last_serial_ms if x > last_serial_ms else last_serial_ms / x

            if unit_force_text == "Auto-detect (Assume Spike)" and last_serial_ms > 0:
                # Pick the option that is strictly greater than the baseline, if one exists.
                spikes = [x for x in possible_ms_values if x > last_serial_ms]
                if spikes:
                    best_val = min(spikes, key=_get_ratio)
                else:
                    best_val = min(possible_ms_values, key=_get_ratio)
            elif unit_force_text == "Auto-detect (Assume Drop)" and last_serial_ms > 0:
                # Pick the option that is strictly less than the baseline, if one exists.
                drops = [x for x in possible_ms_values if x < last_serial_ms]
                if drops:
                    best_val = min(drops, key=_get_ratio)
                else:
                    best_val = min(possible_ms_values, key=_get_ratio)
            else:
                best_val = min(possible_ms_values, key=_get_ratio)

            if (
                apply_dev_filter
                and getattr(self, "ocr_enforce_dev_chk", None)
                and self.ocr_enforce_dev_chk.isChecked()
            ):
                max_dev = getattr(self, "ocr_max_dev_spin", None)
                dev_thresh = (max_dev.value() / 100.0) if max_dev else 0.5
                if abs(best_val - last_serial_ms) / last_serial_ms > dev_thresh:
                    return 0.0

            return best_val

        cnn_best = _get_best_permutation(val_cnn, True)

        dual_mode = getattr(self, "ocr_dual_mode_chk", None) and self.ocr_dual_mode_chk.isChecked()
        discard_bad = getattr(self, "ocr_discard_chk", None) and self.ocr_discard_chk.isChecked()

        if dual_mode:
            cv_best = _get_best_permutation(val_cv, True)
            final_val = cnn_best if cnn_best != 0.0 else cv_best
        else:
            final_val = cnn_best

        if final_val == 0.0 and not discard_bad:
            cnn_raw = _get_best_permutation(val_cnn, False)
            if dual_mode:
                cv_raw = _get_best_permutation(val_cv, False)
                result = cnn_raw if cnn_raw != 0.0 else cv_raw
            else:
                result = cnn_raw if cnn_raw != 0.0 else None
        else:
            result = final_val if final_val != 0.0 else None

        if (
            result is not None
            and getattr(self, "ocr_phys_max_chk", None)
            and self.ocr_phys_max_chk.isChecked()
        ):
            limit_spin = getattr(self, "ocr_phys_max_spin", None)
            if limit_spin and result > limit_spin.value():
                return None

        return result

    def on_ocr_readings(self, readings):
        import time
        import datetime

        if not getattr(self, "ocr_running_logged", False):
            self.set_ocr_status("🎥 OCR: Running (1 Hz)", "#27ae60")
            self.ocr_running_logged = True

        now = datetime.datetime.now()
        dt_str = now.strftime("%Y-%m-%d %H:%M:%S")

        if len(readings) > 0 and readings[0] is not None:
            c1_cnn, c1_cv, c1_conf = readings[0]
            min_conf = (
                getattr(self.ocr_pipeline, "min_confidence", 0.0) if hasattr(self, "ocr_pipeline") else 0.0
            )
            rejected = False
            if min_conf > 0.0 and c1_conf < min_conf:
                c1_cnn = 0.0
                rejected = True

            meter1_start = getattr(self.meter1, "first_serial_time", None)
            if meter1_start is None or time.time() - meter1_start >= 10:
                last_ms = getattr(self.meter1, "last_serial_cond_ms", 0.0)
                if last_ms == 0.0:
                    last_ms = getattr(self.meter1, "last_ocr_cond_ms", 0.0)
                c1 = self._apply_ocr_consensus(c1_cnn, c1_cv, last_ms)
                if c1 is not None and c1 != 0.0:
                    self.meter1.last_ocr_cond_ms = c1
                if c1 is not None:
                    row_data1 = (dt_str, "OCR", f"{c1 * 1000:.1f}", f"{c1:.3f}", "-", f"{c1_conf * 100:.1f}")
                    if (
                        self.meter1.is_recording
                        and getattr(self, "ocr_autolog_checkbox", None)
                        and self.ocr_autolog_checkbox.isChecked()
                    ):
                        self.meter1.signals.update_tree.emit(now, row_data1)
                elif rejected:
                    pass

            # Update status bar indicator for Meter 1
            if hasattr(self, "lbl_ocr_status"):
                if rejected:
                    self.lbl_ocr_status.setText(f"OCR: Rejected (< {min_conf * 100:.0f}%)")
                    self.lbl_ocr_status.setStyleSheet(
                        "font-weight: bold; margin-left: 20px; color: #e74c3c;"
                    )  # Red
                else:
                    self.lbl_ocr_status.setText(f"OCR: OK ({c1_conf * 100:.0f}%)")
                    self.lbl_ocr_status.setStyleSheet(
                        "font-weight: bold; margin-left: 20px; color: #2ecc71;"
                    )  # Green

        if len(readings) > 1 and readings[1] is not None:
            c2_cnn, c2_cv, c2_conf = readings[1]
            if min_conf > 0.0 and c2_conf < min_conf:
                c2_cnn = 0.0

            meter2_start = getattr(self.meter2, "first_serial_time", None)
            if meter2_start is None or time.time() - meter2_start >= 10:
                last_ms = getattr(self.meter2, "last_serial_cond_ms", 0.0)
                if last_ms == 0.0:
                    last_ms = getattr(self.meter2, "last_ocr_cond_ms", 0.0)
                c2 = self._apply_ocr_consensus(c2_cnn, c2_cv, last_ms)
                if c2 is not None and c2 != 0.0:
                    self.meter2.last_ocr_cond_ms = c2
                if c2 is not None:
                    row_data2 = (dt_str, "OCR", f"{c2 * 1000:.1f}", f"{c2:.3f}", "-", f"{c2_conf * 100:.1f}")
                    if (
                        self.meter2.is_recording
                        and getattr(self, "ocr_autolog_checkbox", None)
                        and self.ocr_autolog_checkbox.isChecked()
                    ):
                        self.meter2.signals.update_tree.emit(now, row_data2)

    def get_used_ports(self):
        used = []
        if self.meter1.is_connected and self.meter1.ser:
            used.append(self.meter1.ser.port)
        if self.meter2.is_connected and self.meter2.ser:
            used.append(self.meter2.ser.port)
        return used

    def closeEvent(self, event):
        if self.meter1.unsaved_changes or self.meter2.unsaved_changes:
            reply = QMessageBox.question(
                self,
                "Unsaved Data",
                "You have unsaved experimental data!\nAre you sure you want to close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        self.meter1.disconnect_meter()
        self.meter2.disconnect_meter()

        try:
            self.meter1.config["app_geometry"] = self.saveGeometry().toHex().data().decode()
            self.meter1.config["app_tab"] = self.tabs.currentIndex()
            self.meter1.config[f"col_widths_{self.meter1.meter_id}"] = [
                self.meter1.table.columnWidth(i) for i in range(self.meter1.table.columnCount())
            ]
            self.meter1.config[f"col_widths_{self.meter2.meter_id}"] = [
                self.meter2.table.columnWidth(i) for i in range(self.meter2.table.columnCount())
            ]

            self.meter1.config[f"main_splitter_{self.meter1.meter_id}"] = (
                self.meter1.main_splitter.saveState().toHex().data().decode()
            )
            self.meter1.config[f"mid_splitter_{self.meter1.meter_id}"] = (
                self.meter1.mid_splitter.saveState().toHex().data().decode()
            )
            self.meter1.config[f"main_splitter_{self.meter2.meter_id}"] = (
                self.meter2.main_splitter.saveState().toHex().data().decode()
            )
            self.meter1.config[f"mid_splitter_{self.meter2.meter_id}"] = (
                self.meter2.mid_splitter.saveState().toHex().data().decode()
            )

            self.meter1.save_config()
        except Exception:
            pass

        event.accept()


def create_app(argv=None):
    """Create the QApplication with the app icon and dark theme (shared by main() and scripts)."""
    import ctypes

    from PyQt6.QtGui import QIcon

    try:
        myappid = "apera.conductivity.meter.gui"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication.instance() or QApplication(sys.argv if argv is None else argv)

    icon_path = os.path.join(script_dir, "Conductivity meter.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    app.setStyleSheet(qdarktheme.load_stylesheet("dark"))
    return app


def _self_test(app, window):
    """Check that the packaged app starts, then exit: 0 = OK, 1 = window broken, 2 = OCR model missing.

    Used by the release build (``--self-test``); never touches the meters or saves anything.
    """
    import json

    from PyQt6.QtCore import QTimer

    def finish():
        tabs = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        ocr = getattr(window, "ocr_pipeline", None)
        report = {
            "version": __version__,
            "frozen": bool(getattr(sys, "frozen", False)),
            "tabs": tabs,
            "ocr_model_loaded": getattr(ocr, "model", None) is not None,
            "data_dir": DATA_DIR,
        }
        code = 0
        if not {"Settings", "Meter 1", "Meter 2"} <= set(tabs):
            code = 1
        elif not report["ocr_model_loaded"]:
            code = 2
        report["exit_code"] = code
        text = json.dumps(report, indent=2)
        print(text, flush=True)
        out = os.environ.get("APERA_SELF_TEST_REPORT")
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        os._exit(code)  # skip the close prompts and background threads

    QTimer.singleShot(3000, finish)
    return app.exec()


def _log_to_file_if_needed():
    """Send output to <data folder>/app.log when there is no console to show it.

    That is the case for pythonw.exe (Windows launcher and shortcut), the standalone build, and
    the macOS launcher/shortcut (which set APERA_LOG_TO_FILE=1). Keeps one older log (app.log.1).
    """
    if sys.stderr is not None and os.environ.get("APERA_LOG_TO_FILE") != "1":
        return None
    path = os.path.join(DATA_DIR, "app.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
            os.replace(path, path + ".1")
        log = open(path, "a", encoding="utf-8", buffering=1)  # kept open for the life of the app
    except OSError:
        return None
    log.write(
        f"\n--- Apera Meter GUI {__version__} started {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ---\n"
    )
    sys.stdout = sys.stderr = log
    return path


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    self_test = "--self-test" in argv
    # Errors go to logs/apera_meter.log in the data folder (before stdout is redirected to app.log).
    crash_log.setup("apera_meter", os.path.join(DATA_DIR, "logs"), __version__)
    _log_to_file_if_needed()
    app = create_app([a for a in argv if a != "--self-test"])
    # One copy at a time (two would fight over the meters' COM ports): opening the app again
    # brings this window to the front instead. The release-build self-test takes a lock of its
    # own, so it still runs (and checks the lock works) while the app is open.
    guard = single_instance.acquire(f"{APP_NAME} self-test" if self_test else APP_NAME)
    if guard is None:
        sys.exit(0)
    window = MeterApp()
    guard.set_window(window)
    window.show()
    if self_test:
        sys.exit(_self_test(app, window))
    window._start_background_checks()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
