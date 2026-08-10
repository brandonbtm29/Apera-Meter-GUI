import os
import sys
import subprocess

def build():
    print("Building executable for Apera Meter GUI...")
    print("Ensuring dependencies are installed...")
    
    # Run pip install
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    
    # Run pyinstaller
    print("Running PyInstaller...")
    cmd = [
        "pyinstaller",
        "-y",
        "--noconsole",
        "--onefile",
        "--name", "Apera_Meter_GUI",
        "--exclude-module", "PySide6",
        "--exclude-module", "PyQt5",
        "meter_app.py"
    ]
    
    subprocess.check_call(cmd)
    
    print("\n=============================================")
    print("BUILD COMPLETE!")
    if sys.platform == "win32":
        print("Your executable is located in the 'dist' folder: dist\\Apera_Meter_GUI.exe")
        print("You can right-click this .exe and select 'Create Shortcut' to place it on your Desktop.")
    elif sys.platform == "darwin":
        print("Your application bundle is located in the 'dist' folder: dist/Apera_Meter_GUI.app")
        print("You can drag this .app file to your Applications folder or Desktop.")
    else:
        print("Your executable is located in the 'dist' folder.")
    print("=============================================")

if __name__ == "__main__":
    build()
