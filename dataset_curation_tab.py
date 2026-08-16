import os
import glob
from datetime import datetime
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QFileDialog, QLineEdit, QMessageBox, QComboBox,
                             QStackedWidget, QListWidget, QListWidgetItem,
                             QTableWidget, QTableWidgetItem, QHeaderView)
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
        
        top_layout.addWidget(self.btn_back)
        top_layout.addSpacing(20)
        top_layout.addWidget(self.lbl_info)
        top_layout.addStretch()
        
        viewer_layout.addLayout(top_layout)
        
        # Image display
        self.table_images = QTableWidget()
        self.table_images.setColumnCount(6)
        self.table_images.setHorizontalHeaderLabels(["Date/Time", "Burst Entry #", "Meter ID", "Serial Value", "Hardware Lag (ms)", "Thumbnail"])
        self.table_images.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table_images.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table_images.verticalHeader().setVisible(False)
        self.table_images.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table_images.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table_images.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_images.setIconSize(QSize(250, 250))
        self.table_images.setStyleSheet("background-color: #1E1E1E; color: #FFF; font-size: 14px;")
        self.table_images.itemSelectionChanged.connect(self.on_image_selected)
        viewer_layout.addWidget(self.table_images, stretch=1)
        
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
        viewer_layout.addLayout(control_layout)
        
        self.stacked.addWidget(self.page_viewer)
        
        self.update_ui_state()
        self.populate_sessions()

    def populate_sessions(self):
        self.session_list.clear()
        
        base_dir = os.getcwd()
        dirs = []
        for d in os.listdir(base_dir):
            full_path = os.path.join(base_dir, d)
            if os.path.isdir(full_path):
                if d.startswith("calibration_session_") or d.startswith("dataset_"):
                    crop_dir = os.path.join(full_path, "extracted_crops")
                    if os.path.isdir(crop_dir):
                        dirs.append((os.path.getmtime(full_path), crop_dir))
        
        dirs.sort(key=lambda x: x[0], reverse=True)
        
        for mtime, path in dirs:
            dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            num_items = len(glob.glob(os.path.join(path, "*.jpg")))
            parent_name = os.path.basename(os.path.dirname(path))
            display_name = f"📁 {parent_name} \n   └ {num_items} images | Modified: {dt}"
            
            item = QListWidgetItem(display_name)
            item.setData(Qt.ItemDataRole.UserRole, path)
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
            
    def load_directory(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Dataset Folder (Extracted Crops)")
        if dir_path:
            self.current_dir = dir_path
            self.refresh_file_list()
            self.stacked.setCurrentWidget(self.page_viewer)

    def refresh_file_list(self):
        self.table_images.setRowCount(0)
        self.image_files = []
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
                
            self.table_images.setRowHeight(row, 250)
            
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
        
        row = selected_items[0].row()
        icon_item = self.table_images.item(row, 5)
        if not icon_item: return
        file_path = icon_item.data(Qt.ItemDataRole.UserRole)
        filename = os.path.basename(file_path)
        parts = filename.replace('.jpg', '').split('_')
        
        if len(parts) >= 4:
            screen_val = parts[0]
            try:
                serial_ts = float(parts[1])
                frame_ts = float(parts[2])
                meter_id = parts[3]
                
                lag_ms = (frame_ts - serial_ts) * 1000.0
                
                reply = QMessageBox.question(
                    self, "Confirm Lag Calibration", 
                    f"Set hardware lag to {lag_ms:.1f} ms for Meter {meter_id}?\n\n"
                    f"This tells the software that the camera feed is {lag_ms:.1f} ms behind the serial measurement for M{meter_id}.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                
                if reply == QMessageBox.StandardButton.Yes:
                    if self.app:
                        if str(meter_id) == "1":
                            self.app.meter1.config["hardware_lag_ms"] = lag_ms
                            self.app.meter1.spin_lag.setValue(lag_ms)
                            self.app.meter1.save_config()
                        elif str(meter_id) == "2":
                            self.app.meter2.config["hardware_lag_ms"] = lag_ms
                            self.app.meter2.spin_lag.setValue(lag_ms)
                            self.app.meter2.save_config()
                        QMessageBox.information(self, "Success", f"Hardware lag saved to Meter {meter_id}: {lag_ms:.1f} ms")
            except ValueError:
                QMessageBox.warning(self, "Error", "Filename timestamp format invalid.")
        else:
            QMessageBox.warning(self, "Error", "Filename format invalid for calibration.")

    def update_ui_state(self):
        has_selection = len(self.table_images.selectedItems()) > 0
        self.btn_save_label.setEnabled(has_selection)
        self.btn_delete.setEnabled(has_selection)
        self.btn_set_sync.setEnabled(has_selection)
        self.edit_label.setEnabled(has_selection)
