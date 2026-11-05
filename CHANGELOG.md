# Changelog

All notable changes to Apera Meter GUI. Versions follow [Semantic Versioning](https://semver.org/):
new features bump the middle number, fixes bump the last, and changes that break existing setups
bump the first. The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [1.5.0] - 2026-11-05

Data safety: fixes from the 4 Nov 2026 review. Nothing to do after updating, except clicking
**Connect** once on each computer (see "Changed").

### Fixed
- **USB dropouts during a run**: when the meter's USB connection drops, the tab now closes the port,
  shows **Connect** again and keeps trying to reconnect to the same port (after 2, 5, 10, 20, 30 s,
  then every minute) until the meter is back or you click Connect/Disconnect. Before, the button still
  said Disconnect, the port stayed open and readings just stopped until the app was restarted. With
  Discord alerts on, you get a message when the connection drops and when it's back.
- **Timestamps** come from the computer's clock, not the meter's. A meter whose clock was wrong (e.g.
  after a battery change) gave wrong times, negative durations and serial rows out of step with camera
  rows. The meter's own time is kept in a new **Meter Clock** column, and the event log warns when the
  meter's clock is more than a minute off. **Auto-Sync Time on Connect** is now on for new setups.
- **Auto-save failures are visible**: the status bar turns red, the reason goes to the event log, the
  error log and (with alerts on) Discord. If the file is open in Excel, the data is saved as
  `<name> (2).xlsx` and auto-save carries on there. (They used to go only to `~/autosave_error.log`.)
- **One auto-save file per session**: auto-save and Quick Save overwrite the same file, named once
  at the session's first save. With the default `<Meter ID>_<Date>_<Time>` template every auto-save
  used to make a new copy (about 96 files for an 8 h run). Clear Data starts a new file.
- **No more pinging COM4**: Meter 1 no longer defaults to COM4 and connects to it at start. A port is
  kept only once the meter has answered on it, and it's remembered for this computer only. A port
  that never answers like an Apera meter is let go after 6 s (at most three pings). Auto-Detect only
  tries USB serial ports (not Bluetooth or built-in ports), its timeout grows with the number of
  ports, and a meter found after the timeout no longer connects anyway.
- **Settings can't be lost to a half-written file**: `config.json` is written to a temporary file and
  then swapped in, the previous version is kept as `config.json.bak`, and it's only written when
  something changed (typing used to rewrite it on every key). If it can't be read, the last good copy
  is used, the unreadable file is kept as `config.json.unreadable`, and the app says so (it used to
  start from the defaults without a word).
- Readings in other meter modes (pH, mV, TDS…) are no longer logged as conductivity; the event log says
  which mode the meter is in. A reading in an unknown unit is skipped with a message.
- µS/cm readings keep all their digits in the mS/cm column and the graph (12.3 µS/cm was 0.012 mS/cm).
- **Download Raw Memory** works: it saves the readings stored in the meter to
  `Meter<N>_Memory_<date>.xlsx` in the save folder (whether or not recording is on) and the button
  comes back when it's done. It used to stay greyed out with "Downloading Memory…".
- The graph stays responsive on long runs: a redraw takes about 0.2 s at any length (it was 0.5–1.5 s
  at 3–11 h and growing). Long runs are thinned for drawing, keeping each stretch's highest and lowest
  point. With **Auto Time** off, only the Time Window is drawn instead of everything.
- **d(log C)/dt** is per minute (it was per reading, so it changed with the sampling rate).
- Auto-save and auto-stop run on their own timer, so they also work when readings stop.
- **Excel export** is always in time order (not the table's sort order), DateTime is a real Excel
  date-time, and camera readings the OCR couldn't read are blank cells instead of 0.0, so they don't
  break the chart's log axis or averages.
- **Recover Session** restores temperatures (it read the OCR column, giving 0 °C).
- Resuming after **Pause Recording** keeps the session's start, so durations carry on.
- **Settings tab** changes are saved as you make them (there was a Save All Settings button, and
  changes were lost without it).
- The **Missing Data Streams** warning only lists streams in use: a recording meter without its
  connection, a connected meter that isn't recording, and the camera when a reading area is drawn for
  a recording meter. An unplugged, unused Meter 2 and an unused camera are no longer listed every time.
- The calibration-burst button now updates when a burst starts.

### Added
- **Save and Close**: closing with unsaved readings offers Save and Close, Close Without Saving or
  Cancel (it was a Yes/No question). **Clear Data** asks first when readings aren't saved.
- **No-reading warning** (Settings → Meter N): while recording, the status bar, event log and Discord
  say when no reading has arrived for 60 s (adjustable, 0 = off), and again when readings are back.
  "Also alert when the connection drops, readings stop or a save fails" turns those Discord messages
  off.
- **Download the meter's memory every N hours** (Settings → Meter N, off by default); skipped when the
  memory hasn't changed.
- Copy Diagnostics shows each meter's reconnect state, number of readings and its auto-save and
  backup file names.

### Changed
- **Ports are remembered per computer**, in `local_state.json` next to the app (not synced). After
  updating, click **Connect** once on each computer; from then on the app connects at start by
  itself. A port saved by an older version is only preselected.
- **Backups**: one file per recording session and computer, in
  `Data Backups/<computer>/Meter<N>_<start>.csv`, instead of one ever-growing file per meter that two
  computers could write at once. Recover Session lists this computer's sessions, other computers'
  (marked "from <computer>") and the old `Apera_Meter<N>_Backup.csv`.
- **Logs per computer**: `logs/<computer>/apera_meter.log` and `logs/<computer>/app.log` in the data
  folder, so the lab PC and the Mac no longer write the same synced file. Serial and packet errors now
  go to the error log (and Copy Diagnostics) instead of only `app.log`. The old `logs/apera_meter.log`
  and `app.log` can be deleted.
- The window size and dividers are remembered per computer (`local_state.json`) instead of in the
  shared `config.json`.
- The serial protocol, the files the app writes and the graph's maths moved out of `meter_app.py` into
  `meter_protocol.py`, `recording.py` and `plot_math.py`, with tests (packet decoding, backups and
  recovery, auto-save, export, reconnecting).

### Removed
- `Scripts/preview.py` and `Scripts/test.py` (old manual probes).

## [1.4.2] - 2026-10-29

### Fixed
- Camera OCR: while a meter is recording, a display the OCR can't read (or a reading it turns down) is
  logged as 0.0 in every OCR mode, so the gap shows in the data. Before, only the default Dual-Mode
  setting did this; with Dual-Mode off, with **Discard Bad Reads (Return 0.0)** on, or for a reading
  above **Enforce Absolute Max**, that second was skipped with no row. A 0.0 is never used as the last
  good reading.

## [1.4.1] - 2026-10-29

### Fixed
- Camera OCR: Meter 2's camera reading no longer depends on a Meter 1 reading being present.
- Camera OCR: the saved confidence threshold and smoothing setting now apply as soon as the app
  starts, not only after changing one of them.

## [1.4.0] - 2026-10-28

### Added
- **Error log**: errors, with tracebacks (also from background threads), go to `logs/apera_meter.log` in
  the data folder. An unexpected error is shown in a dialog and the app keeps running instead of closing.
- `Setup_Windows.bat` / `Setup_Mac.command`: one double-click creates the `.venv` and installs everything
  (asks whether to add the camera OCR). Use them to repair or reinstall; a first install no longer needs
  them (see the next point).
- `Launch_Windows.bat` and `Launch_Mac.command` set the app up by themselves the first time: they create
  the `.venv`, install what the app needs (asking whether to add the camera OCR), then start the app. It
  takes a minute or two; after that they start the app straight away. If setup fails, the window stays
  open and shows why.
- Only one copy of the app runs at a time, so two copies can't fight over a meter's COM port. Opening it
  again brings the open window to the front. Restarting after an update or a settings-folder change
  still works.
- **Open Log Folder** and **Copy Diagnostics** buttons in **Settings → Application Preferences**. Copy
  Diagnostics copies the app version, its folders, the meter connections and the end of the error log,
  ready to paste into a GitHub issue or an email. Home-folder names and anything that looks like a
  password or token are left out.
- A **Check for updates when the app starts** checkbox in the same box (on by default). Turning it off
  also stops the daily check; **Check for Updates** still works by hand.

## [1.3.0] - 2026-10-15

### Added
- The version number is shown in the bottom-right corner. A few seconds after start (and once a
  day while the app runs) it checks GitHub; if a newer release is out, it turns amber and reads
  "v1.3.0 – v1.4.0 available". Click it to see what changed and update.
- When the app has no console (launchers, desktop shortcut, standalone app), its messages and
  errors are written to `app.log` in the data folder.

### Changed
- `Launch_Windows.bat` and `Launch_Mac.command` no longer leave a console or Terminal window open
  while the app runs. They still show an error and wait if something is missing.
- The macOS desktop shortcut is now an `Apera Meter GUI.app` with the meter icon that starts the
  app without Terminal (it replaces the `.command` file from 1.2.0).
- An existing desktop shortcut is refreshed when the app starts, so its icon and target stay
  current after updates or if the app folder moves. The app never creates one on its own.

## [1.2.0] - 2026-10-15

### Added
- **Create Desktop Shortcut** button on the Settings tab, next to Check for Updates. On Windows it
  makes a shortcut with the meter icon (no console window, and it finds OneDrive desktops); on a
  Mac it puts an `Apera Meter GUI.command` launcher on the desktop. The shortcut keeps working
  after updates; pressing the button again replaces it.

## [1.1.0] - 2026-10-14

The app is now available as a standalone Windows download and can update itself. (Version
numbers jump from 0.1.0 to 1.1.0 to reflect that the app is a finished, everyday tool.)

### Added
- **Standalone Windows app**: each release has a zip with the app, Python and the OCR model
  included. No Python install needed.
- **Check for Updates** on the Settings tab (replaces **Restart Application**): shows the new
  release's notes and, for a git clone, updates and restarts in one click. Local edits are never
  overwritten.
- The version number is shown on the Settings tab and in the window title.
- Python 3.14 support.

### Changed
- On computers without the lab's SyncThing folder, data now goes to `~/Documents/Apera Meter Data`
  instead of creating `~/SyncThing/UAkron Playground/...`. Lab computers are unaffected.
- The dark theme now uses the maintained `pyqtdarktheme-fork` (same look).
- Interface polish: section titles show "&" instead of "_", the X-axis and unit menus are no
  longer clipped, the DateTime column fits a full timestamp, the d(log C)/dt label no longer
  overlaps the conductivity axis, roomier tabs, and "General" instead of "Global & Application
  Settings".

### Fixed
- Turning the camera on with no camera attached showed "Camera: Feed Active"; it now says
  "Camera: Not found".
- The Discord test alert showed a literal `\n` instead of a line break.
- Closing the app with Ctrl+C or during shutdown is no longer swallowed by catch-all error
  handlers.

## [0.1.0] - 2026-10-14

First tracked release. The app had been in daily lab use since mid-2026; this release adds
documentation, tests and CI around it.

Earlier versions were co-developed with Google Gemini in Antigravity; development continues
with Claude (Anthropic).

### Features at this release
- Two Apera EC400S / EC8500 meters side by side, with automatic COM-port discovery.
- Live conductivity, rate-of-change and temperature graphs.
- Excel export with notes and a chart, file names built from tokens, auto-save and auto-stop.
- Meter memory download, by hand or on a timer, and meter clock sync.
- Crash recovery from a per-meter backup CSV.
- Optional Discord alerts on sharp conductivity changes.
- Optional camera reading of the meter display (CRNN model) and a tab for labelling training
  images.
- Settings can live in a synced folder shared by several computers.

### Fixed
- The app no longer crashes on startup with `UnboundLocalError: ... 'QTimer'` (introduced
  2 October 2026).

### Added
- README with screenshot, setup steps, file locations and limitations.
- `pyproject.toml` with an `ocr` extra (PyTorch) and a `dev` extra; the version is defined once
  in `meter_app.py`.
- Headless smoke tests, ruff lint/format, and CI on Windows and Linux (Python 3.11–3.13) with a
  secret scan of the full history.
- `SECURITY.md`, this changelog, and `Scripts/make_screenshot.py`.

### Changed
- `requirements.txt` now installs the project with the OCR extra.
- Line endings are pinned in `.gitattributes`, so Windows clones no longer show whole-file
  changes.

### Removed
- `seaborn` from the dependencies (it was never used).

### Security
- Personal email addresses, home-folder paths and machine names were removed from the files and
  the git history. Commit IDs before this release changed as a result.

[Unreleased]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v1.4.2...HEAD
[1.4.2]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v0.1.0...v1.1.0
[0.1.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/releases/tag/v0.1.0
