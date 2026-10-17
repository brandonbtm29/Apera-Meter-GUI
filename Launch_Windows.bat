@echo off
rem Double-click launcher for Windows. Starts the app without leaving a console window open.
cd /d "%~dp0"
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
