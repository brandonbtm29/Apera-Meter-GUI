"""Put a shortcut to the app on the desktop (Settings -> Create Desktop Shortcut).

* **Windows**: a normal ``.lnk`` shortcut with the meter icon. For the standalone build it points
  at ``Apera Meter GUI.exe``; for a git clone it starts ``meter_app.py`` with the clone's
  ``pythonw.exe`` (no console window).
* **macOS**: a small ``Apera Meter GUI.app`` with the meter icon that starts ``meter_app.py`` in
  the background (no Terminal window). Built with the system's ``osacompile``, ``sips`` and
  ``iconutil``.
* **Linux**: an ``Apera Meter GUI.desktop`` launcher.

The shortcut points at this copy of the app, so it keeps working after Check for Updates.
Running it again replaces the old shortcut (the app also refreshes an existing shortcut at start,
so its icon and target stay current). No Qt here, so it can be tested on its own.
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
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


def mac_launch_command(app_dir, python=None):
    """Shell command the macOS shortcut runs: start the app in the background, output to its log."""
    app_dir = Path(app_dir)
    python = python or sys.executable
    return (
        f"cd {shlex.quote(str(app_dir))} && APERA_LOG_TO_FILE=1 "
        f"nohup {shlex.quote(str(python))} meter_app.py >/dev/null 2>&1 &"
    )


def mac_applescript(app_dir, python=None):
    command = mac_launch_command(app_dir, python)
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    return f'do shell script "{escaped}"'


def _run(cmd, what):
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ShortcutError(f"Couldn't {what}.\n\n{(result.stderr or result.stdout).strip()}")
    return result


def make_icns(ico, out_icns, workdir):
    """Convert the app's .ico into a macOS .icns with sips and iconutil."""
    workdir = Path(workdir)
    base = workdir / "base.png"
    _run(["sips", "-s", "format", "png", str(ico), "--out", str(base)], "convert the icon")
    iconset = workdir / "AppIcon.iconset"
    iconset.mkdir(exist_ok=True)
    sizes = {
        "icon_16x16": 16,
        "icon_16x16@2x": 32,
        "icon_32x32": 32,
        "icon_32x32@2x": 64,
        "icon_128x128": 128,
        "icon_128x128@2x": 256,
        "icon_256x256": 256,
    }
    for name, size in sizes.items():
        _run(
            ["sips", "-z", str(size), str(size), str(base), "--out", str(iconset / f"{name}.png")],
            "resize the icon",
        )
    _run(["iconutil", "-c", "icns", str(iconset), "-o", str(out_icns)], "build the .icns icon")
    return Path(out_icns)


def _create_mac_app(path, app_dir):
    app_dir = Path(app_dir)
    with tempfile.TemporaryDirectory() as tmp:
        built = Path(tmp) / f"{APP_NAME}.app"
        _run(["osacompile", "-o", str(built), "-e", mac_applescript(app_dir)], "create the app shortcut")
        resources = built / "Contents" / "Resources"
        icon = app_dir / ICON_NAME
        if icon.exists():
            make_icns(icon, resources / "applet.icns", tmp)
            plist = built / "Contents" / "Info.plist"
            # Newer macOS applets also name an asset-catalog icon, which would hide ours.
            subprocess.run(
                ["plutil", "-remove", "CFBundleIconName", str(plist)], capture_output=True, check=False
            )
            assets = resources / "Assets.car"
            if assets.exists():
                assets.unlink()
        # Re-sign (ad hoc) after editing the bundle so macOS still runs it.
        subprocess.run(
            ["codesign", "--force", "--deep", "-s", "-", str(built)], capture_output=True, check=False
        )
        if path.exists():
            shutil.rmtree(path)
        shutil.move(str(built), str(path))
    os.utime(path)  # nudge Finder to pick up the new icon
    return path


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
        old_command = desktop / f"{APP_NAME}.command"  # what version 1.2.0 created
        if old_command.exists():
            old_command.unlink()
        return _create_mac_app(desktop / f"{APP_NAME}.app", app_dir)

    path = desktop / f"{APP_NAME}.desktop"
    text = linux_desktop_text(app_dir)
    try:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
    except OSError as e:
        raise ShortcutError(f"Couldn't write {path}.\n\n{e}") from e
    return path


def shortcut_path(desktop=None, platform=None):
    """Where this app's desktop shortcut lives (whether or not it exists)."""
    platform = platform or sys.platform
    desktop = Path(desktop) if desktop else desktop_folder()
    suffix = {"win32": ".lnk", "darwin": ".app"}.get(platform, ".desktop")
    return desktop / f"{APP_NAME}{suffix}"


def refresh_existing_shortcut(app_dir, desktop=None, platform=None):
    """Re-create the shortcut if the user already has one; returns its path, or None.

    Keeps the icon and target current after an update or after the app folder moved. Never
    creates a shortcut the user didn't ask for.
    """
    platform = platform or sys.platform
    desktop = Path(desktop) if desktop else desktop_folder()
    existing = shortcut_path(desktop, platform)
    old_mac = desktop / f"{APP_NAME}.command"
    if not existing.exists() and not (platform == "darwin" and old_mac.exists()):
        return None
    return create_desktop_shortcut(app_dir, desktop=desktop, platform=platform)
