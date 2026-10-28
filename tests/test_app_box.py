"""Settings → Application Preferences (Open Log Folder, Copy Diagnostics, check on start) and the
one-copy-at-a-time lock in main() and Restart."""

import json
import subprocess

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

import diagnostics
import meter_app
import single_instance


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    app = QApplication.instance() or QApplication([])
    win = meter_app.MeterApp()
    yield win
    win.close()
    app.processEvents()
    meter_app.plt.close("all")  # each window makes its own graphs


def test_buttons_are_in_the_app_box(window):
    tab = window.settings_tab
    assert tab.btn_open_logs.text().endswith("Open Log Folder")
    assert tab.btn_copy_diagnostics.text().endswith("Copy Diagnostics")
    # Same box as Check for Updates
    assert tab.btn_open_logs.parentWidget() is tab.btn_update.parentWidget()


def test_copy_diagnostics_puts_a_report_on_the_clipboard(window):
    QApplication.clipboard().setText("")
    window.settings_tab.btn_copy_diagnostics.click()
    text = QApplication.clipboard().text()
    assert text.startswith(f"Apera Meter GUI {meter_app.__version__}")
    assert "Meter 1: not connected" in text
    assert "Meter 2: not connected" in text
    assert "Settings folder:" in text
    assert "Data folder:" in text
    assert "Diagnostics copied" in window.settings_tab.lbl_diagnostics.text()


def test_open_log_folder(window, monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(meter_app.crash_log, "log_path", lambda: str(tmp_path / "logs" / "apera_meter.log"))
    monkeypatch.setattr(diagnostics, "open_folder", opened.append)
    window.settings_tab.btn_open_logs.click()
    assert opened == [str(tmp_path / "logs")]


def test_open_log_folder_explains_errors(window, monkeypatch):
    warnings = []

    def fail(path):
        raise OSError("There is no folder to open yet.")

    monkeypatch.setattr(diagnostics, "open_folder", fail)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    window.open_log_folder()
    assert warnings and "no folder to open" in warnings[0]


def test_check_on_start_setting(window, monkeypatch):
    checks = []
    monkeypatch.setattr(window, "_background_update_check", lambda: checks.append(1))
    chk = window.settings_tab.chk_update_on_start
    assert chk.isChecked()  # on by default

    window._automatic_update_check()
    assert checks == [1]

    chk.setChecked(False)
    window._automatic_update_check()
    assert checks == [1]  # turned off: no check at start or once a day
    with open(window.meter1.config_path, encoding="utf-8") as f:
        assert json.load(f)["check_updates_on_start"] is False

    chk.setChecked(True)
    window._automatic_update_check()
    assert checks == [1, 1]


class _FakeWindow:
    def show(self):
        pass

    def _start_background_checks(self):
        pass


@pytest.fixture
def quiet_main(monkeypatch):
    """main() without the crash-log hooks, the real window or the event loop."""
    monkeypatch.setattr(meter_app.crash_log, "setup", lambda *a, **k: None)
    monkeypatch.setattr(QApplication, "exec", lambda *a: 0)
    built = []
    monkeypatch.setattr(meter_app, "MeterApp", lambda: built.append(_FakeWindow()) or built[-1])
    return built


def test_main_takes_the_single_instance_lock(quiet_main, monkeypatch):
    calls = []

    class Guard:
        def set_window(self, window):
            calls.append(("window", window))

    monkeypatch.setattr(single_instance, "acquire", lambda name, **k: calls.append(name) or Guard())
    with pytest.raises(SystemExit) as e:
        meter_app.main(["meter_app.py"])
    assert e.value.code == 0
    assert calls == ["Apera Meter GUI", ("window", quiet_main[0])]


def test_second_copy_quits_quietly(quiet_main, monkeypatch):
    monkeypatch.setattr(single_instance, "acquire", lambda name, **k: None)  # another copy has it
    with pytest.raises(SystemExit) as e:
        meter_app.main(["meter_app.py"])
    assert e.value.code == 0
    assert quiet_main == []  # no second window


def test_restart_releases_the_single_instance_lock_first(window, monkeypatch):
    """Restart (also used by Update and Restart and the Settings Folder box) lets the new copy start."""
    events = []
    monkeypatch.setattr(single_instance, "release", lambda: events.append("release"))
    monkeypatch.setattr(subprocess, "Popen", lambda args, **k: events.append("start"))
    monkeypatch.setattr(QApplication, "quit", lambda *a: events.append("quit"))
    window.restart_app()
    assert events == ["release", "start", "quit"]
