#!/usr/bin/env python3
"""Deterministic colour metrics for the 4-ink Waveshare e-ink panel.

The display can only show white / black / red / yellow. Good board art (a) uses
a balance of all four inks instead of looking monochrome, and (b) avoids hues
the panel cannot render (blue, green, purple...) which quantize into muddy
speckle. This module measures both straight from the pixels, so it is objective
and reproducible (unlike asking the vision judge).

Analyse the HERO art (the model's raw output), not the composed board -- the
board's white text strip and black headline would skew the balance.
"""
import colorsys
import io
from pathlib import Path

from PIL import Image


# Exact device inks (must match scripts/render/palette_quantize.py).
DEVICE_PALETTE = [
    ("white", (255, 255, 255)),
    ("black", (0, 0, 0)),
    ("red", (220, 0, 0)),
    ("yellow", (220, 200, 0)),
]

_PALETTE_IMG = Image.new("P", (1, 1))
_vals = []
for _name, _rgb in DEVICE_PALETTE:
    _vals += list(_rgb)
_vals += [0, 0, 0] * (256 - len(DEVICE_PALETTE))
_PALETTE_IMG.putpalette(_vals)
_RGB_TO_NAME = {rgb: name for name, rgb in DEVICE_PALETTE}

# Saturated source hues outside red/orange/yellow cannot be shown by the panel.
_PRESENT_MIN = 0.03  # an ink "counts" toward balance above 3% coverage


def _load(image):
    if isinstance(image, (str, Path)):
        return Image.open(image).convert("RGB")
    if isinstance(image, (bytes, bytearray)):
        return Image.open(io.BytesIO(image)).convert("RGB")
    return image.convert("RGB")


def _hue_name(deg):
    if deg < 70:
        return "orange"  # still inside the on-palette band; shouldn't be flagged
    if deg < 160:
        return "green"
    if deg < 200:
        return "cyan"
    if deg < 260:
        return "blue"
    if deg < 300:
        return "purple"
    return "magenta"


def analyze(image):
    """Return colour metrics for an image (path, bytes, or PIL.Image)."""
    im = _load(image)
    im.thumbnail((160, 160))
    w, h = im.size
    total = max(1, w * h)

    # Coverage: snap each pixel to the nearest device ink (no dither), like the
    # panel does, then tally the four buckets.
    quant = im.quantize(palette=_PALETTE_IMG, dither=Image.NONE).convert("RGB")
    counts = {name: 0 for name, _ in DEVICE_PALETTE}
    for cnt, rgb in quant.getcolors(maxcolors=100000) or []:
        name = _RGB_TO_NAME.get(rgb)
        if name:
            counts[name] += cnt
    coverage = {name: round(counts[name] / total, 4) for name, _ in DEVICE_PALETTE}

    # Off-palette: chromatic pixels whose hue is not red/orange/yellow.
    px = im.load()
    off = 0
    buckets = {}
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            hh, ss, vv = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if ss >= 0.25 and vv >= 0.20:
                deg = hh * 360
                if not (deg <= 70 or deg >= 330):
                    off += 1
                    name = _hue_name(deg)
                    buckets[name] = buckets.get(name, 0) + 1
    off_pct = round(off / total, 4)

    dominant_frac = max(coverage.values())
    present = [name for name, frac in coverage.items() if frac >= _PRESENT_MIN]
    both_accents = coverage["red"] >= _PRESENT_MIN and coverage["yellow"] >= _PRESENT_MIN

    # Balance score: all four inks present and not dominated by one => high.
    score = 2.5 * len(present)
    if dominant_frac >= 0.90:
        score -= 3
    elif dominant_frac >= 0.80:
        score -= 1.5
    if not both_accents:
        score -= 2
    score = max(0.0, min(10.0, round(score, 1)))

    return {
        "palette_balance": score,
        "coverage": coverage,  # fraction per ink
        "off_palette_pct": off_pct,
        "off_palette_hue": max(buckets, key=buckets.get) if buckets else "",
        "dominant_ink": max(coverage, key=coverage.get),
        "monochrome": (coverage["red"] + coverage["yellow"]) < _PRESENT_MIN,
    }


def coverage_str(coverage):
    """Compact 'W/K/R/Y' percentage string for reports."""
    return "/".join(str(round(coverage[name] * 100)) for name, _ in DEVICE_PALETTE)


if __name__ == "__main__":
    import sys
    print(analyze(sys.argv[1]))
