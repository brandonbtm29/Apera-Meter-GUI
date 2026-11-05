"""Shared test setup.

meter_app changes into its data folder and reads settings as soon as it is imported, so the
environment must point it at throwaway folders *before* any test imports it. Qt runs headless.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_tmp = Path(tempfile.mkdtemp(prefix="apera-meter-tests-"))
os.environ["UAKRON_APP_DATA_DIR"] = str(_tmp / "data")
os.environ["UAKRON_SETTINGS_DIR"] = str(_tmp / "settings")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("MPLBACKEND", "QtAgg")
os.environ["APERA_NO_BACKGROUND_CHECKS"] = "1"  # no network calls or desktop changes from tests

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


import pytest  # noqa: E402 - after the environment is set up


@pytest.fixture(autouse=True)
def _app_test_defaults(monkeypatch, tmp_path):
    """Closing a window with unsaved readings asks Save / Close / Cancel: tests always close. And
    this computer's state (COM ports, window layout) goes to a temp file, not the repo folder."""
    import meter_app

    monkeypatch.setattr(meter_app.MeterApp, "_close_choice", lambda self, lines: "close")
    monkeypatch.setattr(meter_app, "LOCAL_STATE_PATH", str(tmp_path / "local_state.json"))
