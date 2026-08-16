import re

with open('meter_app.py', 'r', encoding='utf-8') as f:
    content = f.read()

replacement_code = """from PyQt6.QtWidgets import QRubberBand, QSlider, QPushButton, QHBoxLayout
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
        layout = QVBoxLayout()
        
        # --- Toolbar Controls ---
        control_layout = QHBoxLayout()
        
        self.btn_draw = QPushButton("🖌️ Draw ROI")
        self.btn_draw.setCheckable(True)
        self.btn_draw.toggled.connect(self.toggle_drawing)
        
        self.btn_set_m1_cond = QPushButton("✅ M1 Cond")
        self.btn_set_m1_cond.clicked.connect(lambda: self.set_specific_roi("m1_cond"))
        
        self.btn_set_m1_temp = QPushButton("✅ M1 Temp")
        self.btn_set_m1_temp.clicked.connect(lambda: self.set_specific_roi("m1_temp"))
        
        self.btn_set_m2_cond = QPushButton("✅ M2 Cond")
        self.btn_set_m2_cond.clicked.connect(lambda: self.set_specific_roi("m2_cond"))
        
        self.btn_set_m2_temp = QPushButton("✅ M2 Temp")
        self.btn_set_m2_temp.clicked.connect(lambda: self.set_specific_roi("m2_temp"))
        
        self.btn_clear_roi = QPushButton("🗑️ Clear All")
        self.btn_clear_roi.clicked.connect(self.clear_roi)
        
        self.focus_slider = QSlider(Qt.Orientation.Horizontal)
        self.focus_slider.setRange(-1, 255)
        self.focus_slider.setValue(-1)
        self.focus_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.focus_slider.setTickInterval(10)
        self.focus_label = QLabel("Focus: Auto")
        self.focus_slider.valueChanged.connect(self.on_focus_changed)
        
        control_layout.addWidget(self.btn_draw)
        control_layout.addWidget(self.btn_set_m1_cond)
        control_layout.addWidget(self.btn_set_m1_temp)
        control_layout.addWidget(self.btn_set_m2_cond)
        control_layout.addWidget(self.btn_set_m2_temp)
        control_layout.addWidget(self.btn_clear_roi)
        control_layout.addStretch()
        control_layout.addWidget(QLabel("Manual Focus:"))
        control_layout.addWidget(self.focus_slider)
        control_layout.addWidget(self.focus_label)
        
        layout.addLayout(control_layout)
        
        # --- Image Label ---
        self.image_label = InteractiveImageLabel("Waiting for OCR Camera Feed...")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: black; color: white; font-size: 18px;")
        self.image_label.roi_selected.connect(self.on_roi_selected)
        layout.addWidget(self.image_label)
        
        self.setLayout(layout)
        self.image_ready.connect(self.set_image)
        self.last_frame = None
        self.current_drawn_rect = None

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
        
        self.app.ocr_pipeline.rois[name] = (norm_x, norm_y, norm_w, norm_h)
        self.app.ocr_pipeline.save_rois()
        
        # Hide rubberband
        if self.image_label.rubberBand:
            self.image_label.rubberBand.hide()
        self.current_drawn_rect = None
        
        import QMessageBox
        QMessageBox.information(self, "ROI Set", f"Successfully set ROI for {name}.")

    def clear_roi(self):
        self.app.ocr_pipeline.rois = {"m1_cond": None, "m1_temp": None, "m2_cond": None, "m2_temp": None}
        self.app.ocr_pipeline.save_rois()
        if self.image_label.rubberBand:
            self.image_label.rubberBand.hide()
        import QMessageBox
        QMessageBox.information(self, "ROI Cleared", "All ROIs have been reset to defaults.")

    def receive_frame_threadsafe(self, frame):
        self.last_frame = frame.copy()
        self.image_ready.emit(frame)

    def set_image(self, frame):
        rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb_image.shape
        bytes_per_line = ch * w
        q_img = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        
        pixmap = QPixmap.fromImage(q_img)
        scaled_pixmap = pixmap.scaled(self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.image_label.setPixmap(scaled_pixmap)"""

# Delete the barebones OCRFeedTab
content = re.sub(r'class OCRFeedTab\(QWidget\):.*?class MeterApp\(QMainWindow\):', replacement_code + '\n\nclass MeterApp(QMainWindow):', content, flags=re.DOTALL)

with open('meter_app.py', 'w', encoding='utf-8') as f:
    f.write(content)
