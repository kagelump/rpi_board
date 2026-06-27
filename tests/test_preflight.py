"""Tests for scripts/ops/preflight.validate_config"""
from scripts.common import load_settings
from scripts.ops.preflight import validate_config


def _base():
    return {
        "display": {"width": 960, "height": 640, "panel_fraction": 0.25},
        "fal": {"image_generation_parameters": {"image_size": {"width": 960, "height": 480}}},
        "pipeline": {"image_provider": "fal"},
        "context": {"events_mode": "online_model"},
    }


class TestValidateConfig:
    def test_consistent_config_has_no_issues(self):
        assert validate_config(_base()) == []

    def test_real_settings_file_is_valid(self):
        # Guards against shipping a settings.json that fails its own checks.
        assert validate_config(load_settings()) == []

    def test_image_size_height_mismatch_flagged(self):
        s = _base()
        s["fal"]["image_generation_parameters"]["image_size"]["height"] = 640
        issues = validate_config(s)
        assert any("art region" in i for i in issues)

    def test_image_size_width_mismatch_flagged(self):
        s = _base()
        s["fal"]["image_generation_parameters"]["image_size"]["width"] = 1024
        assert any("image_size.width" in i for i in validate_config(s))

    def test_bad_panel_fraction(self):
        s = _base()
        s["display"]["panel_fraction"] = 1.5
        assert any("panel_fraction" in i for i in validate_config(s))

    def test_bad_dimensions(self):
        s = _base()
        s["display"]["width"] = 0
        assert any("display.width" in i for i in validate_config(s))

    def test_unknown_provider(self):
        s = _base()
        s["pipeline"]["image_provider"] = "midjourney"
        assert any("image_provider" in i for i in validate_config(s))

    def test_unknown_events_mode(self):
        s = _base()
        s["context"]["events_mode"] = "telepathy"
        assert any("events_mode" in i for i in validate_config(s))

    def test_enum_string_image_size_not_flagged(self):
        # Non-dict image_size (a fal preset enum) is left to the API to validate.
        s = _base()
        s["fal"]["image_generation_parameters"]["image_size"] = "landscape_4_3"
        assert validate_config(s) == []
