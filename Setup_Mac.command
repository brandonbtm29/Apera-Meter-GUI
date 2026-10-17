#!/bin/bash
# One-click setup for Apera Meter GUI on macOS: creates this folder's own Python environment
# (.venv) and installs everything the app needs. Run it again any time to repair or update it.
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1
fail() { echo; echo "$1"; read -r -p "Press Enter to close"; exit 1; }
new_enough() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }
echo "Setting up Apera Meter GUI in $PWD"
echo
if [ -x .venv/bin/python ]; then
  echo "Using the existing .venv folder."
else
  PYTHON=""
  for p in python3.12 python3.13 python3.11 python3; do
    if command -v "$p" >/dev/null && new_enough "$p"; then PYTHON="$p"; break; fi
  done
  [ -n "$PYTHON" ] || fail "Python 3.11 or newer isn't installed. Get it from https://www.python.org/downloads/ and run this again."
  echo "Creating .venv with $PYTHON ..."
  "$PYTHON" -m venv .venv || fail "Couldn't create the .venv folder."
fi
PY=.venv/bin/python
new_enough "$PY" || fail "The .venv folder uses a Python older than 3.11. Delete it, install a newer Python, and run this again."
"$PY" -m pip install --upgrade pip || fail "Setup didn't finish. Copy the messages above when asking for help."
"$PY" -m pip install -e . || fail "Setup didn't finish. Copy the messages above when asking for help."

read -r -p "Also install the camera OCR (PyTorch, a 1-2 GB download)? [y/N] " ANSWER
if [[ "$ANSWER" =~ ^[Yy] ]]; then "$PY" -m pip install -e ".[ocr]" || fail "Installing failed. Copy the messages above when asking for help."; fi

echo
echo "Checking that the app can start..."
"$PY" -c "import PyQt6.QtWidgets, serial, matplotlib, qdarktheme" || fail "Setup didn't finish. Copy the messages above when asking for help."
echo
echo "Done. Start Apera Meter GUI with Launch_Mac.command."
read -r -p "Press Enter to close"
