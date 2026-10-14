"""Scripts/cleanup_1000x_gaps.py repairs logs where the meter switched between mS/cm and uS/cm."""

import importlib.util

import pandas as pd
import pytest

from conftest import REPO


def _load():
    spec = importlib.util.spec_from_file_location(
        "cleanup_1000x_gaps", REPO / "Scripts" / "cleanup_1000x_gaps.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixes_a_1000x_drop(tmp_path):
    src, out = tmp_path / "log.csv", tmp_path / "log_cleaned.csv"
    pd.DataFrame({"Cond (mS/cm)": [1.20, 1.21, 0.00122, 0.00123, 1.24]}).to_csv(src, index=False)

    _load().clean_data(str(src), str(out))

    fixed = pd.read_csv(out)["Cond (mS/cm) fixed"].tolist()
    assert fixed == pytest.approx([1.20, 1.21, 1.22, 1.23, 1.24], rel=1e-9)
