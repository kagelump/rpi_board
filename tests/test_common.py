import json

import pytest

from scripts import common


def test_settings_use_example_until_local_config_exists(tmp_path, monkeypatch):
    local = tmp_path / "settings.json"
    example = tmp_path / "settings.example.json"
    example.write_text(json.dumps({"display": {"mode": "local_preview"}}))
    monkeypatch.setattr(common, "SETTINGS_PATH", local)
    monkeypatch.setattr(common, "EXAMPLE_SETTINGS_PATH", example)

    assert common.load_settings()["display"]["mode"] == "local_preview"
    local.write_text(json.dumps({"display": {"mode": "pi_display"}}))
    assert common.load_settings()["display"]["mode"] == "pi_display"
    assert json.loads(example.read_text())["display"]["mode"] == "local_preview"


def test_invalid_local_settings_do_not_silently_use_example(tmp_path, monkeypatch):
    local = tmp_path / "settings.json"
    example = tmp_path / "settings.example.json"
    local.write_text("invalid json")
    example.write_text("{}")
    monkeypatch.setattr(common, "SETTINGS_PATH", local)
    monkeypatch.setattr(common, "EXAMPLE_SETTINGS_PATH", example)

    with pytest.raises(json.JSONDecodeError):
        common.load_settings()
