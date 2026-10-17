#!/bin/bash
# Double-click launcher for macOS. Starts the app in the background and closes this Terminal
# window. Errors after start-up go to app.log in the data folder.
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
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
