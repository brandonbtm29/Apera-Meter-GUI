@echo off
cd /d "%~dp0"
echo Launching Apera Dual-Meter Control Center...
python meter_app.py
if %errorlevel% neq 0 (
    echo.
    echo An error occurred while running the application.
    pause
)
