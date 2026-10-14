import sys

print("start", flush=True)
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
print("imported app", flush=True)
from meter_app import MeterApp

print("imported MeterApp", flush=True)

import threading


def watchdog():
    import time, traceback

    time.sleep(2)
    print("Watchdog triggered, printing stack trace:")
    for th in threading.enumerate():
        print(th)
        traceback.print_stack(sys._current_frames()[th.ident])


t = threading.Thread(target=watchdog)
t.daemon = True
t.start()

window = MeterApp()
print("done", flush=True)
