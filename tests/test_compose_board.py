"""Tests for board rendering and helpers in scripts/render/compose_board.py."""
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from scripts.render.compose_board import (
    _accent_color,
    _ascii_only,
    _cover_crop_top_center,
    _is_degraded_source,
    _layout_panel_text,
    draw_weather_glyph,
    render_board,
    weather_glyph_kind,
)


# ---------------------------------------------------------------------------
# _is_degraded_source
# ---------------------------------------------------------------------------

class TestIsDegradedSource:
    def test_openrouter_is_fresh(self):
        assert _is_degraded_source("openrouter") is False

    def test_cached_is_fresh(self):
        assert _is_degraded_source("cached") is False

    def test_deterministic_is_degraded(self):
        assert _is_degraded_source("deterministic") is True
        assert _is_degraded_source("deterministic_fallback_error") is True

    def test_missing_is_degraded(self):
        assert _is_degraded_source(None) is True
        assert _is_degraded_source(42) is True


RECORDED_COPY = json.loads(
    (Path(__file__).parent / "fixtures" / "issue4_panel_copy.json").read_text()
)


@pytest.mark.parametrize("copy", RECORDED_COPY, ids=lambda copy: copy["run_id"])
def test_recorded_copy_renders_complete_within_panel(copy, tmp_path, monkeypatch):
    """Check the actual draw calls and ink bounds of the finished 960x640 board."""
    panel_calls = []
    original_text = ImageDraw.ImageDraw.text

    def capture_text(draw, xy, text, *args, **kwargs):
        if xy[1] >= 480:
            box = draw.textbbox(xy, text, font=kwargs["font"])
            panel_calls.append((text, box, kwargs["font"].size))
        return original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    settings = {
        "display": {"width": 960, "height": 640, "panel_fraction": 0.25},
        "runtime": {"hero_file": str(tmp_path / "missing.png"),
                    "stale_file": str(tmp_path / "status.json")},
    }
    payload = {
        "brief": copy,
        "today": {"daily_summary": {"date": "2026-10-01", "weather_code": 51,
                                    "temp_min_c": 18, "temp_max_c": 30}},
        "generated_at_local": "2026-10-01T08:00:00+09:00",
        "brief_source": "openrouter",
    }
    output = tmp_path / "board.png"
    preview = tmp_path / "preview.png"
    render_board(settings, payload, str(output), str(preview))

    assert " ".join(text for text, _, _ in panel_calls) == (
        copy["headline"] + " " + copy["subtitle"]
    )
    assert len(panel_calls) <= 4
    assert panel_calls[0][2] >= panel_calls[-1][2]
    previous_bottom = 480
    for _, (left, top, right, bottom), size in panel_calls:
        assert 22 <= left < right <= 938
        assert previous_bottom < top < bottom <= 624
        assert size >= 26
        previous_bottom = bottom
    assert Image.open(output).size == (960, 640)
    assert Image.open(preview).size == (480, 320)


def test_short_copy_keeps_large_fonts():
    draw = ImageDraw.Draw(Image.new("RGB", (960, 640)))
    heading, detail = _layout_panel_text(draw, "Rain by 3pm", "Bring a coat.", 916, 126)
    assert heading["font"].size == 64
    assert detail["font"].size == 38
    assert heading["lines"] == ["Rain by 3pm"]
    assert detail["lines"] == ["Bring a coat."]


def test_long_headline_wraps_without_losing_words():
    draw = ImageDraw.Draw(Image.new("RGB", (960, 640)))
    headline = "Heavy rain through the morning, clearing after lunch with gusty winds"
    heading, detail = _layout_panel_text(draw, headline, "Carry an umbrella.", 916, 126)
    assert len(heading["lines"]) == 2
    assert " ".join(heading["lines"]) == headline
    assert heading["font"].size >= 32
    assert detail["font"].size >= 26


@pytest.mark.parametrize("headline,subtitle,width,height", [
    ("W" * 200, "Bring a coat.", 916, 126),
    ("Rain today", "Bring a coat. " * 100, 916, 126),
    ("Rain today", "Bring a coat.", 916, 20),
    ("Rain today", "Bring a coat.", 20, 126),
])
def test_unfittable_copy_requires_rewrite(headline, subtitle, width, height):
    draw = ImageDraw.Draw(Image.new("RGB", (960, 640)))
    with pytest.raises(ValueError, match="rewrite more concisely"):
        _layout_panel_text(draw, headline, subtitle, width, height)


# ---------------------------------------------------------------------------
# weather_glyph_kind
# ---------------------------------------------------------------------------

class TestWeatherGlyphKind:
    def test_clear(self):
        assert weather_glyph_kind(0) == "clear"
        assert weather_glyph_kind(1) == "clear"

    def test_partly(self):
        assert weather_glyph_kind(2) == "partly"

    def test_cloud(self):
        assert weather_glyph_kind(3) == "cloud"
        assert weather_glyph_kind(45) == "cloud"

    def test_rain(self):
        for code in (51, 61, 63, 65, 80, 82):
            assert weather_glyph_kind(code) == "rain"

    def test_snow(self):
        for code in (71, 75, 85, 86):
            assert weather_glyph_kind(code) == "snow"

    def test_storm(self):
        assert weather_glyph_kind(95) == "storm"
        assert weather_glyph_kind(99) == "storm"

    def test_unknown_defaults_cloud(self):
        assert weather_glyph_kind(123) == "cloud"
        assert weather_glyph_kind(None) == "cloud"
        assert weather_glyph_kind("x") == "cloud"


class TestDrawWeatherGlyph:
    def test_draws_without_error_for_all_kinds(self):
        # Smoke test: each family renders into a small box without raising.
        for code in (0, 2, 3, 61, 75, 95):
            img = Image.new("RGB", (64, 64), (255, 255, 255))
            draw_weather_glyph(ImageDraw.Draw(img), (4, 4, 60, 60), code)
            # Something was drawn (not still all-white).
            assert img.getcolors(maxcolors=100000) is not None
            assert len(img.getcolors(maxcolors=100000)) > 1


# ---------------------------------------------------------------------------
# _accent_color
# ---------------------------------------------------------------------------

class TestAccentColor:
    def test_red(self):
        assert _accent_color("red") == (200, 0, 0)

    def test_yellow(self):
        assert _accent_color("yellow") == (230, 170, 0)

    def test_none_is_black(self):
        assert _accent_color("none") == (0, 0, 0)

    def test_unknown_defaults_black(self):
        assert _accent_color("teal") == (0, 0, 0)

    def test_case_and_whitespace_insensitive(self):
        assert _accent_color("  RED ") == (200, 0, 0)

    def test_non_string_defaults_black(self):
        assert _accent_color(None) == (0, 0, 0)
        assert _accent_color(42) == (0, 0, 0)


# ---------------------------------------------------------------------------
# _ascii_only
# ---------------------------------------------------------------------------

class TestAsciiOnly:
    def test_pure_ascii_unchanged(self):
        assert _ascii_only("Heavy rain today.") == "Heavy rain today."

    def test_replaces_multibyte_with_space(self):
        result = _ascii_only("Rain 雨 today")
        assert "雨" not in result
        assert "Rain" in result
        assert "today" in result

    def test_collapses_whitespace(self):
        result = _ascii_only("too   many    spaces")
        assert result == "too many spaces"

    def test_strips_leading_trailing(self):
        assert _ascii_only("  hello  ") == "hello"

    def test_non_string_returns_empty(self):
        assert _ascii_only(None) == ""
        assert _ascii_only(42) == ""
        assert _ascii_only([]) == ""

    def test_empty_string(self):
        assert _ascii_only("") == ""

    def test_all_multibyte_returns_empty(self):
        assert _ascii_only("雨天注意") == ""

    def test_mixed_collapses_correctly(self):
        result = _ascii_only("Hello 世界 World")
        assert result == "Hello   World" or result == "Hello World"
        assert "世界" not in result


# ---------------------------------------------------------------------------
# _cover_crop_top_center
# ---------------------------------------------------------------------------

def _make_image(w, h, color=(128, 64, 32)):
    img = Image.new("RGB", (w, h), color)
    return img


class TestCoverCropTopCenter:
    def test_output_size_matches_target(self):
        img = _make_image(800, 600)
        result = _cover_crop_top_center(img, 400, 300)
        assert result.size == (400, 300)

    def test_wide_source_crops_horizontally(self):
        # 1000x200 → target 200x200 (aspect ratio 1:1)
        # src_ratio=5.0 > target_ratio=1.0 → crop horizontally, centered
        img = _make_image(1000, 200)
        result = _cover_crop_top_center(img, 200, 200)
        assert result.size == (200, 200)

    def test_tall_source_crops_vertically_from_top(self):
        # 200x1000 → target 200x200 (aspect ratio 1:1)
        # src_ratio=0.2 < target_ratio=1.0 → crop vertically from top
        img = _make_image(200, 1000)
        result = _cover_crop_top_center(img, 200, 200)
        assert result.size == (200, 200)

    def test_exact_aspect_ratio_no_crop_needed(self):
        img = _make_image(960, 640)
        result = _cover_crop_top_center(img, 960, 640)
        assert result.size == (960, 640)

    def test_upscale_small_source(self):
        img = _make_image(100, 100)
        result = _cover_crop_top_center(img, 400, 400)
        assert result.size == (400, 400)

    def test_tall_source_preserves_top_row_color(self):
        # Create a tall image: top half red, bottom half blue.
        img = Image.new("RGB", (100, 200))
        for y in range(100):
            for x in range(100):
                img.putpixel((x, y), (255, 0, 0))
        for y in range(100, 200):
            for x in range(100):
                img.putpixel((x, y), (0, 0, 255))

        # Target 100x100: should crop from top, so result should be mostly red
        result = _cover_crop_top_center(img, 100, 100)
        # Sample the center pixel of the result
        cx, cy = result.size[0] // 2, result.size[1] // 2
        r, g, b = result.getpixel((cx, cy))
        assert r > 200, "Top-anchored crop should show the red (top) half"
        assert b < 50

    def test_wide_source_centers_horizontally(self):
        # Create a wide image: left third green, center third red, right third green.
        w, h = 300, 100
        img = Image.new("RGB", (w, h), (0, 255, 0))
        for y in range(h):
            for x in range(100, 200):
                img.putpixel((x, y), (255, 0, 0))

        # Target 100x100 from 300x100: src_ratio=3 > target_ratio=1
        # crop_h=100, crop_w=round(100*1)=100, left=(300-100)//2=100
        # So the cropped region is exactly the red center strip.
        result = _cover_crop_top_center(img, 100, 100)
        cx, cy = result.size[0] // 2, result.size[1] // 2
        r, g, b = result.getpixel((cx, cy))
        assert r > 200, "Center-anchored crop should show the red center strip"
        assert g < 50

    def test_returns_rgb_image(self):
        img = _make_image(400, 300)
        result = _cover_crop_top_center(img, 200, 150)
        assert result.mode == "RGB"
