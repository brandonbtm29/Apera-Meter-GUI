#!/bin/bash
# Double-click launcher for macOS. Starts the app in the background and closes this Terminal
# window. Errors after start-up go to app.log in the data folder.
# The first time (no .venv folder yet) it sets the app up first, the same way Setup_Mac.command does.
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

# First run: create .venv and install the app (same steps as Setup_Mac.command).
if [ ! -x ".venv/bin/python" ]; then
  fail() { echo; echo "$1"; read -r -p "Press Enter to close"; exit 1; }
  new_enough() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; }
  retry="To try again, double-click Setup_Mac.command."
  echo "Setting up Apera Meter GUI for the first time in $PWD"
  echo "This takes a minute or two. The app starts when it's done."
  echo
  PYTHON=""
  for p in python3.12 python3.13 python3.11 python3; do
    if command -v "$p" >/dev/null && new_enough "$p"; then PYTHON="$p"; break; fi
  done
  [ -n "$PYTHON" ] || fail "Python 3.11 or newer isn't installed. Get it from https://www.python.org/downloads/ and run this again."
  echo "Creating .venv with $PYTHON ..."
  "$PYTHON" -m venv .venv || fail "Couldn't create the .venv folder."
  PY=.venv/bin/python
  "$PY" -m pip install --upgrade pip || fail "Setup didn't finish. Copy the messages above when asking for help. $retry"
  "$PY" -m pip install -e . || fail "Setup didn't finish. Copy the messages above when asking for help. $retry"

  read -r -p "Also install the camera OCR (PyTorch, a 1-2 GB download)? [y/N] " ANSWER
  if [[ "$ANSWER" =~ ^[Yy] ]]; then "$PY" -m pip install -e ".[ocr]" || fail "Installing failed. Copy the messages above when asking for help. $retry"; fi
  echo
  echo "Setup finished. Starting Apera Meter GUI..."
fi

if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; fi

# Quick check here, so missing packages show an error instead of nothing happening.
if ! "$PY" -c "import PyQt6.QtWidgets, serial, matplotlib, qdarktheme"; then
  echo
  echo "The app could not start. Copy the messages above when asking for help."
  echo "To install what it needs, double-click Setup_Mac.command."
  read -r -p "Press Enter to close"
  exit 1
fi

# Start the app in its own session (so closing this window doesn't stop it), then ask Terminal
# to close this window once the script has finished.
"$PY" - "$PY" "$(tty)" <<'PYEOF'
import os, subprocess, sys
python, tty = sys.argv[1], sys.argv[2]
quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
subprocess.Popen([python, "meter_app.py"], env=dict(os.environ, APERA_LOG_TO_FILE="1"), **quiet)
script = (
    "delay 1\n"
    'tell application "Terminal"\n'
    "  repeat with w in (every window)\n"
    f'    if tty of selected tab of w is "{tty}" then close w\n'
    "  end repeat\n"
    "end tell"
)
try:
    subprocess.Popen(["osascript", "-e", script], **quiet)
except OSError:
    pass
PYEOF
exit 0
