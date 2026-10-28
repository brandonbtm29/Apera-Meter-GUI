# Changelog

All notable changes to Apera Meter GUI. Versions follow [Semantic Versioning](https://semver.org/):
new features bump the middle number, fixes bump the last, and changes that break existing setups
bump the first. The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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

[Unreleased]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v0.1.0...v1.1.0
[0.1.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/releases/tag/v0.1.0
