"""The meter tab's data path, end to end with a fake serial port: connecting only to a port that
answers like a meter, reconnecting after a USB dropout, timestamps, modes, auto-save, export,
recovery, the memory download, the no-readings alert, settings and closing with unsaved data."""

import datetime
import json
import os
import time

import openpyxl
import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

import meter_app
import meter_protocol
import recording
from test_meter_protocol import packet


class FakeSerial:
    """Stands in for serial.Serial. A meter answers pings with "#>"; anything else stays silent."""

    def __init__(self, port="COM9", is_meter=True):
        self.port = port
        self.is_meter = is_meter
        self.is_open = True
        self.written = []
        self.buffer = bytearray()
        self.unplugged = False

    @property
    def in_waiting(self):
        if self.unplugged:
            raise OSError(5, "Input/output error (device unplugged)")
        return len(self.buffer)

    def read(self, n):
        data = bytes(self.buffer[:n])
        del self.buffer[:n]
        return data

    def write(self, data):
        if self.unplugged:
            raise OSError(5, "Input/output error")
        self.written.append(bytes(data))
        if self.is_meter and data == meter_protocol.PING:
            self.buffer += b"#>"

    def feed(self, data):
        self.buffer += data

    def setDTR(self, value):
        pass

    def setRTS(self, value):
        pass

    def close(self):
        self.is_open = False


class Port:
    def __init__(self, device):
        self.device = device
        self.description = f"USB Serial ({device})"
        self.vid = 0x10C4


def wait_until(condition, timeout=5.0):
    app = QApplication.instance()
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@pytest.fixture
def ports(monkeypatch):
    """The COM ports the app sees and the fake serial ports it opens."""
    state = {"devices": ["COM9"], "opened": [], "is_meter": True}

    def open_port(port, *args, **kwargs):
        if port not in state["devices"]:
            raise OSError(2, f"could not open port {port}")
        ser = FakeSerial(port, is_meter=state["is_meter"])
        state["opened"].append(ser)
        return ser

    monkeypatch.setattr(meter_app.serial, "Serial", open_port)
    listed = lambda: [Port(d) for d in state["devices"]]  # noqa: E731
    monkeypatch.setattr(meter_app.serial.tools.list_ports, "comports", listed)
    monkeypatch.setattr(meter_app, "RECONNECT_DELAYS_S", (0.1,))
    monkeypatch.setattr(meter_app, "CONFIRM_TIMEOUT_MS", 400)
    return state


@pytest.fixture
def window(monkeypatch, tmp_path, ports):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *a, **k: None)
    app = QApplication.instance() or QApplication([])
    win = meter_app.MeterApp()
    saved_config = dict(win.shared_config)  # put back before closing: the settings folder is shared
    alerts = []
    for meter in (win.meter1, win.meter2):
        meter.backup_dir = str(tmp_path / "Data Backups")
        meter.send_discord_alert = lambda url, msg, alerts=alerts: alerts.append(msg)
    win.shared_config.update(
        global_default_save_folder=str(tmp_path / "Data"),
        alert_enabled_1=True,
        alert_webhook_1="https://discord.invalid/webhook",
    )
    os.makedirs(tmp_path / "Data", exist_ok=True)
    win.alerts = alerts
    win.tmp = tmp_path
    yield win
    for meter in (win.meter1, win.meter2):
        meter.disconnect_meter()
    win.shared_config.clear()
    win.shared_config.update(saved_config)
    win.close()
    app.processEvents()
    meter_app.plt.close("all")


def events(meter):
    return meter.txt_system_log.toPlainText()


def connect(meter, port="COM9"):
    meter.refresh_ports()
    meter._finish_connect(port)
    assert wait_until(lambda: meter._confirmed), "the fake meter never answered"
    return meter.ser


def rows(meter):
    return [list(r) for r in meter.records]


# --- connecting ------------------------------------------------------------------------------
def test_new_install_doesnt_connect_to_com4_at_start(window):
    assert window.meter1.port_combo.currentText() == meter_app.AUTO_DETECT
    assert not window.meter1.is_connected


def test_port_is_remembered_per_computer_once_the_meter_answers(window):
    connect(window.meter1)
    assert window.meter1.btn_connect.text() == "🔌 Disconnect"
    assert window.local_state["ports"]["1"] == "COM9"
    with open(meter_app.LOCAL_STATE_PATH, encoding="utf-8") as f:
        assert json.load(f)["ports"]["1"] == "COM9"
    assert "COM9" in window.status_m1.text()


def test_a_port_that_doesnt_answer_like_a_meter_is_let_go(window, ports):
    ports["is_meter"] = False  # e.g. a pump or balance on that port
    m = window.meter1
    m.refresh_ports()
    m._finish_connect("COM9")
    assert wait_until(lambda: not m.is_connected, timeout=3)
    assert wait_until(lambda: not ports["opened"][0].is_open)
    assert len([w for w in ports["opened"][0].written if w == meter_protocol.PING]) <= 3
    assert "No Apera meter answered on COM9" in window.status_m1.text()
    assert "ports" not in window.local_state


def test_usb_dropout_closes_the_port_and_reconnects(window, ports):
    m = window.meter1
    m.is_recording = True
    first = connect(m)
    first.unplugged = True  # USB cable pulled
    assert wait_until(lambda: not m.is_connected)
    assert not first.is_open
    assert m.btn_connect.text() == "🔌 Connect"
    assert "Lost the connection to COM9" in events(m)
    assert any("lost its USB connection" in a for a in window.alerts)

    assert wait_until(lambda: m._confirmed and m.ser is not first, timeout=5)  # plugged back in
    assert m.btn_connect.text() == "🔌 Disconnect"
    assert "Reconnected to COM9" in events(m)
    assert any("reconnected" in a for a in window.alerts)


def test_reconnect_waits_for_the_port_and_stops_on_disconnect(window, ports):
    m = window.meter1
    first = connect(m)
    ports["devices"] = []  # unplugged: the port is gone
    first.unplugged = True
    assert wait_until(lambda: "Waiting for COM9" in window.status_m1.text(), timeout=3)
    m.toggle_connection()  # the user gives up
    assert m._reconnect_port is None
    ports["devices"] = ["COM9"]
    time.sleep(0.3)
    QApplication.instance().processEvents()
    assert not m.is_connected


# --- readings --------------------------------------------------------------------------------
def test_rows_use_the_pc_clock_and_keep_the_meter_clock(window):
    m = window.meter1
    m.is_recording = True
    m.process_packet(packet(when=datetime.datetime(2020, 1, 1, 0, 0, 5)))  # meter clock years off
    assert wait_until(lambda: m.records)
    rec = m.records[0]
    logged = datetime.datetime.strptime(rec[0], recording.TIME_FORMAT)
    assert abs((logged - datetime.datetime.now()).total_seconds()) < 5
    assert float(rec[1]) >= 0  # no more negative durations
    assert rec[7] == "2020-01-01 00:00:05"
    assert "clock" in events(m) and "behind" in events(m)


def test_other_modes_are_not_logged_as_conductivity(window):
    m = window.meter1
    m.is_recording = True
    m.process_packet(packet(mode=1, decimals=1, digits=735))  # mV
    QApplication.instance().processEvents()
    assert m.records == []
    assert "mV mode" in events(m)


def test_us_readings_keep_their_digits(window):
    m = window.meter1
    m.is_recording = True
    m.process_packet(packet(unit=1, decimals=1, digits=123))
    assert wait_until(lambda: m.records)
    assert m.records[0][3:5] == ("12.3", "0.012300")
    assert m.plot_conds[0] == pytest.approx(0.0123)


def test_repeated_packets_are_dropped(window):
    m = window.meter1
    m.is_recording = True
    for _ in range(3):
        m.process_packet(packet())
    m.process_packet(packet(when=datetime.datetime(2026, 11, 4, 12, 0, 6)))
    QApplication.instance().processEvents()
    assert len(m.records) == 2


def test_readings_go_to_this_sessions_backup(window):
    m = window.meter1
    m.is_recording = True
    m.process_packet(packet())
    assert wait_until(lambda: m.records)
    assert os.path.dirname(m.backup_path) == os.path.join(m.backup_dir, meter_app.COMPUTER)
    with open(m.backup_path, encoding="utf-8") as f:
        assert len(f.read().strip().splitlines()) == 2  # header + the reading
    m.clear_log(ask=False)
    assert m.backup_path is None  # the next session gets its own file


def test_recovery_restores_temperatures_and_keeps_appending(window):
    m = window.meter1
    m.is_recording = True
    for s in range(3):
        m.process_packet(packet(temp_tenths=200 + s, when=datetime.datetime(2026, 11, 4, 12, 0, s)))
    assert wait_until(lambda: len(m.records) == 3)
    path = m.backup_path
    m.clear_log(ask=False)
    sessions = m.scan_backup_sessions()
    assert len(sessions) == 1 and sessions[0]["this_computer"]
    assert m.restore_session(sessions[0]) == 3
    assert m.plot_temps == pytest.approx([20.0, 20.1, 20.2])  # used to read the OCR column (0.0)
    assert m.backup_path == path and m.unsaved_changes


# --- saving ----------------------------------------------------------------------------------
def _record(m, n=3):
    m.is_recording = True
    for s in range(n):
        m.process_packet(packet(digits=1000 + s, when=datetime.datetime(2026, 11, 4, 12, 0, s)))
    assert wait_until(lambda: len(m.records) == n)


def test_autosave_overwrites_one_file_per_session(window):
    m = window.meter1
    _record(m)
    assert m.save_excel(auto=True, is_background_autosave=True)
    m.wait_for_background_save()
    time.sleep(1.1)  # <Time> in the template would give a new name now
    _record(m, 4)
    m.save_excel(auto=True, is_background_autosave=True)
    m.wait_for_background_save()
    assert wait_until(lambda: not m.unsaved_changes)
    files = [f for f in os.listdir(window.tmp / "Data") if f.endswith(".xlsx")]
    assert len(files) == 1
    assert openpyxl.load_workbook(window.tmp / "Data" / files[0])["Data"].max_row == 5


def test_failed_autosave_is_visible_and_falls_back_to_a_new_name(window, monkeypatch):
    m = window.meter1
    _record(m)
    m.autosave_path = str(window.tmp / "Data" / "run.xlsx")
    real_replace = recording.replace_file

    def locked(src, dst, attempts=10, wait=0.1):
        if dst.endswith("run.xlsx"):
            raise PermissionError(13, "Permission denied (open in Excel)", dst)
        real_replace(src, dst, attempts, wait)

    monkeypatch.setattr(recording, "replace_file", locked)
    m.save_excel(auto=True, is_background_autosave=True)
    m.wait_for_background_save()
    assert wait_until(lambda: m.autosave_path.endswith("run (2).xlsx"))
    assert os.path.exists(window.tmp / "Data" / "run (2).xlsx")
    assert "couldn't be written" in events(m)

    def broken(src, dst, attempts=10, wait=0.1):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(recording, "replace_file", broken)
    m.process_packet(packet(when=datetime.datetime(2026, 11, 4, 12, 1, 0)))
    m.save_excel(auto=True, is_background_autosave=True)
    m.wait_for_background_save()
    assert wait_until(lambda: "Save failed" in window.status_m1.text())
    assert "No space left" in events(m)
    assert m.unsaved_changes
    assert any("failed" in a for a in window.alerts)


def test_export_is_in_time_order_even_when_the_table_is_sorted(window, monkeypatch):
    m = window.meter1
    _record(m, 5)
    m.table.sortItems(3)  # the user clicked the Cond column
    m.table.sortItems(3, meter_app.Qt.SortOrder.DescendingOrder)
    path = str(window.tmp / "export.xlsx")
    monkeypatch.setattr(meter_app.QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    assert m.save_excel(auto=False)
    data = openpyxl.load_workbook(path)["Data"]
    times = [data.cell(row=r, column=1).value for r in range(2, 7)]
    assert times == sorted(times) and isinstance(times[0], datetime.datetime)


def test_autosave_and_autostop_run_without_readings(window):
    m = window.meter1
    _record(m)
    m.combo_autosave.setCurrentText("Every X Minutes")
    m._last_autosave -= 3600  # an hour since the last save, and no reading since
    m.combo_autostop.setCurrentText("Stop Meter")
    m.ent_autostop_min.setText("1")
    m.session_start_time = datetime.datetime.now() - datetime.timedelta(minutes=2)
    m._housekeeping()
    m.wait_for_background_save()
    assert not m.is_recording
    assert "Auto-Stop" in events(m)
    assert wait_until(lambda: any(f.endswith(".xlsx") for f in os.listdir(window.tmp / "Data")))


def test_save_and_close(window, monkeypatch):
    m = window.meter1
    _record(m)
    monkeypatch.setattr(meter_app.MeterApp, "_close_choice", lambda self, lines: "save")
    window.close()
    assert [f for f in os.listdir(window.tmp / "Data") if f.endswith(".xlsx")]
    assert not m.unsaved_changes


def test_cancel_keeps_the_window_open(window, monkeypatch):
    _record(window.meter1)
    monkeypatch.setattr(meter_app.MeterApp, "_close_choice", lambda self, lines: "cancel")
    window.show()
    assert window.close() is False
    monkeypatch.setattr(meter_app.MeterApp, "_close_choice", lambda self, lines: "close")


# --- memory, alerts, settings ----------------------------------------------------------------
def test_memory_download_saves_the_stored_readings(window):
    m = window.meter1
    ser = connect(m)
    m.download_memory()
    assert meter_protocol.DOWNLOAD_MEMORY in ser.written
    assert not m.btn_download.isEnabled()
    for s in range(3):
        ser.feed(packet(kind=b"DS", when=datetime.datetime(2026, 10, 1, 8, 0, s)))
    assert wait_until(lambda: len(m._memory or []) == 3)
    m._memory_last -= 5  # the meter has gone quiet
    m._check_memory_download()
    assert m._memory is None and m.btn_download.isEnabled()
    files = [f for f in os.listdir(window.tmp / "Data") if "Memory" in f]
    assert len(files) == 1
    ws = openpyxl.load_workbook(window.tmp / "Data" / files[0])["Meter Memory"]
    assert ws.max_row == 4 and ws["A2"].value == datetime.datetime(2026, 10, 1, 8, 0, 0)
    assert m.records == []  # memory readings don't go into the live log


def test_memory_download_with_nothing_stored_finishes(window):
    m = window.meter1
    connect(m)
    m.download_memory()
    m._memory_started -= 11
    m._check_memory_download()
    assert m._memory is None and m.btn_download.isEnabled()
    assert "no stored readings" in events(m)


def test_no_readings_alert(window):
    m = window.meter1
    connect(m)
    m.is_recording = True
    m.last_data_time = time.time() - 120
    m._check_stall()
    assert "No reading" in window.status_m1.text()
    assert any("no reading for" in a for a in window.alerts)
    m.last_data_time = time.time()
    m._check_stall()
    assert any("arriving again" in a for a in window.alerts)


def test_settings_tab_saves_without_a_button(window):
    tab = window.settings_tab
    tab.meter_fields[2]["web"].setText("https://discord.invalid/2")
    tab.meter_fields[2]["stall"].setValue(90)
    window.save_shared_config(now=True)
    with open(window.config_path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["alert_webhook_2"] == "https://discord.invalid/2"
    assert saved["stall_alert_s_2"] == 90
    assert saved["default_save_location_2"] == ""  # "Not Set" isn't saved as a folder name


def test_unreadable_config_is_recovered_with_a_warning(monkeypatch, tmp_path):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, title, text, *a, **k: shown.append(text))
    path = os.path.join(meter_app.CONFIG_DIR, "config.json")
    os.makedirs(meter_app.CONFIG_DIR, exist_ok=True)
    recording.write_json_atomic(path, {"alert_webhook_1": "https://discord.invalid/kept"})
    recording.write_json_atomic(path, {"alert_webhook_1": "https://discord.invalid/kept", "n": 1})
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"alert_webhook_1": "https://disc')  # power cut mid-write
    app = QApplication.instance() or QApplication([])
    win = meter_app.MeterApp()
    try:
        assert win.shared_config["alert_webhook_1"] == "https://discord.invalid/kept"
        win._warn_config_problem()
        assert shown and "last good copy" in shown[0]
    finally:
        win.close()
        app.processEvents()
        meter_app.plt.close("all")
        for suffix in ("", ".bak", ".unreadable"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def test_calibration_burst_button_updates_from_the_serial_thread(window):
    window.meter1.signals.burst_started.emit()
    QApplication.instance().processEvents()
    assert window.btn_calibration_burst.text() == "Recording Burst..."


def test_missing_streams_ignores_an_unused_meter_and_camera(window, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, title, text, *a, **k: shown.append(text))
    window.meter1.is_connected = window.meter1.is_recording = True
    window._check_streams_active()
    assert shown == []  # Meter 2 unplugged and not recording = not in use
    window.ocr_pipeline.rois = {"m1_cond": [1, 2, 3, 4]}
    window._check_streams_active()
    assert shown and "OCR Camera" in shown[0] and "Meter 2" not in shown[0]
    window.meter1.is_recording = window.meter1.is_connected = False
