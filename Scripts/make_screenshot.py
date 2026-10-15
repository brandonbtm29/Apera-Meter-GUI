"""Render docs/screenshot.png: the Meter 1 tab with one hour of made-up readings.

Runs headless and points the app at a temporary data/settings folder, so it never touches real
data or settings. Run from the repository folder:

    python Scripts/make_screenshot.py                     # docs/screenshot.png
    python Scripts/make_screenshot.py --tab Settings --out settings.png
"""

import argparse
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

import meter_app  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tab", default="Meter 1", help="tab to show (default: Meter 1)")
    parser.add_argument("--out", default=str(OUT), help="output PNG (default: docs/screenshot.png)")
    parser.add_argument("--size", default="1600x950", help="window size, WIDTHxHEIGHT")
    args = parser.parse_args()

    app = meter_app.create_app([])
    window = meter_app.MeterApp()
    width, height = (int(v) for v in args.size.split("x"))
    window.resize(width, height)
    window.show()

    meter = window.meter1
    tabs = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    window.tabs.setCurrentIndex(tabs.index(args.tab))
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
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    window.grab().save(str(out))
    print(f"Saved {out}")
    os._exit(0)  # skip the "unsaved data" prompt and background threads


if __name__ == "__main__":
    main()
