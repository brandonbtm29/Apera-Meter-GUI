@echo off
rem Repair or reinstall Apera Meter GUI: creates this folder's own Python environment (.venv) if
rem needed and (re)installs everything the app needs. Launch_Windows.bat does this by itself the
rem first time, so you only need this to repair or update the packages, or to add the camera OCR.
cd /d "%~dp0"
echo Setting up Apera Meter GUI in %CD%
echo.
if exist ".venv\Scripts\python.exe" (
  echo Using the existing .venv folder.
) else (
  echo Creating .venv ...
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" goto nopython
)
set "PY=.venv\Scripts\python.exe"
"%PY%" -c "import sys; sys.exit(sys.version_info < (3, 11))" || goto oldpython
"%PY%" -m pip install --upgrade pip || goto failed
"%PY%" -m pip install -e . || goto failed

set "ANSWER="
set /p ANSWER="Also install the camera OCR (PyTorch, a 1-2 GB download)? [y/N] "
if /i "%ANSWER%"=="y" "%PY%" -m pip install -e ".[ocr]" || goto failed

echo.
echo Checking that the app can start...
"%PY%" -c "import PyQt6.QtWidgets, serial, matplotlib, qdarktheme" || goto failed
echo.
echo Done. Start Apera Meter GUI with Launch_Windows.bat.
pause
exit /b 0

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

:failed
echo.
echo Setup didn't finish. Copy the messages above when asking for help.
pause
exit /b 1
