"""settings_location decides where config.json lives; a wrong answer silently loses settings."""

import json

import settings_location as sl


def test_defaults_to_app_folder(tmp_path, monkeypatch):
    monkeypatch.delenv(sl.ENV_VAR, raising=False)
    assert sl.resolve(str(tmp_path / "app"), str(tmp_path / "data")) == str(tmp_path / "app")


def test_relative_pointer_is_inside_data_folder(tmp_path, monkeypatch):
    monkeypatch.delenv(sl.ENV_VAR, raising=False)
    app, data = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    (app / sl.POINTER).write_text(json.dumps({"settings_dir": "Settings"}), encoding="utf-8")
    resolved = sl.resolve(str(app), str(data))
    assert resolved == str(data / "Settings")
    assert (data / "Settings").is_dir()


def test_environment_variable_overrides_pointer(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / sl.POINTER).write_text(json.dumps({"settings_dir": "Settings"}), encoding="utf-8")
    monkeypatch.setenv(sl.ENV_VAR, str(tmp_path / "elsewhere"))
    assert sl.resolve(str(app), str(tmp_path / "data")) == str(tmp_path / "elsewhere")


def test_change_copies_missing_files_and_keeps_existing(tmp_path, monkeypatch):
    monkeypatch.delenv(sl.ENV_VAR, raising=False)
    app, data = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    (app / "config.json").write_text("{}", encoding="utf-8")
    (app / "global_rois.json").write_text("[]", encoding="utf-8")
    target = data / "Settings"
    target.mkdir(parents=True)
    (target / "global_rois.json").write_text('["synced"]', encoding="utf-8")

    new_dir, copied, kept = sl.change(str(app), str(data), str(target), ["config.json", "global_rois.json"])

    assert new_dir == str(target)
    assert copied == ["config.json"] and kept == ["global_rois.json"]
    assert (target / "global_rois.json").read_text(encoding="utf-8") == '["synced"]'
    assert json.loads((app / sl.POINTER).read_text(encoding="utf-8")) == {"settings_dir": "Settings"}
