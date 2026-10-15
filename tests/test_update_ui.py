"""The Settings tab's Check for Updates button, with GitHub faked."""

import time

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

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


def wait_for(condition, timeout=10):
    app = QApplication.instance()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


def fake_release(tag):
    return updater.Release(
        tag=tag,
        version=updater.parse_version(tag),
        name=f"Apera Meter GUI {tag}",
        notes="### Added\n- Something",
        url="https://example.invalid/release",
    )


def test_update_button_replaces_restart(window):
    tab = window.settings_tab
    assert tab.btn_update.text().endswith("Check for Updates")
    assert meter_app.__version__ in tab.lbl_version.text()


def test_reports_up_to_date(window, monkeypatch):
    shown = []
    monkeypatch.setattr(updater, "fetch_latest_release", lambda: fake_release(f"v{meter_app.__version__}"))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))

    window.check_for_updates()

    assert wait_for(lambda: shown)
    assert "latest version" in shown[0]
    assert "Up to date" in window.settings_tab.lbl_update_status.text()


def test_offers_newer_release(window, monkeypatch):
    dialogs = []
    monkeypatch.setattr(updater, "fetch_latest_release", lambda: fake_release("v99.0.0"))

    def fake_exec(dialog):
        dialogs.append(dialog.windowTitle())
        return QDialog.DialogCode.Rejected  # "Later"

    monkeypatch.setattr(QDialog, "exec", fake_exec)

    window.check_for_updates()

    assert wait_for(lambda: dialogs)
    assert "v99.0.0" in dialogs[0]
    assert "v99.0.0 is available" in window.settings_tab.lbl_update_status.text()


def test_shows_check_errors(window, monkeypatch):
    warnings = []

    def offline():
        raise updater.UpdateError("Couldn't reach GitHub")

    monkeypatch.setattr(updater, "fetch_latest_release", offline)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))

    window.check_for_updates()

    assert wait_for(lambda: warnings)
    assert window.settings_tab.btn_update.isEnabled()
