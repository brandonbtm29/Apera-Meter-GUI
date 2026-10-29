"""The "Missing Data Streams" warning, Brandon's guard against recording one meter and forgetting the
other: 15 s after a run starts, it lists every meter that's disconnected or not recording."""

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

import meter_app


@pytest.fixture
def window(monkeypatch, tmp_path):
    shown = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda _parent, title, text, *a, **k: shown.append((title, text))
    )
    app = QApplication.instance() or QApplication([])
    win = meter_app.MeterApp()
    for meter in (win.meter1, win.meter2):
        meter.backup_path = str(tmp_path / f"backup_{meter.meter_id}.csv")
        meter.is_connected = True
        meter.is_recording = True
    win.ocr_checkbox.blockSignals(True)  # mark the camera as on without opening a camera
    win.ocr_checkbox.setChecked(True)
    win.ocr_checkbox.blockSignals(False)
    win.shown_warnings = shown
    yield win
    win.close()
    app.processEvents()
    meter_app.plt.close("all")


def test_warns_when_the_other_meter_is_not_recording(window):
    window.meter2.is_recording = False
    window._check_streams_active()
    assert len(window.shown_warnings) == 1
    title, text = window.shown_warnings[0]
    assert title == "Missing Data Streams Warning"
    assert "Meter 2 Recording Toggle (Rec M2)" in text
    assert "Meter 1" not in text


def test_warns_when_a_meter_is_disconnected(window):
    window.meter1.is_connected = False
    window._check_streams_active()
    assert "Meter 1 Serial Connection (Disconnected)" in window.shown_warnings[0][1]


def test_no_warning_when_everything_is_on(window):
    window._check_streams_active()
    assert window.shown_warnings == []


def test_no_warning_after_recording_stopped(window):
    window.meter1.is_recording = window.meter2.is_recording = False
    window._check_streams_active()
    assert window.shown_warnings == []
