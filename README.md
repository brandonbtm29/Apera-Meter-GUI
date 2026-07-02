# Apera Electrical Conductivity Meter Controller

A Python-based, dual-meter control center with a graphical user interface (GUI) designed for recording, logging, and plotting data from Apera Electrical Conductivity (EC) meters.

## Supported Models & Modes
This software has been developed and tested to work with the following models:
* **Apera EC400S Series**
* **Apera EC8500 Series**

It specifically monitors and logs **Conductivity** (in µS/cm and mS/cm) and **Temperature** (°C) modes.

## Features
* **Dual-Meter Support:** Run and log two different meters simultaneously in separate tabs.
* **Auto-Discovery:** Automatically scans and connects to Apera meters on available COM ports.
* **Live Plotting:** Real-time graphs using `matplotlib` to monitor Conductivity and Temperature over time.
* **Auto-Dump Memory:** Automatically requests memory dumps from the meter at set hour intervals.
* **Excel Export:** Save your session logs, notes, and records directly to an `.xlsx` file.
* **Emergency Session Recovery:** Automatically backs up data to a temporary CSV file so you can recover data if the app closes unexpectedly.

## Requirements
All dependencies are listed in `requirements.txt`. You can install them via:
```bash
pip install -r requirements.txt
```
Dependencies include:
* `PyQt6` & `pyqtdarktheme` (for the dark-mode modern GUI)
* `pyserial` (for serial communication)
* `matplotlib` (for live graph plotting)
* `openpyxl` (for Excel exporting)
* `pyinstaller` (for building a standalone executable)

## Usage

### Running from Source
Simply run the script or the provided batch file:
```bash
python meter_app.py
```
Or double-click `Launch_GUI.bat` on Windows.

### Building a Standalone Executable
You can build a standalone executable for your operating system using the provided build script:
```bash
python build_app.py
```
This will create a `dist/` directory containing the executable (`Apera_Meter_GUI.exe` on Windows, or `Apera_Meter_GUI.app` on macOS).
