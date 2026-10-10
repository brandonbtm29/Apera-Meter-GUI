# Apera Electrical Conductivity Meter Controller

Dual-meter PyQt6 app for Apera EC400S / EC8500 conductivity meters (serial auto-discovery, live plots, auto memory dumps, Excel export, session recovery), plus a CRNN OCR model that reads the meter's 7-segment LCD from a camera feed. Part of Brandon's UAkron lab workspace (shared conventions in `../CLAUDE.md` when the repos are cloned side by side in `GitHub Projects/UAkron/`). GitHub: `brandonbtm29/Apera-Meter-GUI`.

## Run / build
- Install: `pip install -r requirements.txt` (includes PyTorch/torchvision and OpenCV for OCR inference)
- Run: `python meter_app.py`
- Build: `python Scripts/build_app.py` → `dist/Apera_Meter_GUI`
- Use a per-machine virtualenv: `python -m venv .venv` in this folder (gitignored). Never sync or commit it.
- `meter_ocr_model.pt` is stored with **Git LFS** (`.gitattributes`). Run `git lfs install` on each machine before cloning.

## Code map
- `meter_app.py` (~3,200 lines): `MeterController` (per-meter tab and serial), `SettingsTab`, `OCRFeedTab` (camera + ROI via `InteractiveImageLabel`, ROIs saved in `global_rois.json` in the data folder), `MeterApp` (main window).
- `ocr_model.py`: `MeterCRNN`, the deployed model definition. A copy lives in `LCD-Digit-CRNN`; keep the two in sync if the architecture changes.
- `ocr_pipeline.py`: preprocessing and inference; loads `meter_ocr_model.pt` from this folder via `__file__`.
- `dataset_curation_tab.py`: labelling UI; writes crops to `Sessions/Master_Dataset/` in the data folder (the training set for LCD-Digit-CRNN).
- `Scripts/`: `build_app.py`, `cleanup_1000x_gaps.py`, `preview.py`, `test.py` (app helpers only).

## OCR research
Training, the DoE sweeps, the adversarial benchmark and error profiling moved to the private repo `brandonbtm29/LCD-Digit-CRNN` (`../LCD-Digit-CRNN`) on 10 Oct 2026, with their file history. Read its `CLAUDE.md` for research status. To deploy a new model, copy the trained checkpoint over `meter_ocr_model.pt` here (Git LFS) and commit it.

## Where code and data live
- **Code** (this repo) is cloned outside SyncThing: `C:\Users\<you>\GitHub Projects\UAkron\Apera-Meter-GUI` on Windows, `~/GitHub Projects/UAkron/Apera-Meter-GUI` on the Mac. It syncs through GitHub only.
- **Instrument data** stays in SyncThing: `C:\Users\<you>\SyncThing\UAkron Playground\Electrical Conductivity Meter\` (Mac: `~/SyncThing/UAkron Playground/Electrical Conductivity Meter/`). Never copy data into the repo, and never commit the settings files listed below.
- **How it works:** at startup the app `chdir`s into its SyncThing data folder (`DATA_DIR`, override with `UAKRON_APP_DATA_DIR` or `UAKRON_DATA_DIR`). Every relative data path (`Data/`, `Data Backups/`, `Sessions/`, run logs) therefore resolves inside SyncThing, and the default save folder is `Data` there. Files that ship with the code (icons, the OCR model) must be loaded via `script_dir` / `__file__`, never by bare relative name. Run the app from this clone.
- **Settings folder (since 10 Oct 2026):** the settings files (`config.json`, `global_rois.json`) are read from `CONFIG_DIR`, resolved by `settings_location.py` (identical copy in every UAkron repo). Default: this repo folder (gitignored), so a fresh clone starts from the app's defaults. The per-machine pointer `settings_location.json` (gitignored), set from the app's "Settings Folder" box, can move them; a relative path is relative to the data folder. Brandon's machines point at `Settings`, i.e. `UAkron Playground/<App>/Settings/` in SyncThing, so settings stay identical across computers (including `secrets.json`; never print it). `UAKRON_SETTINGS_DIR` overrides the pointer. Store save folders relative to the data folder (e.g. `Data`), not as absolute paths.

## Launch
Double-click `Launch_Windows.bat` / `Launch_Mac.command`. They use this repo's `.venv` (create it with `python -m venv .venv` and `pip install -r requirements.txt`). Windows Smart App Control is on for the lab PC; if a DLL import is "blocked by an Application Control policy", retry once before investigating.
