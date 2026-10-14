"""Smoke tests: the app imports, reports its version and builds its main window headless."""

import importlib.metadata
import os

import pytest


def test_version_matches_package_metadata():
    import meter_app

    assert meter_app.__version__ == importlib.metadata.version("apera-meter-gui")


def test_data_and_settings_go_to_the_configured_folders():
    import meter_app

    assert os.path.samefile(meter_app.DATA_DIR, os.environ["UAKRON_APP_DATA_DIR"])
    assert os.path.samefile(meter_app.CONFIG_DIR, os.environ["UAKRON_SETTINGS_DIR"])


def test_main_window_builds_and_closes(monkeypatch):
    from PyQt6.QtWidgets import QApplication, QMessageBox

    import meter_app

    # Never block on a dialog in a headless run.
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)

    app = QApplication.instance() or QApplication([])
    window = meter_app.MeterApp()
    try:
        tab_names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        for expected in ("Settings", "Meter 1", "Meter 2"):
            assert expected in tab_names
        app.processEvents()
    finally:
        window.close()
        app.processEvents()


@pytest.mark.parametrize("module", ["ocr_pipeline", "dataset_curation_tab", "settings_location"])
def test_helper_modules_import(module):
    __import__(module)
