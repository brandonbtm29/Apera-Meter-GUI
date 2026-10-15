"""Bottom-right version label, its "update available" state, and logging without a console."""

import os
import sys

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

import meter_app
import updater


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    app = QApplication.instance() or QApplication([])
    win = meter_app.MeterApp()
    yield win
    win.close()
    app.processEvents()


def release(tag):
    return updater.Release(tag=tag, version=updater.parse_version(tag), name=tag, notes="", url="")


def test_version_shows_bottom_right(window):
    status_bar = window.statusBar()
    assert window.btn_version.parent() is status_bar
    assert window.btn_version.text() == f"v{meter_app.__version__}"
    # It's the last permanent widget, so Qt puts it in the bottom-right corner.
    assert window.btn_version.x() >= max(w.x() for w in status_bar.findChildren(type(window.btn_version)))


def test_newer_release_marks_version_as_old(window):
    window._on_background_update_checked(release("v99.0.0"), None)
    assert "v99.0.0 available" in window.btn_version.text()
    assert "#f39c12" in window.btn_version.styleSheet()
    assert "v99.0.0 is available" in window.settings_tab.lbl_update_status.text()


def test_current_release_and_offline_look_normal(window):
    window._on_background_update_checked(release(f"v{meter_app.__version__}"), None)
    assert window.btn_version.text() == f"v{meter_app.__version__}"
    assert "Up to date" in window.btn_version.toolTip()
    window._on_background_update_checked(None, "offline")
    assert window.btn_version.text() == f"v{meter_app.__version__}"
    assert "Couldn't check" in window.btn_version.toolTip()


def test_output_goes_to_app_log_without_a_console(monkeypatch):
    monkeypatch.setenv("APERA_LOG_TO_FILE", "1")
    old_out, old_err = sys.stdout, sys.stderr
    try:
        path = meter_app._log_to_file_if_needed()
        print("hello from the test")
        sys.stdout.flush()
    finally:
        log, sys.stdout, sys.stderr = sys.stdout, old_out, old_err
        log.close()
    assert path == os.path.join(meter_app.DATA_DIR, "app.log")
    text = open(path, encoding="utf-8").read()
    assert f"Apera Meter GUI {meter_app.__version__} started" in text
    assert "hello from the test" in text


def test_no_log_file_when_there_is_a_console(monkeypatch):
    monkeypatch.delenv("APERA_LOG_TO_FILE", raising=False)
    assert meter_app._log_to_file_if_needed() is None
