"""Auto-Record OCR Training Dataset: each serial reading saves a labelled crop and a readings.csv row."""

import csv
import glob
import os
import struct
import time

import numpy as np


def _ds_packet(dp, dot, second, unit_b=1):
    """A <DS conductivity packet: 2026-10-23 12:34:<second>, 22.5 C, reading dp / 10**dot in uS/cm."""
    w1 = 2 * 4096 + 34 * 64 + second  # mode 2 = conductivity, mm, ss
    w2 = 26 * 512 + 10 * 32 + 23  # yy, month, day
    w3 = 12 * 2048 + 225  # hour, NTC = 22.5 C
    w4 = unit_b * 8192 + dot * 2048 + dp  # unit (1 = uS/cm), decimals, digits
    return b"<DS" + struct.pack(">HHHH", w1, w2, w3, w4) + b">"


def _wait_for_rows(path, n, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            if len(rows) >= n:
                return rows
        time.sleep(0.05)
    raise AssertionError(f"readings.csv did not reach {n} rows")


def test_header_keeps_the_original_columns_first():
    import meter_app

    cols = meter_app.DATASET_CSV_HEADER.strip().split(",")
    assert cols[:5] == ["timestamp", "meter_id", "screen_val", "unit", "raw_cond"]
    assert len(cols) == len(set(cols)) == 12


def test_serial_reading_saves_labelled_crops(monkeypatch):
    from PyQt6.QtWidgets import QApplication, QMessageBox

    import meter_app

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    app = QApplication.instance() or QApplication([])
    window = meter_app.MeterApp()
    try:
        window.shared_config["apply_hardware_lag"] = False
        window.toggle_dataset_mode(2)
        frame = np.full((480, 640, 3), 40, dtype=np.uint8)
        window.ocr_feed_tab.last_frame = frame

        window.meter1.process_packet(_ds_packet(1662, 1, second=10))  # 166.2 uS/cm
        window.meter1.process_packet(_ds_packet(1662, 1, second=11))  # unchanged display
        rows = _wait_for_rows(window.dataset_csv_path, 2)

        # Each capture runs on its own thread, so rows can land in either order.
        first, second = sorted(rows, key=lambda r: r["meter_clock"])
        assert first["display_str"] == "166.2"
        assert first["display_unit"] == "uS/cm"
        assert first["meter_clock"] == "2026-10-23 12:34:10"
        assert first["prev_display_str"] == ""
        assert second["prev_display_str"] == "166.2"
        assert first["meter_id"] == "1"

        crop_dir = os.path.join(window.dataset_session_dir, "extracted_crops")
        native_dir = os.path.join(window.dataset_session_dir, "extracted_crops_native")
        assert first["crop_file"].startswith("166.2_")
        assert os.path.exists(os.path.join(crop_dir, first["crop_file"]))
        stem = os.path.splitext(first["crop_file"])[0]
        assert glob.glob(os.path.join(native_dir, stem + ".png"))
    finally:
        window.toggle_dataset_mode(0)
        window.close()
        app.processEvents()
