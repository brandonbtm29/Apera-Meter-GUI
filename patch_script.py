import re

with open('meter_app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add OCRFeedTab
ocr_feed_tab_code = """
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QImage, QPixmap
import cv2

class OCRFeedTab(QWidget):
    image_ready = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout()
        self.image_label = QLabel("Waiting for OCR Camera Feed...")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: black; color: white; font-size: 18px;")
        layout.addWidget(self.image_label)
        self.setLayout(layout)
        self.image_ready.connect(self.set_image)
        self.last_frame = None

    def receive_frame_threadsafe(self, frame):
        self.last_frame = frame.copy()
        self.image_ready.emit(frame)

    def set_image(self, frame):
        # Convert BGR to RGB
        rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        q_img = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        
        # Scale to fit window while keeping aspect ratio
        pixmap = QPixmap.fromImage(q_img)
        scaled_pixmap = pixmap.scaled(self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.image_label.setPixmap(scaled_pixmap)
"""

content = content.replace("class MeterApp(QMainWindow):", ocr_feed_tab_code + "\n\nclass MeterApp(QMainWindow):")

# 2. Add toolbar and global record logic to MeterApp.__init__
toolbar_code = """
        # --- Toolbar for Global OCR & Dataset Toggle ---
        toolbar = self.addToolBar("Global Mode")
        toolbar.setMovable(False)
        
        self.ocr_checkbox = QCheckBox("Enable OCR High-Sampling Mode (Camera)")
        self.ocr_checkbox.setStyleSheet("font-weight: bold; margin-left: 10px; margin-right: 10px;")
        self.ocr_checkbox.stateChanged.connect(self.toggle_ocr_mode)
        toolbar.addWidget(self.ocr_checkbox)
        
        self.dataset_checkbox = QCheckBox("Record Smart Dataset (Save 1 frame per Serial Reading)")
        self.dataset_checkbox.setStyleSheet("font-weight: bold; margin-left: 10px; margin-right: 10px; color: #2980b9;")
        self.dataset_checkbox.stateChanged.connect(self.toggle_dataset_mode)
        toolbar.addWidget(self.dataset_checkbox)
        
        self.ocr_status_label = QLabel("OCR: Off")
        self.ocr_status_label.setStyleSheet("color: gray;")
        toolbar.addWidget(self.ocr_status_label)
        
        from ocr_pipeline import MeterOCR
        self.ocr_pipeline = MeterOCR()
        self.ocr_pipeline.add_callback(self.on_ocr_readings)

        self.ocr_feed_tab = OCRFeedTab(self)
        self.ocr_pipeline.add_frame_callback(self.ocr_feed_tab.receive_frame_threadsafe)
        
        self.dataset_session_dir = None
        self.dataset_csv_path = None
"""

init_target = """        self.setCentralWidget(self.tabs)
        
        self.meter1 = MeterController(meter_id=1, parent_app=self)"""

content = content.replace(init_target, toolbar_code + "\n" + init_target)

# 3. Add tabs to the UI
tabs_target = """        self.tabs.addTab(self.meter1, "Meter 1")
        self.tabs.addTab(self.meter2, "Meter 2")"""
tabs_replacement = tabs_target + "\n        self.tabs.addTab(self.ocr_feed_tab, \"Live OCR Feed\")"
content = content.replace(tabs_target, tabs_replacement)

# 4. Add the MeterApp methods
methods_code = """
    def set_ocr_status(self, text, color):
        self.ocr_status_label.setText(text)
        self.ocr_status_label.setStyleSheet(f"color: {color};")

    def toggle_ocr_mode(self, state):
        import time
        if state:
            self.set_ocr_status("OCR: Starting Camera...", "#f39c12")
            self.meter1.ocr_active = True
            self.meter2.ocr_active = True
            self.ocr_pipeline.start()
            self.ocr_start_time = time.time()
            self.set_ocr_status("OCR: Warming up (10s delay)...", "#f39c12")
        else:
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
                f.write("timestamp,meter_id,screen_val,unit,raw_cond\\n")
            
            # Save ROIs immediately
            import json
            rois_path = os.path.join(self.dataset_session_dir, "rois.json")
            with open(rois_path, "w") as f:
                json.dump(self.ocr_pipeline.rois, f)
        else:
            self.dataset_session_dir = None
            self.dataset_csv_path = None

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
            c1, t1 = readings[0]
            row_data1 = (dt_str, "OCR", str(c1*1000), str(c1), f"{t1:.1f}", "0")
            if self.meter1.is_recording:
                self.meter1.signals.update_tree.emit(now, row_data1)
                
        if len(readings) > 1 and readings[1] is not None:
            c2, t2 = readings[1]
            row_data2 = (dt_str, "OCR", str(c2*1000), str(c2), f"{t2:.1f}", "0")
            if self.meter2.is_recording:
                self.meter2.signals.update_tree.emit(now, row_data2)
"""
content = content.replace("    def get_used_ports(self):", methods_code + "\n    def get_used_ports(self):")


# 5. Modify update_serial_reading in MeterController
serial_target = """                if self.is_recording:
                    self.signals.update_tree.emit(dt_obj, row_data)"""
serial_replacement = """                if self.is_recording:
                    if getattr(self, 'ocr_active', False):
                        # Throttle OCR visual logging to 10s if we wanted to
                        pass
                    self.signals.update_tree.emit(dt_obj, row_data)
                    
                    # --- SMART DATASET RECORDING LOGIC ---
                    app = self.parent_app
                    if getattr(app, 'dataset_checkbox', None) and app.dataset_checkbox.isChecked() and app.dataset_session_dir:
                        # Grab the current frame
                        frame = app.ocr_feed_tab.last_frame
                        if frame is not None:
                            import cv2
                            import time
                            import os
                            ts = time.time()
                            frame_path = os.path.join(app.dataset_session_dir, "frames", f"frame_{ts}.jpg")
                            cv2.imwrite(frame_path, frame)
                            
                            with open(app.dataset_csv_path, "a", encoding="utf-8") as f:
                                # Write true screen_val from Serial
                                f.write(f"{ts},{self.meter_id},{cond_ms},mS,{raw_cond}\\n")
"""
content = content.replace(serial_target, serial_replacement)

# 6. Checkbox Save
config_target = """self.config[f"save_template_{self.meter_id}"] = self.ent_save_template.text()"""
config_replacement = config_target + """
        self.config[f"save_on_pause_{self.meter_id}"] = getattr(self, 'chk_save_on_pause', QCheckBox()).isChecked()"""
content = content.replace(config_target, config_replacement)

# 7. Clear path
clear_target = "self.table.setRowCount(0)"
clear_replacement = clear_target + "\\n        self.last_auto_filepath = None"
content = content.replace(clear_target, clear_replacement)

with open('meter_app.py', 'w', encoding='utf-8') as f:
    f.write(content)
print("meter_app.py patched!")
