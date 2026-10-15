"""Settings -> Create Desktop Shortcut, written to a temporary 'desktop'."""

import os
import subprocess
import sys

import pytest

import shortcuts
from conftest import REPO


def test_linux_launcher_points_at_this_copy(tmp_path):
    path = shortcuts.create_desktop_shortcut(REPO, desktop=tmp_path, platform="linux")
    text = path.read_text(encoding="utf-8")
    assert path.name == "Apera Meter GUI.desktop"
    assert f'"{REPO / "meter_app.py"}"' in text
    assert f"Icon={REPO / 'Conductivity meter.ico'}" in text
    if os.name != "nt":
        assert os.access(path, os.X_OK)


def test_mac_shortcut_starts_the_app_in_the_background():
    script = shortcuts.mac_applescript("/Users/me/GitHub Projects/Apera Meter", "/Users/me/.venv/bin/python")
    assert script.startswith('do shell script "')
    assert "cd '/Users/me/GitHub Projects/Apera Meter'" in script
    assert "APERA_LOG_TO_FILE=1 nohup /Users/me/.venv/bin/python meter_app.py" in script
    assert script.rstrip('"').endswith("&")  # returns at once instead of waiting for the app


@pytest.mark.skipif(sys.platform != "darwin", reason="builds a real macOS app bundle")
def test_mac_shortcut_is_an_app_with_the_meter_icon(tmp_path):
    (tmp_path / "Apera Meter GUI.command").write_text("old 1.2.0 shortcut")
    path = shortcuts.create_desktop_shortcut(REPO, desktop=tmp_path, platform="darwin")
    assert path.name == "Apera Meter GUI.app"
    assert not (tmp_path / "Apera Meter GUI.command").exists()  # the old style is replaced
    icon = path / "Contents" / "Resources" / "applet.icns"
    assert icon.stat().st_size > 10_000
    assert subprocess.run(["codesign", "--verify", str(path)], check=False).returncode == 0
    script = subprocess.run(
        ["osadecompile", str(path / "Contents" / "Resources" / "Scripts" / "main.scpt")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "meter_app.py" in script


def test_refresh_only_touches_an_existing_shortcut(tmp_path):
    assert shortcuts.refresh_existing_shortcut(REPO, desktop=tmp_path, platform="linux") is None
    assert list(tmp_path.iterdir()) == []
    stale = tmp_path / "Apera Meter GUI.desktop"
    stale.write_text("[Desktop Entry]\nExec=/old/place/meter_app.py\n")
    assert shortcuts.refresh_existing_shortcut(REPO, desktop=tmp_path, platform="linux") == stale
    assert str(REPO / "meter_app.py") in stale.read_text(encoding="utf-8")


def test_running_again_replaces_the_shortcut(tmp_path):
    first = shortcuts.create_desktop_shortcut(REPO, desktop=tmp_path, platform="linux")
    second = shortcuts.create_desktop_shortcut(REPO, desktop=tmp_path, platform="linux")
    assert first == second
    assert len(list(tmp_path.iterdir())) == 1


def test_missing_desktop_is_explained(tmp_path):
    with pytest.raises(shortcuts.ShortcutError, match="desktop folder"):
        shortcuts.create_desktop_shortcut(REPO, desktop=tmp_path / "nope", platform="linux")


def test_windows_target_for_a_clone_uses_pythonw(tmp_path):
    python = tmp_path / "python.exe"
    (tmp_path / "pythonw.exe").write_text("")
    target, args, workdir, icon = shortcuts.windows_target(REPO, python=python, frozen=False)
    assert target == str(tmp_path / "pythonw.exe")
    assert args == f'"{REPO / "meter_app.py"}"'
    assert workdir == str(REPO)
    assert icon.endswith("Conductivity meter.ico")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts need Windows")
def test_windows_shortcut_is_a_real_lnk(tmp_path):
    path = shortcuts.create_desktop_shortcut(REPO, desktop=tmp_path, platform="win32")
    assert path.name == "Apera Meter GUI.lnk"
    read = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"(New-Object -ComObject WScript.Shell).CreateShortcut('{path}').Arguments",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "meter_app.py" in read.stdout


def test_settings_tab_has_the_button(monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QApplication, QMessageBox

    import meter_app

    shown = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(shortcuts, "desktop_folder", lambda: tmp_path)
    app = QApplication.instance() or QApplication([])
    window = meter_app.MeterApp()
    try:
        assert "Desktop Shortcut" in window.settings_tab.btn_shortcut.text()
        if sys.platform != "win32":  # the Windows .lnk path is covered above
            window.settings_tab.btn_shortcut.click()
            assert shown and "Shortcut created" in shown[0]
            assert len(list(tmp_path.iterdir())) == 1
    finally:
        window.close()
        app.processEvents()
