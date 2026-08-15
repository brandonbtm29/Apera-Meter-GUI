import os
import glob
from datetime import datetime
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QFileDialog, QLineEdit, QMessageBox, QComboBox,
                             QStackedWidget, QListWidget, QListWidgetItem)
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtCore import Qt

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
        self.lbl_image = QLabel("Select a folder to view extracted crops.")
        self.lbl_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_image.setStyleSheet("background-color: #000; color: #FFF; font-size: 16px;")
        self.lbl_image.setMinimumSize(400, 400)
        viewer_layout.addWidget(self.lbl_image, stretch=1)
        
        # Label editor
        edit_layout = QHBoxLayout()
        edit_layout.addWidget(QLabel("Label (screen_val):"))
        self.edit_label = QLineEdit()
        self.edit_label.returnPressed.connect(self.save_label)
        self.btn_save_label = QPushButton("💾 Save Label")
        self.btn_save_label.clicked.connect(self.save_label)
        
        self.btn_delete = QPushButton("🗑️ Delete Image")
        self.btn_delete.setStyleSheet("background-color: #E74C3C; color: white;")
        self.btn_delete.clicked.connect(self.delete_image)
        
        edit_layout.addWidget(self.edit_label)
        edit_layout.addWidget(self.btn_save_label)
        edit_layout.addStretch()
        edit_layout.addWidget(self.btn_delete)
        viewer_layout.addLayout(edit_layout)
        
        # Navigation
        nav_layout = QHBoxLayout()
        self.btn_prev = QPushButton("◀ Previous (Left Arrow)")
        self.btn_prev.clicked.connect(self.prev_image)
        
        self.btn_set_sync = QPushButton("✅ Set as Sync Point (Calculate Lag)")
        self.btn_set_sync.setStyleSheet("background-color: #2ECC71; color: white; font-weight: bold;")
        self.btn_set_sync.clicked.connect(self.set_sync_point)
        
        self.btn_next = QPushButton("Next (Right Arrow) ▶")
        self.btn_next.clicked.connect(self.next_image)
        
        nav_layout.addWidget(self.btn_prev)
        nav_layout.addWidget(self.btn_set_sync)
        nav_layout.addWidget(self.btn_next)
        viewer_layout.addLayout(nav_layout)
        
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
        self.current_index = 0 if self.image_files else -1
        self.show_current_image()
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
        if event.key() == Qt.Key.Key_Left:
            self.prev_image()
        elif event.key() == Qt.Key.Key_Right:
            self.next_image()
        elif event.key() == Qt.Key.Key_Delete:
            self.delete_image()
        else:
            super().keyPressEvent(event)
            
    def load_directory(self):
        dir_path = QFileDialog.getExistingDirectory(self, "Select Dataset Folder (Extracted Crops)")
        if dir_path:
            self.current_dir = dir_path
            self.refresh_file_list()
            self.current_index = 0 if self.image_files else -1
            self.show_current_image()
            self.stacked.setCurrentWidget(self.page_viewer)

    def refresh_file_list(self):
        if not self.current_dir: return
        search_path = os.path.join(self.current_dir, "*.jpg")
        self.image_files = sorted(glob.glob(search_path))

    def show_current_image(self):
        self.update_ui_state()
        if self.current_index < 0 or self.current_index >= len(self.image_files):
            self.lbl_image.setText("No images to display.")
            return
            
        file_path = self.image_files[self.current_index]
        pixmap = QPixmap(file_path)
        if not pixmap.isNull():
            scaled_pixmap = pixmap.scaled(self.lbl_image.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.lbl_image.setPixmap(scaled_pixmap)
            
            filename = os.path.basename(file_path)
            self.lbl_info.setText(f"[{self.current_index + 1}/{len(self.image_files)}] {filename}")
            
            parts = filename.replace('.jpg', '').split('_')
            if len(parts) >= 1:
                self.edit_label.setText(parts[0])
        else:
            self.lbl_image.setText("Failed to load image.")

    def prev_image(self):
        if self.image_files and self.current_index > 0:
            self.current_index -= 1
            self.show_current_image()

    def next_image(self):
        if self.image_files and self.current_index < len(self.image_files) - 1:
            self.current_index += 1
            self.show_current_image()

    def save_label(self):
        if self.current_index < 0 or self.current_index >= len(self.image_files): return
        
        new_val = self.edit_label.text().strip()
        if not new_val: return
        
        old_path = self.image_files[self.current_index]
        filename = os.path.basename(old_path)
        parts = filename.split('_')
        
        if len(parts) >= 2:
            parts[0] = new_val
            new_filename = "_".join(parts)
            new_path = os.path.join(self.current_dir, new_filename)
            
            try:
                os.rename(old_path, new_path)
                self.image_files[self.current_index] = new_path
                self.show_current_image()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not rename file: {e}")

    def delete_image(self):
        if self.current_index < 0 or self.current_index >= len(self.image_files): return
        
        reply = QMessageBox.question(self, "Confirm Delete", "Delete this image permanently?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if reply == QMessageBox.StandardButton.Yes:
            file_path = self.image_files[self.current_index]
            try:
                os.remove(file_path)
                del self.image_files[self.current_index]
                if self.current_index >= len(self.image_files):
                    self.current_index = len(self.image_files) - 1
                self.show_current_image()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Could not delete file: {e}")

    def set_sync_point(self):
        if self.current_index < 0 or self.current_index >= len(self.image_files): return
        
        file_path = self.image_files[self.current_index]
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
                    f"Set hardware lag to {lag_ms:.1f} ms?\n\n"
                    f"This tells the software that the camera feed is {lag_ms:.1f} ms behind the serial measurement.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                
                if reply == QMessageBox.StandardButton.Yes:
                    if self.app:
                        self.app.meter1.config["hardware_lag_ms"] = lag_ms
                        self.app.meter1.save_config()
                        self.app.meter2.config["hardware_lag_ms"] = lag_ms
                        self.app.meter2.save_config()
                        QMessageBox.information(self, "Success", f"Hardware lag saved: {lag_ms:.1f} ms")
            except ValueError:
                QMessageBox.warning(self, "Error", "Filename timestamp format invalid.")
        else:
            QMessageBox.warning(self, "Error", "Filename format invalid for calibration.")

    def update_ui_state(self):
        has_images = len(self.image_files) > 0
        self.btn_save_label.setEnabled(has_images)
        self.btn_delete.setEnabled(has_images)
        self.btn_prev.setEnabled(self.current_index > 0)
        self.btn_next.setEnabled(self.current_index < len(self.image_files) - 1)
        self.btn_set_sync.setEnabled(has_images)
        self.edit_label.setEnabled(has_images)
