"""Render docs/screenshot.png: the Meter 1 tab with one hour of made-up readings.

Runs headless and points the app at a temporary data/settings folder, so it never touches real
data or settings. Run from the repository folder:

    python Scripts/make_screenshot.py
"""

import datetime
import math
import os
import random
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "screenshot.png"

_tmp = tempfile.mkdtemp(prefix="apera-screenshot-")
os.environ["UAKRON_APP_DATA_DIR"] = os.path.join(_tmp, "data")
os.environ["UAKRON_SETTINGS_DIR"] = os.path.join(_tmp, "settings")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(REPO))

import qdarktheme  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import meter_app  # noqa: E402


def main():
    app = QApplication([])
    app.setStyleSheet(qdarktheme.load_stylesheet("dark"))
    window = meter_app.MeterApp()
    window.resize(1600, 950)
    window.show()

    meter = window.meter1
    window.tabs.setCurrentWidget(meter)
    meter.set_device_info("Model", "EC400S")
    meter.set_device_info("SN", "DEMO-0001")

    rng = random.Random(4)  # noqa: S311 - fake data, not security
    start = datetime.datetime(2026, 10, 14, 9, 0, 0)
    for i in range(180):
        t = start + datetime.timedelta(seconds=20 * i)
        cond_ms = 1.20 * math.exp(-i / 140) + 0.35 + rng.gauss(0, 0.004)
        temp_c = 22.4 + 0.6 * math.sin(i / 40) + rng.gauss(0, 0.03)
        row = (
            t.strftime("%Y-%m-%d %H:%M:%S"),
            "Live",
            f"{cond_ms * 1000:.1f}",
            f"{cond_ms:.4f}",
            f"{temp_c:.1f}",
        )
        meter.log_point_ui(t, row)

    meter.plot_data()
    for _ in range(20):
        app.processEvents()
    OUT.parent.mkdir(exist_ok=True)
    window.grab().save(str(OUT))
    print(f"Saved {OUT}")
    os._exit(0)  # skip the "unsaved data" prompt and background threads


if __name__ == "__main__":
    main()
