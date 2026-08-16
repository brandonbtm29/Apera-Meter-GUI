import sys
from PyQt6.QtWidgets import QApplication
from meter_app import MeterApp
import time
import threading

def run_test():
    app = QApplication(sys.argv)
    window = MeterApp()
    window.show()
    
    # Simulate user clicking OCR toggle after 1 second
    def simulate():
        time.sleep(1)
        print("Simulating OCR toggle...", flush=True)
        window.ocr_checkbox.setChecked(True)
        time.sleep(5)
        print("Test complete without crash.", flush=True)
        app.quit()
        
    threading.Thread(target=simulate, daemon=True).start()
    sys.exit(app.exec())

if __name__ == "__main__":
    run_test()
