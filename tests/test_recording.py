"""Settings, per-computer state, crash backups and Excel export (recording.py)."""

import datetime
import json
import os

import openpyxl
import pytest

import plot_math
import recording

T0 = datetime.datetime(2026, 11, 4, 9, 0, 0)


def row(minute, ms="1.234", mode="Cond", clock="2026-11-04 09:00:00"):
    when = T0 + datetime.timedelta(minutes=minute)
    us = f"{float(ms) * 1000:.1f}"
    return (when.strftime(recording.TIME_FORMAT), f"{minute:.2f}", mode, us, ms, "22.5", "-", clock)


# --- settings --------------------------------------------------------------------------------
def test_config_written_atomically_with_backup(tmp_path):
    path = str(tmp_path / "config.json")
    assert recording.write_json_atomic(path, {"a": 1})
    assert recording.write_json_atomic(path, {"a": 2})
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {"a": 2}
    assert json.loads((tmp_path / "config.json.bak").read_text(encoding="utf-8")) == {"a": 1}
    assert not (tmp_path / "config.json.tmp").exists()


def test_unchanged_config_is_not_rewritten(tmp_path):
    path = str(tmp_path / "config.json")
    recording.write_json_atomic(path, {"a": 1})
    mtime = os.stat(path).st_mtime_ns
    assert recording.write_json_atomic(path, {"a": 1}) is False
    assert os.stat(path).st_mtime_ns == mtime


def test_missing_config_is_not_a_problem(tmp_path):
    assert recording.load_json(str(tmp_path / "config.json")) == ({}, None)


def test_unreadable_config_uses_the_backup_and_is_kept(tmp_path):
    path = str(tmp_path / "config.json")
    recording.write_json_atomic(path, {"alert_webhook_1": "kept"})
    recording.write_json_atomic(path, {"alert_webhook_1": "kept", "x": 1})
    (tmp_path / "config.json").write_text('{"alert_webhook_1": "kep', encoding="utf-8")  # cut off mid-write
    data, problem = recording.load_json(path)
    assert data == {"alert_webhook_1": "kept"}
    assert "last good copy" in problem
    assert (tmp_path / "config.json.unreadable").exists()


def test_unreadable_config_without_backup_says_so(tmp_path):
    (tmp_path / "config.json").write_text("[1, 2", encoding="utf-8")
    data, problem = recording.load_json(str(tmp_path / "config.json"))
    assert data == {}
    assert "default settings" in problem


def test_pick_port_never_swaps_a_missing_port():
    assert recording.pick_port("COM5", "COM4", ["COM3", "COM5"]) == "COM5"
    assert recording.pick_port(None, "COM4", ["COM3", "COM4"]) == "COM4"
    assert recording.pick_port("COM5", "COM4", ["COM3"]) is None


def test_computer_folder_name_is_safe():
    name = recording.computer_folder_name()
    assert name and all(c.isalnum() or c in "._-" for c in name)


# --- backups ---------------------------------------------------------------------------------
def test_backup_per_session_and_computer(tmp_path):
    root = str(tmp_path / "Data Backups")
    path = recording.new_backup_path(root, 1, T0, computer="Lab-PC")
    assert path.endswith(os.path.join("Lab-PC", "Meter1_2026-11-04_090000.csv"))
    assert recording.new_backup_path(root, 1, T0, computer="Lab-PC") == path  # not created yet
    for m in range(3):
        recording.append_backup_row(path, row(m))
    assert recording.new_backup_path(root, 1, T0, computer="Lab-PC") != path  # same second: new name
    assert recording.backup_files(root, 1) == [path]
    assert recording.backup_files(root, 2) == []


def test_sessions_are_found_split_and_restored(tmp_path):
    root = str(tmp_path / "Data Backups")
    path = recording.new_backup_path(root, 1, T0, computer="Lab-PC")
    for m in (0, 1, 2, 60, 61):  # a 58-minute gap
        recording.append_backup_row(path, row(m))
    sessions = recording.scan_sessions(recording.backup_files(root, 1), gap_minutes=30, computer="Lab-PC")
    assert [s["points"] for s in sessions] == [3, 2]
    assert sessions[0]["this_computer"]
    rows = recording.read_session_rows(sessions[1])
    assert [r[0] for r in rows] == [row(60)[0], row(61)[0]]
    assert len(rows[0]) == len(recording.COLUMNS)

    recording.delete_session(sessions[0])
    left = recording.scan_sessions([path], computer="Lab-PC")
    assert [s["points"] for s in left] == [2]
    recording.delete_session(left[0])
    assert not os.path.exists(path)  # nothing left: the file goes


def test_old_backup_formats_still_recover(tmp_path):
    root = tmp_path / "Data Backups"
    root.mkdir()
    legacy = root / "Apera_Meter1_Backup.csv"
    legacy.write_text(
        "DateTime,Duration (min),Mode,Cond (uS/cm),Cond (mS/cm),Temp (°C),OCR Conf (%)\n"
        "2026-10-01 10:00:00,0.00,Cond,1234.0,1.234,22.5,-\n"
        "2026-10-01 10:00:02,0.5,1234.0,1.234,22.6,-\n",  # the 6-column format (hours)
        encoding="utf-8",
    )
    sessions = recording.scan_sessions(recording.backup_files(str(root), 1), computer="Lab-PC")
    assert len(sessions) == 1 and sessions[0]["computer"] == "" and not sessions[0]["this_computer"]
    rows = recording.read_session_rows(sessions[0])
    assert rows[0][5] == "22.5" and rows[0][7] == ""
    assert rows[1][1] == "30.00" and rows[1][2] == "Cond" and rows[1][5] == "22.6"


# --- Excel -----------------------------------------------------------------------------------
def test_export_is_in_time_order_with_real_datetimes_and_blank_ocr_gaps():
    records = [row(2), row(0), row(1, ms="0.000", mode="OCR")]
    out = recording.export_rows(records)
    assert [r[0] for r in out] == [T0, T0 + datetime.timedelta(minutes=1), T0 + datetime.timedelta(minutes=2)]
    assert isinstance(out[0][0], datetime.datetime)
    assert out[1][2] == "OCR" and out[1][3] is None and out[1][4] is None  # was 0.0
    assert out[0][4] == pytest.approx(1.234) and out[0][5] == pytest.approx(22.5)
    assert out[0][6] is None  # "-"


def test_write_excel(tmp_path):
    path = str(tmp_path / "run.xlsx")
    recording.write_excel(path, [row(1), row(0)], 1, "SN-1", [("Material", "PEI"), ("Run #", "3")], "notes")
    wb = openpyxl.load_workbook(path)
    data = wb["Data"]
    assert [c.value for c in data[1]] == list(recording.COLUMNS)
    assert data["A2"].value == T0 and data["A2"].number_format == "yyyy-mm-dd hh:mm:ss"
    assert data["E3"].value == pytest.approx(1.234)
    doc = wb["Documentation"]
    values = [c.value for r in doc.iter_rows() for c in r if c.value is not None]
    assert "PEI" in values and 3 in values and "notes" in values
    assert "Graph" in wb.sheetnames
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]


def test_locked_file_falls_back_to_a_new_name(tmp_path):
    target = str(tmp_path / "run.xlsx")
    written = []

    def write(path):
        if path == target:
            raise PermissionError(13, "open in Excel", path)
        written.append(path)

    assert recording.save_with_fallback(write, target) == str(tmp_path / "run (2).xlsx")
    assert written == [str(tmp_path / "run (2).xlsx")]


def test_other_save_errors_are_raised(tmp_path):
    def write(path):
        raise OSError(28, "No space left on device")

    with pytest.raises(OSError):
        recording.save_with_fallback(write, str(tmp_path / "run.xlsx"))


# --- graph maths -----------------------------------------------------------------------------
def test_derivative_is_per_minute_whatever_the_sampling_rate():
    for step_s in (2, 10, 60):
        minutes = [i * step_s / 60 for i in range(60)]
        values = [10 ** (0.01 * t) for t in minutes]  # rises 0.01 decade per minute
        d = plot_math.derivative_per_minute(minutes, values, smooth_window=5)
        assert d[5:-5] == pytest.approx([0.01] * (len(d) - 10), rel=1e-6)


def test_derivative_skips_gaps_and_repeated_times():
    d = plot_math.derivative_per_minute([0, 1, 1, 2, 3], [1, 10, 10, 0, 1000], smooth_window=3)
    assert d[3] != d[3]  # NaN for the 0.0 reading
    assert d[1] == pytest.approx(d[2])


def test_thinning_keeps_spikes():
    values = [1.0] * 100_000
    values[54_321] = 50.0
    idx = plot_math.thin_indices(values, max_points=1000)
    assert len(idx) <= 1002
    assert 54_321 in idx and 0 in idx and 99_999 in idx


def test_window_start():
    times = [0, 1, 2, 5, 9, 10]
    assert plot_math.window_start(times, 5) == 3
    assert plot_math.window_start([], 5) == 0
