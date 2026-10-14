# Changelog

All notable changes to Apera Meter GUI. Versions follow [Semantic Versioning](https://semver.org/):
while the app is below 1.0, new features bump the middle number and fixes bump the last.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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

[Unreleased]: https://github.com/brandonbtm29/Apera-Meter-GUI/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/brandonbtm29/Apera-Meter-GUI/releases/tag/v0.1.0
