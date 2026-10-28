@echo off
rem Double-click launcher for Windows. Starts the app without leaving a console window open.
rem The first time (no .venv folder yet) it sets the app up first, the same way Setup_Windows.bat does.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto firstrun

:launch
set "PY=python"
set "PYW=pythonw"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if exist ".venv\Scripts\pythonw.exe" set "PYW=.venv\Scripts\pythonw.exe"

rem Quick check here, so missing packages show an error instead of nothing happening.
"%PY%" -c "import PyQt6.QtWidgets, serial, matplotlib, qdarktheme" || goto failed

rem Start the app in the background; this window closes. Later errors go to app.log in the data folder.
start "" "%PYW%" meter_app.py
exit /b 0

:failed
echo.
echo The app could not start. Copy the messages above when asking for help.
echo To install what it needs, double-click Setup_Windows.bat.
pause
exit /b 1

rem --- First run: create .venv and install the app (same steps as Setup_Windows.bat) ---
:firstrun
echo Setting up Apera Meter GUI for the first time in %CD%
echo This takes a minute or two. The app starts when it's done.
echo.
echo Creating .venv ...
py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
if not exist ".venv\Scripts\python.exe" goto nopython
set "PY=.venv\Scripts\python.exe"
"%PY%" -c "import sys; sys.exit(sys.version_info < (3, 11))" || goto oldpython
"%PY%" -m pip install --upgrade pip || goto setupfailed
"%PY%" -m pip install -e . || goto setupfailed

set "ANSWER="
set /p ANSWER="Also install the camera OCR (PyTorch, a 1-2 GB download)? [y/N] "
if /i "%ANSWER%"=="y" "%PY%" -m pip install -e ".[ocr]" || goto setupfailed
echo.
echo Setup finished. Starting Apera Meter GUI...
goto launch

:nopython
echo.
echo Python 3.11 or newer isn't installed, or isn't on PATH.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this again.
pause
exit /b 1

:oldpython
echo.
echo The .venv folder uses a Python older than 3.11. Delete the .venv folder, install a newer
echo Python from https://www.python.org/downloads/, then run this again.
pause
exit /b 1

:setupfailed
echo.
echo Setup didn't finish. Copy the messages above when asking for help.
echo To try again, double-click Setup_Windows.bat.
pause
exit /b 1
