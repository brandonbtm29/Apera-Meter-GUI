import os
import glob
from datetime import datetime
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QFileDialog, QLineEdit, QMessageBox, QComboBox,
                             QStackedWidget, QListWidget, QListWidgetItem,
                             QTableWidget, QTableWidgetItem, QHeaderView, QSplitter)
from PyQt6.QtGui import QPixmap, QImage, QIcon
from PyQt6.QtCore import Qt, QSize

class DatasetCurationTab(QWidget):
    def __init__(self, parent_app=None):
        super().__init__()
        self.app = parent_app
        self.current_dir = None
        self.image_files = []
        self.current_index = -1
        
        main_layout = QVBoxLayout(self)
        self.stacked = QStackedWidget()
        main_layout.addWidget(self.stacked)
        
        # --- Page 0: Session List ---
        self.page_list = QWidget()
        list_layout = QVBoxLayout(self.page_list)
        
        list_top = QHBoxLayout()
        lbl_title = QLabel("Available Datasets & Calibration Sessions")
        lbl_title.setStyleSheet("font-weight: bold; font-size: 16px;")
        list_top.addWidget(lbl_title)
        list_top.addStretch()
        
        self.btn_refresh = QPushButton("🔄 Refresh List")
        self.btn_refresh.clicked.connect(self.populate_sessions)
        
        self.btn_load_manual = QPushButton("📁 Browse Custom Folder...")
        self.btn_load_manual.clicked.connect(self.load_directory)
        
        self.btn_delete_folder = QPushButton("🗑️ Delete Selected Folder")
        self.btn_delete_folder.setStyleSheet("background-color: #E74C3C; color: white; font-weight: bold;")
        self.btn_delete_folder.clicked.connect(self.delete_selected_folder)
        
        list_top.addWidget(self.btn_refresh)
        list_top.addWidget(self.btn_load_manual)
        list_top.addWidget(self.btn_delete_folder)
        list_layout.addLayout(list_top)
        
        self.session_list = QListWidget()
        self.session_list.setStyleSheet("font-size: 14px; padding: 5px;")
        self.session_list.itemDoubleClicked.connect(self.on_session_double_clicked)
        list_layout.addWidget(self.session_list)
        
        lbl_hint = QLabel("Double-click a session to open it.")
        lbl_hint.setStyleSheet("font-style: italic; color: gray;")
        list_layout.addWidget(lbl_hint)
        
        self.stacked.addWidget(self.page_list)
        
        # --- Page 1: Viewer ---
        self.page_viewer = QWidget()
        viewer_layout = QVBoxLayout(self.page_viewer)
        
        # Top controls
        top_layout = QHBoxLayout()
        self.btn_back = QPushButton("◀ Back to Sessions")
        self.btn_back.setStyleSheet("background-color: #34495E; color: white; font-weight: bold; padding: 5px;")
        self.btn_back.clicked.connect(lambda: self.stacked.setCurrentWidget(self.page_list))
        
        self.lbl_info = QLabel("No directory loaded.")
        
        self.lbl_m1_lag = QLabel("M1 Lag: N/A")
        self.lbl_m1_lag.setStyleSheet("color: #4CAF50; font-weight: bold; font-size: 14px;")
        self.lbl_m2_lag = QLabel("M2 Lag: N/A")
        self.lbl_m2_lag.setStyleSheet("color: #2196F3; font-weight: bold; font-size: 14px;")
        
        top_layout.addWidget(self.btn_back)
        top_layout.addSpacing(20)
        top_layout.addWidget(self.lbl_info)
        top_layout.addStretch()
        top_layout.addWidget(self.lbl_m1_lag)
        top_layout.addSpacing(20)
        top_layout.addWidget(self.lbl_m2_lag)
        top_layout.addSpacing(10)
        
        viewer_layout.addLayout(top_layout)
        
        # Create Splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        viewer_layout.addWidget(self.splitter, stretch=1)
        
        # Left Side: Table and Controls
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        
        # Image display
        self.table_images = QTableWidget()
        self.table_images.setColumnCount(6)
        self.table_images.setHorizontalHeaderLabels(["Date/Time", "Burst Entry #", "Meter ID", "Serial Value", "Hardware Lag (ms)", "Thumbnail"])
        self.table_images.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table_images.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table_images.verticalHeader().setVisible(False)
        self.table_images.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table_images.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table_images.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_images.setIconSize(QSize(100, 100))
        self.table_images.setStyleSheet("background-color: #1E1E1E; color: #FFF; font-size: 14px;")
        self.table_images.itemSelectionChanged.connect(self.on_image_selected)
        left_layout.addWidget(self.table_images, stretch=1)
        
        # Control Panel (Bottom)
        control_layout = QHBoxLayout()
        control_layout.addWidget(QLabel("Label (screen_val):"))
        self.edit_label = QLineEdit()
        self.edit_label.returnPressed.connect(self.save_label)
        self.btn_save_label = QPushButton("💾 Save Label")
        self.btn_save_label.clicked.connect(self.save_label)
        
        self.btn_set_sync = QPushButton("✅ Set as Sync Point (Calculate Lag)")
        self.btn_set_sync.setStyleSheet("background-color: #2ECC71; color: white; font-weight: bold;")
        self.btn_set_sync.clicked.connect(self.set_sync_point)
        
        self.btn_delete = QPushButton("🗑️ Delete Image")
        self.btn_delete.setStyleSheet("background-color: #E74C3C; color: white;")
        self.btn_delete.clicked.connect(self.delete_image)
        
        control_layout.addWidget(self.edit_label)
        control_layout.addWidget(self.btn_save_label)
        control_layout.addStretch()
        control_layout.addWidget(self.btn_set_sync)
        control_layout.addSpacing(20)
        control_layout.addWidget(self.btn_delete)
        left_layout.addLayout(control_layout)
        
        self.splitter.addWidget(left_widget)
        
        # Right Side: Lag Measurements
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        
        lbl_right_title = QLabel("Hardware Lag Measurements")
        lbl_right_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        right_layout.addWidget(lbl_right_title)
        
        # Meter 1 List
        right_layout.addWidget(QLabel("Meter 1 (ms):"))
        self.list_m1_lag = QListWidget()
        right_layout.addWidget(self.list_m1_lag, stretch=1)
        
        m1_btn_layout = QHBoxLayout()
        self.btn_del_m1 = QPushButton("Delete Entry")
        self.btn_del_m1.clicked.connect(lambda: self.delete_lag_entry(1))
        self.btn_avg_m1 = QPushButton("Average & Apply")
        self.btn_avg_m1.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold;")
        self.btn_avg_m1.clicked.connect(lambda: self.average_and_apply(1))
        m1_btn_layout.addWidget(self.btn_del_m1)
        m1_btn_layout.addWidget(self.btn_avg_m1)
        right_layout.addLayout(m1_btn_layout)
        
        right_layout.addSpacing(15)
        
        # Meter 2 List
        right_layout.addWidget(QLabel("Meter 2 (ms):"))
        self.list_m2_lag = QListWidget()
        right_layout.addWidget(self.list_m2_lag, stretch=1)
        
        m2_btn_layout = QHBoxLayout()
        self.btn_del_m2 = QPushButton("Delete Entry")
        self.btn_del_m2.clicked.connect(lambda: self.delete_lag_entry(2))
        self.btn_avg_m2 = QPushButton("Average & Apply")
        self.btn_avg_m2.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
        self.btn_avg_m2.clicked.connect(lambda: self.average_and_apply(2))
        m2_btn_layout.addWidget(self.btn_del_m2)
        m2_btn_layout.addWidget(self.btn_avg_m2)
        right_layout.addLayout(m2_btn_layout)
        
        self.splitter.addWidget(right_widget)
        self.splitter.setSizes([800, 300])  # Initial relative widths
        
        self.stacked.addWidget(self.page_viewer)
        
        self.update_ui_state()
        self.populate_sessions()

    def populate_sessions(self):
        self.session_list.clear()
        
        base_dir = os.path.join(os.getcwd(), "Sessions")
        if not os.path.exists(base_dir):
            os.makedirs(base_dir, exist_ok=True)
            
        dirs = []
        for d in os.listdir(base_dir):
            full_path = os.path.join(base_dir, d)
            if os.path.isdir(full_path):
                crop_dir = os.path.join(full_path, "extracted_crops")
                # If there are extracted crops, it's valid
                if os.path.exists(crop_dir) and len(glob.glob(os.path.join(crop_dir, "*.jpg"))) > 0:
                    dirs.append((full_path, os.path.getmtime(full_path)))
                    
        # Sort descending by modified time
        dirs.sort(key=lambda x: x[1], reverse=True)
        
        for path, mtime in dirs:
            dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            crop_dir = os.path.join(path, "extracted_crops")
            num_items = len(glob.glob(os.path.join(crop_dir, "*.jpg")))
            parent_name = os.path.basename(path)
            display_name = f"📁 {parent_name} \n   └ {num_items} images | Modified: {dt}"
            
            item = QListWidgetItem(display_name)
            item.setData(Qt.ItemDataRole.UserRole, crop_dir)
            self.session_list.addItem(item)
            
    def on_session_double_clicked(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        self.current_dir = path
        self.refresh_file_list()
        self.stacked.setCurrentWidget(self.page_viewer)

    def delete_selected_folder(self):
        selected = self.session_list.selectedItems()
        if not selected:
            QMessageBox.warning(self, "No Selection", "Please select a session folder from the list first.")
            return
            
        item = selected[0]
        crop_path = item.data(Qt.ItemDataRole.UserRole)
        session_dir = os.path.dirname(crop_path)
        
        reply = QMessageBox.question(self, "Confirm Delete", 
                                     f"Are you sure you want to completely delete this dataset/calibration folder and all its contents?\n\n{session_dir}",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
                                     
        if reply == QMessageBox.StandardButton.Yes:
            import shutil
            try:
                shutil.rmtree(session_dir)
                self.populate_sessions()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not delete folder:\n{e}")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            self.delete_image()
        else:
            super().keyPressEvent(event)
            
    def format_source(self, src):
        try:
            parts = src.split('_')
            if len(parts) >= 3 and parts[-1].isdigit():
                ts = int(parts[-1])
                dt_str = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                return f"Session: {dt_str}"
        except Exception:
            pass
        return src

    def refresh_lag_labels(self):
        if not self.app: return
        
        m1_lag = self.app.meter1.config.get("hardware_lag_ms_1", 0.0)
        self.lbl_m1_lag.setText(f"M1 Lag: {m1_lag:.1f} ms")
        self.list_m1_lag.clear()
        for item in self.app.meter1.config.get("lag_measurements_1", []):
            if isinstance(item, dict):
                src_fmt = self.format_source(item.get('source', 'Unknown'))
                self.list_m1_lag.addItem(f"{item.get('lag', 0.0):.1f} ms ({src_fmt})")
            else:
                self.list_m1_lag.addItem(f"{item:.1f} ms (Unknown)")
            
        m2_lag = self.app.meter2.config.get("hardware_lag_ms_2", 0.0)
        self.lbl_m2_lag.setText(f"M2 Lag: {m2_lag:.1f} ms")
        self.list_m2_lag.clear()
        for item in self.app.meter2.config.get("lag_measurements_2", []):
            if isinstance(item, dict):
                src_fmt = self.format_source(item.get('source', 'Unknown'))
                self.list_m2_lag.addItem(f"{item.get('lag', 0.0):.1f} ms ({src_fmt})")
            else:
                self.list_m2_lag.addItem(f"{item:.1f} ms (Unknown)")
        
    def load_directory(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Dataset Folder (Extracted Crops)")
        if dir_path:
            self.current_dir = dir_path
            self.refresh_file_list()
            self.stacked.setCurrentWidget(self.page_viewer)

    def refresh_file_list(self):
        self.table_images.setRowCount(0)
        self.image_files = []
        self.refresh_lag_labels()
        if not self.current_dir: return
        
        search_path = os.path.join(self.current_dir, "*.jpg")
        self.image_files = sorted(glob.glob(search_path))
        self.lbl_info.setText(f"Found {len(self.image_files)} images.")
        
        self.table_images.setRowCount(len(self.image_files))
        
        for row, file_path in enumerate(self.image_files):
            filename = os.path.basename(file_path)
            parts = filename.replace('.jpg', '').split('_')
            
            # Column 5: Thumbnail (Moved to end)
            icon_item = QTableWidgetItem()
            icon_item.setIcon(QIcon(file_path))
            icon_item.setData(Qt.ItemDataRole.UserRole, file_path)
            self.table_images.setItem(row, 5, icon_item)
            
            # Column 1: Burst Entry #
            entry_item = QTableWidgetItem(str(row + 1))
            entry_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table_images.setItem(row, 1, entry_item)
            
            if len(parts) >= 4:
                screen_val = parts[0]
                try:
                    serial_ts = float(parts[1])
                    frame_ts = float(parts[2])
                    meter_id = parts[3]
                    lag_ms = (frame_ts - serial_ts) * 1000.0
                    dt_str = datetime.fromtimestamp(serial_ts).strftime('%Y-%m-%d %H:%M:%S')
                except:
                    serial_ts, frame_ts, meter_id, lag_ms, dt_str = 0.0, 0.0, "?", 0.0, "Unknown"
                    
                # Column 0: Date/Time
                dt_item = QTableWidgetItem(dt_str)
                dt_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table_images.setItem(row, 0, dt_item)
                
                # Column 2: Meter ID
                m_item = QTableWidgetItem(f"Meter {meter_id}")
                m_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table_images.setItem(row, 2, m_item)
                
                # Column 3: Serial Value
                val_item = QTableWidgetItem(screen_val)
                val_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table_images.setItem(row, 3, val_item)
                
                # Column 4: Hardware Lag
                lag_item = QTableWidgetItem(f"{lag_ms:.1f} ms")
                lag_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table_images.setItem(row, 4, lag_item)
            else:
                self.table_images.setItem(row, 0, QTableWidgetItem("Unknown"))
                self.table_images.setItem(row, 2, QTableWidgetItem("Unknown"))
                self.table_images.setItem(row, 3, QTableWidgetItem("Unknown"))
                self.table_images.setItem(row, 4, QTableWidgetItem("Unknown"))
                
            self.table_images.setRowHeight(row, 100)
            
        self.update_ui_state()

    def on_image_selected(self):
        selected_items = self.table_images.selectedItems()
        if not selected_items:
            self.edit_label.clear()
            self.update_ui_state()
            return
            
        row = selected_items[0].row()
        icon_item = self.table_images.item(row, 5)
        if icon_item:
            file_path = icon_item.data(Qt.ItemDataRole.UserRole)
            filename = os.path.basename(file_path)
            parts = filename.replace('.jpg', '').split('_')
            if len(parts) >= 1:
                self.edit_label.setText(parts[0])
            
        self.update_ui_state()

    def save_label(self):
        selected_items = self.table_images.selectedItems()
        if not selected_items: return
        
        row = selected_items[0].row()
        icon_item = self.table_images.item(row, 5)
        val_item = self.table_images.item(row, 3)
        if not icon_item: return
        
        new_val = self.edit_label.text().strip()
        if not new_val: return
        
        old_path = icon_item.data(Qt.ItemDataRole.UserRole)
        filename = os.path.basename(old_path)
        parts = filename.split('_')
        
        if len(parts) >= 2:
            parts[0] = new_val
            new_filename = "_".join(parts)
            new_path = os.path.join(self.current_dir, new_filename)
            
            try:
                os.rename(old_path, new_path)
                icon_item.setData(Qt.ItemDataRole.UserRole, new_path)
                icon_item.setIcon(QIcon(new_path))
                if val_item:
                    val_item.setText(new_val)
                if old_path in self.image_files:
                    idx = self.image_files.index(old_path)
                    self.image_files[idx] = new_path
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not rename file: {e}")

    def delete_image(self):
        selected_items = self.table_images.selectedItems()
        if not selected_items: return
        
        reply = QMessageBox.question(self, "Confirm Delete", "Delete this image permanently?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            row = selected_items[0].row()
            icon_item = self.table_images.item(row, 5)
            if not icon_item: return
            file_path = icon_item.data(Qt.ItemDataRole.UserRole)
            try:
                os.remove(file_path)
                if file_path in self.image_files:
                    self.image_files.remove(file_path)
                
                self.table_images.removeRow(row)
                self.update_ui_state()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not delete file: {e}")

    def set_sync_point(self):
        selected_items = self.table_images.selectedItems()
        if not selected_items: return
        
        # Get unique selected rows
        selected_rows = sorted(list(set([item.row() for item in selected_items])))
        
        frame_ts_list = []
        base_serial_ts = None
        base_meter_id = None
        
        for row in selected_rows:
            icon_item = self.table_images.item(row, 5)
            if not icon_item: continue
            file_path = icon_item.data(Qt.ItemDataRole.UserRole)
            filename = os.path.basename(file_path)
            parts = filename.replace('.jpg', '').split('_')
            
            if len(parts) >= 4:
                try:
                    serial_ts = float(parts[1])
                    frame_ts = float(parts[2])
                    meter_id = parts[3]
                    
                    if base_serial_ts is None:
                        base_serial_ts = serial_ts
                        base_meter_id = meter_id
                    elif base_serial_ts != serial_ts or base_meter_id != meter_id:
                        QMessageBox.warning(self, "Mismatch", "All selected rows must belong to the same serial measurement (same serial_ts and meter_id).")
                        return
                        
                    frame_ts_list.append(frame_ts)
                except ValueError:
                    QMessageBox.warning(self, "Error", "Filename timestamp format invalid.")
                    return
            else:
                QMessageBox.warning(self, "Error", "Filename format invalid for calibration.")
                return
                
        if not frame_ts_list:
            return
            
        frame_ts_list.sort()
        n = len(frame_ts_list)
        if n % 2 != 0:
            median_frame_ts = frame_ts_list[n // 2]
        else:
            median_frame_ts = (frame_ts_list[(n // 2) - 1] + frame_ts_list[n // 2]) / 2.0
            
        lag_ms = (median_frame_ts - base_serial_ts) * 1000.0
        
        reply = QMessageBox.question(
            self, "Confirm Lag Calibration", 
            f"Calculate median lag from {n} selected frame(s).\nAdd {lag_ms:.1f} ms to Meter {base_meter_id}'s hardware lag measurements?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if self.app:
                meter = self.app.meter1 if str(base_meter_id) == "1" else self.app.meter2
                measurements = meter.config.setdefault(f"lag_measurements_{base_meter_id}", [])
                
                # Try to extract the folder name as the source
                source_name = "Unknown"
                if self.current_dir:
                    source_name = os.path.basename(os.path.dirname(self.current_dir))
                    
                measurements.append({"lag": lag_ms, "source": source_name})
                meter.save_config()
                self.refresh_lag_labels()
                QMessageBox.information(self, "Success", f"Measurement added to Meter {base_meter_id}.")

    def update_ui_state(self):
        has_selection = len(self.table_images.selectedItems()) > 0
        self.btn_save_label.setEnabled(has_selection)
        self.btn_delete.setEnabled(has_selection)
        self.btn_set_sync.setEnabled(has_selection)
        self.edit_label.setEnabled(has_selection)

    def delete_lag_entry(self, meter_id):
        lst = self.list_m1_lag if meter_id == 1 else self.list_m2_lag
        meter = self.app.meter1 if meter_id == 1 else self.app.meter2
        row = lst.currentRow()
        if row < 0: return
        
        measurements = meter.config.get(f"lag_measurements_{meter_id}", [])
        if 0 <= row < len(measurements):
            measurements.pop(row)
            meter.save_config()
            self.refresh_lag_labels()

    def average_and_apply(self, meter_id):
        meter = self.app.meter1 if meter_id == 1 else self.app.meter2
        measurements = meter.config.get(f"lag_measurements_{meter_id}", [])
        if not measurements:
            QMessageBox.warning(self, "Error", "No measurements to average.")
            return
            
        values = []
        for m in measurements:
            if isinstance(m, dict):
                values.append(m.get('lag', 0.0))
            else:
                values.append(m)
                
        avg_lag = sum(values) / len(values)
        
        reply = QMessageBox.question(self, "Confirm Average",
                                     f"Average is {avg_lag:.1f} ms.\nApply this to Meter {meter_id}?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            meter.config[f"hardware_lag_ms_{meter_id}"] = avg_lag
            meter.spin_lag.setValue(avg_lag)
            meter.save_config()
            self.refresh_lag_labels()
            QMessageBox.information(self, "Success", f"Applied {avg_lag:.1f} ms to Meter {meter_id}.")
