"""Put a shortcut to the app on the desktop (Settings -> Create Desktop Shortcut).

* **Windows**: a normal ``.lnk`` shortcut with the meter icon. For the standalone build it points
  at ``Apera Meter GUI.exe``; for a git clone it starts ``meter_app.py`` with the clone's
  ``pythonw.exe`` (no console window).
* **macOS**: ``Apera Meter GUI.command`` on the desktop, which runs the clone's
  ``Launch_Mac.command`` (it opens a Terminal window, like the launcher itself).
* **Linux**: an ``Apera Meter GUI.desktop`` launcher.

The shortcut points at this copy of the app, so it keeps working after Check for Updates.
Running it again replaces the old shortcut. No Qt here, so it can be tested on its own.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "Apera Meter GUI"
ICON_NAME = "Conductivity meter.ico"


class ShortcutError(Exception):
    """The shortcut couldn't be created; the message is shown to the user."""


def _frozen():
    return bool(getattr(sys, "frozen", False))


def desktop_folder():
    """The user's desktop, including Windows desktops redirected to OneDrive."""
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "[Environment]::GetFolderPath('Desktop')",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            path = result.stdout.strip()
            if result.returncode == 0 and path and os.path.isdir(path):
                return Path(path)
        except (OSError, subprocess.SubprocessError):
            pass
    elif sys.platform.startswith("linux") and shutil.which("xdg-user-dir"):
        result = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, check=False)
        if result.returncode == 0 and os.path.isdir(result.stdout.strip()):
            return Path(result.stdout.strip())
    return Path.home() / "Desktop"


def windows_target(app_dir, python=None, frozen=None):
    """(target, arguments, working folder, icon) for the Windows shortcut."""
    frozen = _frozen() if frozen is None else frozen
    app_dir = Path(app_dir)
    if frozen:
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent), str(exe)
    python = Path(python or sys.executable)
    pythonw = python.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else python
    icon = app_dir / ICON_NAME
    return str(runner), f'"{app_dir / "meter_app.py"}"', str(app_dir), str(icon) if icon.exists() else ""


def mac_command_text(app_dir):
    launcher = Path(app_dir) / "Launch_Mac.command"
    return f'#!/bin/bash\n# Starts {APP_NAME} from {app_dir}\nexec "{launcher}"\n'


def linux_desktop_text(app_dir, python=None):
    app_dir = Path(app_dir)
    if _frozen():
        command = f'"{sys.executable}"'
    else:
        command = f'"{python or sys.executable}" "{app_dir / "meter_app.py"}"'
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec={command}\n"
        f"Path={app_dir}\n"
        f"Icon={app_dir / ICON_NAME}\n"
        "Terminal=false\n"
        "Categories=Science;\n"
    )


def _create_windows_shortcut(path, target, arguments, workdir, icon):
    # Paths go through environment variables so spaces and quotes need no escaping.
    script = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:APERA_LNK);"
        "$s.TargetPath = $env:APERA_TARGET;"
        "$s.Arguments = $env:APERA_ARGS;"
        "$s.WorkingDirectory = $env:APERA_WORKDIR;"
        "if ($env:APERA_ICON) { $s.IconLocation = $env:APERA_ICON };"
        f"$s.Description = '{APP_NAME}';"
        "$s.Save()"
    )
    env = dict(
        os.environ,
        APERA_LNK=str(path),
        APERA_TARGET=target,
        APERA_ARGS=arguments,
        APERA_WORKDIR=workdir,
        APERA_ICON=icon,
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 or not Path(path).exists():
        raise ShortcutError(f"Windows couldn't create the shortcut.\n\n{result.stderr.strip()}")


def create_desktop_shortcut(app_dir, desktop=None, platform=None):
    """Create (or replace) the desktop shortcut and return its path."""
    platform = platform or sys.platform
    desktop = Path(desktop) if desktop else desktop_folder()
    if not desktop.is_dir():
        raise ShortcutError(f"Couldn't find the desktop folder ({desktop}).")

    if platform == "win32":
        path = desktop / f"{APP_NAME}.lnk"
        _create_windows_shortcut(path, *windows_target(app_dir))
        return path

    if platform == "darwin":
        if _frozen():
            raise ShortcutError("Drag the app from its folder to the Dock or desktop instead.")
        path = desktop / f"{APP_NAME}.command"
        text = mac_command_text(app_dir)
    else:
        path = desktop / f"{APP_NAME}.desktop"
        text = linux_desktop_text(app_dir)
    try:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    except OSError as e:
        raise ShortcutError(f"Couldn't write {path}.\n\n{e}") from e
    return path
