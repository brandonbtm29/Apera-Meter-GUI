"""Where the app keeps its data when no environment variable says otherwise."""

import os

import meter_app


def test_uses_its_own_syncthing_folder(tmp_path):
    new = tmp_path / "SyncThing" / "Electrical Conductivity Meter"
    new.mkdir(parents=True)
    (tmp_path / "SyncThing" / "UAkron Playground" / "Electrical Conductivity Meter").mkdir(parents=True)
    assert meter_app.resolve_data_dir({}, str(tmp_path)) == str(new)


def test_keeps_the_old_shared_folder_until_the_new_one_arrives(tmp_path):
    old = tmp_path / "SyncThing" / "UAkron Playground" / "Electrical Conductivity Meter"
    old.mkdir(parents=True)
    assert meter_app.resolve_data_dir({}, str(tmp_path)) == str(old)


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
