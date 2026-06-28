"""Tests for the deterministic e-ink palette metrics."""
from PIL import Image

from scripts.render.palette_metrics import analyze, coverage_str


def _solid(rgb, size=(80, 80)):
    return Image.new("RGB", size, rgb)


def _quadrants(tl, tr, bl, br, size=80):
    im = Image.new("RGB", (size, size))
    h = size // 2
    im.paste(_solid(tl, (h, h)), (0, 0))
    im.paste(_solid(tr, (h, h)), (h, 0))
    im.paste(_solid(bl, (h, h)), (0, h))
    im.paste(_solid(br, (h, h)), (h, h))
    return im


class TestOffPalette:
    def test_pure_blue_is_off_palette(self):
        m = analyze(_solid((0, 0, 255)))
        assert m["off_palette_pct"] > 0.9
        assert m["off_palette_hue"] == "blue"

    def test_pure_green_is_off_palette(self):
        m = analyze(_solid((0, 200, 0)))
        assert m["off_palette_pct"] > 0.9
        assert m["off_palette_hue"] == "green"

    def test_red_and_yellow_are_on_palette(self):
        assert analyze(_solid((220, 0, 0)))["off_palette_pct"] < 0.02
        assert analyze(_solid((220, 200, 0)))["off_palette_pct"] < 0.02


class TestBalance:
    def test_all_four_inks_scores_high(self):
        m = analyze(_quadrants((255, 255, 255), (0, 0, 0), (220, 0, 0), (220, 200, 0)))
        assert m["palette_balance"] >= 8
        assert m["monochrome"] is False
        for ink in ("white", "black", "red", "yellow"):
            assert m["coverage"][ink] > 0.15

    def test_black_white_only_is_monochrome(self):
        m = analyze(_quadrants((255, 255, 255), (0, 0, 0), (255, 255, 255), (0, 0, 0)))
        assert m["monochrome"] is True
        assert m["palette_balance"] < 5

    def test_single_ink_dominance_penalized(self):
        # Mostly black with a sliver of red/yellow -> not balanced.
        im = _solid((0, 0, 0))
        im.paste(_solid((220, 0, 0), (8, 8)), (0, 0))
        assert analyze(im)["palette_balance"] < 6


def test_coverage_str_format():
    m = analyze(_quadrants((255, 255, 255), (0, 0, 0), (220, 0, 0), (220, 200, 0)))
    parts = coverage_str(m["coverage"]).split("/")
    assert len(parts) == 4 and all(p.isdigit() for p in parts)
