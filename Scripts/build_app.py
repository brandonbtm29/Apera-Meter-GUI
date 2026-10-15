"""Build a standalone copy of the app with PyInstaller.

Run from the repository folder, in an environment with the OCR and dev extras installed:

    python -m pip install -e ".[ocr,dev]"
    python Scripts/build_app.py            # build dist/Apera Meter GUI/ and a zip next to it
    python Scripts/build_app.py --no-zip   # just the folder

The result is a folder (not a single .exe): PyTorch is large, and a one-file build would unpack
it to a temp folder on every start. The OCR model and icon are bundled inside the folder.
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_NAME = "Apera Meter GUI"
MODEL = REPO / "meter_ocr_model.pt"
ICON = REPO / "Conductivity meter.ico"


def app_version():
    text = (REPO / "meter_app.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.MULTILINE).group(1)


def check_inputs():
    if not MODEL.exists() or MODEL.stat().st_size < 1_000_000:
        sys.exit(
            f"{MODEL.name} is missing or is a Git LFS pointer. Run 'git lfs install' and "
            "'git lfs pull' first."
        )
    try:
        import torch  # noqa: F401
    except ImportError:
        sys.exit('PyTorch is not installed. Run: python -m pip install -e ".[ocr,dev]"')


def build():
    sep = os.pathsep  # PyInstaller's SOURCE<sep>DEST separator for --add-data
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--icon",
        str(ICON),
        "--add-data",
        f"{MODEL}{sep}.",
        "--add-data",
        f"{ICON}{sep}.",
        "--collect-data",
        "qdarktheme",
        # Newer torchvision ships _C_stable instead of _C, which PyInstaller's hook misses;
        # without it the app fails at start with "operator torchvision::nms does not exist".
        "--collect-binaries",
        "torchvision",
        "--exclude-module",
        "PySide6",
        "--exclude-module",
        "PyQt5",
        "--exclude-module",
        "tkinter",
        "--distpath",
        str(REPO / "dist"),
        "--workpath",
        str(REPO / "build"),
        "--specpath",
        str(REPO / "build"),
        str(REPO / "meter_app.py"),
    ]
    print("Running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=REPO)
    return REPO / "dist" / APP_NAME


def make_zip(folder):
    system = {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), platform.system().lower())
    base = REPO / "dist" / f"Apera-Meter-GUI-{app_version()}-{system}"
    archive = shutil.make_archive(str(base), "zip", root_dir=folder.parent, base_dir=folder.name)
    return Path(archive)


def main():
    parser = argparse.ArgumentParser(description="Build a standalone copy of the app.")
    parser.add_argument("--no-zip", action="store_true", help="don't zip the result")
    args = parser.parse_args()

    check_inputs()
    folder = build()
    exe = folder / (f"{APP_NAME}.exe" if os.name == "nt" else APP_NAME)
    print(f"\nBuilt {folder}\nStart it with: {exe}")
    if not args.no_zip:
        print(f"Zipped to {make_zip(folder)}")


if __name__ == "__main__":
    main()
