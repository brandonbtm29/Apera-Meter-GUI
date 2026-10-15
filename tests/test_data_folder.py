"""Where the app keeps its data when no environment variable says otherwise."""

import os

import meter_app


def test_uses_syncthing_folder_when_it_exists(tmp_path):
    (tmp_path / "SyncThing" / "UAkron Playground").mkdir(parents=True)
    expected = tmp_path / "SyncThing" / "UAkron Playground" / "Electrical Conductivity Meter"
    assert meter_app.resolve_data_dir({}, str(tmp_path)) == str(expected)


def test_falls_back_to_documents_without_syncthing(tmp_path):
    expected = tmp_path / "Documents" / "Apera Meter Data"
    assert meter_app.resolve_data_dir({}, str(tmp_path)) == str(expected)


def test_environment_variables_win(tmp_path):
    exact = str(tmp_path / "exact")
    root = str(tmp_path / "root")
    assert meter_app.resolve_data_dir({"UAKRON_APP_DATA_DIR": exact}, str(tmp_path)) == exact
    assert meter_app.resolve_data_dir({"UAKRON_DATA_DIR": root}, str(tmp_path)) == os.path.join(
        root, "Electrical Conductivity Meter"
    )
