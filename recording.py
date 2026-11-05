"""Files the app writes while recording: settings, per-computer state, crash backups and Excel.

No Qt here, so all of it can be tested on its own (tests/test_recording.py).

* **Settings** (``config.json``) are written atomically: a crash or power cut mid-write leaves the
  previous file, and the previous version is kept as ``config.json.bak``. A file that can't be read
  is moved aside (``.unreadable``) instead of being silently replaced by the defaults.
* **Per-computer state** (``local_state.json`` in the app folder, never synced or committed): the
  meters' COM ports and the window layout, which differ between the lab PC and a Mac.
* **Backups**: one CSV per recording session and computer, in
  ``Data Backups/<computer>/Meter<N>_<start>.csv``; Recover Session reads them back.
* **Excel**: rows in time order, real date-times and blank cells for missing values.
"""

import csv
import datetime
import glob
import json
import logging
import os
import platform
import re
import shutil
import time

log = logging.getLogger("apera_meter")

COLUMNS = (
    "DateTime",
    "Duration (min)",
    "Mode",
    "Cond (uS/cm)",
    "Cond (mS/cm)",
    "Temp (°C)",
    "OCR Conf (%)",
    "Meter Clock",
)
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LEGACY_BACKUP = "Apera_Meter{meter_id}_Backup.csv"  # one file per meter, before 1.5.0


def computer_folder_name():
    """This computer's name, safe as a folder name (e.g. "Lab-PC").

    Keeps the files each computer writes (logs, backups) apart in the shared SyncThing folder.
    """
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", platform.node().split(".")[0]).strip("-.")
    return name[:40] or "this-computer"


# --- JSON files written safely -----------------------------------------------------------------
def replace_file(src, dst, attempts=10, wait=0.1):
    """os.replace, retried for about a second when Windows says "Access is denied".

    SyncThing, antivirus or the search indexer can hold a file open for a moment.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait)


_last_written = {}  # path -> text last written by write_json_atomic


def write_json_atomic(path, data, keep_backup=True):
    """Write JSON so a crash mid-write can't leave half a file. Returns False if nothing changed.

    The previous version is kept as ``<path>.bak`` (once per write that changes something).
    """
    text = json.dumps(data, indent=4, ensure_ascii=False)
    if _last_written.get(path) == text and os.path.exists(path):
        return False
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if keep_backup and os.path.exists(path):
        try:
            shutil.copy2(path, f"{path}.bak")
        except OSError:
            log.warning("Couldn't keep a backup copy of %s", path, exc_info=True)
    replace_file(tmp, path)
    _last_written[path] = text
    return True


def _read_json_dict(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{os.path.basename(path)} doesn't hold a settings object")
    return data


def load_json(path):
    """Read a settings file: returns (dict, problem).

    ``problem`` is None when the file was fine or doesn't exist yet. Otherwise it explains what
    happened, for a warning: the unreadable file is moved to ``<path>.unreadable`` (so saving the
    defaults can't destroy it) and the last good copy (``.bak``) is used if there is one.
    """
    if not os.path.exists(path):
        return {}, None
    try:
        data = _read_json_dict(path)
        _last_written[path] = json.dumps(data, indent=4, ensure_ascii=False)
        return data, None
    except (OSError, ValueError) as e:
        log.error("Couldn't read %s: %s", path, e)
        name = os.path.basename(path)
        kept = f"{path}.unreadable"
        try:
            replace_file(path, kept)
            kept_note = f"The unreadable file was kept as {name}.unreadable."
        except OSError:
            kept_note = ""
        backup = f"{path}.bak"
        if os.path.exists(backup):
            try:
                data = _read_json_dict(backup)
                log.warning("Using the last good copy of the settings (%s)", backup)
                return data, (
                    f"{name} couldn't be read ({e}), so the last good copy ({name}.bak) was used. {kept_note}"
                ).strip()
            except (OSError, ValueError):
                log.error("The backup %s can't be read either", backup)
        return {}, (
            f"{name} couldn't be read ({e}), so the app started with default settings. {kept_note}"
        ).strip()


def load_local_state(path):
    """This computer's own state ({} if missing or unreadable)."""
    try:
        return _read_json_dict(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.warning("Couldn't read %s; starting without it", path, exc_info=True)
        return {}


def save_local_state(path, state):
    try:
        write_json_atomic(path, state, keep_backup=False)
    except OSError:
        log.warning("Couldn't save %s", path, exc_info=True)


def pick_port(here, shared, available):
    """The port to preselect for a meter: this computer's last port, else the shared one.

    A port that isn't on this computer is never swapped for another (that used to connect to
    whatever port came first). Returns None when neither is here.
    """
    for port in (here, shared):
        if port and port in available:
            return port
    return None


# --- Crash-recovery backups --------------------------------------------------------------------
def new_backup_path(backup_root, meter_id, start, computer=None):
    computer = computer or computer_folder_name()
    folder = os.path.join(backup_root, computer)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"Meter{meter_id}_{start:%Y-%m-%d_%H%M%S}.csv")
    n = 2
    while os.path.exists(path):  # two sessions started in the same second
        path = os.path.join(folder, f"Meter{meter_id}_{start:%Y-%m-%d_%H%M%S}_{n}.csv")
        n += 1
    return path


def append_backup_row(path, row, columns=COLUMNS):
    is_new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(columns)
        writer.writerow(row)


def backup_files(backup_root, meter_id):
    """All backups for this meter: every computer's session files, then the old single file."""
    files = sorted(glob.glob(os.path.join(backup_root, "*", f"Meter{meter_id}_*.csv")))
    legacy = os.path.join(backup_root, LEGACY_BACKUP.format(meter_id=meter_id))
    if os.path.exists(legacy):
        files.append(legacy)
    return files


def normalize_row(row):
    """A backup row in today's 8 columns (older backups had 6 or 7)."""
    row = list(row)
    if len(row) == 6:  # DateTime, hours, uS, mS, temp, conf (before the Mode column)
        try:
            duration = f"{float(row[1]) * 60:.2f}"
        except ValueError:
            duration = "0.00"
        row = [row[0], duration, "Cond", row[2], row[3], row[4], row[5]]
    if len(row) < 7:
        return None
    row = row[: len(COLUMNS)]
    row += [""] * (len(COLUMNS) - len(row))
    return row


def _read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        return header, list(reader)


def scan_sessions(paths, gap_minutes=30.0, computer=None):
    """Recording sessions in these backup files, split where readings are gap_minutes apart.

    Each session: path, start_idx/end_idx (rows in that file), start_ts, end_ts, points and
    computer (the folder it came from; "" for the old single file).
    """
    computer = computer or computer_folder_name()
    sessions = []
    for path in paths:
        try:
            _, rows = _read_rows(path)
        except (OSError, csv.Error, UnicodeDecodeError):
            log.warning("Couldn't read backup %s", path, exc_info=True)
            continue
        parent = os.path.basename(os.path.dirname(path))
        source = "" if os.path.basename(path).startswith("Apera_Meter") else parent
        current, last = None, None
        for idx, row in enumerate(rows):
            if len(row) < 6:
                continue
            try:
                ts = datetime.datetime.strptime(row[0], TIME_FORMAT)
            except ValueError:
                continue
            if current is None or (last is not None and (ts - last).total_seconds() / 60.0 >= gap_minutes):
                if current:
                    sessions.append(current)
                current = {
                    "path": path,
                    "start_idx": idx,
                    "end_idx": idx,
                    "start_ts": row[0],
                    "end_ts": row[0],
                    "points": 0,
                    "computer": source,
                    "this_computer": source == computer,
                    "rows_in_file": len(rows),
                }
            current["end_idx"] = idx
            current["end_ts"] = row[0]
            current["points"] += 1
            last = ts
        if current:
            sessions.append(current)
    sessions.sort(key=lambda s: s["start_ts"])
    return sessions


def read_session_rows(session):
    """The session's rows, in today's 8 columns."""
    _, rows = _read_rows(session["path"])
    out = []
    for row in rows[session["start_idx"] : session["end_idx"] + 1]:
        norm = normalize_row(row)
        if norm is not None:
            out.append(norm)
    return out


def delete_session(session):
    """Remove a session's rows from its backup file (and the file, once it's empty)."""
    header, rows = _read_rows(session["path"])
    del rows[session["start_idx"] : session["end_idx"] + 1]
    if not rows:
        os.remove(session["path"])
        return
    tmp = session["path"] + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if header:
            writer.writerow(header)
        writer.writerows(rows)
    replace_file(tmp, session["path"])


# --- Excel -------------------------------------------------------------------------------------
def _number(text):
    if text is None:
        return None
    text = str(text).strip()
    if text in ("", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def export_rows(records):
    """Rows for Excel: in time order, DateTime as a real date-time, numbers as numbers.

    Values the app logs as 0.0 because the camera couldn't read the display become blank cells
    (the Mode column still says OCR), so they don't break the chart's log axis or an average.
    """
    out = []
    for i, rec in enumerate(records):
        rec = list(rec) + [""] * (len(COLUMNS) - len(rec))
        try:
            when = datetime.datetime.strptime(rec[0], TIME_FORMAT)
        except (TypeError, ValueError):
            when = None
        mode = rec[2]
        us, ms = _number(rec[3]), _number(rec[4])
        if mode == "OCR" and not ms:
            us = ms = None
        meter_clock = rec[7] or None
        row = [when or rec[0], _number(rec[1]), mode, us, ms, _number(rec[5]), _number(rec[6]), meter_clock]
        out.append((when or datetime.datetime.max, i, row))
    out.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in out]


def fallback_path(path, n):
    """'run.xlsx' -> 'run (2).xlsx' for n=2."""
    stem, ext = os.path.splitext(path)
    return f"{stem} ({n}){ext}"


def _save_workbook(wb, path):
    """Save via a temporary file, so a crash never leaves a half-written workbook."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, f"~{os.path.basename(path)}.tmp")
    try:
        wb.save(tmp)
        replace_file(tmp, path, attempts=3)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def save_with_fallback(write, path, attempts=9):
    """Call write(path); if the file is locked (open in Excel), try 'name (2).xlsx' and so on.

    Returns the path actually written. Raises the last error if every name failed.
    """
    try:
        write(path)
        return path
    except PermissionError as first_error:
        log.warning("%s is locked (open in another program?); saving under a new name", path)
        for n in range(2, attempts + 2):
            candidate = fallback_path(path, n)
            if os.path.exists(candidate):
                continue
            try:
                write(candidate)
                return candidate
            except PermissionError:
                continue
        raise first_error


def write_excel(path, records, meter_id, serial_number="", tokens=(), notes="", exported=None):
    """Write the recording to an .xlsx with a Data sheet, a Documentation sheet and a chart."""
    import openpyxl
    from openpyxl.chart import Reference, ScatterChart, Series
    from openpyxl.chart.axis import ChartLines
    from openpyxl.styles import Alignment, Font

    rows = export_rows(records)
    wb = openpyxl.Workbook()
    data_ws = wb.active
    data_ws.title = "Data"
    data_ws.append(list(COLUMNS))
    for row in rows:
        data_ws.append(row)
    for cell in data_ws["A"][1:]:
        if isinstance(cell.value, datetime.datetime):
            cell.number_format = "yyyy-mm-dd hh:mm:ss"
    data_ws.column_dimensions["A"].width = 20
    data_ws.column_dimensions["H"].width = 20
    data_ws.freeze_panes = "A2"

    doc_ws = wb.create_sheet(title="Documentation")
    doc_ws["A1"] = f"Meter {meter_id} - Experiment Documentation"
    doc_ws["A1"].font = Font(size=14, bold=True)
    doc_ws["A3"] = "Export Date:"
    doc_ws["B3"] = (exported or datetime.datetime.now()).strftime(TIME_FORMAT)
    doc_ws["A4"] = "Meter SN:"
    doc_ws["B4"] = serial_number
    doc_ws["A5"] = "Times:"
    doc_ws["B5"] = "DateTime is the computer's clock; Meter Clock is the meter's own clock."
    current_row = 7
    doc_ws.cell(row=current_row, column=1, value="Metadata Tokens").font = Font(bold=True)
    current_row += 1
    doc_ws.cell(row=current_row, column=1, value="Token Name").font = Font(bold=True, italic=True)
    doc_ws.cell(row=current_row, column=2, value="Token Value").font = Font(bold=True, italic=True)
    current_row += 1
    for name, text in tokens:
        doc_ws.cell(row=current_row, column=1, value=name)
        value = _number(text)
        doc_ws.cell(row=current_row, column=2, value=value if value is not None else text)
        current_row += 1
    current_row += 1
    doc_ws.cell(row=current_row, column=1, value="Experiment Notes:").font = Font(bold=True)
    current_row += 1
    doc_ws.cell(row=current_row, column=1, value=notes).alignment = Alignment(wrap_text=True, vertical="top")
    doc_ws.row_dimensions[current_row].height = 100
    doc_ws.column_dimensions["A"].width = 25
    doc_ws.column_dimensions["B"].width = 35

    if rows:
        chart = ScatterChart()
        chart.title = f"Meter {meter_id} Conductivity Over Time"
        chart.style = 13
        chart.x_axis.title = "Duration (min)"
        chart.y_axis.title = "Conductivity (mS/cm)"
        chart.y_axis.scaling.logBase = 10
        chart.x_axis.majorGridlines = ChartLines()
        chart.y_axis.majorGridlines = ChartLines()
        chart.width = 22
        chart.height = 12
        max_row = len(rows) + 1
        xvalues = Reference(data_ws, min_col=2, min_row=2, max_row=max_row)
        values = Reference(data_ws, min_col=5, min_row=1, max_row=max_row)
        chart.series.append(Series(values, xvalues, title_from_data=True))
        wb.create_sheet(title="Graph").add_chart(chart, "A1")

    _save_workbook(wb, path)


def write_memory_excel(path, readings, meter_id, serial_number=""):
    """Save readings downloaded from the meter's memory (meter_protocol.Reading objects)."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Meter Memory"
    ws.append(["Meter Clock", "Mode", "Value", "Unit", "Cond (mS/cm)", "Temp (°C)"])
    for r in readings:
        ws.append(
            [r.meter_time or r.meter_time_text, r.mode_name, r.value, r.unit or "", r.cond_ms, r.temp_c]
        )
    for cell in ws["A"][1:]:
        if isinstance(cell.value, datetime.datetime):
            cell.number_format = "yyyy-mm-dd hh:mm:ss"
    ws.column_dimensions["A"].width = 20
    info = wb.create_sheet("Documentation")
    info["A1"] = f"Meter {meter_id} memory, downloaded {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
    info["A2"] = f"Meter SN: {serial_number}"
    info["A3"] = "Times are the meter's own clock."
    _save_workbook(wb, path)
