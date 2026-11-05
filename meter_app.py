import sys
import os

__version__ = "1.5.0"  # single source of truth; read by pyproject.toml

# Columns of Sessions/dataset_session_*/readings.csv (Auto-Record OCR Training Dataset / Manual Snap).
# The first five are the original format ("screen_val" holds the reading in mS/cm). The rest label each
# crop with what the meter itself reported, for LCD-Digit-CRNN's auto-labelled training data:
# display_str = the digits on the LCD as sent over serial, display_unit = uS/cm or mS/cm,
# meter_clock = the meter's own timestamp (it drifts from the PC clock), prev_display_str = the previous
# serial reading from this meter (equal values = the display was stable), ocr_value/ocr_conf = the live
# OCR result when the frame was saved, crop_file = the crop's file name in extracted_crops/.
DATASET_CSV_HEADER = (
    "timestamp,meter_id,screen_val,unit,raw_cond,"
    "display_str,display_unit,meter_clock,prev_display_str,ocr_value,ocr_conf,crop_file\n"
)

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

import meter_protocol
import plot_math
import recording

APP_NAME = "Apera Meter GUI"
APP_DIR = settings_location.app_folder(script_dir)
CONFIG_DIR = settings_location.resolve(APP_DIR, DATA_DIR)
SETTINGS_FILES = ["config.json", "global_rois.json"]
# Each computer keeps its own logs and backups inside the shared (SyncThing) data folder, and its
# own COM ports and window layout in the app folder (local_state.json, gitignored, never synced).
COMPUTER = recording.computer_folder_name()
LOG_DIR = os.path.join(DATA_DIR, "logs", COMPUTER)
LOCAL_STATE_PATH = os.path.join(APP_DIR, "local_state.json")
AUTO_DETECT = "Auto-Detect Apera Meter"

import bisect
import logging
import serial
import serial.tools.list_ports
import threading
import time
import math
import requests
import datetime
import json

log = logging.getLogger("apera_meter")

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
    """Signals the serial threads use to hand work to the GUI thread."""

    update_tree = pyqtSignal(object, tuple)
    update_status = pyqtSignal(str, str)
    update_device_info = pyqtSignal(str, str)
    reset_manual_snap = pyqtSignal()
    meter_confirmed = pyqtSignal(int)  # connection number: the meter answered
    connection_lost = pyqtSignal(int, str)  # connection number, reason
    memory_reading = pyqtSignal(object)  # a meter_protocol.Reading from Download Raw Memory
    notice = pyqtSignal(str)  # a message for the event log (e.g. the meter's clock is off)
    burst_started = pyqtSignal()
    save_finished = pyqtSignal(str, str, int)  # path written, error ("" if none), rows saved
    scan_finished = pyqtSignal(int, str)  # auto-detect scan number, port found ("" if none)


# Seconds between reconnect attempts after the USB connection drops (the last one repeats).
RECONNECT_DELAYS_S = (2, 5, 10, 20, 30, 60)
CONFIRM_TIMEOUT_MS = 6000  # a port that sends nothing an Apera meter would send in this time is let go
CLOCK_WARN_S = 60  # warn when the meter's clock is further off than this


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
        self.recent_meter_times = {}  # meter clock text -> PC time first seen (drops repeated packets)
        self.baseline_log_cond = None
        self.last_alert_time = 0
        self.alert_start_time = None

        self.records = []  # every logged row (the COLUMNS of recording.py), in arrival order
        self.plot_dates = []  # kept sorted by time
        self.plot_daynums = []  # the same times as matplotlib date numbers (for drawing)
        self.plot_conds = []  # mS/cm
        self.plot_temps = []
        self.backup_dir = os.path.join(os.getcwd(), "Data Backups")
        self.backup_path = None  # this session's backup CSV, created with its first reading
        self.autosave_path = None  # this session's auto-save / Quick Save file, fixed at its first save
        self._save_thread = None

        self.last_auto_dump_time = time.time()
        self.last_data_time = 0  # PC time of the last data packet (keep-alive, stall alert)
        self._last_autosave = time.monotonic()

        # Connection state. Each connection gets a number, so threads of an earlier one stop.
        self._conn_gen = 0
        self._confirmed = False  # the port answered like an Apera meter
        self._user_disconnected = True
        self._reconnect_port = None
        self._reconnect_attempt = 0
        self._was_recording_when_lost = False
        self._stall_alerted = False
        self._clock_checked = False
        self._last_mode_notice = None
        self._memory = None  # readings of a running memory download, else None
        self._memory_auto = False
        self._last_memory_signature = None

        # Signals for thread-safe UI updates
        self.signals = WorkerSignals()
        self.signals.update_tree.connect(self.log_point_ui)
        self.signals.update_status.connect(self.set_status)
        self.signals.update_device_info.connect(self.set_device_info)
        self.signals.reset_manual_snap.connect(self._reset_manual_snap_ui)
        self.signals.meter_confirmed.connect(self._on_meter_confirmed)
        self.signals.connection_lost.connect(self._on_connection_lost)
        self.signals.memory_reading.connect(self._on_memory_reading)
        self.signals.notice.connect(self.log_event)
        self.signals.burst_started.connect(self._on_burst_started)
        self.signals.save_finished.connect(self._on_save_finished)
        self.signals.scan_finished.connect(self._on_scan_finished)

        self.config_path = os.path.join(CONFIG_DIR, "config.json")
        if self.app_controller is not None and not hasattr(self.app_controller, "shared_config"):
            self.app_controller.shared_config, _ = recording.load_json(self.config_path)
        self.config = self.app_controller.shared_config if self.app_controller else {}

        # Set defaults. The COM port is chosen per computer (local_state.json), see below.
        self.config.setdefault(f"default_com_port_{meter_id}", AUTO_DETECT)
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

        self.columns = recording.COLUMNS
        self.init_ui()

        # Auto-save, auto-stop, the no-readings alert and the memory auto-dump run on their own
        # timer, so they keep working when readings stop.
        self.housekeeping_timer = QTimer(self)
        self.housekeeping_timer.timeout.connect(self._housekeeping)
        self.housekeeping_timer.start(5000)
        self.memory_timer = QTimer(self)
        self.memory_timer.timeout.connect(self._check_memory_download)

        self.refresh_ports()
        here = self._local_port()
        shared = self.config.get(f"default_com_port_{self.meter_id}")
        available = [self.port_combo.itemText(i) for i in range(self.port_combo.count())]
        target_com = recording.pick_port(here, shared if shared != AUTO_DETECT else None, available)
        idx = self.port_combo.findText(target_com or AUTO_DETECT)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)

        # Connect at start only to a port where this computer has already found the meter. A port
        # from the shared settings (perhaps saved on another computer, or the old COM4 default) is
        # only preselected: it could be another instrument's port.
        if here and target_com == here:
            QTimer.singleShot(2000, self.auto_connect_if_valid)
        elif here:
            self.log_event(
                f"The meter's port on this computer ({here}) isn't connected. Plug it in and click Connect."
            )
        elif target_com:
            self.log_event(
                f"{target_com} is preselected from the saved settings. Click Connect: once the meter "
                "answers there, this computer remembers the port and connects at start."
            )

    def _local_port(self):
        state = getattr(self.app_controller, "local_state", None) or {}
        return state.get("ports", {}).get(str(self.meter_id))

    def _remember_port(self, port):
        """Remember the port for this computer only (it's where the meter answered)."""
        app = self.app_controller
        if app is None or not hasattr(app, "local_state"):
            return
        ports = app.local_state.setdefault("ports", {})
        if ports.get(str(self.meter_id)) != port:
            ports[str(self.meter_id)] = port
            app.save_local_state()

    def auto_connect_if_valid(self):
        if not self.is_connected and not getattr(self, "is_scanning", False):
            self.connect_meter()

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

    def save_config(self, now=False):
        """Save the shared settings (shortly after the last change, or at once with now=True)."""
        app = self.app_controller
        if app is not None and hasattr(app, "save_shared_config"):
            app.save_shared_config(now=now)
            return
        try:
            recording.write_json_atomic(self.config_path, self.config)
        except OSError:
            log.exception("Couldn't save the settings to %s", self.config_path)

    def save_notes(self):
        self.config[f"meter_notes_{self.meter_id}"] = self.txt_notes.toPlainText().strip()
        self.save_config()

    def get_ports(self):
        ports = serial.tools.list_ports.comports()
        return [AUTO_DETECT] + [p.device for p in ports]

    def refresh_ports(self, checked=False):
        current = self.port_combo.currentText()
        ports = self.get_ports()
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        idx = self.port_combo.findText(current)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)

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
            if not self.records:
                self.session_start_time = None  # a new session starts with its first reading
            # (Resuming after Pause keeps the session's start, so durations carry on.)
            self._last_autosave = time.monotonic()
            self._stall_alerted = False
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

            if self.combo_autosave.currentText() == "Every X Minutes" and self.unsaved_changes:
                self.save_excel(auto=True, is_background_autosave=True)
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

    def clear_log(self, checked=False, ask=True):
        """Clear Data: empty the table and graph and start a new session (new backup and auto-save
        file). Asks first when there are readings that aren't saved to Excel."""
        if ask and self.unsaved_changes and self.records:
            reply = QMessageBox.question(
                self,
                "Clear Data",
                f"Meter {self.meter_id} has {len(self.records)} readings that aren't saved to Excel "
                "(Recover Session can still bring them back from the backup).\n\nClear them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.table.setRowCount(0)
        self.records.clear()
        self.recent_meter_times.clear()
        self.plot_dates.clear()
        self.plot_daynums.clear()
        self.plot_conds.clear()
        self.plot_temps.clear()
        self.session_start_time = None
        self.backup_path = None  # the next reading starts a new backup file
        self.autosave_path = None  # ... and a new auto-save file
        self.unsaved_changes = False
        self.last_auto_dump_time = time.time()
        self.plot_data()

    def scan_backup_sessions(self, gap_minutes=30.0):
        """Recording sessions found in the backups (this computer's, other computers' and the old
        single file), oldest first."""
        paths = recording.backup_files(self.backup_dir, self.meter_id)
        if self.backup_path and os.path.exists(self.backup_path) and self.backup_path not in paths:
            paths.append(self.backup_path)
        return recording.scan_sessions(paths, gap_minutes, computer=COMPUTER)

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
                where = ""
                if not s.get("this_computer"):
                    where = f" | from {s['computer']}" if s.get("computer") else " | old backup file"
                rb = QRadioButton(
                    f"Session {i + 1} | {s['start_ts']} -> {s['end_ts']} | {s['points']} pts{where}"
                )
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
                if self.unsaved_changes and self.records:
                    reply = QMessageBox.question(
                        self,
                        "Recover Session",
                        f"The {len(self.records)} readings now in the table aren't saved to Excel and "
                        "will be replaced (they stay in their backup). Recover the selected session?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        return
                try:
                    n = self.restore_session(sessions[idx])
                    QMessageBox.information(self, "Recover", f"Recovered {n} points.")
                except (OSError, ValueError) as e:
                    log.exception("Recovering a session failed")
                    QMessageBox.critical(self, "Error", f"Recovery failed: {e}")
            dialog.accept()

        def do_delete(checked=False):
            idx = bg.checkedId()
            if idx >= 0:
                s = sessions[idx]
                reply = QMessageBox.question(
                    self,
                    "Delete Session",
                    f"Permanently delete the backup of the session {s['start_ts']} -> {s['end_ts']} "
                    f"({s['points']} points)?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
                try:
                    recording.delete_session(s)
                    QMessageBox.information(self, "Deleted", "Session permanently deleted from backup.")
                except OSError as e:
                    QMessageBox.critical(self, "Error", f"Deletion failed: {e}")
            dialog.accept()

        btn_restore.clicked.connect(do_restore)
        btn_delete.clicked.connect(do_delete)
        btn_ignore.clicked.connect(dialog.reject)
        dialog.exec()

    def restore_session(self, session):
        """Load a backup session into the table and graph (Recover Session). Returns the points."""
        rows = recording.read_session_rows(session)
        self.clear_log(ask=False)
        self.table.setSortingEnabled(False)
        for row in rows:
            try:
                dt = datetime.datetime.strptime(row[0], recording.TIME_FORMAT)
            except ValueError:
                continue
            self._add_record(dt, tuple(row), write_backup=False)
        self.table.setSortingEnabled(True)
        self.table.scrollToBottom()
        # Readings recorded after this go on in the same backup file if it's this computer's and
        # the session runs to its end; otherwise a new backup file starts with the next reading.
        if session.get("this_computer") and session["end_idx"] == session.get("rows_in_file", 0) - 1:
            self.backup_path = session["path"]
        self.unsaved_changes = bool(self.records)
        self.plot_data()
        self.log_event(f"♻️ Recovered {len(rows)} readings ({session['start_ts']} -> {session['end_ts']}).")
        return len(rows)

    # --- Connection ------------------------------------------------------------------------
    def toggle_connection(self, checked=False):
        if self.is_connected:
            self.disconnect_meter()
        elif self._reconnect_port:
            self._cancel_reconnect()
            self.set_status("Disconnected", "gray")
            self.log_event("Stopped trying to reconnect.")
        else:
            self.connect_meter()

    def find_apera_meter(self):
        """Probe the free USB serial ports for an Apera meter; returns its port or None."""
        with scan_lock:
            used_ports = self.app_controller.get_used_ports() if self.app_controller else []
            candidates = [
                p.device
                for p in serial.tools.list_ports.comports()
                if p.device not in used_ports
                and meter_protocol.plausible_port(p.device, p.description or "", p.vid)
            ]
            log.info("Meter %s auto-detect: trying %s", self.meter_id, ", ".join(candidates) or "no ports")
            for p in candidates:
                if getattr(self, "_scan_cancelled", False):
                    return None
                try:
                    ts = serial.Serial(p, meter_protocol.BAUD, timeout=0.5)
                    ts.setDTR(False)
                    ts.setRTS(False)
                    ts.write(meter_protocol.PING)
                    time.sleep(0.3)
                    resp = ts.read(ts.in_waiting)
                    ts.close()
                    if meter_protocol.is_meter_reply(resp):
                        time.sleep(1.0)  # let Windows fully release the COM port handle
                        return p
                except Exception as e:
                    log.debug("Probing %s failed: %s", p, e)
            return None

    def connect_meter(self):
        self._cancel_reconnect()
        port = self.port_combo.currentText()
        if port == AUTO_DETECT:
            if getattr(self, "is_scanning", False):
                return
            self.set_status("Scanning COM ports...", "gray")
            self.btn_connect.setEnabled(False)
            self.is_scanning = True
            self._scan_cancelled = False
            self.scan_id = getattr(self, "scan_id", 0) or 0
            self.scan_id += 1
            current_id = self.scan_id
            n_ports = max(1, len(serial.tools.list_ports.comports()))

            def timeout_scan():
                if getattr(self, "scan_id", None) == current_id and self.is_scanning:
                    self.scan_id = None  # a meter found after this doesn't connect any more
                    self._scan_cancelled = True
                    self.btn_connect.setText("🔌 Connect")
                    self.btn_connect.setEnabled(True)
                    self.set_status("Auto-Detect Timeout", "gray")
                    self.is_scanning = False

            # About 1 s per port (probe + Windows releasing it), plus some slack.
            QTimer.singleShot(int((8 + 1.5 * n_ports) * 1000), timeout_scan)
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
            time.sleep(0.5)  # extra buffer time after scanner closes the port
        # Back to the GUI thread (a QTimer started from this thread would never fire).
        self.signals.scan_finished.emit(scan_id or 0, found_port or "")

    def _on_scan_finished(self, scan_id, port):
        self._finish_connect(port or None, scan_id)

    def _finish_connect(self, port, scan_id=None, reconnect=False):
        if scan_id is not None and getattr(self, "scan_id", None) != scan_id:
            return
        self.is_scanning = False
        self.btn_connect.setEnabled(True)
        if self.is_connected:
            return
        if not port:
            self.btn_connect.setText("🔌 Connect")
            self.set_status("Auto-Detect Failed", "red")
            QMessageBox.critical(
                self,
                "Connection Error",
                f"Could not auto-detect Apera Meter {self.meter_id} on any USB serial port.\n\n"
                "Check that the meter is on and plugged in, or pick its port from the list.",
            )
            return
        other_ports = self.app_controller.get_used_ports() if self.app_controller else []
        if port in other_ports:
            self.btn_connect.setText("🔌 Connect")
            self.set_status(f"{port} is already used by the other meter", "red")
            return

        try:
            idx = self.port_combo.findText(port)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)

            ser = serial.Serial(port, meter_protocol.BAUD, timeout=0.1)
            ser.setDTR(False)
            ser.setRTS(False)
        except Exception as e:
            self.btn_connect.setText("🔌 Connect")
            if reconnect:
                self.set_status(f"Reconnecting to {port}... ({type(e).__name__})", "red")
                self._schedule_reconnect()
            else:
                self.set_status(f"Failed to connect: {e}", "red")
                log.warning("Meter %s: couldn't open %s: %s", self.meter_id, port, e)
            return

        self.ser = ser
        self._conn_gen += 1
        gen = self._conn_gen
        self._confirmed = False
        self._clock_checked = False
        self._user_disconnected = False
        self.is_connected = True
        self.last_data_time = 0
        self.btn_connect.setText("🔌 Disconnect")
        self.set_status(f"Connecting ({port})... waiting for the meter", "gray")
        try:
            ser.write(meter_protocol.PING)
        except Exception as e:
            log.warning("Meter %s: first ping on %s failed: %s", self.meter_id, port, e)
        threading.Thread(target=self.read_serial_loop, args=(gen, ser), daemon=True).start()
        threading.Thread(target=self.keep_alive_loop, args=(gen, ser), daemon=True).start()
        QTimer.singleShot(CONFIRM_TIMEOUT_MS, lambda: self._check_confirmed(gen, port, reconnect))

    def _check_confirmed(self, gen, port, reconnect=False):
        """Let go of a port that never answered like an Apera meter (it may be another device)."""
        if gen != self._conn_gen or not self.is_connected or self._confirmed:
            return
        self._close_connection()
        if reconnect:
            self.set_status(f"Reconnecting to {port}... (no answer yet)", "red")
            self._schedule_reconnect()
            return
        self.set_status(f"No Apera meter answered on {port}", "red")
        self.log_event(f"No Apera meter answered on {port}, so the port was closed again.")
        log.warning("Meter %s: no meter answered on %s", self.meter_id, port)

    def _on_meter_confirmed(self, gen):
        if gen != self._conn_gen or not self.is_connected or self._confirmed:
            return
        self._confirmed = True
        port = self.ser.port if self.ser else ""
        was_reconnecting = self._reconnect_port is not None
        self._reconnect_port = None
        self._reconnect_attempt = 0
        self.last_auto_dump_time = time.time()
        self.last_data_time = time.time()  # the no-readings alert counts from here
        if self.parent_app:
            self.parent_app.update_meter_status(self.meter_id, True, port)
        self.btn_download.setEnabled(True)
        self.btn_sync.setEnabled(True)
        self.set_status(f"Connected ({port})", "#27ae60")
        self._remember_port(port)
        if was_reconnecting:
            self.log_event(f"🔌 Reconnected to {port}.")
            log.info("Meter %s reconnected on %s", self.meter_id, port)
            if self._was_recording_when_lost:
                self._send_status_alert(
                    f"✅ **Meter {self.meter_id}** reconnected on {port}; recording goes on."
                )
        else:
            self.log_event(f"🔌 Connected to {port}.")
            log.info("Meter %s connected on %s", self.meter_id, port)
        if self.app_controller and self.app_controller.shared_config.get("global_auto_sync", True):
            QTimer.singleShot(500, self.sync_time)

    def _close_connection(self):
        """Stop this connection's threads; the reader thread closes the port itself."""
        self.is_connected = False
        self._confirmed = False
        self._conn_gen += 1
        self.btn_connect.setText("🔌 Connect")
        self.btn_download.setEnabled(False)
        self.btn_sync.setEnabled(False)
        if self._memory is not None:
            self._finish_memory_download()
        if self.parent_app:
            self.parent_app.update_meter_status(self.meter_id, False)

    def disconnect_meter(self):
        self._user_disconnected = True
        self._cancel_reconnect()
        was_connected = self.is_connected
        self._close_connection()
        self.set_status("Disconnected", "gray")
        self.sn_label.setText("SN: Unknown")
        self.model_label.setText("Model: Unknown")
        if was_connected:
            self.log_event("❌ Disconnected.")

    def _on_connection_lost(self, gen, reason):
        """The USB connection dropped (unplugged, meter off, cable): reset and keep trying to reconnect."""
        if gen != self._conn_gen:
            return
        port = self.ser.port if self.ser else self.port_combo.currentText()
        self._was_recording_when_lost = self.is_recording
        self._close_connection()
        self.set_status(f"Connection lost ({reason.split(':')[0]}), reconnecting...", "red")
        self.log_event(f"⚠️ Lost the connection to {port} ({reason}). Trying to reconnect...")
        log.warning("Meter %s lost the connection on %s: %s", self.meter_id, port, reason)
        if self.is_recording:
            self._send_status_alert(
                f"⚠️ **Meter {self.meter_id}** lost its USB connection on {port} while recording "
                f"({reason}). The app keeps trying to reconnect."
            )
        if port and port != AUTO_DETECT:
            self._reconnect_port = port
            self._reconnect_attempt = 0
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        if not self._reconnect_port or self._user_disconnected:
            return
        delay = RECONNECT_DELAYS_S[min(self._reconnect_attempt, len(RECONNECT_DELAYS_S) - 1)]
        self._reconnect_attempt += 1
        port = self._reconnect_port
        QTimer.singleShot(int(delay * 1000), lambda: self._try_reconnect(port))

    def _try_reconnect(self, port):
        if self.is_connected or self._user_disconnected or self._reconnect_port != port:
            return
        present = [p.device for p in serial.tools.list_ports.comports()]
        if port not in present:
            self.set_status(f"Waiting for {port} to come back (try {self._reconnect_attempt})", "red")
            self._schedule_reconnect()
            return
        self.refresh_ports()
        self._finish_connect(port, reconnect=True)

    def _cancel_reconnect(self):
        self._reconnect_port = None
        self._reconnect_attempt = 0

    def read_serial_loop(self, gen=None, ser=None):
        """Read packets until this connection ends. Owns the port: it closes it when done."""
        gen = self._conn_gen if gen is None else gen
        ser = ser or self.ser
        buffer = b""
        error = None
        while self.is_connected and gen == self._conn_gen:
            try:
                waiting = ser.in_waiting
                if waiting > 0:
                    packets, buffer = meter_protocol.split_packets(buffer + ser.read(waiting))
                    for packet in packets:
                        try:
                            self.process_packet(packet, gen)
                        except Exception:
                            log.exception("Meter %s: couldn't handle packet %r", self.meter_id, packet[:16])
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                break
            time.sleep(0.05)
        try:
            ser.close()
        except Exception:  # noqa: S110 - the port may already be gone with the USB device
            pass
        if error and gen == self._conn_gen:
            self.signals.connection_lost.emit(gen, error)

    def keep_alive_loop(self, gen=None, ser=None):
        """Ping the meter when it has been quiet for 2 s. A port that hasn't answered like a meter
        gets at most three pings (it may be another instrument)."""
        gen = self._conn_gen if gen is None else gen
        ser = ser or self.ser
        unconfirmed_pings = 0
        while self.is_connected and gen == self._conn_gen:
            if self.config.get(f"keep_alive_{self.meter_id}", True) or not self._confirmed:
                if time.time() - self.last_data_time > 2.0 and (self._confirmed or unconfirmed_pings < 3):
                    try:
                        if ser.is_open:
                            ser.write(meter_protocol.PING)
                            if not self._confirmed:
                                unconfirmed_pings += 1
                    except Exception:  # noqa: S110 - the reader thread reports a lost connection
                        pass
            time.sleep(1.5)

    def _send_status_alert(self, msg):
        """A Discord message about the connection, if alerts are on for this meter."""
        mid = self.meter_id
        if self.config.get(f"alert_enabled_{mid}", False) and self.config.get(
            f"alert_connection_{mid}", True
        ):
            self.send_discord_alert(self.config.get(f"alert_webhook_{mid}", ""), msg)

    def process_packet(self, packet, gen=None):
        """Handle one packet from the meter (runs in the serial thread)."""
        if not packet:
            return
        packet = packet.strip(b"\r\n")
        gen = self._conn_gen if gen is None else gen
        if getattr(self, "_confirm_sent_gen", None) != gen and meter_protocol.is_meter_reply(packet):
            self._confirm_sent_gen = gen
            self.signals.meter_confirmed.emit(gen)
        if len(packet) < 3 or packet == b">" or packet == b"#>":
            return
        info = meter_protocol.parse_info_packet(packet)
        if info is not None:
            self.signals.update_device_info.emit(*info)
            return
        try:
            reading = meter_protocol.parse_data_packet(packet)
        except ValueError:
            reading = None
        if reading is not None and reading.stored and self._memory is not None:
            self.last_data_time = time.time()
            self.signals.memory_reading.emit(reading)  # Download Raw Memory is running
            return

        if reading is not None:
            self.last_data_time = time.time()
            try:
                # Times in the log are the computer's clock: the meter's own clock can be far off
                # (e.g. after a battery change). The meter's time is kept in the Meter Clock column.
                pc_now = datetime.datetime.now()
                temp_c = reading.temp_c
                self.last_serial_temp = f"{temp_c:.1f}"
                mode_str = reading.mode_name
                dt_str = reading.meter_time_text  # the meter's own clock
                screen_str = reading.display
                w4 = reading.raw_word4

                if not self._clock_checked:
                    self._clock_checked = True
                    offset = meter_protocol.clock_offset_seconds(reading.meter_time, pc_now)
                    if offset is None or abs(offset) > CLOCK_WARN_S:
                        if offset is None:
                            how = "isn't set to a valid date"
                        else:
                            rel = meter_protocol.describe_offset(offset).replace(" ahead", " ahead of")
                            how = f"is {rel} the computer's"
                        self.signals.notice.emit(
                            f"⚠️ The meter's clock {how} (it says {dt_str}). The log uses the computer's "
                            "clock; Sync Meter Time sets the meter's."
                        )
                        log.warning("Meter %s clock %s (meter says %s)", self.meter_id, how, dt_str)

                if not reading.is_conductivity:
                    if self._last_mode_notice != reading.mode:
                        self._last_mode_notice = reading.mode
                        self.signals.notice.emit(
                            f"The meter is in {mode_str} mode; only conductivity readings are logged. "
                            "Switch the meter to conductivity to record."
                        )
                    return
                new_ms = reading.cond_ms
                if new_ms is None:
                    if self._last_mode_notice != ("unit", reading.unit_code):
                        self._last_mode_notice = ("unit", reading.unit_code)
                        self.signals.notice.emit(
                            f"The meter sent a conductivity reading in an unknown unit (code "
                            f"{reading.unit_code}); it wasn't logged."
                        )
                    return
                self._last_mode_notice = None
                unit_str = reading.unit
                cond_us_val = new_ms * 1000
                cond_us = f"{cond_us_val:.1f}"
                # A reading in µS/cm keeps all its digits in the mS/cm column (12.3 µS = 0.012300 mS).
                cond_ms = f"{new_ms:.3f}" if unit_str == "mS/cm" else f"{new_ms:.6f}"

                val_changed = False
                if getattr(self, "first_serial_time", None) is None:
                    self.first_serial_time = time.time()
                if getattr(self, "last_serial_cond_ms", None) != new_ms:
                    if hasattr(self.app_controller, "ocr_pipeline"):
                        if not hasattr(self.app_controller.ocr_pipeline, "latest_serial_ms"):
                            self.app_controller.ocr_pipeline.latest_serial_ms = {}
                        self.app_controller.ocr_pipeline.latest_serial_ms[f"m{self.meter_id}_cond"] = new_ms
                    val_changed = True
                self.last_serial_cond_ms = new_ms

                if cond_us_val > 0:
                    self._check_change_alert(math.log10(cond_us_val), cond_us_val)

                # The meter sends the same reading more than once; drop repeats of a meter-clock
                # second seen in the last 3 s (a stuck meter clock still gives a reading every 3 s).
                now_s = time.time()
                first_seen = self.recent_meter_times.get(dt_str)
                if first_seen is not None and now_s - first_seen < 3.0:
                    return
                self.recent_meter_times[dt_str] = now_s
                if len(self.recent_meter_times) > 200:
                    self.recent_meter_times = {
                        k: v for k, v in list(self.recent_meter_times.items()) if now_s - v < 10.0
                    }

                row_data = (
                    pc_now.strftime(recording.TIME_FORMAT),
                    mode_str,
                    cond_us,
                    cond_ms,
                    f"{temp_c:.1f}",
                    "-",
                    dt_str,
                )

                # Previous serial reading from this meter, recorded with dataset crops as a stability check
                prev_display_str = getattr(self, "last_dataset_display_str", "")
                self.last_dataset_display_str = screen_str

                app = self.parent_app
                is_continuous = getattr(app, "shared_config", {}).get("dataset_continuous", False)
                is_manual = getattr(app, "manual_snap_requested", False)

                if self.is_recording:
                    self.signals.update_tree.emit(pc_now, row_data)

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
                        f.write(DATASET_CSV_HEADER)
                    rois_path = os.path.join(app.dataset_session_dir, "rois.json")
                    with open(rois_path, "w") as f:
                        json.dump(app.ocr_pipeline.rois, f)

                if (is_continuous or is_manual) and getattr(app, "dataset_session_dir", None):
                    # The physical LCD updates ~Xms AFTER the serial packet is sent.
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
                        u_str=unit_str,
                        m_clock=dt_str,
                        p_disp=prev_display_str,
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

                            crop_file = ""
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
                                # Lossless copy at the camera's resolution (no 128x128 squash), so
                                # LCD-Digit-CRNN can try other input sizes and aspect ratios.
                                native_dir = os.path.join(app.dataset_session_dir, "extracted_crops_native")
                                os.makedirs(native_dir, exist_ok=True)
                                native_name = os.path.splitext(filename)[0] + ".png"
                                cv2.imwrite(os.path.join(native_dir, native_name), roi_img)
                                crop_file = filename

                            ocr_val, ocr_conf = "", ""
                            parsed = getattr(app.ocr_pipeline, "last_parsed_vals", {}).get(roi_name)
                            if isinstance(parsed, tuple) and len(parsed) == 3:
                                ocr_val, ocr_conf = f"{parsed[0]:g}", f"{parsed[2]:.4f}"
                            with open(app.dataset_csv_path, "a", encoding="utf-8") as f:
                                f.write(
                                    f"{cap_ts},{m_id},{c_ms},mS,{r_cond},"
                                    f"{s_val},{u_str},{m_clock},{p_disp},{ocr_val},{ocr_conf},{crop_file}\n"
                                )

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
                        self.signals.burst_started.emit()  # the button is updated in the GUI thread

            except Exception:
                log.exception("Meter %s: couldn't handle reading %r", self.meter_id, packet[:16])

    def _check_change_alert(self, curr_log, cond_us_val):
        """Discord alert when log10(conductivity) moved by the threshold since the baseline."""
        if not self.config.get(f"alert_enabled_{self.meter_id}", False):
            return
        webhook_url = self.config.get(f"alert_webhook_{self.meter_id}", "")
        try:
            threshold = float(self.config.get(f"alert_threshold_{self.meter_id}", 0.5))
        except (TypeError, ValueError):
            threshold = 0.5
        try:
            cooldown_min = float(self.config.get(f"alert_cooldown_{self.meter_id}", 5.0))
        except (TypeError, ValueError):
            cooldown_min = 5.0
        try:
            delay_min = float(self.config.get(f"alert_delay_{self.meter_id}", 5.0))
        except (TypeError, ValueError):
            delay_min = 5.0

        if self.alert_start_time is None:
            self.alert_start_time = time.time()

        if time.time() - self.alert_start_time < delay_min * 60:
            self.baseline_log_cond = curr_log
            return
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

    def _on_burst_started(self):
        app = self.app_controller
        if app is not None and hasattr(app, "btn_calibration_burst"):
            app.btn_calibration_burst.setText("Recording Burst...")
            app.btn_calibration_burst.setEnabled(False)

    def _reset_manual_snap_ui(self):
        if hasattr(self.app_controller, "btn_manual_snap"):
            self.app_controller.btn_manual_snap.setText("🎯 Arm Capture (Press M+ on Meter)")
            self.app_controller.btn_manual_snap.setEnabled(True)

    def log_point_ui(self, dt, row_data):
        """Add a reading (from the serial thread or the camera) to the table, graph and backup.

        row_data: serial readings (DateTime, Mode, uS, mS, Temp, OCR conf, Meter Clock); camera
        readings (DateTime, "OCR", uS, mS, "-", conf). The duration is added here.
        """
        if not getattr(self, "session_start_time", None):
            self.session_start_time = dt
        duration_min = (dt - self.session_start_time).total_seconds() / 60.0
        r = list(row_data)
        if len(r) >= len(self.columns):  # already a full row
            full_row = tuple(r)
        else:
            r += ["-", ""][len(r) - 5 :]  # no OCR confidence / meter clock
            full_row = (r[0], f"{duration_min:.2f}", *r[1:7])
        self._add_record(dt, full_row)
        self.unsaved_changes = True
        self.schedule_plot()

    def _add_record(self, dt, full_row, write_backup=True):
        full_row = tuple("" if v is None else str(v) for v in full_row)
        full_row += ("",) * (len(self.columns) - len(full_row))
        self.records.append(full_row)

        try:
            cond = float(full_row[4])
        except ValueError:
            cond = 0.0
        try:
            temp = float(full_row[5])
        except ValueError:
            temp = float("nan")
        # The graph's lists stay in time order (camera and serial readings can cross by a moment).
        if not self.plot_dates or dt >= self.plot_dates[-1]:
            self.plot_dates.append(dt)
            self.plot_daynums.append(mdates.date2num(dt))
            self.plot_conds.append(cond)
            self.plot_temps.append(temp)
        else:
            i = bisect.bisect_right(self.plot_dates, dt)
            self.plot_dates.insert(i, dt)
            self.plot_daynums.insert(i, mdates.date2num(dt))
            self.plot_conds.insert(i, cond)
            self.plot_temps.insert(i, temp)

        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        row_idx = self.table.rowCount()
        self.table.insertRow(row_idx)
        for col, text in enumerate(full_row):
            self.table.setItem(row_idx, col, NumericTableWidgetItem(text))
        self.table.setSortingEnabled(sorting)
        self.table.scrollToBottom()

        if write_backup:
            self._write_backup(dt, full_row)

    def _write_backup(self, dt, full_row):
        """Every reading also goes to this session's backup CSV (Recover Session reads it)."""
        try:
            if self.backup_path is None:
                start = getattr(self, "session_start_time", None) or dt
                self.backup_path = recording.new_backup_path(self.backup_dir, self.meter_id, start, COMPUTER)
            recording.append_backup_row(self.backup_path, full_row, self.columns)
            self._backup_failed = False
        except OSError as e:
            if not getattr(self, "_backup_failed", False):
                self._backup_failed = True
                log.exception("Meter %s: couldn't write the backup %s", self.meter_id, self.backup_path)
                self.log_event(f"❌ Couldn't write the crash-recovery backup: {e}")

    def _connected_status(self):
        """The status-bar text for the connection as it is now (after a passing message)."""
        if self._reconnect_port:
            return f"Connection lost, reconnecting to {self._reconnect_port}...", "red"
        if self.is_connected and self._confirmed and self.ser is not None:
            if self._stall_alerted:
                return "No reading from the meter", "red"
            return f"Connected ({self.ser.port})", "#27ae60"
        if self.is_recording:
            return "Recording...", "gray"
        return "Disconnected", "gray"

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

    def _restore_status_later(self, ms=4000):
        QTimer.singleShot(ms, lambda: self.set_status(*self._connected_status()))

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
                log.warning("Discord alert for Meter %s failed: %s", self.meter_id, e)

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
            QMessageBox.warning(self, "No Webhook", "Please enter a Discord Webhook URL first.")

    # --- Timers that don't depend on readings arriving -------------------------------------
    def _housekeeping(self):
        """Every 5 s: auto-save, auto-stop, the no-readings alert and the memory auto-dump."""
        for job in (self._autosave_if_due, self._autostop_if_due, self._check_stall, self._auto_dump_if_due):
            try:
                job()
            except Exception:
                log.exception("Meter %s: %s failed", self.meter_id, job.__name__)

    def _autosave_if_due(self):
        if not (self.is_recording and self.combo_autosave.currentText() == "Every X Minutes"):
            return
        try:
            interval_min = max(0.1, float(self.ent_autosave_min.text()))
        except ValueError:
            interval_min = 5.0
        if time.monotonic() - self._last_autosave >= interval_min * 60:
            self._last_autosave = time.monotonic()
            if self.unsaved_changes and self.records:
                self.save_excel(auto=True, is_background_autosave=True)

    def _autostop_if_due(self):
        mode = self.combo_autostop.currentText()
        start = getattr(self, "session_start_time", None)
        if not self.is_recording or mode == "Off" or start is None:
            return
        try:
            limit_min = float(self.ent_autostop_min.text())
        except ValueError:
            limit_min = 60.0
        elapsed_min = (datetime.datetime.now() - start).total_seconds() / 60.0
        if elapsed_min < limit_min:
            return
        if mode in ("Stop Meter", "Stop Both"):
            self.toggle_record()
            self.log_event(f"🛑 Auto-Stop: recording stopped after {elapsed_min:.1f} minutes.")
        app = self.parent_app
        if mode in ("Stop OCR Log", "Stop Both") and app is not None and hasattr(app, "ocr_autolog_checkbox"):
            if app.ocr_autolog_checkbox.isChecked():
                app.ocr_autolog_checkbox.setChecked(False)
                self.log_event(f"🔴 Auto-Stop: OCR logging stopped after {elapsed_min:.1f} minutes.")

    def _check_stall(self):
        """Warn (status bar, event log, Discord) when a recording meter stops sending readings."""
        if not (self.is_connected and self._confirmed and self.is_recording):
            self._stall_alerted = False
            return
        try:
            limit_s = float(self.config.get(f"stall_alert_s_{self.meter_id}", 60))
        except (TypeError, ValueError):
            limit_s = 60.0
        if limit_s <= 0:
            return
        quiet_s = time.time() - self.last_data_time
        port = self.ser.port if self.ser else ""
        if quiet_s >= limit_s and not self._stall_alerted:
            self._stall_alerted = True
            self.set_status(f"No reading for {quiet_s:.0f} s", "red")
            self.log_event(f"⚠️ No reading from the meter for {quiet_s:.0f} s (still connected on {port}).")
            log.warning("Meter %s: no reading for %.0f s on %s", self.meter_id, quiet_s, port)
            self._send_status_alert(
                f"⚠️ **Meter {self.meter_id}**: no reading for {quiet_s:.0f} s while recording. "
                "Check the meter (is it on and in conductivity mode?)."
            )
        elif quiet_s < limit_s and self._stall_alerted:
            self._stall_alerted = False
            self.set_status(*self._connected_status())
            self.log_event("✅ Readings are arriving again.")
            self._send_status_alert(f"✅ **Meter {self.meter_id}**: readings are arriving again.")

    def _auto_dump_if_due(self):
        if not (self.is_connected and self._confirmed) or self._memory is not None:
            return
        if not self.config.get(f"auto_dump_enabled_{self.meter_id}", False):
            return
        try:
            hours = float(self.config.get(f"default_auto_dump_hrs_{self.meter_id}", 2.0))
        except (TypeError, ValueError):
            return
        if hours > 0 and time.time() - self.last_auto_dump_time >= hours * 3600:
            self.last_auto_dump_time = time.time()
            self.download_memory(automatic=True)

    # --- Graph ----------------------------------------------------------------------------
    def schedule_plot(self):
        if not getattr(self, "_plot_scheduled", False):
            self._plot_scheduled = True
            # Long runs redraw a little less often, so the window stays responsive.
            QTimer.singleShot(500 if len(self.plot_dates) < 5000 else 1500, self.plot_data)

    def plot_data(self):
        import numpy as np

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

        self.ax2.set_ylabel("d(log C)/dt (per min)", color="#9b59b6")
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

            is_duration = self.xaxis_combo.currentText() == "X-Axis: Duration (min)"
            if not getattr(self, "session_start_time", None):
                self.session_start_time = self.plot_dates[0]

            # With a Time Window, only that window is drawn (it used to draw everything and zoom).
            first = 0
            win_min = None
            if not self.auto_time_chk.isChecked():
                try:
                    win_min = float(self.ent_time_win.text())
                    first = plot_math.window_start(self.plot_dates, datetime.timedelta(minutes=win_min))
                except ValueError:
                    win_min = None
            dates = self.plot_dates[first:]
            if len(self.plot_daynums) != len(self.plot_dates):
                self.plot_daynums = list(mdates.date2num(self.plot_dates))
            day_nums = np.asarray(self.plot_daynums[first:], dtype=float)
            minutes = (day_nums - mdates.date2num(self.session_start_time)) * 1440.0
            conds = np.asarray(self.plot_conds[first:], dtype=float) * (1000.0 if is_us else 1.0)
            temps = np.asarray(self.plot_temps[first:], dtype=float)
            conds_drawn = np.where(conds > 0, conds, np.nan)  # 0.0 = no reading: a gap, not a dip

            if is_duration:
                x_all = minutes
                self.ax_temp.set_xlabel("Duration (min)", color="white")
            else:
                x_all = day_nums
                self.ax_temp.set_xlabel("Time", color="white")

            idx = plot_math.thin_indices(conds_drawn)  # long runs: thinned, keeping peaks and dips
            marker = "o" if len(idx) <= 300 else None
            x = x_all[idx]
            self.ax.plot(x, conds_drawn[idx], marker=marker, markersize=4, linestyle="-", color="#3498db")

            if win_min is not None:
                xmax = x_all[-1]
                self.ax.set_xlim(xmax - (win_min if is_duration else win_min / 1440.0), xmax)

            if not self.auto_cond_chk.isChecked():
                try:
                    self.ax.set_ylim(float(self.ent_cond_min.text()), float(self.ent_cond_max.text()))
                except ValueError:
                    pass

            good_t = ~np.isnan(temps[idx])
            self.ax_temp.plot(
                x[good_t], temps[idx][good_t], marker="s" if marker else None, markersize=3, color="#e74c3c"
            )

            # Rate of change per minute (not per reading), so it doesn't depend on the sampling rate
            if len(dates) > 1:
                deriv = plot_math.derivative_per_minute(minutes, conds, self.spin_deriv_smooth.value())
                self.ax2.plot(x, deriv[idx], color="#9b59b6", linestyle="-", alpha=0.6)
                if not self.auto_deriv_chk.isChecked():
                    try:
                        self.ax2.set_ylim(float(self.ent_deriv_min.text()), float(self.ent_deriv_max.text()))
                    except ValueError:
                        pass

            if not self.auto_temp_chk.isChecked():
                try:
                    self.ax_temp.set_ylim(float(self.ent_temp_min.text()), float(self.ent_temp_max.text()))
                except ValueError:
                    pass

            if is_duration:
                self.ax_temp.xaxis.set_major_formatter(ticker.ScalarFormatter())
                self.fig.autofmt_xdate(rotation=0, ha="center")
            else:
                self.ax_temp.xaxis.set_major_locator(mdates.AutoDateLocator())
                self.ax_temp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                self.fig.autofmt_xdate()

            # Hide x-axis labels on top plot
            plt.setp(self.ax.get_xticklabels(), visible=False)
            self.ax.xaxis.get_offset_text().set_visible(False)  # e.g. "+2.07e4" for date numbers
            self.ax.set_xlabel("")

        self.fig.tight_layout()
        self.canvas.draw_idle()

    # --- Meter commands -------------------------------------------------------------------
    def sync_time(self, checked=False):
        if self.is_connected and self.ser:
            try:
                self.ser.write(meter_protocol.set_clock_command(datetime.datetime.now()))
                self.log_event("⏱️ Set the meter's clock to the computer's.")
            except Exception as e:
                log.warning("Meter %s: setting the clock failed: %s", self.meter_id, e)
                self.log_event(f"❌ Couldn't set the meter's clock: {e}")

    def download_memory(self, checked=False, automatic=False):
        """Ask the meter for the readings stored in its memory and save them to an Excel file."""
        if not (self.is_connected and self.ser) or self._memory is not None:
            return
        self._memory = []
        self._memory_auto = automatic
        self._memory_started = self._memory_last = time.time()
        self.btn_download.setEnabled(False)
        self.set_status("Downloading memory...", "gray")
        self.log_event("📥 Downloading the meter's memory" + (" (automatic)..." if automatic else "..."))
        try:
            self.ser.write(meter_protocol.DOWNLOAD_MEMORY)
        except Exception as e:
            self._finish_memory_download(error=str(e))
            return
        self.memory_timer.start(1000)

    def _on_memory_reading(self, reading):
        if self._memory is None:
            return
        self._memory.append(reading)
        self._memory_last = time.time()
        if len(self._memory) % 25 == 0:
            self.set_status(f"Downloading memory... {len(self._memory)} readings", "gray")

    def _check_memory_download(self):
        if self._memory is None:
            self.memory_timer.stop()
            return
        now = time.time()
        # Done when the readings stop for 3 s (or nothing came in 10 s).
        if (self._memory and now - self._memory_last > 3) or (
            not self._memory and now - self._memory_started > 10
        ):
            self._finish_memory_download()

    def _finish_memory_download(self, error=None):
        self.memory_timer.stop()
        readings, self._memory = self._memory or [], None
        automatic = self._memory_auto
        if self.is_connected:
            self.btn_download.setEnabled(True)
        if error:
            log.warning("Meter %s: memory download failed: %s", self.meter_id, error)
            self.set_status("Memory download failed", "red")
            self.log_event(f"❌ Memory download failed: {error}")
            return
        if not readings:
            self.set_status(*self._connected_status())
            self.log_event("📥 The meter sent no stored readings.")
            if not automatic:
                QMessageBox.information(self, "Download Raw Memory", "The meter sent no stored readings.")
            return
        signature = (len(readings), readings[-1].meter_time_text)
        if automatic and signature == self._last_memory_signature:
            self.set_status(*self._connected_status())
            self.log_event("📥 The meter's memory hasn't changed since the last download; nothing saved.")
            return
        path = os.path.join(
            self._save_folder(), f"Meter{self.meter_id}_Memory_{datetime.datetime.now():%Y-%m-%d_%H%M%S}.xlsx"
        )
        sn = self.sn_label.text().replace("SN: ", "")
        try:
            path = recording.save_with_fallback(
                lambda p: recording.write_memory_excel(p, readings, self.meter_id, sn), path
            )
        except Exception as e:
            log.exception("Meter %s: couldn't save the memory download", self.meter_id)
            self.set_status("Saving the memory download failed", "red")
            self.log_event(f"❌ Couldn't save the {len(readings)} stored readings: {e}")
            if not automatic:
                QMessageBox.critical(self, "Download Raw Memory", f"Couldn't save the readings:\n{e}")
            return
        self._last_memory_signature = signature
        self.set_status(f"Saved {len(readings)} stored readings", "#27ae60")
        self._restore_status_later()
        self.log_event(f"📥 Saved {len(readings)} readings from the meter's memory to {path}.")
        if not automatic:
            QMessageBox.information(
                self, "Download Raw Memory", f"Saved {len(readings)} stored readings to:\n{path}"
            )

    # --- Excel ----------------------------------------------------------------------------
    def _save_folder(self):
        """Where exports go: the global or this meter's save folder (relative = in the data folder)."""
        if self.chk_use_global_template.isChecked() and self.app_controller:
            folder = self.app_controller.shared_config.get("global_default_save_folder", "Data")
        else:
            folder = self.config.get(f"default_save_location_{self.meter_id}", "")
        if folder in ("", "Not Set"):
            folder = "Data"
        if not os.path.isdir(folder) and not os.path.isabs(folder):
            try:
                os.makedirs(folder, exist_ok=True)  # e.g. "Data" in the data folder
            except OSError:
                pass
        if not os.path.isdir(folder):
            log.warning("Save folder %s doesn't exist; using Documents", folder)
            folder = os.path.expanduser("~/Documents")
        return os.path.abspath(folder)

    def _template(self):
        if self.chk_use_global_template.isChecked() and self.app_controller:
            template = self.app_controller.shared_config.get(
                "global_filename_template", "<Meter ID>_<Date>_<Time>"
            )
        else:
            template = self.ent_save_template.text()
        return template if template.strip() else "<Meter ID>_<Date>_<Time>"

    def _default_save_path(self):
        filename = self.get_resolved_string(self._template())
        if not filename.lower().endswith(".xlsx"):
            filename += ".xlsx"
        return os.path.join(self._save_folder(), filename)

    def _save_snapshot(self):
        """Everything the Excel file needs, copied so a background save can't see later changes."""
        return {
            "records": list(self.records),
            "meter_id": self.meter_id,
            "serial_number": self.sn_label.text().replace("SN: ", ""),
            "tokens": [(name, ent.text().strip()) for name, ent in getattr(self, "token_inputs", {}).items()],
            "notes": self.txt_notes.toPlainText(),
        }

    @staticmethod
    def _write_excel(path, snapshot):
        return recording.save_with_fallback(lambda p: recording.write_excel(p, **snapshot), path)

    def save_excel(self, auto=False, is_background_autosave=False, quiet=False):
        """Save the readings to Excel. Returns True if saved (or a background save started).

        auto=False: Export As (asks for a file). auto=True: Quick Save / auto-save, to this
        session's auto-save file, whose name is fixed at its first save, so each save overwrites it
        instead of making a new file. If that file is open in Excel, "<name> (2).xlsx" is used and
        auto-save carries on there. Auto-saves run in the background; failures show in the status
        bar, the event log and the error log.
        """
        if not self.records:
            if not auto:
                QMessageBox.information(self, "Export", "No data to export.")
            return False

        if auto:
            if self.autosave_path is None:
                self.autosave_path = self._default_save_path()
            filepath = self.autosave_path
        else:
            filepath, _ = QFileDialog.getSaveFileName(
                self, "Export Excel", self._default_save_path(), "Excel files (*.xlsx);;All files (*.*)"
            )
            if not filepath:
                return False

        snapshot = self._save_snapshot()
        if is_background_autosave:
            if self._save_thread is not None and self._save_thread.is_alive():
                return False  # the previous auto-save is still writing

            def work():
                try:
                    written = self._write_excel(filepath, snapshot)
                    self.signals.save_finished.emit(written, "", len(snapshot["records"]))
                except Exception as e:
                    log.exception("Meter %s: auto-save to %s failed", self.meter_id, filepath)
                    self.signals.save_finished.emit(filepath, f"{type(e).__name__}: {e}", 0)

            self._save_thread = threading.Thread(target=work, daemon=True)
            self._save_thread.start()
            return True

        self.wait_for_background_save()
        try:
            written = self._write_excel(filepath, snapshot)
        except Exception as e:
            log.exception("Meter %s: saving %s failed", self.meter_id, filepath)
            self._save_failed(filepath, f"{type(e).__name__}: {e}", show_dialog=not quiet)
            return False
        self._saved(written, len(snapshot["records"]), is_autosave_file=auto)
        if not quiet:
            QMessageBox.information(self, "Save Successful", f"File successfully saved to:\n{written}")
        return True

    def wait_for_background_save(self, timeout=60):
        if self._save_thread is not None and self._save_thread.is_alive():
            self._save_thread.join(timeout)

    def _on_save_finished(self, path, error, n_rows):
        if error:
            self._save_failed(path, error, show_dialog=False)
        else:
            self._saved(path, n_rows, is_autosave_file=True)

    def _saved(self, written, n_rows, is_autosave_file):
        if is_autosave_file and self.autosave_path and written != self.autosave_path:
            self.log_event(
                f"⚠️ {os.path.basename(self.autosave_path)} couldn't be written (open in Excel?), so "
                f"the data went to {os.path.basename(written)}. Auto-save carries on there."
            )
            self.autosave_path = written
        if n_rows >= len(self.records):
            self.unsaved_changes = False
        self.set_status(f"Saved {os.path.basename(written)}", "#27ae60")
        self._restore_status_later(3000)
        self.log_event(f"💾 Saved {n_rows} readings to {written}")

    def _save_failed(self, path, error, show_dialog):
        msg = f"Saving {os.path.basename(path)} failed: {error}"
        self.set_status("Save failed! See the event log", "red")
        self.log_event(f"❌ {msg}")
        if self.is_recording:
            self._send_status_alert(f"❌ **Meter {self.meter_id}**: {msg}")
        if show_dialog:
            QMessageBox.critical(self, "Error", f"Export failed!\n\n{msg}")


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
                save_cb=lambda: self.parent_app.save_shared_config(now=True),
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
        self.auto_sync_chk.setToolTip(
            "Set each meter's clock to the computer's when it connects (the log itself always uses "
            "the computer's clock; the meter's is kept in the Meter Clock column)"
        )
        self.auto_sync_chk.setChecked(self.parent_app.meter1.config.get("global_auto_sync", True))
        l_save_inner.addWidget(self.auto_sync_chk)
        l_global.addWidget(gb_save)

        l_global.addStretch()

        self.sidebar.addItem("🌍 General")
        self.stack.addWidget(page_global)

        # 2. and 3. Meter settings
        self.meter_fields = {}
        for meter in (self.parent_app.meter1, self.parent_app.meter2):
            page, fields = self._meter_page(meter)
            self.meter_fields[meter.meter_id] = fields
            icon = "📈" if meter.meter_id == 1 else "📉"
            self.sidebar.addItem(f"{icon} Meter {meter.meter_id} Settings")
            self.stack.addWidget(page)
        # Old names, kept for anything that still uses them
        f1, f2 = self.meter_fields[1], self.meter_fields[2]
        self.lbl_dir1, self.ka1_chk, self.al1_chk, self.al1_web = f1["dir"], f1["ka"], f1["alert"], f1["web"]
        self.lbl_dir2, self.ka2_chk, self.al2_chk, self.al2_web = f2["dir"], f2["ka"], f2["alert"], f2["web"]

        self.sidebar.currentRowChanged.connect(self.stack.setCurrentIndex)

        # Every change is saved as it's made (it used to wait for a "Save All Settings" click and
        # was lost when you didn't).
        hint = QLabel("Changes are saved as you make them.")
        hint.setStyleSheet("color: #9aa0a6; padding: 6px;")
        right_layout.addWidget(hint)
        main_layout.addLayout(right_layout)

        self.combo_style.currentTextChanged.connect(self.save_all)
        self.ent_template.textChanged.connect(self.save_all)
        self.auto_sync_chk.toggled.connect(self.save_all)
        for fields in self.meter_fields.values():
            for key in ("ka", "alert", "conn_alert", "dump"):
                fields[key].toggled.connect(self.save_all)
            for key in ("web", "thr", "cool", "delay"):
                fields[key].textChanged.connect(self.save_all)
            for key in ("stall", "dump_hrs"):
                fields[key].valueChanged.connect(self.save_all)

    def _meter_page(self, meter):
        mid = meter.meter_id
        cfg = meter.config
        page = QWidget()
        form = QFormLayout(page)
        f = {}
        saved_dir = cfg.get(f"default_save_location_{mid}", "") or ""
        f["dir"] = QLabel(saved_dir if saved_dir and saved_dir != "Not Set" else "Not Set")
        btn_dir = QPushButton("Browse...")
        btn_dir.clicked.connect(lambda checked=False, lbl=f["dir"], m=mid: self.choose_meter_dir(m, lbl))
        row = QHBoxLayout()
        row.addWidget(f["dir"])
        row.addWidget(btn_dir)
        form.addRow(f"Meter {mid} Save Directory:", row)

        f["ka"] = QCheckBox("Enable Keep-Alive (Ping every 2s)")
        f["ka"].setChecked(cfg.get(f"keep_alive_{mid}", True))
        form.addRow("", f["ka"])

        f["dump"] = QCheckBox("Download the meter's memory to Excel every")
        f["dump"].setToolTip("Saves Meter<N>_Memory_<date>.xlsx in the save folder (not if nothing changed)")
        f["dump"].setChecked(bool(cfg.get(f"auto_dump_enabled_{mid}", False)))
        f["dump_hrs"] = QDoubleSpinBox()
        f["dump_hrs"].setRange(0.25, 168.0)
        f["dump_hrs"].setDecimals(2)
        f["dump_hrs"].setSuffix(" h")
        try:
            f["dump_hrs"].setValue(float(cfg.get(f"default_auto_dump_hrs_{mid}", 2.0)))
        except (TypeError, ValueError):
            f["dump_hrs"].setValue(2.0)
        dump_row = QHBoxLayout()
        dump_row.addWidget(f["dump"])
        dump_row.addWidget(f["dump_hrs"])
        dump_row.addStretch()
        form.addRow("", dump_row)

        f["alert"] = QCheckBox("Enable Discord Alerts")
        f["alert"].setChecked(cfg.get(f"alert_enabled_{mid}", False))
        f["web"] = QLineEdit(cfg.get(f"alert_webhook_{mid}", ""))
        f["thr"] = QLineEdit(str(cfg.get(f"alert_threshold_{mid}", "0.5")))
        f["cool"] = QLineEdit(str(cfg.get(f"alert_cooldown_{mid}", "5.0")))
        f["delay"] = QLineEdit(str(cfg.get(f"alert_delay_{mid}", "5.0")))
        f["conn_alert"] = QCheckBox("Also alert when the connection drops, readings stop or a save fails")
        f["conn_alert"].setChecked(cfg.get(f"alert_connection_{mid}", True))
        f["stall"] = QSpinBox()
        f["stall"].setRange(0, 3600)
        f["stall"].setSuffix(" s")
        f["stall"].setSpecialValueText("Off")
        f["stall"].setMaximumWidth(140)
        f["stall"].setToolTip("While recording: warn when no reading has arrived for this long (0 = off)")
        try:
            f["stall"].setValue(int(float(cfg.get(f"stall_alert_s_{mid}", 60))))
        except (TypeError, ValueError):
            f["stall"].setValue(60)
        btn_test = QPushButton("Test Webhook")
        btn_test.clicked.connect(meter.test_discord_alert)
        form.addRow("", f["alert"])
        form.addRow("Discord Webhook URL:", f["web"])
        form.addRow("Log Change Threshold:", f["thr"])
        form.addRow("Baseline Delay (Minutes):", f["delay"])
        form.addRow("Cooldown (Minutes):", f["cool"])
        form.addRow("", f["conn_alert"])
        form.addRow("No-reading warning after:", f["stall"])
        form.addRow("", btn_test)
        return page, f

    def choose_global_dir(self, checked=False):
        d = QFileDialog.getExistingDirectory(self, "Select Global Directory")
        if d:
            self.lbl_folder.setText(settings_location.portable(d, DATA_DIR))
            self.save_all()

    def choose_meter_dir(self, meter_id, lbl):
        d = QFileDialog.getExistingDirectory(self, f"Select Directory for Meter {meter_id}")
        if d:
            lbl.setText(settings_location.portable(d, DATA_DIR))
            self.save_all()

    def save_all(self, *args):
        """Copy the Settings tab into the shared settings and save them (called on every change)."""
        app = self.parent_app
        shared = app.meter1.config  # one dict, shared by both meters and the app
        shared["global_default_save_folder"] = self.lbl_folder.text()
        shared["global_filename_template"] = self.ent_template.text()
        shared["global_auto_sync"] = self.auto_sync_chk.isChecked()
        for mid, f in self.meter_fields.items():
            folder = f["dir"].text()
            shared[f"default_save_location_{mid}"] = "" if folder == "Not Set" else folder
            shared[f"keep_alive_{mid}"] = f["ka"].isChecked()
            shared[f"auto_dump_enabled_{mid}"] = f["dump"].isChecked()
            shared[f"default_auto_dump_hrs_{mid}"] = f["dump_hrs"].value()
            shared[f"alert_enabled_{mid}"] = f["alert"].isChecked()
            shared[f"alert_webhook_{mid}"] = f["web"].text().strip()
            shared[f"alert_threshold_{mid}"] = f["thr"].text()
            shared[f"alert_delay_{mid}"] = f["delay"].text()
            shared[f"alert_cooldown_{mid}"] = f["cool"].text()
            shared[f"alert_connection_{mid}"] = f["conn_alert"].isChecked()
            shared[f"stall_alert_s_{mid}"] = f["stall"].value()

        style = self.combo_style.currentText()
        shared["button_style"] = style
        for meter in (app.meter1, app.meter2):
            meter.update_button_styles(style)
            if meter.chk_use_global_template.isChecked():
                meter.ent_save_template.setText(self.ent_template.text())
        app.save_shared_config()


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
        self.setWindowTitle(f"Apera Meter GUI {__version__}")
        self.resize(1400, 800)
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("QTabBar::tab { padding: 6px 16px; }")

        self.config_path = os.path.join(CONFIG_DIR, "config.json")
        # An unreadable config.json (half-written, sync conflict) is moved aside and the last good
        # copy used, with a warning once the window is up; it used to fall back to defaults silently.
        self.shared_config, self.config_problem = recording.load_json(self.config_path)
        self._config_warning_timer = QTimer(self)
        self._config_warning_timer.setSingleShot(True)
        self._config_warning_timer.timeout.connect(self._warn_config_problem)
        if self.config_problem:
            self._config_warning_timer.start(1500)
        self.local_state = recording.load_local_state(LOCAL_STATE_PATH)  # this computer only
        self._config_timer = QTimer(self)
        self._config_timer.setSingleShot(True)
        self._config_timer.setInterval(1000)
        self._config_timer.timeout.connect(lambda: self.save_shared_config(now=True))

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
            lambda s: (self.shared_config.update({"apply_hardware_lag": bool(s)}), self.save_shared_config())
        )

        self.chk_burst_on_snap = QCheckBox("Capture 30-Frame Burst on Armed Capture")
        self.chk_burst_on_snap.setChecked(self.shared_config.get("burst_on_snap", False))
        self.chk_burst_on_snap.setStyleSheet("color: #8E44AD; font-size: 13px; margin-left: 20px;")
        self.chk_burst_on_snap.stateChanged.connect(
            lambda s: (self.shared_config.update({"burst_on_snap": bool(s)}), self.save_shared_config())
        )

        from ocr_pipeline import MeterOCR

        self.ocr_pipeline = MeterOCR()
        self._apply_ocr_settings()  # the saved threshold and smoothing, before the first reading

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

        self._restore_layout()

    # --- Settings ------------------------------------------------------------------------------
    def save_shared_config(self, now=False):
        """Save config.json: a second after the last change, or at once with now=True.

        Written atomically with a .bak copy, and only when something changed, so typing in a box
        doesn't rewrite the file (in a folder SyncThing shares) on every keystroke.
        """
        if not now:
            self._config_timer.start()
            return
        self._config_timer.stop()
        try:
            recording.write_json_atomic(self.config_path, self.shared_config)
        except (OSError, TypeError, ValueError) as e:
            log.exception("Couldn't save the settings to %s", self.config_path)
            if not getattr(self, "_config_save_warned", False):
                self._config_save_warned = True
                self.statusBar().showMessage(f"Couldn't save the settings: {e}", 15000)

    def save_local_state(self):
        recording.save_local_state(LOCAL_STATE_PATH, self.local_state)

    def _warn_config_problem(self):
        QMessageBox.warning(self, "Settings", f"{self.config_problem}\n\nSettings folder: {CONFIG_DIR}")

    def _layout_value(self, key):
        """A window-layout value: this computer's, else the one older versions kept in config.json."""
        layout = self.local_state.get("layout", {})
        return layout[key] if key in layout else self.shared_config.get(key)

    def _restore_layout(self):
        from PyQt6.QtCore import QByteArray

        try:
            geometry = self._layout_value("app_geometry")
            if geometry:
                self.restoreGeometry(QByteArray.fromHex(geometry.encode()))
            tab = self._layout_value("app_tab")
            if isinstance(tab, int):
                self.tabs.setCurrentIndex(tab)
            for meter in (self.meter1, self.meter2):
                mid = meter.meter_id
                for i, w in enumerate(self._layout_value(f"col_widths_{mid}") or []):
                    meter.table.setColumnWidth(i, w)
                for name, splitter in (("main", meter.main_splitter), ("mid", meter.mid_splitter)):
                    state = self._layout_value(f"{name}_splitter_{mid}")
                    if state:
                        splitter.restoreState(QByteArray.fromHex(state.encode()))
        except Exception:
            log.warning("Couldn't restore the window layout", exc_info=True)

    def _remember_layout(self):
        """Window size and dividers, kept per computer (a Mac's layout doesn't fit the lab PC)."""
        layout = self.local_state.setdefault("layout", {})
        layout["app_geometry"] = self.saveGeometry().toHex().data().decode()
        layout["app_tab"] = self.tabs.currentIndex()
        for meter in (self.meter1, self.meter2):
            mid = meter.meter_id
            table = meter.table
            layout[f"col_widths_{mid}"] = [table.columnWidth(i) for i in range(table.columnCount())]
            layout[f"main_splitter_{mid}"] = meter.main_splitter.saveState().toHex().data().decode()
            layout[f"mid_splitter_{mid}"] = meter.mid_splitter.saveState().toHex().data().decode()
        self.save_local_state()

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
                log.warning("Couldn't refresh the desktop shortcut: %s", e)

        threading.Thread(target=refresh_shortcut, daemon=True).start()

    def update_check_on_start(self):
        """Settings → "Check for updates when the app starts" (on unless the user turned it off)."""
        return bool(self.meter1.config.get("check_updates_on_start", True))

    def set_update_check_on_start(self, on):
        self.meter1.config["check_updates_on_start"] = bool(on)
        self.save_shared_config(now=True)

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
                state = "connected" if meter._confirmed else "waiting for a reply"
                state += f" on {ser.port}"
            elif meter._reconnect_port:
                state = f"connection lost, reconnecting to {meter._reconnect_port}"
            else:
                state = f"not connected (port: {meter.port_combo.currentText() or 'none'})"
            model = meter.model_label.text().removeprefix("Model: ")
            if model and model != "Unknown":
                state += f", model {model}"
            if meter.is_recording:
                state += ", recording"
            if meter.records:
                state += f", {len(meter.records)} readings" + (" (unsaved)" if meter.unsaved_changes else "")
            extra[f"Meter {meter.meter_id}"] = state
            files = [os.path.basename(p) for p in (meter.autosave_path, meter.backup_path) if p]
            if files:
                extra[f"Meter {meter.meter_id} files"] = ", ".join(files)
        ocr = getattr(self, "ocr_pipeline", None)
        extra["OCR model"] = "loaded" if getattr(ocr, "model", None) is not None else "not loaded"
        output_log = os.path.join(LOG_DIR, "app.log")
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

    def _apply_ocr_settings(self):
        """Give the OCR pipeline the settings it reads itself (confidence threshold, smoothing).

        Called when the pipeline is created and whenever an OCR setting changes; call it again
        anywhere a new pipeline is made.
        """
        if getattr(self, "ocr_pipeline", None) is None:
            return
        self.ocr_pipeline.use_midpoint_filter = self.ocr_midpoint_chk.isChecked()
        self.ocr_pipeline.min_confidence = self.ocr_conf_spin.value()

    def _save_ocr_config(self):
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

        self._apply_ocr_settings()
        self.save_shared_config()

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
        """15 s after a run starts: warn about streams that look forgotten.

        Only streams that are in use are checked: a recording meter without its serial
        connection, a connected meter that isn't recording, and the camera when a reading area is
        drawn for a recording meter but the camera is off. A meter that is neither connected nor
        recording is taken as unused (it used to be listed every time, with the camera).
        """
        self._warning_timer_active = False
        meters = [m for m in (getattr(self, "meter1", None), getattr(self, "meter2", None)) if m is not None]
        recording_meters = [m for m in meters if m.is_recording]
        if not recording_meters:
            return  # User stopped recording before timer fired

        missing = []
        rois = getattr(getattr(self, "ocr_pipeline", None), "rois", {}) or {}
        camera_needed = any(rois.get(f"m{m.meter_id}_cond") for m in recording_meters)
        if camera_needed and not self.ocr_checkbox.isChecked():
            missing.append("OCR Camera (Turn Camera On)")
        for m in meters:
            if m.is_recording and not m.is_connected:
                missing.append(f"Meter {m.meter_id} Serial Connection (Disconnected)")
            elif m.is_connected and not m.is_recording:
                missing.append(f"Meter {m.meter_id} Recording Toggle (Rec M{m.meter_id})")

        if missing:
            QMessageBox.warning(
                self,
                "Missing Data Streams Warning",
                "You have started recording a run, but the following streams are offline "
                "or not recording:\n\n"
                + "\n".join([f"- {m}" for m in missing])
                + "\n\nIf you intended to record all of them, please enable them now. "
                "(This check can be turned off on the Live OCR Feed tab.)",
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
                f.write(DATASET_CSV_HEADER)

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
                f.write(DATASET_CSV_HEADER)
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
                result = cnn_raw
        else:
            result = final_val

        if result and getattr(self, "ocr_phys_max_chk", None) and self.ocr_phys_max_chk.isChecked():
            limit_spin = getattr(self, "ocr_phys_max_spin", None)
            if limit_spin and result > limit_spin.value():
                return 0.0

        # No usable reading (display unreadable, below the confidence threshold or filtered out):
        # 0.0, in every OCR mode, so the gap shows up in the log while recording.
        return result or 0.0

    def on_ocr_readings(self, readings):
        import time
        import datetime

        if not getattr(self, "ocr_running_logged", False):
            self.set_ocr_status("🎥 OCR: Running (1 Hz)", "#27ae60")
            self.ocr_running_logged = True

        now = datetime.datetime.now()
        dt_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # One confidence threshold (OCR settings) for both meters. Read it before either meter's
        # branch so Meter 2 still has it when Meter 1 sent no reading.
        min_conf = getattr(self.ocr_pipeline, "min_confidence", 0.0) if hasattr(self, "ocr_pipeline") else 0.0

        if len(readings) > 0 and readings[0] is not None:
            c1_cnn, c1_cv, c1_conf = readings[0]
            rejected = False
            if min_conf > 0.0 and c1_conf < min_conf:
                c1_cnn = 0.0
                rejected = True

            meter1_start = getattr(self.meter1, "first_serial_time", None)
            if meter1_start is None or time.time() - meter1_start >= 10:
                last_ms = getattr(self.meter1, "last_serial_cond_ms", 0.0)
                if last_ms == 0.0:
                    last_ms = getattr(self.meter1, "last_ocr_cond_ms", 0.0)
                c1 = self._apply_ocr_consensus(c1_cnn, c1_cv, last_ms)  # 0.0 = no usable reading
                if c1 != 0.0:
                    self.meter1.last_ocr_cond_ms = c1
                self._review_ocr_reading(1, self.meter1, readings[0][0], c1, c1_conf)
                row_data1 = (dt_str, "OCR", f"{c1 * 1000:.1f}", f"{c1:.3f}", "-", f"{c1_conf * 100:.1f}")
                if (
                    self.meter1.is_recording
                    and getattr(self, "ocr_autolog_checkbox", None)
                    and self.ocr_autolog_checkbox.isChecked()
                ):
                    self.meter1.signals.update_tree.emit(now, row_data1)

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
                c2 = self._apply_ocr_consensus(c2_cnn, c2_cv, last_ms)  # 0.0 = no usable reading
                if c2 != 0.0:
                    self.meter2.last_ocr_cond_ms = c2
                self._review_ocr_reading(2, self.meter2, readings[1][0], c2, c2_conf)
                row_data2 = (dt_str, "OCR", f"{c2 * 1000:.1f}", f"{c2:.3f}", "-", f"{c2_conf * 100:.1f}")
                if (
                    self.meter2.is_recording
                    and getattr(self, "ocr_autolog_checkbox", None)
                    and self.ocr_autolog_checkbox.isChecked()
                ):
                    self.meter2.signals.update_tree.emit(now, row_data2)

    def _review_ocr_reading(self, meter_id, meter, ocr_display, value_ms, conf):
        """While Auto-Record is on, snapshot logged OCR readings that look wrong (see ocr_review.py).

        The snapshots land in the current dataset session with a review.csv row, and the Dataset
        Curation tab lists them under "Needs review" so they can be labelled and added to training.
        """
        if not (self.shared_config.get("dataset_continuous") and getattr(self, "dataset_session_dir", None)):
            return
        import time

        from ocr_review import OcrAnomalyDetector, save_review_snapshot

        if not hasattr(self, "_ocr_detectors"):
            self._ocr_detectors = {}
        key = (meter_id, self.dataset_session_dir)  # a new session starts with fresh history
        det = self._ocr_detectors.get(key)
        if det is None:
            det = self._ocr_detectors[key] = OcrAnomalyDetector()
        now = time.time()
        last_t = getattr(meter, "last_data_time", None)
        age = now - last_t if last_t else None
        flags = det.check(value_ms, conf, getattr(meter, "last_serial_cond_ms", None), age)
        if not det.should_snapshot(flags, now):
            return
        frame = getattr(getattr(self, "ocr_feed_tab", None), "last_frame", None)
        if frame is None:
            return
        try:
            roi_imgs, _ = self.ocr_pipeline.extract_crops(frame)
            serial_display = getattr(meter, "last_dataset_display_str", "")
            # Within 5 s of a serial reading the display shows that value, so it is a usable label.
            label = serial_display if serial_display and age is not None and age <= 5 else "Unknown"
            save_review_snapshot(
                self.dataset_session_dir,
                roi_imgs.get(f"m{meter_id}_cond"),
                meter_id,
                now,
                label,
                {
                    "ocr_display": f"{ocr_display:g}",
                    "ocr_logged_ms": f"{value_ms:g}",
                    "ocr_conf": f"{conf:.4f}",
                    "flags": " ".join(flags),
                    "serial_display": serial_display,
                    "serial_age_s": f"{age:.1f}" if age is not None else "",
                    "local_median_ms": f"{det.median:g}" if det.median else "",
                },
            )
        except Exception as e:
            print(f"Review snapshot failed: {e}")

    def get_used_ports(self):
        used = []
        if self.meter1.is_connected and self.meter1.ser:
            used.append(self.meter1.ser.port)
        if self.meter2.is_connected and self.meter2.ser:
            used.append(self.meter2.ser.port)
        return used

    def _close_choice(self, lines):
        """Ask how to close with unsaved data: 'save', 'close' or 'cancel'."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved Data")
        box.setText("Close the app?\n\n" + "\n".join(lines))
        box.setInformativeText(
            "Save and Close writes each meter's readings to its auto-save file in the save folder. "
            "Readings are also in the crash-recovery backup (Recover Session)."
        )
        save_btn = box.addButton("Save and Close", QMessageBox.ButtonRole.AcceptRole)
        close_btn = box.addButton("Close Without Saving", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return "save"
        if clicked is close_btn:
            return "close"
        return "cancel"

    def closeEvent(self, event):
        meters = (self.meter1, self.meter2)
        unsaved = [m for m in meters if m.unsaved_changes and m.records]
        busy = [m for m in meters if m.is_recording]
        if unsaved or busy:
            lines = []
            for m in meters:
                if m.is_recording or (m.unsaved_changes and m.records):
                    state = "is recording" if m.is_recording else "has readings that aren't saved"
                    lines.append(f"- Meter {m.meter_id} {state} ({len(m.records)} readings)")
            choice = self._close_choice(lines)
            if choice == "cancel":
                event.ignore()
                return
            if choice == "save":
                for m in unsaved:
                    if not m.save_excel(auto=True, quiet=True):
                        QMessageBox.critical(
                            self,
                            "Unsaved Data",
                            f"Meter {m.meter_id}'s readings couldn't be saved (see its event log), so the "
                            "app stays open. Use Export As to save them somewhere else.",
                        )
                        event.ignore()
                        return

        self._config_warning_timer.stop()
        for m in meters:
            m.wait_for_background_save(timeout=30)
            m.disconnect_meter()
            m.housekeeping_timer.stop()

        try:
            self._remember_layout()
        except Exception:
            log.warning("Couldn't save the window layout", exc_info=True)
        self.save_shared_config(now=True)
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
    """Send output to <data folder>/logs/<computer>/app.log when there is no console to show it.

    That is the case for pythonw.exe (Windows launcher and shortcut), the standalone build, and
    the macOS launcher/shortcut (which set APERA_LOG_TO_FILE=1). Keeps one older log (app.log.1).
    """
    if sys.stderr is not None and os.environ.get("APERA_LOG_TO_FILE") != "1":
        return None
    path = os.path.join(LOG_DIR, "app.log")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
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
    # Errors go to logs/<computer>/apera_meter.log in the data folder (before stdout is redirected
    # to app.log there). One folder per computer, so two computers never write the same synced file.
    crash_log.setup("apera_meter", LOG_DIR, __version__)
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
