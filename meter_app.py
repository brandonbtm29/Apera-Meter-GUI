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
    QTableWidgetItem, QHeaderView, QMessageBox, QFileDialog,
    QFormLayout, QFrame, QDialog, QGroupBox, QGridLayout
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QFont, QColor

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

class FilenameBuilderDialog(QDialog):
    def __init__(self, parent=None, initial_template="", tokens_data=None):
        super().__init__(parent)
        self.setWindowTitle("Filename Builder & Custom Tokens")
        self.setMinimumWidth(600)
        
        self.tokens_data = tokens_data or []
        
        main_layout = QVBoxLayout(self)
        
        gb_tokens = QGroupBox("Metadata Tokens")
        self.layout_tokens = QVBoxLayout(gb_tokens)
        self.token_rows = []
        self.grid_buttons = QGridLayout()
        
        btn_add_token = QPushButton("+ Add Custom Token")
        btn_add_token.clicked.connect(lambda: self.add_token_row("", ""))
        self.layout_tokens.addWidget(btn_add_token)
        
        for t in self.tokens_data:
            self.add_token_row(t.get("name", ""), t.get("value", ""))
            
        main_layout.addWidget(gb_tokens)
        
        gb_temp = QGroupBox("Filename Template")
        l_temp = QVBoxLayout(gb_temp)
        l_temp.addWidget(QLabel("Click a token below to insert it at the cursor position:"))
        self.ent_template = QLineEdit(initial_template)
        l_temp.addWidget(self.ent_template)
        
        l_temp.addLayout(self.grid_buttons)
        self.refresh_token_buttons()
        main_layout.addWidget(gb_temp)
        
        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("OK")
        btn_cancel = QPushButton("Cancel")
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        main_layout.addLayout(btn_layout)
        
        btn_ok.clicked.connect(self.accept)
        btn_cancel.clicked.connect(self.reject)
        
    def add_token_row(self, name, value):
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        
        ent_name = QLineEdit(name)
        ent_name.setPlaceholderText("Token Name")
        ent_name.textChanged.connect(self.refresh_token_buttons)
        ent_value = QLineEdit(value)
        ent_value.setPlaceholderText("Value")
        
        btn_remove = QPushButton("X")
        btn_remove.setFixedWidth(30)
        btn_remove.setStyleSheet("background-color: #e74c3c; color: white;")
        
        row_layout.addWidget(ent_name)
        row_layout.addWidget(ent_value)
        row_layout.addWidget(btn_remove)
        
        idx = self.layout_tokens.count() - 1
        self.layout_tokens.insertWidget(idx, row_widget)
        
        row_data = (row_widget, ent_name, ent_value)
        self.token_rows.append(row_data)
        
        btn_remove.clicked.connect(lambda: self.remove_token_row(row_data))
        self.refresh_token_buttons()
        
    def remove_token_row(self, row_data):
        row_widget, ent_name, ent_value = row_data
        if row_data in self.token_rows:
            self.token_rows.remove(row_data)
        self.layout_tokens.removeWidget(row_widget)
        row_widget.deleteLater()
        self.refresh_token_buttons()
        
    def refresh_token_buttons(self):
        while self.grid_buttons.count():
            item = self.grid_buttons.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        tokens = ["Meter ID", "Date", "Time"]
        for _, ent_name, _ in self.token_rows:
            n = ent_name.text().strip()
            if n and n not in tokens: tokens.append(n)
            
        row = 0; col = 0
        for tok in tokens:
            btn = QPushButton(f"<{tok}>")
            btn.clicked.connect(lambda checked, t=tok: self.ent_template.insert(f"<{t}>"))
            self.grid_buttons.addWidget(btn, row, col)
            col += 1
            if col > 3:
                col = 0
                row += 1

    def get_token_data(self):
        data = []
        for _, ent_name, ent_value in self.token_rows:
            data.append({
                "name": ent_name.text().strip(),
                "value": ent_value.text().strip()
            })
        return data

class MeterController(QWidget):
    def __init__(self, meter_id=1, parent_app=None):
        super().__init__()
        self.meter_id = meter_id
        self.app_controller = parent_app

        self.ser = None
        self.is_connected = False
        self.unsaved_changes = False
        self.is_recording = False
        self.received_timestamps = set()
        
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
        
        docs = self.config.get(f"default_save_location_{meter_id}", "")
        if not docs or not os.path.isdir(docs):
            docs = os.path.join(os.environ.get('USERPROFILE', os.path.expanduser('~')), 'Documents')
        self.emergency_backup_path = os.path.join(docs, f"Apera_Meter{meter_id}_Backup.csv")
        self.columns = ("DateTime", "Duration (min)", "Mode", "Cond (uS/cm)", "Cond (mS/cm)", "Temp (°C)", "Raw Cond_ADC")
        
        self.init_ui()
        
        has_backup = False
        if os.path.exists(self.emergency_backup_path):
            with open(self.emergency_backup_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                if len(lines) > 1:
                    has_backup = True
                    
        if has_backup:
            self.recover_session()
        else:
            self.init_backup_file()
        
        self.refresh_ports()
        target_com = self.config.get(f"default_com_port_{self.meter_id}", "Auto-Detect Apera Meter")
        idx = self.port_combo.findText(target_com)
        if idx >= 0:
            self.port_combo.setCurrentIndex(idx)
        
        # Connect on startup if matching
        QTimer.singleShot(500, self._auto_connect_check)

    def _auto_connect_check(self):
        if self.port_combo.currentText() == self.config.get(f"default_com_port_{self.meter_id}"):
            self.connect_meter()

    def init_ui(self):
        main_layout = QVBoxLayout(self)
        
        self.top_btn_layout = QHBoxLayout()
        main_layout.addLayout(self.top_btn_layout)
        self.btn_sync = QPushButton("⏱️ Sync Meter Time")
        self.btn_sync.setEnabled(False)
        self.btn_sync.clicked.connect(self.sync_time)
        self.top_btn_layout.addWidget(self.btn_sync)
        
        self.btn_download = QPushButton("📥 Download Raw Memory")
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self.download_memory)
        self.top_btn_layout.addWidget(self.btn_download)
        
        self.btn_export = QPushButton("💾 Save Log to Excel")
        self.btn_export.setStyleSheet("background-color: #27ae60; color: white;")
        self.btn_export.clicked.connect(lambda: self.save_excel(auto=False))
        self.top_btn_layout.addWidget(self.btn_export)
        
        self.btn_save_graph = QPushButton("🖼️ Save Graph PNG")
        self.btn_save_graph.clicked.connect(self.save_graph)
        self.top_btn_layout.addWidget(self.btn_save_graph)
        
        self.btn_recover = QPushButton("♻️ Recover Session")
        self.btn_recover.clicked.connect(self.recover_session)
        self.top_btn_layout.addWidget(self.btn_recover)
        
        self.btn_clear = QPushButton("🗑️ Clear Data")
        self.btn_clear.setStyleSheet("background-color: #c0392b; color: white;")
        self.btn_clear.clicked.connect(self.clear_log)
        self.top_btn_layout.addWidget(self.btn_clear)
        
        self.top_btn_layout.addStretch()

        
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(self.main_splitter)
        
        # Left Panel (Settings)
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(10, 10, 10, 10)
        
        lbl_title = QLabel(f"Meter {self.meter_id}")
        lbl_title.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        left_layout.addWidget(lbl_title)
        
        lbl_rec = QLabel("Recording Settings")
        lbl_rec.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        left_layout.addWidget(lbl_rec)
        
        self.btn_record = QPushButton("▶️ Start Recording")
        self.btn_record.setStyleSheet("background-color: #2ecc71; color: white;")
        self.btn_record.clicked.connect(self.toggle_record)
        left_layout.addWidget(self.btn_record)
        
        self.auto_dump_chk = QCheckBox("Auto-Dump Memory (hrs):")
        self.auto_dump_chk.setChecked(self.config.get(f"auto_dump_enabled_{self.meter_id}", False))
        self.auto_dump_chk.stateChanged.connect(self._save_auto_dump_config)
        left_layout.addWidget(self.auto_dump_chk)
        self.auto_dump_entry = QLineEdit(str(self.config.get(f"default_auto_dump_hrs_{self.meter_id}", 2.0)))
        self.auto_dump_entry.textChanged.connect(self._save_auto_dump_config)
        left_layout.addWidget(self.auto_dump_entry)
        

        
        line1 = QFrame()
        line1.setFrameShape(QFrame.Shape.HLine)
        left_layout.addWidget(line1)
        
        lbl_axis = QLabel("Axis Limits")
        lbl_axis.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        left_layout.addWidget(lbl_axis)
        
        self.auto_time_chk = QCheckBox("Auto Time")
        self.auto_time_chk.setChecked(self.config.get(f"auto_time_{self.meter_id}", True))
        self.auto_time_chk.stateChanged.connect(self._auto_chk_changed)
        left_layout.addWidget(self.auto_time_chk)
        
        self.auto_cond_chk = QCheckBox("Auto Cond")
        self.auto_cond_chk.setChecked(self.config.get(f"auto_cond_{self.meter_id}", True))
        self.auto_cond_chk.stateChanged.connect(self._auto_chk_changed)
        left_layout.addWidget(self.auto_cond_chk)
        
        self.auto_temp_chk = QCheckBox("Auto Temp")
        self.auto_temp_chk.setChecked(self.config.get(f"auto_temp_{self.meter_id}", True))
        self.auto_temp_chk.stateChanged.connect(self._auto_chk_changed)
        left_layout.addWidget(self.auto_temp_chk)
        
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
        
        left_layout.addLayout(form_layout)
        
        self.btn_apply_lims = QPushButton("📊 Apply Limits")
        self.btn_apply_lims.clicked.connect(self.apply_limits)
        left_layout.addWidget(self.btn_apply_lims)
        
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        left_layout.addWidget(line2)
        
        self.btn_settings = QPushButton("⚙️ Advanced Settings")
        self.btn_settings.clicked.connect(self.open_settings)
        left_layout.addWidget(self.btn_settings)
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

        # --- Auto-Save & Filename ---
        gb_save = QGroupBox("Auto-Save & Filename")
        l_save = QGridLayout()
        gb_save.setLayout(l_save)
        
        l_save.addWidget(QLabel("Filename Template:"), 0, 0)
        self.ent_save_template = QLineEdit(self.config.get(f"save_template_{self.meter_id}", ""))
        l_save.addWidget(self.ent_save_template, 0, 1)
        btn_edit_template = QPushButton("Edit")
        btn_edit_template.clicked.connect(self.open_filename_builder)
        l_save.addWidget(btn_edit_template, 0, 2)
        
        l_save.addWidget(QLabel("Auto-Save Mode:"), 1, 0)
        self.combo_autosave = QComboBox()
        self.combo_autosave.addItems(["Off", "On Auto-Stop", "Every X Minutes"])
        self.combo_autosave.setCurrentText(self.config.get(f"autosave_mode_{self.meter_id}", "Off"))
        l_save.addWidget(self.combo_autosave, 1, 1, 1, 2)
        
        l_save.addWidget(QLabel("Auto-Save Interval (min):"), 2, 0)
        self.ent_autosave_min = QLineEdit(self.config.get(f"autosave_min_{self.meter_id}", "5.0"))
        l_save.addWidget(self.ent_autosave_min, 2, 1, 1, 2)
        
        self.combo_autosave.currentIndexChanged.connect(self.toggle_autosave_interval)
        self.combo_autosave.currentIndexChanged.connect(self._save_autosave_config)
        self.ent_autosave_min.textChanged.connect(self._save_autosave_config)
        self.ent_save_template.textChanged.connect(self._save_autosave_config)
        self.toggle_autosave_interval()
        
        right_layout.addWidget(gb_save)
        
        self.lbl_status = QLabel("Status: Disconnected")
        self.lbl_status.setStyleSheet("color: gray;")
        right_layout.addWidget(self.lbl_status)
        
        self.sn_label = QLabel("SN: Unknown")
        self.model_label = QLabel("Model: Unknown")
        right_layout.addWidget(self.sn_label)
        right_layout.addWidget(self.model_label)
        
        line3 = QFrame()
        line3.setFrameShape(QFrame.Shape.HLine)
        right_layout.addWidget(line3)
        
        lbl_notes = QLabel("Experiment Notes")
        lbl_notes.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        right_layout.addWidget(lbl_notes)
        self.txt_notes = QTextEdit()
        self.txt_notes.setText(self.config.get(f"meter_notes_{self.meter_id}", ""))
        self.txt_notes.textChanged.connect(self.save_notes)
        right_layout.addWidget(self.txt_notes)
        

        
        right_layout.addStretch()
        
        right_scroll = QScrollArea()
        right_scroll.setWidget(right_widget)
        right_scroll.setWidgetResizable(True)
        
        left_scroll.setMinimumWidth(250)
        right_scroll.setMinimumWidth(250)
        
        # Add to main splitter
        self.main_splitter.addWidget(left_scroll)
        self.main_splitter.addWidget(mid_splitter)
        self.main_splitter.addWidget(right_scroll)
        self.main_splitter.setStretchFactor(0, 1)
        self.main_splitter.setStretchFactor(1, 3)
        self.main_splitter.setStretchFactor(2, 1)
        
        self.update_button_styles(self.config.get("button_style", "Icon + Text"))

    def init_backup_file(self):
        try:
            if not os.path.exists(self.emergency_backup_path):
                with open(self.emergency_backup_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(self.columns)
        except Exception: pass

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

    def refresh_ports(self):
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
        self.save_config()

    def toggle_autosave_interval(self):
        self.ent_autosave_min.setEnabled(self.combo_autosave.currentText() == "Every X Minutes")

    def open_filename_builder(self):
        default_tokens = [
            {"name": "Column Name", "value": ""},
            {"name": "Material", "value": ""},
            {"name": "Flow rate", "value": ""},
            {"name": "Bed length", "value": ""},
            {"name": "Run #", "value": ""}
        ]
        tokens_data = self.config.get(f"meter_tokens_{self.meter_id}", default_tokens)
        dialog = FilenameBuilderDialog(self, self.ent_save_template.text(), tokens_data)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.ent_save_template.setText(dialog.ent_template.text())
            self.config[f"meter_tokens_{self.meter_id}"] = dialog.get_token_data()
            self.save_config()

    def get_resolved_string(self, template_str):
        if not template_str: return ""
        s = template_str
        s = s.replace("<Meter ID>", str(self.meter_id))
        s = s.replace("<Date>", datetime.datetime.now().strftime("%Y-%m-%d"))
        s = s.replace("<Time>", datetime.datetime.now().strftime("%H%M%S"))
        
        custom_tokens = self.config.get(f"meter_tokens_{self.meter_id}", [])
        for t in custom_tokens:
            name = t.get("name", "")
            value = t.get("value", "")
            if name:
                s = s.replace(f"<{name}>", value)
                
        return s

    def toggle_record(self):
        if not self.is_recording:
            self.is_recording = True
            self.session_start_time = None
            self.last_autosave_min = 0.0
            self.btn_record.setText("⏸ Pause Recording")
            self.btn_record.setStyleSheet("background-color: #e74c3c; color: white;")
            self.lbl_status.setText("Status: Recording...")
            self.unsaved_changes = True
        else:
            self.is_recording = False
            self.btn_record.setText("⏺ Start Recording")
            self.btn_record.setStyleSheet("background-color: #2ecc71; color: white;")
            self.lbl_status.setText("Status: Paused")
            
            if self.combo_autosave.currentText() == "On Auto-Stop":
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

    def apply_limits(self):
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

    def save_graph(self):
        filepath, _ = QFileDialog.getSaveFileName(self, "Save Graph", "", "PNG (*.png)")
        if filepath:
            try:
                self.fig.savefig(filepath, dpi=300, bbox_inches='tight')
                QMessageBox.information(self, "Success", "Graph saved!")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def open_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Settings - Meter {self.meter_id}")
        dlg.resize(400, 200)
        layout = QVBoxLayout(dlg)
        
        layout.addWidget(QLabel("Default Save Directory:"))
        lbl_dir = QLabel(self.config.get(f"default_save_location_{self.meter_id}", "Not Set"))
        layout.addWidget(lbl_dir)
        
        def choose_dir():
            d = QFileDialog.getExistingDirectory(self, "Select Directory")
            if d:
                lbl_dir.setText(d)
                self.config[f"default_save_location_{self.meter_id}"] = d
                self.save_config()
                
        btn_dir = QPushButton("Browse...")
        btn_dir.clicked.connect(choose_dir)
        layout.addWidget(btn_dir)
        
        ka_chk = QCheckBox("Enable Keep-Alive (Ping every 2s)")
        ka_chk.setChecked(self.config.get(f"keep_alive_{self.meter_id}", True))
        
        auto_sync_chk = QCheckBox("Auto-Sync Time on Connect")
        auto_sync_chk.setChecked(self.config.get(f"auto_sync_{self.meter_id}", False))
        
        def save_and_close():
            self.config[f"keep_alive_{self.meter_id}"] = ka_chk.isChecked()
            self.config[f"auto_sync_{self.meter_id}"] = auto_sync_chk.isChecked()
            self.save_config()
            dlg.accept()
            
        layout.addWidget(ka_chk)
        layout.addWidget(auto_sync_chk)
        
        def wipe_backup():
            reply = QMessageBox.question(dlg, "Confirm", "Are you sure you want to permanently delete the backup logs?",
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.init_backup_file()
                QMessageBox.information(dlg, "Success", "Backup logs wiped successfully.")
        
        btn_wipe = QPushButton("🗑️ Clear Backup Logs")
        btn_wipe.setStyleSheet("background-color: #e74c3c; color: white;")
        btn_wipe.clicked.connect(wipe_backup)
        layout.addWidget(btn_wipe)
        
        btn_close = QPushButton("Save & Close")
        btn_close.clicked.connect(save_and_close)
        layout.addWidget(btn_close)
        
        dlg.exec()


        
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
        self.received_timestamps.clear()
        self.plot_dates.clear()
        self.plot_conds.clear()
        self.plot_temps.clear()
        self.session_start_time = None
        self.last_auto_dump_time = time.time()
        
        if init_file:
            try:
                with open(self.emergency_backup_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(self.columns)
            except: pass
        self.plot_data()

    def recover_session(self):
        if not os.path.exists(self.emergency_backup_path):
            QMessageBox.information(self, "Recover", "No backup file found to recover.")
            return
            
        try:
            with open(self.emergency_backup_path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                rows = list(reader)
                
            self.clear_log(init_file=False)
            
            self.table.setSortingEnabled(False)
            for row in rows:
                if len(row) >= 6:
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
            QMessageBox.information(self, "Recover", f"Recovered {len(rows)} points.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Recovery failed: {e}")

    def toggle_connection(self):
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
            self.lbl_status.setText("Status: Scanning COM ports...")
            self.btn_connect.setEnabled(False)
            
            self.scan_id = getattr(self, 'scan_id', 0) + 1
            current_id = self.scan_id
            
            def timeout_scan():
                if getattr(self, 'scan_id', None) == current_id:
                    self.scan_id = None
                    self.btn_connect.setEnabled(True)
                    self.lbl_status.setText("Status: Auto-Detect Timeout")
                    self.lbl_status.setStyleSheet("color: #E74C3C;")
                    
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
        self.scan_id = None
        self.btn_connect.setEnabled(True)
        if not port or port == "No Ports Found":
            self.lbl_status.setText("Status: Auto-Detect Failed")
            self.lbl_status.setStyleSheet("color: #E74C3C;")
            return
            
        try:
            idx = self.port_combo.findText(port)
            if idx >= 0: self.port_combo.setCurrentIndex(idx)
            
            self.ser = serial.Serial(port, 115200, timeout=0.1)
            self.ser.setDTR(False)
            self.ser.setRTS(False)
            self.is_connected = True
            
            self.btn_connect.setText("❌ Disconnect")
            self.btn_download.setEnabled(True)
            self.btn_sync.setEnabled(True)
            self.lbl_status.setText(f"Status: Connected to {port}")
            self.lbl_status.setStyleSheet("color: #2ECC71;")
            
            self.config[f"default_com_port_{self.meter_id}"] = port
            self.save_config()
            
            self.ser.write(b"&OK#\r\n")
            QTimer.singleShot(500, self.sync_time)
            
            threading.Thread(target=self.read_serial_loop, daemon=True).start()
            threading.Thread(target=self.keep_alive_loop, daemon=True).start()
            threading.Thread(target=self.auto_dump_loop, daemon=True).start()
        except Exception as e:
            self.lbl_status.setText(f"Failed to connect: {e}")
            self.lbl_status.setStyleSheet("color: #E74C3C;")

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
        self.btn_download.setEnabled(False)
        self.btn_sync.setEnabled(False)
        self.lbl_status.setText("Status: Disconnected")
        self.lbl_status.setStyleSheet("color: gray;")
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
                        self.process_packet(packet)
            except Exception:
                self.signals.update_status.emit("Status: Disconnected (Error)", "gray")
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
                mode_str = "Cond" if mode == 2 else f"Mode({mode})"
                
                val_float = DP
                if dot == 1: val_float = DP / 10.0
                elif dot == 2: val_float = DP / 100.0
                elif dot == 3: val_float = DP / 1000.0
                
                cond_us, cond_ms = "", ""
                if mode == 2:
                    u_val = b + 10
                    if u_val == 10: 
                        cond_ms = f"{val_float:.3f}"
                        cond_us = f"{val_float * 1000:.1f}"
                    elif u_val == 11: 
                        cond_ms = f"{val_float / 1000:.3f}"
                        cond_us = f"{val_float:.1f}"
                else:
                    cond_us = str(val_float)
                
                dt_str = f"20{yy:02d}-{mt:02d}-{dd:02d} {hh:02d}:{mm:02d}:{ss:02d}"
                dt_obj = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                
                if dt_str in self.received_timestamps: return
                self.received_timestamps.add(dt_str)
                
                row_data = (dt_str, mode_str, cond_us, cond_ms, f"{temp_c:.1f}", str(w4))
                
                if self.is_recording:
                    self.signals.update_tree.emit(dt_obj, row_data)
                    
            except Exception: pass
                
        elif packet.startswith(b'<SH') and p_len >= 12:
            payload = packet[3:11]
            hex_str = "-".join([f"{b:02X}" for b in payload])
            self.signals.update_device_info.emit("SN", hex_str)
        elif packet.startswith(b'<P2') and p_len >= 12:
            payload = packet[3:11]
            hex_str = "-".join([f"{b:02X}" for b in payload])
            self.signals.update_device_info.emit("Model", hex_str)

    def log_point_ui(self, dt, row_data):
        self.plot_dates.append(dt)
        self.plot_conds.append(float(row_data[2]) if row_data[2] else 0.0)
        self.plot_temps.append(float(row_data[4]) if row_data[4] else 0.0)
        
        self.table.setSortingEnabled(False)
        row_idx = self.table.rowCount()
        self.table.insertRow(row_idx)
        for col, text in enumerate(row_data):
            self.table.setItem(row_idx, col, NumericTableWidgetItem(str(text)))
        self.table.setSortingEnabled(True)
        self.table.scrollToBottom()
        self.unsaved_changes = True
        
        try:
            with open(self.emergency_backup_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(row_data)
        except: pass
        
        self.schedule_plot()

    def set_status(self, text, color):
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"color: {color};")
        if text.endswith("(Error)"):
            self.btn_connect.setText("🔌 Connect")
            self.btn_download.setEnabled(False)
            self.btn_sync.setEnabled(False)

    def set_device_info(self, typ, val):
        if typ == "SN": self.sn_label.setText(f"SN: {val}")
        elif typ == "Model": self.model_label.setText(f"Model: {val}")

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

    def sync_time(self):
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

    def download_memory(self):
        if self.is_connected and self.ser:
            self.lbl_status.setText("Status: Downloading Memory...")
            self.ser.write(b"&WA#\r\n")

    def save_excel(self, auto=False):
        if self.table.rowCount() == 0:
            if not auto: QMessageBox.information(self, "Export", "No data to export.")
            return
            
        filename = self.get_resolved_string(self.ent_save_template.text())
        if not filename:
            filename = f"Meter{self.meter_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        if not filename.endswith(".xlsx"): filename += ".xlsx"
        filename = filename.replace(' ', '_')
        
        if auto:
            docs = self.config.get(f"default_save_location_{self.meter_id}", "")
            if not docs or not os.path.isdir(docs): docs = os.path.expanduser("~/Documents")
            filepath = os.path.join(docs, filename)
        else:
            docs = self.config.get(f"default_save_location_{self.meter_id}", "")
            if not docs or not os.path.isdir(docs): docs = os.path.expanduser("~/Documents")
            initial_file = os.path.join(docs, filename)
            filepath, _ = QFileDialog.getSaveFileName(self, "Export Excel", initial_file, "Excel files (*.xlsx);;All files (*.*)")
            if not filepath: return
        
        try:
            # Extract data from table
            data = []
            for row in range(self.table.rowCount()):
                row_data = []
                for col in range(self.table.columnCount()):
                    item = self.table.item(row, col)
                    row_data.append(item.text() if item else "")
                data.append(row_data)
            
            df = pd.DataFrame(data, columns=self.columns)
            
            # Write to excel with documentation sheet using openpyxl directly
            wb = openpyxl.Workbook()
            doc_ws = wb.active
            doc_ws.title = "Documentation"
            doc_ws["A1"] = "Experiment Documentation"
            doc_ws["A1"].font = Font(size=16, bold=True)
            doc_ws["A3"] = "Export Date:"
            doc_ws["B3"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            doc_ws["A4"] = "Meter SN:"
            doc_ws["B4"] = self.sn_label.text().replace("SN: ", "")
            doc_ws["A5"] = "Experiment Notes:"
            doc_ws["A5"].font = Font(bold=True)
            doc_ws["A6"] = self.txt_notes.toPlainText()
            doc_ws["A6"].alignment = Alignment(wrap_text=True, vertical="top")
            doc_ws.row_dimensions[6].height = 100
            doc_ws.column_dimensions["A"].width = 25
            doc_ws.column_dimensions["B"].width = 35
            
            wb.save(filepath)
            self.lbl_status.setText(f"Status: Exported to Excel!")
            self.unsaved_changes = False
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Export failed! {e}")


class SettingsTab(QWidget):
    def __init__(self, parent_app):
        super().__init__()
        self.parent_app = parent_app
        layout = QVBoxLayout(self)
        
        lbl_style = QLabel("Action Button Style:")
        lbl_style.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        layout.addWidget(lbl_style)
        
        self.combo_style = QComboBox()
        self.combo_style.addItems(["Icon + Text", "Icon Only"])
        current_style = self.parent_app.meter1.config.get("button_style", "Icon + Text")
        self.combo_style.setCurrentText(current_style)
        self.combo_style.currentIndexChanged.connect(self.update_style)
        layout.addWidget(self.combo_style)
        
        layout.addSpacing(20)
        
        lbl_restart = QLabel("Application")
        lbl_restart.setFont(QFont("Arial", 12, QFont.Weight.Bold))
        layout.addWidget(lbl_restart)
        
        btn_restart = QPushButton("🔁 Restart Application")
        btn_restart.setStyleSheet("background-color: #8e44ad; color: white; padding: 10px; font-weight: bold;")
        btn_restart.clicked.connect(self.parent_app.restart_app)
        layout.addWidget(btn_restart)
        
        layout.addStretch()

    def update_style(self):
        style = self.combo_style.currentText()
        self.parent_app.meter1.config["button_style"] = style
        self.parent_app.meter1.save_config()
        self.parent_app.meter1.update_button_styles(style)
        self.parent_app.meter2.update_button_styles(style)

class MeterApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Apera Dual-Meter Control Center (PyQt6)")
        self.resize(1400, 800)
        
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        
        self.meter1 = MeterController(meter_id=1, parent_app=self)
        self.meter2 = MeterController(meter_id=2, parent_app=self)
        
        self.tabs.addTab(self.meter1, "Meter 1")
        self.tabs.addTab(self.meter2, "Meter 2")
        
        self.settings_tab = SettingsTab(self)
        self.tabs.addTab(self.settings_tab, "Settings")
        
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

    def restart_app(self):
        import sys
        import subprocess
        self.close()
        subprocess.Popen([sys.executable] + sys.argv, close_fds=True)
        QApplication.quit()

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
    app = QApplication(sys.argv)
    app.setStyleSheet(qdarktheme.load_stylesheet("dark"))
    
    window = MeterApp()
    window.show()
    sys.exit(app.exec())
