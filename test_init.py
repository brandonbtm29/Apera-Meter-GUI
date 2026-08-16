import traceback
import sys
from PyQt6.QtWidgets import QApplication
from meter_app import MeterApp

try:
    app = QApplication(sys.argv)
    window = MeterApp()
    print('MeterApp created successfully')
except Exception as e:
    print('Error:', repr(e))
    traceback.print_exc()
