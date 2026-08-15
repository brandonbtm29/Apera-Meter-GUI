import sys
import os

# Ensure the working directory is the location of the script
script_dir = os.path.dirname(os.path.abspath(__file__))
if os.getcwd() != script_dir:
    os.chdir(script_dir)

import serial
import serial.tools.list_ports
import threading
import time
import math
import threading
import requests
import datetime
import struct
import csv
import json
import openpyxl
from openpyxl.styles import Font, Alignment
import pandas as pd

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QSplitter, QScrollArea, QLabel, QPushButton,
    QCheckBox, QLineEdit, QComboBox, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QFileDialog, QInputDialog,
    QFormLayout, QFrame, QDialog, QGroupBox, QGridLayout, QListWidget, QStackedWidget,
    QButtonGroup, QRadioButton, QSpinBox
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor, QAction

import qdarktheme

import matplotlib
matplotlib.use('QtAgg')
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

scan_lock = threading.Lock()

class NumericTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        if (isinstance(other, QTableWidgetItem)):
            try:
                return float(self.text()) < float(other.text())
            except ValueError:
                return self.text() < other.text()
        return super(NumericTableWidgetItem, self).__lt__(other)

class WorkerSignals(QObject):
    update_tree = pyqtSignal(object, tuple)
    update_status = pyqtSignal(str, str)
    update_device_info = pyqtSignal(str, str)


class CollapsibleBox(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        
        self.btn_toggle = QPushButton(f"▼ {title}")
        self.btn_toggle.setStyleSheet("text-align: left; font-weight: bold; padding: 5px; background-color: transparent; border: none;")
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
        
        self.config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        
        if self.app_controller and not hasattr(self.app_controller, 'shared_config'):
            self.app_controller.shared_config = {}
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r") as f:
                        self.app_controller.shared_config = json.load(f)
                except: pass
                
        self.config = self.app_controller.shared_config if self.app_controller else {}
        
        # Set defaults
        self.config.setdefault(f"default_com_port_{meter_id}", "COM4" if meter_id == 1 else "Auto-Detect Apera Meter")
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
        
        self.columns = ("DateTime", "Duration (min)", "Mode", "Cond (uS/cm)", "Cond (mS/cm)", "Temp (°C)", "Raw Cond_ADC")
        self.setup_backup_path()
        self.init_ui()
        
        self.refresh_ports()
        target_com = self.config.get(f"default_com_port_{self.meter_id}", "Auto-Detect Apera Meter")
        idx = self.port_combo.findText(target_com)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)
        
        # Auto-connect removed by user request

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
        
        self.auto_dump_chk = QCheckBox("Auto-Dump Memory (hrs):")
        self.auto_dump_chk.setChecked(self.config.get(f"auto_dump_enabled_{self.meter_id}", False))
        self.auto_dump_chk.stateChanged.connect(self._save_auto_dump_config)
        l_rec.addWidget(self.auto_dump_chk)
        self.auto_dump_entry = QLineEdit(str(self.config.get(f"default_auto_dump_hrs_{self.meter_id}", 2.0)))
        self.auto_dump_entry.textChanged.connect(self._save_auto_dump_config)
        l_rec.addWidget(self.auto_dump_entry)
        
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
        
        form_layout = QFormLayout()
        self.ent_time_win = QLineEdit(str(self.config.get(f"time_win_{self.meter_id}", "10.0")))
        self.ent_cond_min = QLineEdit(str(self.config.get(f"cond_min_{self.meter_id}", "0")))
        self.ent_cond_max = QLineEdit(str(self.config.get(f"cond_max_{self.meter_id}", "5000")))
        self.ent_temp_min = QLineEdit(str(self.config.get(f"temp_min_{self.meter_id}", "15")))
        self.ent_temp_max = QLineEdit(str(self.config.get(f"temp_max_{self.meter_id}", "35")))
        form_layout.addRow("Time Window (min):", self.ent_time_win)
        form_layout.addRow("Cond Min:", self.ent_cond_min)
        form_layout.addRow("Cond Max:", self.ent_cond_max)
        form_layout.addRow("Temp Min (°C):", self.ent_temp_min)
        form_layout.addRow("Temp Max (°C):", self.ent_temp_max)
        
        self.xaxis_combo = QComboBox()
        self.xaxis_combo.addItems(["X-Axis: DateTime", "X-Axis: Duration (min)"])
        self.xaxis_combo.setCurrentText(self.config.get(f"xaxis_{self.meter_id}", "X-Axis: DateTime"))
        form_layout.addRow("Plot Mode:", self.xaxis_combo)
        
        l_axis.addLayout(form_layout)
        
        self.btn_apply_lims = QPushButton("📊 Apply Limits")
        self.btn_apply_lims.clicked.connect(self.apply_limits)
        l_axis.addWidget(self.btn_apply_lims)
        
        self.gb_axis.setLayout(l_axis)
        left_layout.addWidget(self.gb_axis)
        

        left_layout.addStretch()
        
        left_scroll = QScrollArea()
        left_scroll.setWidget(left_widget)
        left_scroll.setWidgetResizable(True)
        
        # Middle Panel (Plot & Table)
        mid_splitter = QSplitter(Qt.Orientation.Vertical)
        
        self.fig, self.ax = plt.subplots(figsize=(5, 4))
        self.ax.set_title(f"Live Conductivity (Meter {self.meter_id})")
        self.ax.set_xlabel("Time")
        self.ax.set_ylabel("Conductivity (uS/cm)", color='#3498db')
        self.ax.grid(True, linestyle='--', alpha=0.3)
        self.ax2 = self.ax.twinx()
        self.ax2.set_ylabel("Temperature (°C)", color='#e74c3c')
        self.fig.tight_layout()
        
        self.canvas = FigureCanvasQTAgg(self.fig)
        mid_splitter.addWidget(self.canvas)
        
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.columns))
        self.table.setHorizontalHeaderLabels(self.columns)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSortingEnabled(True)
        mid_splitter.addWidget(self.table)
        
        # Right Panel (Connection & Controls)
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(10, 10, 10, 10)
        
        self.sn_label = QLabel("SN: Unknown")
        self.model_label = QLabel("Model: Unknown")
        self.sn_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        self.model_label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
        right_layout.addWidget(self.sn_label)
        right_layout.addWidget(self.model_label)

        lbl_conn = QLabel("Connection")
        lbl_conn.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        right_layout.addWidget(lbl_conn)
        
        self.port_combo = QComboBox()
        right_layout.addWidget(self.port_combo)
        
        self.btn_refresh = QPushButton("🔄 Refresh Ports")
        self.btn_refresh.clicked.connect(self.refresh_ports)
        right_layout.addWidget(self.btn_refresh)
        
        self.btn_connect = QPushButton("🔌 Connect")
        self.btn_connect.clicked.connect(self.toggle_connection)
        right_layout.addWidget(self.btn_connect)

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
        self.combo_autosave.addItems(["Off", "On Auto-Stop", "Every X Minutes"])
        self.combo_autosave.setCurrentText(self.config.get(f"autosave_mode_{self.meter_id}", "Off"))
        l_save.addRow("Auto-Save:", self.combo_autosave)
        
        self.ent_autosave_min = QLineEdit(self.config.get(f"autosave_min_{self.meter_id}", "5.0"))
        self.ent_autosave_min.setPlaceholderText("Minutes")
        self.ent_autosave_min.setMinimumWidth(50)
        l_save.addRow("Interval (min):", self.ent_autosave_min)
        
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
        
        left_scroll.setMinimumWidth(250)
        right_scroll.setMinimumWidth(360)
        
        # Add to main splitter
        self.main_splitter.addWidget(left_scroll)
        self.main_splitter.addWidget(mid_splitter)
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
        except: pass

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

    def _save_auto_dump_config(self):
        self.config[f"auto_dump_enabled_{self.meter_id}"] = self.auto_dump_chk.isChecked()
        self.config[f"default_auto_dump_hrs_{self.meter_id}"] = self.auto_dump_entry.text()
        self.save_config()

    def _save_autosave_config(self):
        self.config[f"autosave_mode_{self.meter_id}"] = self.combo_autosave.currentText()
        self.config[f"autosave_min_{self.meter_id}"] = self.ent_autosave_min.text()
        self.config[f"save_template_{self.meter_id}"] = self.ent_save_template.text()
        self.config[f"save_on_pause_{self.meter_id}"] = getattr(self, 'chk_save_on_pause', QCheckBox()).isChecked()
        self.save_config()

    def toggle_autosave_interval(self):
        self.ent_autosave_min.setEnabled(self.combo_autosave.currentText() == "Every X Minutes")

    def refresh_tokens(self):
        while self.l_tokens.rowCount() > 0:
            self.l_tokens.removeRow(0)
            
        old_values = {k: v.text() for k, v in getattr(self, 'token_inputs', {}).items()}
        self.token_inputs = {}
        
        default_tokens = [
            {"name": "Column #/Name", "value": ""},
            {"name": "Material", "value": ""},
            {"name": "Flow rate", "value": ""},
            {"name": "Bed length", "value": ""},
            {"name": "Run #", "value": ""},
            {"name": "(De)Sorption", "value": ""}
        ]
        
        global_tokens = getattr(self, 'app_controller', None) and self.app_controller.shared_config.get("global_tokens", None)
        if not global_tokens:
            global_tokens = default_tokens
        existing_names = [t.get("name") for t in global_tokens if t.get("name")]
        if "Column Name" in existing_names:
            for t in global_tokens:
                if t.get("name") == "Column Name": t["name"] = "Column #/Name"
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
        if value: saved_val = value # prefer in-memory value if passed
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
        
        global_tokens = getattr(self, 'app_controller', None) and self.app_controller.shared_config.get("global_tokens", None)
        if not global_tokens:
            global_tokens = [{"name": "Column #/Name"}, {"name": "Material"}, {"name": "Flow rate"}, {"name": "Bed length"}, {"name": "Run #"}, {"name": "(De)Sorption"}]
        for t in global_tokens:
            n = t.get("name")
            if n and n not in all_tokens: all_tokens.append(n)
            
        for name in getattr(self, 'token_inputs', {}):
            if name not in all_tokens: all_tokens.append(name)
            
        row = 0; col = 0
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
        global_tokens = getattr(self, 'app_controller', None) and self.app_controller.shared_config.get("global_tokens", None)
        if global_tokens:
            default_names = [t.get("name", "") for t in global_tokens if t.get("name")]
            
        if hasattr(self, 'token_inputs'):
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
        if getattr(self, 'app', None):
            self.app_controller.shared_config[f"use_global_{self.meter_id}"] = use_global
            self.app_controller.save_config()

    def get_resolved_string(self, template):
        import datetime
        import re
        s = template
        if hasattr(self, 'token_inputs'):
            for name, ent in self.token_inputs.items():
                s = s.replace(f"<{name}>", ent.text())
        s = s.replace("<Meter ID>", str(self.meter_id))
        
        now = datetime.datetime.now()
        start_dt = getattr(self, 'session_start_time', None) or now
        
        s = s.replace("<Start Date>", start_dt.strftime("%Y-%m-%d"))
        s = s.replace("<Start Time>", start_dt.strftime("%H%M%S"))
        s = s.replace("<Date>", now.strftime("%Y-%m-%d"))
        s = s.replace("<Time>", now.strftime("%H%M%S"))
        
        # Strip remaining unresolved tokens
        s = re.sub(r'<[^>]+>', '', s)
        # Sanitize invalid Windows filename characters
        s = re.sub(r'[<>:"/\\|?*]', '_', s)
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
        else:
            self.is_recording = False
            self.btn_record.setText("⏺ Start Recording")
            self.btn_record.setStyleSheet("background-color: #2ecc71; color: white;")
            self.set_status("Paused", "gray")
            
            if self.combo_autosave.currentText() in ["On Auto-Stop", "Every X Minutes"]:
                self.save_excel(auto=True)
                self.txt_notes.append("[Auto-Save] Data saved to directory automatically upon pause.")
            self.ax.set_title(f"Live (Not Recording): Conductivity (Meter {self.meter_id})")
        self.canvas.draw_idle()

    def _auto_chk_changed(self):
        try:
            self.config[f"auto_time_{self.meter_id}"] = self.auto_time_chk.isChecked()
            self.config[f"auto_cond_{self.meter_id}"] = self.auto_cond_chk.isChecked()
            self.config[f"auto_temp_{self.meter_id}"] = self.auto_temp_chk.isChecked()
            self.save_config()
        except: pass
        self.plot_data()

    def apply_limits(self, checked=False):
        try:
            self.config[f"time_win_{self.meter_id}"] = self.ent_time_win.text()
            self.config[f"cond_min_{self.meter_id}"] = self.ent_cond_min.text()
            self.config[f"cond_max_{self.meter_id}"] = self.ent_cond_max.text()
            self.config[f"temp_min_{self.meter_id}"] = self.ent_temp_min.text()
            self.config[f"temp_max_{self.meter_id}"] = self.ent_temp_max.text()
            self.config[f"xaxis_{self.meter_id}"] = self.xaxis_combo.currentText()
            self.config[f"auto_time_{self.meter_id}"] = self.auto_time_chk.isChecked()
            self.config[f"auto_cond_{self.meter_id}"] = self.auto_cond_chk.isChecked()
            self.config[f"auto_temp_{self.meter_id}"] = self.auto_temp_chk.isChecked()
            self.save_config()
        except Exception: pass
        
        self.plot_data()

    def save_graph(self, checked=False):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save Graph", "", "PNG (*.png)")
        if filepath:
            try:
                self.fig.savefig(filepath, dpi=300, bbox_inches='tight')
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

    def scan_backup_sessions(self):
        if not os.path.exists(self.backup_path): return []
        sessions = []
        try:
            with open(self.backup_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                rows = list(reader)
                
            current_session = None
            last_dur = -1.0
            
            for idx, row in enumerate(rows):
                if len(row) >= 6:
                    try:
                        ts = row[0]
                        dur = float(row[1])
                        
                        if current_session is None or dur < last_dur:
                            if current_session:
                                current_session["end_idx"] = idx - 1
                                sessions.append(current_session)
                            current_session = {"start_idx": idx, "start_ts": ts, "end_ts": ts, "points": 0}
                            
                        current_session["end_ts"] = ts
                        current_session["points"] += 1
                        last_dur = dur
                    except: pass
                    
            if current_session:
                current_session["end_idx"] = len(rows) - 1
                sessions.append(current_session)
        except: pass
        return sessions

    def prompt_session_recovery(self, checked=False):
        sessions = self.scan_backup_sessions()
        if not sessions: 
            QMessageBox.information(self, "Recovery", "No recovery sessions found.")
            return
            
        dialog = QDialog(self)
        dialog.setWindowTitle("Restore Session")
        dialog.setMinimumSize(400, 300)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Multiple recording sessions were found. Please select which one to restore:"))
        
        scroll = QScrollArea()
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        
        bg = QButtonGroup(dialog)
        for i, s in enumerate(sessions):
            rb = QRadioButton(f"Session {i+1} | {s['start_ts']} -> {s['end_ts']} | {s['points']} pts")
            if i == len(sessions)-1: rb.setChecked(True)
            bg.addButton(rb, i)
            scroll_layout.addWidget(rb)
            
        scroll_widget.setLayout(scroll_layout)
        scroll.setWidget(scroll_widget)
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll)
        
        btn_layout = QHBoxLayout()
        btn_restore = QPushButton("Restore Selected")
        btn_ignore = QPushButton("Cancel")
        btn_layout.addWidget(btn_restore)
        btn_layout.addWidget(btn_ignore)
        layout.addLayout(btn_layout)
        
        def do_restore(checked=False):
            idx = bg.checkedId()
            if idx >= 0:
                s = sessions[idx]
                try:
                    with open(self.backup_path, 'r', newline='', encoding='utf-8') as f:
                        reader = csv.reader(f)
                        header = next(reader, None)
                        rows = list(reader)
                        
                    self.clear_log(init_file=False)
                    self.table.setSortingEnabled(False)
                    
                    for r_idx in range(s["start_idx"], s["end_idx"] + 1):
                        row = rows[r_idx]
                        if len(row) == 6:
                            dur_min = "0.00"
                            try: dur_min = f"{float(row[1]) * 60:.2f}"
                            except: pass
                            row = [row[0], dur_min, "Cond", row[2], row[3], row[4], row[5]]
                            
                        if len(row) >= 7:
                            self.received_timestamps.add(row[0])
                            r = self.table.rowCount()
                            self.table.insertRow(r)
                            for col, val in enumerate(row):
                                self.table.setItem(r, col, NumericTableWidgetItem(str(val)))
                            
                            try:
                                dt = datetime.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                                if not getattr(self, 'session_start_time', None):
                                    self.session_start_time = dt
                                self.plot_dates.append(dt)
                                self.plot_conds.append(float(row[3]) if row[3] else 0.0)
                                self.plot_temps.append(float(row[5]) if row[5] else 0.0)
                            except: pass
                            
                    self.table.setSortingEnabled(True)
                    self.plot_data()
                    QMessageBox.information(self, "Recover", f"Recovered {s['points']} points.")
                except serial.SerialException as e:
                    self.parent_app.update_meter_status(self.meter_id, False)
                    self.is_connected = False
                    QMessageBox.critical(self, "Error", f"Recovery failed: {e}")
            dialog.accept()
            
        btn_restore.clicked.connect(do_restore)
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
                if p in used_ports: continue
                try:
                    ts = serial.Serial(p, 115200, timeout=0.5)
                    ts.setDTR(False)
                    ts.setRTS(False)
                    ts.write(b'&OK#\r\n')
                    time.sleep(0.3)
                    resp = ts.read(ts.in_waiting)
                    ts.close()
                    if b'<SH' in resp or b'<SL' in resp or b'#>' in resp:
                        return p
                except: pass
            return None

    def connect_meter(self):
        port = self.port_combo.currentText()
        if port == "Auto-Detect Apera Meter":
            if not getattr(self, "is_scanning", False):
                self.set_status("Scanning COM ports...", "gray")
                self.btn_connect.setEnabled(False)
                self.is_scanning = True
                
                self.scan_id = getattr(self, 'scan_id', 0) + 1
            current_id = self.scan_id
            
            def timeout_scan():
                if getattr(self, 'scan_id', None) == current_id:
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
        self._finish_connect(port)

    def _scan_thread_task(self, scan_id=None):
        found_port = self.find_apera_meter()
        QTimer.singleShot(0, lambda: self._finish_connect(found_port, scan_id))

    def _finish_connect(self, port, scan_id=None):
        if scan_id is not None and getattr(self, 'scan_id', None) != scan_id:
            return
        self.is_scanning = False
        self.btn_connect.setEnabled(True)
        if not port:
            self.set_status("Auto-Detect Failed", "red")
            QMessageBox.critical(self, "Connection Error", f"Could not auto-detect Apera Meter {self.meter_id} on any COM port.")
            return
            
        try:
            idx = self.port_combo.findText(port)
            if idx >= 0: self.port_combo.setCurrentIndex(idx)
            
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
            
            self.ser.write(b"&OK#\r\n")
            if self.app_controller and self.app_controller.shared_config.get("global_auto_sync", False):
                QTimer.singleShot(500, self.sync_time)
            
            threading.Thread(target=self.read_serial_loop, daemon=True).start()
            threading.Thread(target=self.keep_alive_loop, daemon=True).start()
            threading.Thread(target=self.auto_dump_loop, daemon=True).start()
        except Exception as e:
            self.set_status(f"Failed to connect: {e}", "red")
            self.btn_connect.setText("🔌 Connect")

    def disconnect_meter(self):
        self.is_connected = False
        
        # Defer ser.close() to a background thread because PySerial close() can block
        # on Windows if another thread is currently polling it.
        def async_close(ser):
            time.sleep(0.1) # Let the read loops exit first
            if ser and ser.is_open:
                try: ser.close()
                except: pass
                
        threading.Thread(target=async_close, args=(self.ser,), daemon=True).start()
        
        self.btn_connect.setText("🔌 Connect")
        self.set_status("Disconnected", "gray")
        self.btn_download.setEnabled(False)
        self.btn_sync.setEnabled(False)
        self.sn_label.setText("SN: Unknown")
        self.model_label.setText("Model: Unknown")

    def read_serial_loop(self):
        buffer = b""
        self.last_data_time = 0
        while self.is_connected:
            try:
                if self.ser.in_waiting > 0:
                    data = self.ser.read(self.ser.in_waiting)
                    buffer += data
                    buffer = buffer.replace(b'#\r\n', b'')
                    while b'>' in buffer:
                        idx = buffer.find(b'>')
                        packet = buffer[:idx+1]
                        buffer = buffer[idx+1:]
                        try:
                            self.process_packet(packet)
                        except Exception as e:
                            print(f"Meter {self.meter_id} Packet Parse Error: {e}")
            except Exception as e:
                print(f"Meter {self.meter_id} Serial Error: {e}")
                self.set_status(f"Disconnected ({type(e).__name__})", "red")
                self.is_connected = False
                break
            time.sleep(0.05)

    def auto_dump_loop(self):
        while self.is_connected:
            time.sleep(10)
            if self.is_connected and self.auto_dump_chk.isChecked():
                try: hours = float(self.auto_dump_entry.text())
                except ValueError: continue
                if time.time() - self.last_auto_dump_time > (hours * 3600):
                    self.last_auto_dump_time = time.time()
                    if self.ser and self.ser.is_open:
                        self.ser.write(b"&WA#\r\n")

    def keep_alive_loop(self):
        while self.is_connected:
            if self.config.get(f"keep_alive_{self.meter_id}", True):
                if time.time() - self.last_data_time > 2.0:
                    try:
                        if self.ser and self.ser.is_open:
                            self.ser.write(b"&OK#\r\n")
                    except: pass
            time.sleep(1.5)

    def process_packet(self, packet):
        if not packet: return
        packet = packet.strip(b'\r\n')
        p_len = len(packet)
        if p_len < 3 or packet == b'>' or packet == b'#>': return
            
        if (packet.startswith(b'<DS') or packet.startswith(b'<DN')) and p_len >= 12:
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
                if dot == 1: val_float = DP / 10.0
                elif dot == 2: val_float = DP / 100.0
                elif dot == 3: val_float = DP / 1000.0
                
                cond_us, cond_ms = "", ""
                cond_us_val = val_float
                if mode == 2:
                    u_val = b + 10
                    if u_val == 10: 
                        cond_ms = f"{val_float:.3f}"
                        cond_us = f"{val_float * 1000:.1f}"
                        cond_us_val = val_float * 1000
                    elif u_val == 11: 
                        cond_ms = f"{val_float / 1000:.3f}"
                        cond_us = f"{val_float:.1f}"
                        cond_us_val = val_float
                    try: self.last_serial_cond_ms = float(cond_ms)
                    except: pass
                else:
                    cond_us = str(val_float)
                
                if cond_us_val > 0:
                    curr_log = math.log10(cond_us_val)
                    if self.config.get(f"alert_enabled_{self.meter_id}", False):
                        webhook_url = self.config.get(f"alert_webhook_{self.meter_id}", "")
                        try: threshold = float(self.config.get(f"alert_threshold_{self.meter_id}", 0.5))
                        except: threshold = 0.5
                        try: cooldown_min = float(self.config.get(f"alert_cooldown_{self.meter_id}", 5.0))
                        except: cooldown_min = 5.0
                        try: delay_min = float(self.config.get(f"alert_delay_{self.meter_id}", 5.0))
                        except: delay_min = 5.0
                        
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
                                    msg = f"🔔 **Meter {self.meter_id} Alert**\nConductivity changed significantly!\nPrevious baseline log: {self.baseline_log_cond:.3f}\nCurrent log: {curr_log:.3f}\nCurrent value: {cond_us_val:.1f} µS/cm"
                                    self.baseline_log_cond = curr_log
                                    self.send_discord_alert(webhook_url, msg)
                
                dt_str = f"20{yy:02d}-{mt:02d}-{dd:02d} {hh:02d}:{mm:02d}:{ss:02d}"
                dt_obj = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                
                is_dn_packet = packet.startswith(b'<DN')
                if is_dn_packet:
                    dt_str += " (Manual)"
                
                if dt_str in self.received_timestamps: return
                self.received_timestamps.add(dt_str)
                
                row_data = (dt_str, mode_str, cond_us, cond_ms, f"{temp_c:.1f}", str(w4))
                
                app = self.parent_app
                is_continuous = getattr(app, 'dataset_checkbox', None) and app.dataset_checkbox.isChecked()
                is_manual = getattr(app, 'manual_snap_requested', False)
                
                if self.is_recording or is_dn_packet or is_manual:
                    if getattr(self, 'ocr_active', False):
                        # Throttle OCR visual logging to 10s if we wanted to
                        pass
                    self.signals.update_tree.emit(dt_obj, row_data)
                    
                # --- SMART DATASET RECORDING LOGIC ---
                if is_manual and not getattr(app, 'dataset_session_dir', None):
                    import os, time, json
                    timestamp = int(time.time())
                    app.dataset_session_dir = os.path.join(os.getcwd(), f"dataset_session_{timestamp}")
                    frames_dir = os.path.join(app.dataset_session_dir, "frames")
                    os.makedirs(frames_dir, exist_ok=True)
                    app.dataset_csv_path = os.path.join(app.dataset_session_dir, "readings.csv")
                    with open(app.dataset_csv_path, "w", encoding="utf-8") as f:
                        f.write("timestamp,meter_id,screen_val,unit,raw_cond\n")
                    rois_path = os.path.join(app.dataset_session_dir, "rois.json")
                    with open(rois_path, "w") as f:
                        json.dump(app.ocr_pipeline.rois, f)

                if (is_continuous or is_manual) and getattr(app, 'dataset_session_dir', None):
                    # The physical LCD updates ~Xms AFTER the serial packet is sent.
                    from PyQt6.QtCore import QTimer
                    
                    ts = time.time()
                    
                    if is_manual:
                        app.manual_snap_requested = False
                        
                    # Retrieve dynamic hardware lag
                    hw_lag = self.config.get("hardware_lag_ms", 300)
                    
                    def capture_delayed_frame(cap_ts=ts, c_ms=cond_ms, r_cond=str(w4), m_id=self.meter_id, m_flag=is_manual):
                        import cv2
                        import os
                        frame = app.ocr_feed_tab.last_frame
                        if frame is not None:
                            frame_path = os.path.join(app.dataset_session_dir, "frames", f"frame_{cap_ts}.jpg")
                            cv2.imwrite(frame_path, frame)
                            with open(app.dataset_csv_path, "a", encoding="utf-8") as f:
                                f.write(f"{cap_ts},{m_id},{c_ms},mS,{r_cond}\n")
                                
                        if m_flag:
                            try:
                                app.btn_manual_snap.setText("📷 Manual Snap (Phase 2)")
                                app.btn_manual_snap.setEnabled(True)
                            except: pass
                            
                    QTimer.singleShot(hw_lag, capture_delayed_frame)
                    
                # --- PHASE 1: CALIBRATION BURST LOGIC ---
                if getattr(app, 'calibration_burst_requested', False):
                    app.calibration_burst_requested = False
                    app.calibration_burst_meter = self.meter_id
                    app.calibration_burst_serial_ts = time.time()
                    app.calibration_burst_screen_val = cond_ms
                    app.calibration_burst_count = 15 # 15 frames @ 30 FPS = ~500ms burst
                    try:
                        app.btn_calibration_burst.setText("Recording Burst...")
                        app.btn_calibration_burst.setEnabled(False)
                    except: pass
                    
            except Exception as e: 
                print(f"Packet error: {e}")
                
        elif packet.startswith(b'<SH') and p_len >= 12:
            payload = packet[3:11]
            hex_str = "-".join([f"{b:02X}" for b in payload])
            self.signals.update_device_info.emit("SN", hex_str)
        elif packet.startswith(b'<P2') and p_len >= 12:
            payload = packet[3:11]
            hex_str = "-".join([f"{b:02X}" for b in payload])
            self.signals.update_device_info.emit("Model", hex_str)

    def log_point_ui(self, dt, row_data):
        if not getattr(self, 'session_start_time', None):
            self.session_start_time = dt
            
        duration_min = (dt - self.session_start_time).total_seconds() / 60.0
        
        if len(row_data) == 6:
            full_row = (row_data[0], f"{duration_min:.2f}", row_data[1], row_data[2], row_data[3], row_data[4], row_data[5])
        else:
            full_row = row_data
            
        self.plot_dates.append(dt)
        self.plot_conds.append(float(full_row[3]) if full_row[3] else 0.0)
        self.plot_temps.append(float(full_row[5]) if full_row[5] else 0.0)
        
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
        except: pass
        
        self.schedule_plot()

    def set_status(self, text, color):
        if self.app_controller:
            status_lbl = self.app_controller.status_m1 if self.meter_id == 1 else self.app_controller.status_m2
            status_lbl.setText(f"M{self.meter_id}: {text}")
            status_lbl.setStyleSheet(f"color: {color}; font-weight: bold; margin-right: 20px;")
            
        if text.endswith("(Error)"):
            self.btn_connect.setText("🔌 Connect")
            self.btn_download.setEnabled(False)
            self.btn_sync.setEnabled(False)

    def set_device_info(self, typ, val):
        if typ == "SN": self.sn_label.setText(f"SN: {val}")
        elif typ == "Model": self.model_label.setText(f"Model: {val}")


    def send_discord_alert(self, url, msg):
        if not url: return
        def worker():
            try:
                requests.post(url, json={"content": msg}, timeout=5)
            except Exception as e:
                print("Webhook error:", e)
        threading.Thread(target=worker, daemon=True).start()

    def test_discord_alert(self, checked=False):
        url = self.config.get(f"alert_webhook_{self.meter_id}", "")
        if url:
            self.send_discord_alert(url, f"🧪 **Test Alert from Meter {self.meter_id}**\\nThis is a test to confirm the webhook works!")
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

        self.ax.set_yscale('log')
        if self.is_recording:
            self.ax.set_title(f"Recording: Conductivity (Meter {self.meter_id})", color='white')
        else:
            self.ax.set_title(f"Live: Conductivity (Meter {self.meter_id})", color='white')
            
        self.ax.set_xlabel("Time", color='white')
        self.ax.set_ylabel("Conductivity (uS/cm)", color='#3498db')
        self.ax.grid(True, linestyle='--', alpha=0.3)
        self.ax.tick_params(colors='white')
        self.ax2.set_ylabel("Temperature (°C)", color='#e74c3c')
        self.ax2.tick_params(colors='white')
        
        # Style adjustments for dark theme integration
        self.fig.patch.set_facecolor('#1e1e2e')
        self.ax.set_facecolor('#1e1e2e')
        
        if self.plot_dates:
            import matplotlib.ticker as ticker
            
            is_duration = self.xaxis_combo.currentText() == "X-Axis: Duration (min)"
            
            if not getattr(self, 'session_start_time', None):
                self.session_start_time = self.plot_dates[0]
                
            durations = [(d - self.session_start_time).total_seconds() / 60.0 for d in self.plot_dates]
            
            if self.is_recording and self.combo_autosave.currentText() == "Every X Minutes":
                try: autosave_interval = float(self.ent_autosave_min.text())
                except: autosave_interval = 5.0
                
                if len(durations) > 0:
                    elapsed_min = durations[-1]
                    last_autosave = getattr(self, 'last_autosave_min', 0.0)
                    if (elapsed_min - last_autosave) >= autosave_interval:
                        self.save_excel(auto=True)
                        self.last_autosave_min = elapsed_min

            if is_duration:
                x_data = durations
                self.ax.set_xlabel("Duration (min)", color='white')
            else:
                x_data = self.plot_dates
                self.ax.set_xlabel("Time", color='white')
                
            self.ax.plot(x_data, self.plot_conds, marker='o', linestyle='-', color='#3498db', label="Cond")
            
            if not self.auto_time_chk.isChecked():
                try:
                    win_min = float(self.ent_time_win.text())
                    xmax = x_data[-1]
                    if is_duration:
                        xmin = xmax - win_min
                    else:
                        xmin = xmax - datetime.timedelta(minutes=win_min)
                    self.ax.set_xlim(xmin, xmax)
                except: pass
            
            if not self.auto_cond_chk.isChecked():
                try:
                    cmin = float(self.ent_cond_min.text())
                    cmax = float(self.ent_cond_max.text())
                    self.ax.set_ylim(cmin, cmax)
                except: pass
                
            self.ax2.plot(x_data, self.plot_temps, marker='s', linestyle='-', color='#e74c3c', label="Temp")
            
            if not self.auto_temp_chk.isChecked():
                try:
                    tmin = float(self.ent_temp_min.text())
                    tmax = float(self.ent_temp_max.text())
                    self.ax2.set_ylim(tmin, tmax)
                except: pass
                
            if is_duration:
                self.ax.xaxis.set_major_formatter(ticker.ScalarFormatter())
                self.fig.autofmt_xdate(rotation=0, ha='center')
            else:
                self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
                self.fig.autofmt_xdate()
            
        self.fig.tight_layout()
        self.canvas.draw_idle()

    def sync_time(self, checked=False):
        if self.is_connected and self.ser:
            now = datetime.datetime.now()
            try:
                packet = bytearray([
                    ord('&'), ord('D'), ord('T'),
                    now.year - 2000, now.month, now.day,
                    now.hour, now.minute, now.second,
                    ord('#')
                ])
                packet.extend(b'\r\n')
                self.ser.write(packet)
            except: pass

    def download_memory(self, checked=False):
        if not self.ser: return
        self.set_status("Downloading Memory...", "gray")
        self.btn_download.setEnabled(False)
        self.ser.write(b"&WA#\r\n")

    def save_excel(self, auto=False):
        if self.table.rowCount() == 0:
            if not auto: QMessageBox.information(self, "Export", "No data to export.")
            return
            
        use_global = self.chk_use_global_template.isChecked()
        if use_global and self.app_controller:
            template = self.app_controller.shared_config.get("global_filename_template", "<Meter ID>_<Date>_<Time>")
            docs = self.app_controller.shared_config.get("global_default_save_folder", "Data")
        else:
            template = self.ent_save_template.text()
            docs = self.config.get(f"default_save_location_{self.meter_id}", "")
            
        if not template.strip(): template = "<Meter ID>_<Date>_<Time>"
        filename = self.get_resolved_string(template)
        if not filename.endswith(".xlsx"): filename += ".xlsx"
        
        if not docs or not os.path.isdir(docs): docs = os.path.expanduser("~/Documents")
        filepath = os.path.join(docs, filename)
        
        if not auto:
            filepath, _ = QFileDialog.getSaveFileName(self, "Export Excel", filepath, "Excel files (*.xlsx);;All files (*.*)")
            if not filepath: return
        
        try:
            from openpyxl.utils.dataframe import dataframe_to_rows
            
            data = []
            for row in range(self.table.rowCount()):
                row_data = []
                for col in range(self.table.columnCount()):
                    item = self.table.item(row, col)
                    row_data.append(item.text() if item else "")
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
            doc_ws["B3"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            doc_ws["A4"] = "Meter SN:"
            doc_ws["B4"] = self.sn_label.text().replace("SN: ", "")
            
            current_row = 6
            doc_ws.cell(row=current_row, column=1, value="Metadata Tokens").font = Font(bold=True)
            current_row += 1
            if hasattr(self, 'token_inputs'):
                for name, ent in self.token_inputs.items():
                    doc_ws.cell(row=current_row, column=1, value=name + ":")
                    doc_ws.cell(row=current_row, column=2, value=ent.text())
                    current_row += 1
                    
            current_row += 1
            doc_ws.cell(row=current_row, column=1, value="Experiment Notes:").font = Font(bold=True)
            current_row += 1
            doc_ws.cell(row=current_row, column=1, value=self.txt_notes.toPlainText()).alignment = Alignment(wrap_text=True, vertical="top")
            doc_ws.row_dimensions[current_row].height = 100
            
            doc_ws.column_dimensions["A"].width = 25
            doc_ws.column_dimensions["B"].width = 35
            
            wb.save(filepath)
            self.set_status("Exported to Excel!", "#27ae60")
            self.unsaved_changes = False
        except Exception as e:
            try:
                with open(os.path.expanduser("~/autosave_error.log"), "a") as f:
                    import traceback
                    f.write(f"{datetime.datetime.now()}: {str(e)}\n{traceback.format_exc()}\n")
            except: pass
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
        
        # 1. Global & Application Settings
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
        
        btn_restart = QPushButton("🔄 Restart Application")
        btn_restart.setStyleSheet("background-color: #8e44ad; color: white; padding: 10px; font-weight: bold;")
        btn_restart.clicked.connect(self.parent_app.restart_app)
        l_app_inner.addWidget(btn_restart)
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
        dir_layout.addWidget(QLabel("Default Global Directory:"))
        dir_layout.addWidget(self.lbl_folder)
        dir_layout.addWidget(btn_folder)
        l_save_inner.addLayout(dir_layout)
        
        l_save_inner.addWidget(QLabel("Filename Template:"))
        self.ent_template = QLineEdit(self.parent_app.meter1.config.get("global_filename_template", "<Meter ID>_<Date>_<Time>"))
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
        
        self.sidebar.addItem("🌍 Global & Application Settings")
        self.stack.addWidget(page_global)
        
        # 2. Meter 1 Settings
        page_m1 = QWidget()
        l_m1 = QFormLayout(page_m1)
        self.lbl_dir1 = QLabel(self.parent_app.meter1.config.get(f"default_save_location_1", "Not Set"))
        btn_dir1 = QPushButton("Browse...")
        btn_dir1.clicked.connect(lambda checked=False: self.choose_meter_dir(1, self.lbl_dir1))
        d1 = QHBoxLayout(); d1.addWidget(self.lbl_dir1); d1.addWidget(btn_dir1)
        l_m1.addRow("Meter 1 Save Directory:", d1)
        self.ka1_chk = QCheckBox("Enable Keep-Alive (Ping every 2s)")
        self.ka1_chk.setChecked(self.parent_app.meter1.config.get(f"keep_alive_1", True))
        l_m1.addRow("", self.ka1_chk)
        
        self.al1_chk = QCheckBox("Enable Discord Alerts")
        self.al1_chk.setChecked(self.parent_app.meter1.config.get(f"alert_enabled_1", False))
        self.al1_web = QLineEdit(self.parent_app.meter1.config.get(f"alert_webhook_1", ""))
        self.al1_thr = QLineEdit(str(self.parent_app.meter1.config.get(f"alert_threshold_1", "0.5")))
        self.al1_cool = QLineEdit(str(self.parent_app.meter1.config.get(f"alert_cooldown_1", "5.0")))
        self.al1_del = QLineEdit(str(self.parent_app.meter1.config.get(f"alert_delay_1", "5.0")))
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
        self.lbl_dir2 = QLabel(self.parent_app.meter2.config.get(f"default_save_location_2", "Not Set"))
        btn_dir2 = QPushButton("Browse...")
        btn_dir2.clicked.connect(lambda checked=False: self.choose_meter_dir(2, self.lbl_dir2))
        d2 = QHBoxLayout(); d2.addWidget(self.lbl_dir2); d2.addWidget(btn_dir2)
        l_m2.addRow("Meter 2 Save Directory:", d2)
        self.ka2_chk = QCheckBox("Enable Keep-Alive (Ping every 2s)")
        self.ka2_chk.setChecked(self.parent_app.meter2.config.get(f"keep_alive_2", True))
        l_m2.addRow("", self.ka2_chk)

        self.al2_chk = QCheckBox("Enable Discord Alerts")
        self.al2_chk.setChecked(self.parent_app.meter2.config.get(f"alert_enabled_2", False))
        self.al2_web = QLineEdit(self.parent_app.meter2.config.get(f"alert_webhook_2", ""))
        self.al2_thr = QLineEdit(str(self.parent_app.meter2.config.get(f"alert_threshold_2", "0.5")))
        self.al2_cool = QLineEdit(str(self.parent_app.meter2.config.get(f"alert_cooldown_2", "5.0")))
        self.al2_del = QLineEdit(str(self.parent_app.meter2.config.get(f"alert_delay_2", "5.0")))
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
        btn_save.setStyleSheet("background-color: #3498db; color: white; padding: 12px; font-size: 14px; font-weight: bold;")
        btn_save.clicked.connect(self.save_all)
        right_layout.addWidget(btn_save)
        
        main_layout.addLayout(right_layout)

    def choose_global_dir(self, checked=False):
        d = QFileDialog.getExistingDirectory(self, "Select Global Directory")
        if d: self.lbl_folder.setText(d)
        
    def choose_meter_dir(self, meter_id, lbl):
        d = QFileDialog.getExistingDirectory(self, f"Select Directory for Meter {meter_id}")
        if d: lbl.setText(d)

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
        
        self.parent_app.meter1.save_config()
        self.parent_app.meter2.save_config()
        
        QMessageBox.information(self, "Success", "All settings saved successfully.")

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
import cv2

from PyQt6.QtWidgets import QRubberBand, QSlider, QPushButton, QHBoxLayout, QDoubleSpinBox
from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
import cv2

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
        splitter.addWidget(scroll_area)
        
        # Global OCR Controls (Migrated from Toolbar)
        gb_global = QGroupBox("Global Controls")
        l_global = QVBoxLayout(gb_global)
        l_global.addWidget(self.app.ocr_checkbox)
        l_global.addWidget(self.app.mode_combo)
        l_global.addWidget(self.app.btn_calibration_burst)
        l_global.addWidget(self.app.dataset_checkbox)
        l_global.addWidget(self.app.btn_manual_snap)
        
        # Camera Focus & Index
        self.focus_slider = QSlider(Qt.Orientation.Horizontal)
        self.focus_slider.setRange(-1, 255)
        self.focus_slider.setValue(-1)
        self.focus_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.focus_slider.setTickInterval(10)
        self.focus_label = QLabel("Focus: Auto")
        self.focus_slider.valueChanged.connect(self.on_focus_changed)
        
        self.camera_index_spin = QSpinBox()
        self.camera_index_spin.setRange(0, 10)
        self.camera_index_spin.setValue(self.app.ocr_pipeline.camera_index if self.app and hasattr(self.app, 'ocr_pipeline') else 0)
        self.camera_index_spin.valueChanged.connect(self.on_camera_index_changed)
        
        self.ocr_interval_spin = QDoubleSpinBox()
        self.ocr_interval_spin.setRange(0.25, 60.0)
        self.ocr_interval_spin.setDecimals(2)
        self.ocr_interval_spin.setSingleStep(0.1)
        self.ocr_interval_spin.setValue(self.app.ocr_pipeline.ocr_interval if self.app and hasattr(self.app, 'ocr_pipeline') else 1.0)
        self.ocr_interval_spin.valueChanged.connect(self.on_ocr_interval_changed)
        
        l_global.addWidget(QLabel("Camera Focus:"))
        l_global.addWidget(self.focus_slider)
        l_global.addWidget(self.focus_label)
        l_global.addWidget(QLabel("Camera Index (0=default web, 1=external):"))
        l_global.addWidget(self.camera_index_spin)
        l_global.addWidget(QLabel("OCR Sampling Rate (s) [0.25 - 60.0]:"))
        l_global.addWidget(self.ocr_interval_spin)
        
        left_layout.addWidget(gb_global)
        
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
            gb = QGroupBox(title)
            l = QVBoxLayout(gb)
            
            btn_set = QPushButton(f"✅ Assign {title} to Drawn Box")
            btn_set.clicked.connect(lambda checked, n=name: self.set_specific_roi(n))
            l.addWidget(btn_set)
            
            grid = QGridLayout()
            spins = {}
            for i, prop in enumerate(["X", "Y", "W", "H", "Angle"]):
                grid.addWidget(QLabel(prop), i, 0)
                spin = QDoubleSpinBox()
                if prop == "Angle":
                    spin.setRange(-180.0, 180.0)
                else:
                    spin.setRange(0.0, 100.0) # Percentage
                spin.setDecimals(1)
                spin.setSingleStep(0.1)
                spin.valueChanged.connect(lambda val, n=name, p=prop: self.on_spinbox_changed(n, p, val))
                grid.addWidget(spin, i, 1)
                spins[prop] = spin
            
            self.roi_spinboxes[name] = spins
            l.addLayout(grid)
            
            btn_clear_single = QPushButton("🗑️ Clear Single")
            btn_clear_single.clicked.connect(lambda checked, n=name: self.clear_single_roi(n))
            l.addWidget(btn_clear_single)
            
            left_layout.addWidget(gb)
        
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
        if hasattr(self.app, 'ocr_pipeline') and self.app.ocr_pipeline:
            self.app.ocr_pipeline.set_focus(val)

    def on_camera_index_changed(self, val):
        if self.app and hasattr(self.app, 'ocr_pipeline'):
            self.app.ocr_pipeline.camera_index = val
            if self.app.ocr_pipeline.is_running:
                self.app.ocr_pipeline.stop()
                import time
                time.sleep(0.5)
                self.app.ocr_pipeline.start()

    def on_ocr_interval_changed(self, val):
        if self.app and hasattr(self.app, 'ocr_pipeline'):
            self.app.ocr_pipeline.ocr_interval = val

    def on_roi_selected(self, rect):
        self.current_drawn_rect = rect

    def set_specific_roi(self, name):
        if not self.current_drawn_rect or not self.last_frame is not None: return
        
        # Calculate normalized coordinates
        # We need the displayed pixmap size vs the original image size
        if not self.image_label.pixmap(): return
        
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
        if not hasattr(self.app, 'ocr_pipeline') or not self.app.ocr_pipeline: return
        roi = self.app.ocr_pipeline.rois.get(name)
        if not roi or len(roi) < 5:
            roi = [0.0, 0.0, 0.0, 0.0, 0.0]
        else:
            roi = list(roi)
            
        if prop == "X": roi[0] = val / 100.0
        elif prop == "Y": roi[1] = val / 100.0
        elif prop == "W": roi[2] = val / 100.0
        elif prop == "H": roi[3] = val / 100.0
        elif prop == "Angle": roi[4] = val
        
        self.app.ocr_pipeline.rois[name] = roi
        self.app.ocr_pipeline.save_rois()

    def clear_single_roi(self, name):
        self.app.ocr_pipeline.rois[name] = None
        self.app.ocr_pipeline.save_rois()
        self.refresh_spinboxes()
        QMessageBox.information(self, "ROI Cleared", f"{name} ROI cleared.")

    def refresh_spinboxes(self):
        if not hasattr(self.app, 'ocr_pipeline') or not self.app.ocr_pipeline: return
        for name, spins in self.roi_spinboxes.items():
            roi = self.app.ocr_pipeline.rois.get(name)
            for spin in spins.values(): spin.blockSignals(True)
            if roi and len(roi) >= 4:
                spins["X"].setValue(roi[0]*100.0)
                spins["Y"].setValue(roi[1]*100.0)
                spins["W"].setValue(roi[2]*100.0)
                spins["H"].setValue(roi[3]*100.0)
                spins["Angle"].setValue(roi[4] if len(roi)>4 else 0.0)
            else:
                for spin in spins.values(): spin.setValue(0.0)
            for spin in spins.values(): spin.blockSignals(False)

    def receive_frame_threadsafe(self, frame):
        import time
        import os
        import cv2
        self.last_frame = frame.copy()
        
        frame_ts = time.time()
        self.frame_buffer.append((frame_ts, self.last_frame))
        
        # Phase 1: Intercept Calibration Burst
        if getattr(self.app, 'calibration_burst_count', 0) > 0:
            self.app.calibration_burst_count -= 1
            
            if self.app.calibration_burst_count == 0:
                meter_id = getattr(self.app, 'calibration_burst_meter', 1)
                serial_ts = getattr(self.app, 'calibration_burst_serial_ts', 0)
                screen_val = getattr(self.app, 'calibration_burst_screen_val', "0.0")
                
                sess_dir = getattr(self.app, 'calibration_session_dir', None)
                if not sess_dir:
                    sess_dir = os.path.join(os.getcwd(), f"calibration_session_{int(serial_ts)}")
                    self.app.calibration_session_dir = sess_dir
                    os.makedirs(os.path.join(sess_dir, "extracted_crops"), exist_ok=True)
                
                for f_ts, f_img in list(self.frame_buffer):
                    roi_imgs, _ = self.app.ocr_pipeline.extract_crops(f_img)
                    roi_name = f"m{meter_id}_cond"
                    roi_img = roi_imgs.get(roi_name)
                    
                    if roi_img is not None and roi_img.size > 0:
                        cnn_img = cv2.resize(roi_img, (128, 128))
                        # Filename: screenval_serialts_framets_meterid.jpg
                        filename = f"{screen_val}_{serial_ts:.3f}_{f_ts:.3f}_{meter_id}.jpg"
                        out_path = os.path.join(sess_dir, "extracted_crops", filename)
                        cv2.imwrite(out_path, cnn_img)
                        
                try:
                    self.app.btn_calibration_burst.setText("🎯 Capture Calibration Burst (Phase 1)")
                    self.app.btn_calibration_burst.setEnabled(True)
                except: pass
                
                self.app.calibration_session_dir = None

        self.image_ready.emit(frame)

    def set_image(self, frame):
        rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        q_img = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        
        pixmap = QPixmap.fromImage(q_img)
        scaled_pixmap = pixmap.scaled(self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.image_label.setPixmap(scaled_pixmap)

class MeterApp(QMainWindow):


    def __init__(self):
        super().__init__()
        self.setWindowTitle("Apera Dual-Meter Control Center (PyQt6)")
        self.resize(1400, 800)
        

        
        self.tabs = QTabWidget()

        # Instantiate buttons without placing them in the global toolbar
        self.ocr_checkbox = QPushButton("🎥 Turn Camera On")
        self.ocr_checkbox.setCheckable(True)
        self.ocr_checkbox.setStyleSheet("font-weight: bold; font-size: 14px; padding: 5px;")
        self.ocr_checkbox.toggled.connect(self.toggle_ocr_mode)
        
        self.dataset_checkbox = QCheckBox("Record Smart Dataset (Phase 2 Data Collection)")
        self.dataset_checkbox.setStyleSheet("font-weight: bold; margin-left: 10px; margin-right: 10px; color: #2980b9;")
        self.dataset_checkbox.stateChanged.connect(self.toggle_dataset_mode)
        
        self.calibration_burst_requested = False
        self.calibration_burst_count = 0
        self.btn_calibration_burst = QPushButton("🎯 Capture Calibration Burst")
        self.btn_calibration_burst.setStyleSheet("background-color: #8E44AD; color: white; font-weight: bold;")
        self.btn_calibration_burst.clicked.connect(self.request_calibration_burst)
        
        self.manual_snap_requested = False
        self.btn_manual_snap = QPushButton("📷 Manual Snap")
        self.btn_manual_snap.setStyleSheet("background-color: #2980B9; color: white; font-weight: bold;")
        self.btn_manual_snap.clicked.connect(self.request_manual_snap)
        
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Mode: Hardware Calibration (Phase 1)", "Mode: Smart Dataset Collection (Phase 2)"])
        self.mode_combo.setStyleSheet("font-weight: bold; font-size: 14px; padding: 5px; color: #E67E22;")
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        
        from ocr_pipeline import MeterOCR
        self.ocr_pipeline = MeterOCR()
        self.ocr_pipeline.add_callback(self.on_ocr_readings)

        self.ocr_feed_tab = OCRFeedTab(self)
        self.ocr_pipeline.add_frame_callback(self.ocr_feed_tab.receive_frame_threadsafe)
        
        # Populate initial values
        self.ocr_feed_tab.refresh_spinboxes()
        
        self.dataset_session_dir = None
        self.dataset_csv_path = None

        self.setCentralWidget(self.tabs)
        
        # Add Global Status Bar
        self.status_m1 = QLabel("M1: Disconnected")
        self.status_m2 = QLabel("M2: Disconnected")
        self.status_cam = QLabel("Camera: Off")
        
        # Style status labels
        self.status_m1.setStyleSheet("color: gray; font-weight: bold; margin-right: 20px;")
        self.status_m2.setStyleSheet("color: gray; font-weight: bold; margin-right: 20px;")
        self.status_cam.setStyleSheet("color: gray; font-weight: bold; margin-right: 20px;")
        
        self.statusBar().addWidget(self.status_m1)
        self.statusBar().addWidget(self.status_m2)
        self.statusBar().addWidget(self.status_cam)
        
        self.meter1 = MeterController(meter_id=1, parent_app=self)
        self.meter2 = MeterController(meter_id=2, parent_app=self)
        
        self.settings_tab = SettingsTab(self)
        
        from dataset_curation_tab import DatasetCurationTab
        self.dataset_curation_tab = DatasetCurationTab(self)
        
        self.tabs.addTab(self.settings_tab, "Settings")
        self.tabs.addTab(self.meter1, "Meter 1")
        self.tabs.addTab(self.meter2, "Meter 2")
        self.tabs.addTab(self.ocr_feed_tab, "Live OCR Feed")
        self.tabs.addTab(self.dataset_curation_tab, "Dataset Curation")
        
        self.on_mode_changed(0) # Apply initial mode
        
        try:
            if "app_geometry" in self.meter1.config:
                from PyQt6.QtCore import QByteArray
                self.restoreGeometry(QByteArray.fromHex(self.meter1.config["app_geometry"].encode()))
            
            if "app_tab" in self.meter1.config:
                self.tabs.setCurrentIndex(self.meter1.config["app_tab"])
                
            widths1 = self.meter1.config.get(f"col_widths_{self.meter1.meter_id}")
            if widths1:
                for i, w in enumerate(widths1): self.meter1.table.setColumnWidth(i, w)
            
            widths2 = self.meter1.config.get(f"col_widths_{self.meter2.meter_id}")
            if widths2:
                for i, w in enumerate(widths2): self.meter2.table.setColumnWidth(i, w)
                
            state1_main = self.meter1.config.get(f"main_splitter_{self.meter1.meter_id}")
            if state1_main: self.meter1.main_splitter.restoreState(QByteArray.fromHex(state1_main.encode()))
            state1_mid = self.meter1.config.get(f"mid_splitter_{self.meter1.meter_id}")
            if state1_mid: self.meter1.mid_splitter.restoreState(QByteArray.fromHex(state1_mid.encode()))
            
            state2_main = self.meter1.config.get(f"main_splitter_{self.meter2.meter_id}")
            if state2_main: self.meter2.main_splitter.restoreState(QByteArray.fromHex(state2_main.encode()))
            state2_mid = self.meter1.config.get(f"mid_splitter_{self.meter2.meter_id}")
            if state2_mid: self.meter2.mid_splitter.restoreState(QByteArray.fromHex(state2_mid.encode()))
        except Exception: pass

    def restart_app(self, checked=False):
        import sys
        import subprocess
        import os
        
        # If self.close() returns False, the user canceled the closing (e.g. unsaved data)
        if not self.close():
            return
            
        if getattr(sys, 'frozen', False):
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable] + sys.argv
            
        subprocess.Popen(args)
        QApplication.quit()


    def on_mode_changed(self, index):
        if index == 0:
            self.btn_calibration_burst.show()
            self.btn_manual_snap.hide()
            self.dataset_checkbox.hide()
            self.dataset_checkbox.setChecked(False)
        else:
            self.btn_calibration_burst.hide()
            self.btn_manual_snap.show()
            self.dataset_checkbox.show()

    def set_ocr_status(self, text, color):
        if hasattr(self, 'status_cam'):
            self.status_cam.setText(text)
            self.status_cam.setStyleSheet(f"color: {color}; font-weight: bold; margin-right: 20px;")

    def update_meter_status(self, meter_id, is_connected, port=""):
        status_label = self.status_m1 if meter_id == 1 else self.status_m2
        if is_connected:
            status_label.setText(f"M{meter_id}: Connected ({port})")
            status_label.setStyleSheet("color: #27ae60; font-weight: bold; margin-right: 20px;")
        else:
            status_label.setText(f"M{meter_id}: Disconnected")
            status_label.setStyleSheet("color: gray; font-weight: bold; margin-right: 20px;")

    def toggle_ocr_mode(self, state):
        import time
        if state:
            self.ocr_checkbox.setText("🎥 Turn Camera Off")
            self.set_ocr_status("OCR: Starting Camera...", "#f39c12")
            self.meter1.ocr_active = True
            self.meter2.ocr_active = True
            self.ocr_pipeline.start()
            self.ocr_start_time = time.time()
            self.set_ocr_status("OCR: Warming up (10s delay)...", "#f39c12")
        else:
            self.ocr_checkbox.setText("🎥 Turn Camera On")
            self.ocr_pipeline.stop()
            self.set_ocr_status("OCR: Off", "gray")
            self.meter1.ocr_active = False
            self.meter2.ocr_active = False

    def toggle_dataset_mode(self, state):
        import os
        import time
        if state:
            # Create session folder
            timestamp = int(time.time())
            self.dataset_session_dir = os.path.join(os.getcwd(), f"dataset_session_{timestamp}")
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
        if not getattr(self, 'dataset_session_dir', None):
            # Create session folder if it doesn't exist
            timestamp = int(time.time())
            self.dataset_session_dir = os.path.join(os.getcwd(), f"dataset_session_{timestamp}")
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
            QMessageBox.warning(self, "Camera Off", "Please turn on the camera first so we can capture burst crops.")
            return
            
        self.calibration_burst_requested = True
        self.btn_calibration_burst.setText("⏳ Waiting for Serial...")
        self.btn_calibration_burst.setEnabled(False)

    def _sanitize_ocr_value(self, ocr_val, last_serial_ms):
        try:
            if ocr_val == 0.0 or last_serial_ms == 0.0 or last_serial_ms is None:
                return ocr_val
                
            raw_str = str(ocr_val).replace('.', '')
            raw_int = int(raw_str)
            if raw_int == 0: return 0.0
            raw_str = str(raw_int)
            
            possible_ms_values = []
            for i in range(len(raw_str) + 1):
                if i == 0:
                    s = "0." + raw_str
                elif i == len(raw_str):
                    s = raw_str
                else:
                    s = raw_str[:i] + "." + raw_str[i:]
                try:
                    possible_ms_values.append(float(s))
                except: pass
                
            best_val = min(possible_ms_values, key=lambda x: abs(x - last_serial_ms))
            
            if abs(best_val - last_serial_ms) / last_serial_ms > 0.5:
                return ocr_val
                
            return best_val
        except Exception as e:
            print(f"Sanitize error: {e}")
            return ocr_val

    def on_ocr_readings(self, readings):
        import time
        import datetime
        if time.time() - getattr(self, 'ocr_start_time', 0) < 10:
            return
            
        if self.ocr_status_label.text() != "OCR: Running (1 Hz)":
            self.set_ocr_status("OCR: Running (1 Hz)", "#27ae60")
            
        now = datetime.datetime.now()
        dt_str = now.strftime("%Y-%m-%d %H:%M:%S")
        
        if len(readings) > 0 and readings[0] is not None:
            c1, _ = readings[0]
            last_ms = getattr(self.meter1, 'last_serial_cond_ms', 0.0)
            c1 = self._sanitize_ocr_value(c1, last_ms)
            t1_display = getattr(self.meter1, 'last_serial_temp', "0.0")
            row_data1 = (dt_str, "OCR", str(c1*1000), str(c1), t1_display, "0")
            if self.meter1.is_recording:
                self.meter1.signals.update_tree.emit(now, row_data1)
                
        if len(readings) > 1 and readings[1] is not None:
            c2, _ = readings[1]
            last_ms = getattr(self.meter2, 'last_serial_cond_ms', 0.0)
            c2 = self._sanitize_ocr_value(c2, last_ms)
            t2_display = getattr(self.meter2, 'last_serial_temp', "0.0")
            row_data2 = (dt_str, "OCR", str(c2*1000), str(c2), t2_display, "0")
            if self.meter2.is_recording:
                self.meter2.signals.update_tree.emit(now, row_data2)

    def get_used_ports(self):
        used = []
        if self.meter1.is_connected and self.meter1.ser: used.append(self.meter1.ser.port)
        if self.meter2.is_connected and self.meter2.ser: used.append(self.meter2.ser.port)
        return used

    def closeEvent(self, event):
        if self.meter1.unsaved_changes or self.meter2.unsaved_changes:
            reply = QMessageBox.question(self, "Unsaved Data", "You have unsaved experimental data!\nAre you sure you want to close?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
        
        self.meter1.disconnect_meter()
        self.meter2.disconnect_meter()
        
        try:
            self.meter1.config["app_geometry"] = self.saveGeometry().toHex().data().decode()
            self.meter1.config["app_tab"] = self.tabs.currentIndex()
            self.meter1.config[f"col_widths_{self.meter1.meter_id}"] = [self.meter1.table.columnWidth(i) for i in range(self.meter1.table.columnCount())]
            self.meter1.config[f"col_widths_{self.meter2.meter_id}"] = [self.meter2.table.columnWidth(i) for i in range(self.meter2.table.columnCount())]
            
            self.meter1.config[f"main_splitter_{self.meter1.meter_id}"] = self.meter1.main_splitter.saveState().toHex().data().decode()
            self.meter1.config[f"mid_splitter_{self.meter1.meter_id}"] = self.meter1.mid_splitter.saveState().toHex().data().decode()
            self.meter1.config[f"main_splitter_{self.meter2.meter_id}"] = self.meter2.main_splitter.saveState().toHex().data().decode()
            self.meter1.config[f"mid_splitter_{self.meter2.meter_id}"] = self.meter2.mid_splitter.saveState().toHex().data().decode()
            
            self.meter1.save_config()
        except Exception: pass
        
        event.accept()

if __name__ == "__main__":
    import ctypes
    import sys
    import os
    from PyQt6.QtGui import QIcon
    try:
        myappid = 'apera.conductivity.meter.gui'
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

    app = QApplication(sys.argv)
    
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Conductivity meter.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
        
    app.setStyleSheet(qdarktheme.load_stylesheet("dark"))
    
    window = MeterApp()
    window.show()
    sys.exit(app.exec())
