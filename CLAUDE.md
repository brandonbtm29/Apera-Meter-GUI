# Apera Electrical Conductivity Meter Controller

Dual-meter PyQt6 app for Apera EC400S / EC8500 conductivity meters (serial auto-discovery, live plots, auto memory dumps, Excel export, session recovery), plus a CRNN OCR model that reads the meter's 7-segment LCD from a camera feed. Part of Brandon's UAkron lab workspace (shared conventions in `../CLAUDE.md` when the repos are cloned side by side in `GitHub Projects/UAkron/`). GitHub: `brandonbtm29/Apera-Meter-GUI`.

## Run / build
- Install: `pip install -r requirements.txt` (the OCR/training scripts also need PyTorch/torchvision and OpenCV, which aren't listed there yet)
- Run: `python meter_app.py`
- Build: `python Scripts/build_app.py` → `dist/Apera_Meter_GUI`
- Use a per-machine virtualenv: `python -m venv .venv` in this folder (gitignored). Never sync or commit it.
- `meter_ocr_model.pt` is stored with **Git LFS** (`.gitattributes`). Run `git lfs install` on each machine before cloning.

## Code map
- `meter_app.py` (~3,200 lines): `MeterController` (per-meter tab and serial), `SettingsTab`, `OCRFeedTab` (camera + ROI via `InteractiveImageLabel`, ROIs saved in `global_rois.json`), `MeterApp` (main window).
- `ocr_model.py`: `ConfigurableCRNN` (ResNet-18/34 backbone, optional LSTM, heavy downsampling, dropout).
- `ocr_pipeline.py`: preprocessing and inference. `dataset_curation_tab.py`: labelling UI.
- `Scripts/`: `train_model.py`, `test_model.py`, build and helper scripts.
- `Algorithm_Comparison/`: DoE training/evaluation scripts and outputs (`train_combinatorial.py`, `generate_adversarial_benchmark*.py`, `run_doe.py`, `evaluate_*.py`, `plot_*.py`).
- Loose `check_errors*.py`, `plot_*phase3.py`, `analyze_all_errors_phase3.py` at the root are analysis one-offs. Prefer adding new ones under `Algorithm_Comparison/`.

## OCR research status (from Antigravity, Aug–Sep 2026)
- Baselines compared: OpenCV and Tesseract vs CRNN. CRNN wins clearly. Errors are classified as dropped digit, partial match or total mismatch.
- First sweep: 32 configurations (R18/R34 × LSTM × heavy vs standard downsampling × curriculum vs unfrozen × dropout), 80 epochs each. Every one reached ~0.99 clean accuracy, but **adversarial robustness was poor** (best: R34 + LSTM + heavy downsampling + unfrozen + dropout, 4.6%). Heavy downsampling clearly helped.
- 2^5 factorial analysis of that sweep: heavy downsampling had the largest main effect (about +0.8% accuracy); ResNet-34 vs 18 barely mattered, so the bottleneck isn't backbone depth; the top interaction was LSTM × curriculum: an LSTM *hurts* without a frozen-backbone warm-up and helps a lot with one. An "Extra Digit" error category was added.
- DoE Phase 2 plan: build a fixed adversarial benchmark (50,000 images at 256×64 from 1,584 raw crops; blur, glare/cutout, perspective and contrast jitter at 3 severity levels each, labels in filenames). Lock LSTM and heavy downsampling on, then sweep backbone {R18, R34} × epochs {50, 75, 100} × unfreeze epoch {0, 25, 50} × dropout {0.1, 0.3, 0.5}.
- Phase 3 scripts exist (`plot_doe_phase3.py`, `run_drop0_expansion.bat`). Check `Algorithm_Comparison/` outputs for the latest results before assuming anything.
- `../Literature Reviews/report.md` argues the approach (heavy downsampling, frozen backbone against glare, custom LCD adversarial benchmark) is publishable.
- Long training runs take hours. Confirm before starting one, and don't run them in the background without saying so.

## Where code and data live
- **Code** (this repo) is cloned outside SyncThing: `C:\Users\<you>\GitHub Projects\UAkron\Apera-Meter-GUI` on Windows, `~/GitHub Projects/UAkron/Apera-Meter-GUI` on the Mac. It syncs through GitHub only.
- **Instrument data** stays in SyncThing: `C:\Users\<you>\SyncThing\UAkron Playground\Electrical Conductivity Meter\` (Mac: `~/SyncThing/UAkron Playground/Electrical Conductivity Meter/`). Never copy data, `config.json` or `secrets.json` into the repo.
- **Transition status (Oct 2026):** the app still writes its data relative to the folder it's launched from, so lab runs still use the copy inside SyncThing. Planned change: add a per-machine "data folder" setting (stored in the gitignored config) that defaults to the SyncThing path above. Once that lands, run the app from this clone and keep only data in SyncThing.
