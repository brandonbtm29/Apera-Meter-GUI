class OCRFeedTab(QWidget):
    # Emit QImage so PyQt handles cross-thread copying safely
    image_ready = pyqtSignal(QImage)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.app = parent
        layout = QVBoxLayout(self)
        
        control_layout = QHBoxLayout()
        self.focus_slider = QSlider(Qt.Orientation.Horizontal)
        self.focus_slider.setRange(-1, 255)
        self.focus_slider.setValue(-1)
        self.focus_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.focus_slider.setTickInterval(10)
        self.focus_label = QLabel("Focus: Auto")
        
        self.focus_slider.valueChanged.connect(self.on_focus_changed)
        
        control_layout.addWidget(QLabel("Manual Focus:"))
        control_layout.addWidget(self.focus_slider)
        control_layout.addWidget(self.focus_label)
        
        layout.addLayout(control_layout)
        
        self.image_label = InteractiveImageLabel("Waiting for OCR Camera Feed...")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setStyleSheet("background-color: black; color: white; font-size: 18px;")