"""Tests for pure helpers in scripts/render/compose_board.py"""
import pytest
from PIL import Image

from scripts.render.compose_board import (
    _accent_color,
    _ascii_only,
    _cover_crop_top_center,
    _is_degraded_source,
    draw_weather_glyph,
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
from PIL import ImageDraw


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
