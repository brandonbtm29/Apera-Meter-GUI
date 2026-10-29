"""Live OCR check (ocr_review.py) and the review workflow in the Dataset Curation tab."""

import csv
import os

import cv2
import numpy as np

from ocr_review import OcrAnomalyDetector, capture_key, same_reading, save_review_snapshot


def _feed(det, values, conf=0.99):
    return [det.check(v, conf) for v in values]


def test_detector_flags_magnitude_jump_and_low_confidence():
    det = OcrAnomalyDetector()
    assert _feed(det, [0.1662, 0.1663, 0.1661, 0.1662]) == [[], [], [], []]
    assert det.check(0.0166, 0.99) == ["magnitude"]  # 166.x read as 16.x
    assert det.check(0.2200, 0.99) == ["jump"]
    assert det.check(0.1662, 0.80) == ["low_conf"]
    assert det.check(0.0, 0.99) == ["no_read"]


def test_detector_checks_against_a_fresh_serial_reading_only():
    det = OcrAnomalyDetector()
    assert det.check(0.150, 0.99, serial_ms=0.1662, serial_age_s=2) == ["serial_mismatch"]
    assert det.check(0.150, 0.99, serial_ms=0.1662, serial_age_s=40) == []  # serial reading too old


def test_a_new_level_that_persists_is_accepted():
    det = OcrAnomalyDetector(accept_after=5)
    _feed(det, [1.0, 1.0, 1.0, 1.0])
    flags = _feed(det, [10.0] * 5)
    assert flags[0] == ["magnitude"]
    assert det.check(10.0, 0.99) == []  # after 5 readings the new level is the baseline


def test_snapshots_are_rate_limited():
    det = OcrAnomalyDetector(min_interval_s=15, max_snapshots=2)
    assert det.should_snapshot(["magnitude"], now=100)
    assert not det.should_snapshot(["magnitude"], now=110)
    assert not det.should_snapshot([], now=200)
    assert det.should_snapshot(["jump"], now=200)
    assert not det.should_snapshot(["jump"], now=400)  # per-session cap


def test_helpers():
    assert capture_key("166.2_100.000_100.300_1.jpg") == "100.000_100.300_1"
    assert capture_key("/x/Unknown_100.000_100.300_1.png") == "100.000_100.300_1"
    assert same_reading("16.20", "16.2")
    assert not same_reading("16.2", "166.2")
    assert not same_reading("", "16.2") and not same_reading("Unknown", "Unknown")


def _img(value=60):
    return np.full((40, 120, 3), value, dtype=np.uint8)


def _session(tmp_path):
    session = tmp_path / "Sessions" / "dataset_session_1"
    crops = session / "extracted_crops"
    native = session / "extracted_crops_native"
    crops.mkdir(parents=True)
    native.mkdir()
    for name in ("166.2_100.000_100.300_1", "45.1_200.000_200.300_1"):
        cv2.imwrite(str(crops / f"{name}.jpg"), cv2.resize(_img(), (128, 128)))
        cv2.imwrite(str(native / f"{name}.png"), _img())
    with open(session / "readings.csv", "w", newline="", encoding="utf-8") as f:
        f.write("timestamp,meter_id,screen_val,unit,raw_cond,display_str,display_unit,meter_clock,")
        f.write("prev_display_str,ocr_value,ocr_conf,crop_file\n")
        f.write("100.0,1,0.166,mS,1,166.2,uS/cm,2026-10-23 12:00:00,,16.2,0.99,")
        f.write("166.2_100.000_100.300_1.jpg\n")
        f.write("200.0,1,0.045,mS,1,45.1,uS/cm,2026-10-23 12:01:00,166.2,45.1,0.99,")
        f.write("45.1_200.000_200.300_1.jpg\n")
    save_review_snapshot(
        str(session),
        _img(90),
        2,
        300.0,
        "Unknown",
        {"ocr_display": "1.86", "ocr_logged_ms": "0.00186", "ocr_conf": "0.97", "flags": "magnitude"},
    )
    return session, crops


def _row_by_label(tab, label):
    for row in range(tab.table_images.rowCount()):
        if tab.table_images.item(row, 4).text() == label:
            return row
    raise AssertionError(label)


def test_save_review_snapshot_writes_crops_and_csv(tmp_path):
    session, crops = _session(tmp_path)
    assert (crops / "Unknown_300.000_300.000_2.jpg").exists()
    assert (session / "extracted_crops_native" / "Unknown_300.000_300.000_2.png").exists()
    with open(session / "review.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["flags"] == "magnitude" and rows[0]["crop_file"] == "Unknown_300.000_300.000_2.jpg"


def test_curation_tab_review_accept_and_reject(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QApplication, QMessageBox

    from dataset_curation_tab import DatasetCurationTab

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    app = QApplication.instance() or QApplication([])
    session, crops = _session(tmp_path)

    tab = DatasetCurationTab(parent_app=None)
    tab.current_dir = str(crops)
    tab.refresh_file_list()
    assert tab.table_images.rowCount() == 3
    mismatch, ok, flagged = (_row_by_label(tab, x) for x in ("166.2", "45.1", "Unknown"))
    assert tab.table_images.item(mismatch, 6).text() == "16.2"
    assert tab._row_status(mismatch) == "needs review"  # OCR read 16.2, meter said 166.2
    assert tab._row_status(ok) == ""
    assert tab._row_status(flagged) == "needs review"
    assert tab.table_images.item(flagged, 7).text() == "magnitude"

    tab.combo_filter.setCurrentIndex(1)  # Needs review
    assert tab.table_images.isRowHidden(ok) and not tab.table_images.isRowHidden(mismatch)

    tab.table_images.clearSelection()
    tab.table_images.selectRow(mismatch)
    tab.add_to_master_dataset()
    master = tmp_path / "Sessions"
    assert (master / "Master_Dataset" / "166.2" / "166.2_100.000_100.300_1.jpg").exists()
    assert (master / "Master_Dataset_native" / "166.2" / "166.2_100.000_100.300_1.png").exists()

    tab.table_images.clearSelection()
    tab.table_images.selectRow(flagged)
    tab.reject_selected()

    with open(session / "curation_log.csv", encoding="utf-8") as f:
        log = {r["capture"]: r["decision"] for r in csv.DictReader(f)}
    assert log == {"100.000_100.300_1": "accepted", "300.000_300.000_2": "rejected"}

    # Decisions survive reopening the session, and a relabelled crop keeps its history.
    again = DatasetCurationTab(parent_app=None)
    again.current_dir = str(crops)
    again.refresh_file_list()
    assert again._row_status(_row_by_label(again, "166.2")) == "accepted"
    assert again._row_status(_row_by_label(again, "Unknown")) == "rejected"
    assert os.path.exists(again._native_path(str(crops / "166.2_100.000_100.300_1.jpg")))
    app.processEvents()


def test_app_snapshots_a_suspicious_live_reading(monkeypatch):
    from PyQt6.QtWidgets import QApplication, QMessageBox

    import meter_app

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    app = QApplication.instance() or QApplication([])
    window = meter_app.MeterApp()
    try:
        window.toggle_dataset_mode(2)
        window.ocr_feed_tab.last_frame = np.full((480, 640, 3), 40, dtype=np.uint8)
        for _ in range(4):
            window._review_ocr_reading(1, window.meter1, 166.2, 0.1662, 0.99)
        window._review_ocr_reading(1, window.meter1, 16.6, 0.0166, 0.99)
        with open(os.path.join(window.dataset_session_dir, "review.csv"), encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["flags"] == "magnitude" and rows[0]["ocr_display"] == "16.6"
        crop = os.path.join(window.dataset_session_dir, "extracted_crops", rows[0]["crop_file"])
        assert os.path.exists(crop)
    finally:
        window.toggle_dataset_mode(0)
        window.close()
        app.processEvents()
