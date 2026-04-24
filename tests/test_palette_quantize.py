"""Tests for pure pixel-classification helpers in scripts/render/palette_quantize.py"""
import pytest

from scripts.render.palette_quantize import (
    BLACK_LUMA_MAX,
    GRAY_CHANNEL_DELTA_MAX,
    GRAY_CHROMA_MAX,
    GRAY_LUMA_MAX,
    GRAY_LUMA_MIN,
    RED_MAX_B,
    RED_MAX_G,
    RED_MIN_R,
    YELLOW_MAX_B,
    YELLOW_MIN_G,
    YELLOW_MIN_R,
    _is_gray_candidate,
    _is_protected_color,
    _luma,
)


# ---------------------------------------------------------------------------
# _luma
# ---------------------------------------------------------------------------

class TestLuma:
    def test_white(self):
        assert _luma(255, 255, 255) == 255

    def test_black(self):
        assert _luma(0, 0, 0) == 0

    def test_pure_red_darker_than_pure_green(self):
        # Green channel dominates luma (0.7152 coefficient)
        assert _luma(255, 0, 0) < _luma(0, 255, 0)

    def test_midgray(self):
        # All channels equal → luma ≈ that value
        v = 128
        luma = _luma(v, v, v)
        assert abs(luma - v) <= 2  # rounding tolerance

    def test_returns_int(self):
        assert isinstance(_luma(100, 100, 100), int)


# ---------------------------------------------------------------------------
# _is_protected_color
# ---------------------------------------------------------------------------

class TestIsProtectedColor:
    def test_black_is_protected(self):
        assert _is_protected_color(0, 0, 0) is True

    def test_near_black_protected(self):
        v = BLACK_LUMA_MAX
        assert _is_protected_color(v, v, v) is True

    def test_white_is_not_protected(self):
        assert _is_protected_color(255, 255, 255) is False

    def test_pure_red_is_protected(self):
        assert _is_protected_color(220, 0, 0) is True

    def test_red_borderline(self):
        # Just on the threshold: r=RED_MIN_R, g=RED_MAX_G, b=RED_MAX_B
        assert _is_protected_color(RED_MIN_R, RED_MAX_G, RED_MAX_B) is True

    def test_not_red_high_green(self):
        # r is high, g exceeds RED_MAX_G (so not red), b exceeds YELLOW_MAX_B (so not yellow)
        assert _is_protected_color(200, 150, 100) is False

    def test_yellow_is_protected(self):
        assert _is_protected_color(220, 200, 0) is True

    def test_yellow_borderline(self):
        assert _is_protected_color(YELLOW_MIN_R, YELLOW_MIN_G, YELLOW_MAX_B) is True

    def test_not_yellow_high_blue(self):
        assert _is_protected_color(220, 200, 100) is False

    def test_midgray_is_not_protected(self):
        v = 128
        assert _is_protected_color(v, v, v) is False


# ---------------------------------------------------------------------------
# _is_gray_candidate
# ---------------------------------------------------------------------------

class TestIsGrayCandidate:
    def test_neutral_midgray(self):
        assert _is_gray_candidate(128, 128, 128) is True

    def test_light_gray(self):
        # Within luma range and neutral
        assert _is_gray_candidate(180, 180, 180) is True

    def test_too_bright_not_gray(self):
        # luma > GRAY_LUMA_MAX
        assert _is_gray_candidate(220, 220, 220) is False

    def test_too_dark_not_gray(self):
        # luma < GRAY_LUMA_MIN
        assert _is_gray_candidate(20, 20, 20) is False

    def test_high_chroma_not_gray(self):
        # r=200 g=100 b=100 → chroma=100, way above GRAY_CHROMA_MAX
        assert _is_gray_candidate(150, 130, 80) is False

    def test_large_channel_delta_not_gray(self):
        # Slightly warm tint that exceeds GRAY_CHANNEL_DELTA_MAX
        r, g, b = 120, 120 - GRAY_CHANNEL_DELTA_MAX - 1, 120
        assert _is_gray_candidate(r, g, b) is False

    def test_exact_luma_boundaries(self):
        # Exactly at GRAY_LUMA_MIN (neutral)
        v = GRAY_LUMA_MIN
        result = _is_gray_candidate(v, v, v)
        # At exact boundary the luma condition is `luma < GRAY_LUMA_MIN`, so v == GRAY_LUMA_MIN passes
        assert result is True

        # One below minimum
        v2 = GRAY_LUMA_MIN - 1
        assert _is_gray_candidate(v2, v2, v2) is False
