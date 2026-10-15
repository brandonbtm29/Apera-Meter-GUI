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


def test_mac_command_runs_the_launcher(tmp_path):
    path = shortcuts.create_desktop_shortcut(REPO, desktop=tmp_path, platform="darwin")
    text = path.read_text(encoding="utf-8")
    assert path.name == "Apera Meter GUI.command"
    assert text.startswith("#!/bin/bash")
    assert f'exec "{REPO / "Launch_Mac.command"}"' in text


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
