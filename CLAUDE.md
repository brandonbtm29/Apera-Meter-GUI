# Apera Electrical Conductivity Meter Controller

Dual-meter PyQt6 app for Apera EC400S / EC8500 conductivity meters (serial auto-discovery, live plots, auto memory dumps, Excel export, session recovery), plus a CRNN OCR model that reads the meter's 7-segment LCD from a camera feed. Part of Brandon's UAkron lab workspace (shared conventions in `../CLAUDE.md` when the repos are cloned side by side in `GitHub Projects/UAkron/`). GitHub: `brandonbtm29/Apera-Meter-GUI`.

## Run / build / test
- Install: `pip install -e ".[ocr,dev]"` (or `pip install -r requirements.txt`, which installs `-e .[ocr]`). Dependencies live in `pyproject.toml`; `ocr` = PyTorch/torchvision/Pillow, `dev` = pytest/ruff/PyInstaller.
- Run: `python meter_app.py`
- Test: `pytest` (headless; `tests/conftest.py` points the data and settings folders at a temp dir before `meter_app` is imported, because importing it `chdir`s into the data folder).
- Lint/format: `ruff check . && ruff format --check .`. `settings_location.py` is excluded from ruff so it stays byte-identical to the other UAkron repos. Don't enable import sorting (torch must load before PyQt6 on Windows).
- Version: `__version__` in `meter_app.py` is the only place; update `CHANGELOG.md` with every user-facing change. Release steps are in the README.
- Build: `python Scripts/build_app.py` → `dist/Apera Meter GUI/` (PyInstaller folder app with the model and icon) + zip. `--self-test` on the built exe checks it starts and loads the model. Publishing a GitHub release runs `.github/workflows/release.yml`, which builds on Windows and attaches the zip.
- Use a per-machine virtualenv: `python -m venv .venv` in this folder (gitignored). Never sync or commit it.
- `meter_ocr_model.pt` is stored with **Git LFS** (`.gitattributes`). Run `git lfs install` on each machine before cloning. CI deliberately doesn't fetch it.
- Commits: author email `208085582+brandonbtm29@users.noreply.github.com`; end AI-assisted commits with a `Co-Authored-By:` trailer. CI (`.github/workflows/ci.yml`) must stay green.

## Code map
- `meter_app.py` (~3,700 lines; `main()`/`create_app()` at the bottom): `MeterController` (per-meter tab and serial), `SettingsTab`, `OCRFeedTab` (camera + ROI via `InteractiveImageLabel`, ROIs saved in `global_rois.json` in the data folder), `MeterApp` (main window).
- `ocr_model.py`: `MeterCRNN`, the deployed model definition. A copy lives in `LCD-Digit-CRNN`; keep the two in sync if the architecture changes.
- `ocr_pipeline.py`: preprocessing and inference; loads `meter_ocr_model.pt` from this folder via `__file__`.
- `dataset_curation_tab.py`: labelling UI; writes crops to `Sessions/Master_Dataset/` in the data folder (the training set for LCD-Digit-CRNN).
- `shortcuts.py`: Settings → Create Desktop Shortcut (Windows .lnk via PowerShell/WScript.Shell, macOS .app via osacompile + sips/iconutil icon, Linux .desktop; no Qt). An existing shortcut is refreshed at start (`_start_background_checks`).
- `updater.py`: GitHub release check and git-clone update behind Settings → Check for Updates (no Qt; tested in `tests/test_updater.py`).
- `Scripts/`: `build_app.py`, `cleanup_1000x_gaps.py`, `make_screenshot.py` (renders `docs/screenshot.png` with fake data), `preview.py`, `test.py` (app helpers only).
- `tests/`: headless pytest smoke tests.

## Public repo hygiene
- This repo is public. History was rewritten on 10 Oct 2026 to drop the OCR research data and scripts (now in the private `LCD-Digit-CRNN`), the vendor PC Link software, an old remote-training script with a hard-coded login, and `global_rois.json`. It was rewritten again on 14 Oct 2026 to map every author to the GitHub noreply address and remove home-folder paths and machine names. Machine-specific notes (backup locations, local paths) go in `CLAUDE.local.md`, which is gitignored.
- Apera's PC Link software, drivers and the EC400S manual are in SyncThing: `Electrical Conductivity Meter/Reference/Vendor Software/`. Don't commit vendor binaries, manuals, data or credentials here.

## OCR research
Training, the DoE sweeps, the adversarial benchmark and error profiling moved to the private repo `brandonbtm29/LCD-Digit-CRNN` (`../LCD-Digit-CRNN`) on 10 Oct 2026, with their file history. Read its `CLAUDE.md` for research status. To deploy a new model, copy the trained checkpoint over `meter_ocr_model.pt` here (Git LFS) and commit it.

## Where code and data live
- **Code** (this repo) is cloned outside SyncThing, e.g. `~/GitHub Projects/UAkron/Apera-Meter-GUI`. It syncs through GitHub only.
- **Instrument data** stays in SyncThing, by default `~/SyncThing/UAkron Playground/Electrical Conductivity Meter/` (when `~/SyncThing/UAkron Playground` doesn't exist, `~/Documents/Apera Meter Data`). Never copy data into the repo, and never commit the settings files listed below.
- **How it works:** at startup the app `chdir`s into its SyncThing data folder (`DATA_DIR`, override with `UAKRON_APP_DATA_DIR` or `UAKRON_DATA_DIR`). Every relative data path (`Data/`, `Data Backups/`, `Sessions/`, run logs) therefore resolves inside SyncThing, and the default save folder is `Data` there. Files that ship with the code (icons, the OCR model) must be loaded via `script_dir` / `__file__`, never by bare relative name. Run the app from this clone.
- **Settings folder (since 10 Oct 2026):** the settings files (`config.json`, `global_rois.json`) are read from `CONFIG_DIR`, resolved by `settings_location.py` (identical copy in every UAkron repo). Default: this repo folder (gitignored), so a fresh clone starts from the app's defaults. The per-machine pointer `settings_location.json` (gitignored), set from the app's "Settings Folder" box, can move them; a relative path is relative to the data folder. Brandon's machines point at `Settings`, i.e. `UAkron Playground/<App>/Settings/` in SyncThing, so settings stay identical across computers (including `secrets.json`; never print it). `UAKRON_SETTINGS_DIR` overrides the pointer. Store save folders relative to the data folder (e.g. `Data`), not as absolute paths.

## Launch
Double-click `Launch_Windows.bat` / `Launch_Mac.command`. They run a quick import check (errors stay visible), then start the app detached (pythonw / new session) and close their window; output then goes to `<data folder>/app.log` (`_log_to_file_if_needed`). Tests set `APERA_NO_BACKGROUND_CHECKS=1` so no network update check or shortcut refresh runs. They use this repo's `.venv` (create it with `python -m venv .venv` and `pip install -r requirements.txt`). If Windows Smart App Control blocks a DLL import ("blocked by an Application Control policy"), retry once before investigating.
