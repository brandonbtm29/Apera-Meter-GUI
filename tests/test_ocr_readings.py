"""Camera OCR: each meter uses its own reading, even when the other meter has none; a display the OCR
can't read (or a reading it turns down) is logged as 0.0 while recording; and the saved OCR settings
reach the pipeline at start."""

import json

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

import meter_app

# (CNN value, classic CV value, confidence) for one meter, in mS/cm, as the OCR pipeline sends them
M1 = (0.532, 0.532, 0.95)
M2 = (1.234, 1.234, 0.90)
UNREADABLE = (0.0, 0.0, 0.0)  # what the pipeline sends for a display it can't read
ZERO_ROW = ("0.0", "0.000")


@pytest.fixture
def window(monkeypatch, tmp_path):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    app = QApplication.instance() or QApplication([])
    win = meter_app.MeterApp()
    for meter in (win.meter1, win.meter2):
        meter.backup_path = str(tmp_path / f"backup_{meter.meter_id}.csv")  # not the shared data folder
        meter.is_recording = True
    win.ocr_autolog_checkbox.setChecked(True)
    win.ocr_pipeline.min_confidence = 0.5  # a confidence threshold is set
    yield win
    win.close()
    app.processEvents()
    meter_app.plt.close("all")


def _ocr_rows(meter):
    """(uS/cm, mS/cm) of each OCR row in a meter's table."""
    table = meter.table
    return [
        (table.item(r, 3).text(), table.item(r, 4).text())
        for r in range(table.rowCount())
        if table.item(r, 2).text() == "OCR"
    ]


def _set(checkbox, on):
    """Tick or untick an OCR option without saving it to the settings folder the tests share."""
    checkbox.blockSignals(True)
    checkbox.setChecked(on)
    checkbox.blockSignals(False)


@pytest.mark.parametrize(
    ("readings", "meter1_read", "meter2_read"),
    [([None, M2], False, True), ([M1, None], True, False), ([M1, M2], True, True)],
    ids=["meter 2 only", "meter 1 only", "both meters"],
)
def test_each_meter_uses_its_own_reading(window, readings, meter1_read, meter2_read):
    window.on_ocr_readings(readings)  # "meter 2 only" used to raise UnboundLocalError (min_conf)

    assert _ocr_rows(window.meter1) == ([("532.0", "0.532")] if meter1_read else [])
    assert _ocr_rows(window.meter2) == ([("1234.0", "1.234")] if meter2_read else [])
    assert getattr(window.meter1, "last_ocr_cond_ms", None) == (0.532 if meter1_read else None)
    assert getattr(window.meter2, "last_ocr_cond_ms", None) == (1.234 if meter2_read else None)


def test_confidence_threshold_applies_to_meter_2_alone(window):
    """A Meter 2 reading below the threshold is still turned down (logged as 0.0) when Meter 1 has no
    reading."""
    window.on_ocr_readings([None, (1.234, 0.0, 0.2)])
    assert _ocr_rows(window.meter2) == [ZERO_ROW]
    assert getattr(window.meter2, "last_ocr_cond_ms", None) is None

    window.on_ocr_readings([None, (1.234, 0.0, 0.9)])
    assert window.meter2.last_ocr_cond_ms == 1.234


@pytest.mark.parametrize("dual_mode", [True, False], ids=["dual-mode", "CNN only"])
@pytest.mark.parametrize("discard", [False, True], ids=["keep bad reads", "discard bad reads"])
def test_unreadable_display_is_logged_as_zero(window, dual_mode, discard):
    """Every OCR mode logs 0.0 for a display it can't read (CNN-only and Discard Bad Reads logged nothing)."""
    _set(window.ocr_dual_mode_chk, dual_mode)
    _set(window.ocr_discard_chk, discard)

    window.on_ocr_readings([UNREADABLE, M2])

    assert _ocr_rows(window.meter1) == [ZERO_ROW]
    assert _ocr_rows(window.meter2) == [("1234.0", "1.234")]
    assert getattr(window.meter1, "last_ocr_cond_ms", None) is None  # 0.0 isn't kept as the last good read
    assert window.meter2.last_ocr_cond_ms == 1.234


def test_unreadable_display_logs_nothing_while_not_recording(window):
    window.meter2.is_recording = False
    window.on_ocr_readings([UNREADABLE, UNREADABLE])
    assert _ocr_rows(window.meter1) == [ZERO_ROW]
    assert _ocr_rows(window.meter2) == []


def test_reading_above_the_absolute_max_is_logged_as_zero(window):
    _set(window.ocr_phys_max_chk, True)
    window.ocr_phys_max_spin.blockSignals(True)
    window.ocr_phys_max_spin.setValue(199.9)
    window.ocr_phys_max_spin.blockSignals(False)

    window.on_ocr_readings([(250.0, 250.0, 0.95), None])

    assert _ocr_rows(window.meter1) == [ZERO_ROW]


@pytest.mark.parametrize(
    ("discard", "logged"), [(False, ("1500.0", "1.500")), (True, ZERO_ROW)], ids=["keep", "discard"]
)
def test_reading_outside_the_deviation_limit(window, discard, logged):
    """Discard Bad Reads (Return 0.0) logs 0.0 for a read the filters turn down; without it, the raw read."""
    window.meter1.last_serial_cond_ms = 0.5  # the meter's own serial reading, mS/cm
    _set(window.ocr_enforce_dev_chk, True)  # default limit: 50 % from it
    _set(window.ocr_discard_chk, discard)

    window.on_ocr_readings([(1.5, 1.5, 0.95), None])

    assert _ocr_rows(window.meter1) == [logged]


def test_saved_ocr_settings_apply_at_start(monkeypatch, tmp_path):
    """The saved threshold and smoothing are used from the start, not only after changing a setting."""
    (tmp_path / "config.json").write_text(json.dumps({"ocr_conf": 0.8, "ocr_midpoint": False}), "utf-8")
    monkeypatch.setattr(meter_app, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    app = QApplication.instance() or QApplication([])
    win = meter_app.MeterApp()
    try:
        assert win.ocr_pipeline.min_confidence == pytest.approx(0.8)
        assert win.ocr_pipeline.use_midpoint_filter is False
    finally:
        win.close()
        app.processEvents()
        meter_app.plt.close("all")
