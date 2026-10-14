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

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
