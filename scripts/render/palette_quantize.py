#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from PIL import Image

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import absolute_path, load_settings


GRAY_CHROMA_MAX = 10
GRAY_LUMA_MIN = 55
GRAY_LUMA_MAX = 190
GRAY_CHANNEL_DELTA_MAX = 8
BLACK_LUMA_MAX = 35
RED_MIN_R = 120
RED_MAX_G = 95
RED_MAX_B = 95
YELLOW_MIN_R = 135
YELLOW_MIN_G = 120
YELLOW_MAX_B = 95


def _luma(r, g, b):
    # Integer approximation of Rec. 709 luma.
    return (2126 * r + 7152 * g + 722 * b) // 10000


def _is_protected_color(r, g, b):
    luma = _luma(r, g, b)
    if luma <= BLACK_LUMA_MAX:
        return True
    if r >= RED_MIN_R and g <= RED_MAX_G and b <= RED_MAX_B:
        return True
    if r >= YELLOW_MIN_R and g >= YELLOW_MIN_G and b <= YELLOW_MAX_B:
        return True
    return False


def _is_gray_candidate(r, g, b):
    luma = _luma(r, g, b)
    if luma < GRAY_LUMA_MIN or luma > GRAY_LUMA_MAX:
        return False
    chroma = max(r, g, b) - min(r, g, b)
    if chroma > GRAY_CHROMA_MAX:
        return False
    # Keep only near-neutral pixels so warm/cool tints do not dither into red/yellow speckles.
    return (
        abs(r - g) <= GRAY_CHANNEL_DELTA_MAX
        and abs(r - b) <= GRAY_CHANNEL_DELTA_MAX
        and abs(g - b) <= GRAY_CHANNEL_DELTA_MAX
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    settings = load_settings()
    input_path = absolute_path(args.input or settings["runtime"]["final_file"])
    output_path = absolute_path(args.output or settings["runtime"]["final_file"])

    image = Image.open(input_path).convert("RGB")
    palette = Image.new("P", (1, 1))
    # White, Black, Red, Yellow (remaining palette entries stay zeroed).
    palette_values = [255, 255, 255, 0, 0, 0, 220, 0, 0, 220, 200, 0]
    palette_values.extend([0, 0, 0] * (256 - 4))
    palette.putpalette(palette_values)
    # Generate both variants and pick per-pixel: dither only for neutral gray midtones.
    q_nodither = image.quantize(palette=palette, dither=Image.NONE).convert("RGB")
    q_dither = image.quantize(palette=palette, dither=Image.FLOYDSTEINBERG).convert("RGB")

    src = image.load()
    nd = q_nodither.load()
    dd = q_dither.load()
    width, height = image.size

    # Region split mirrors compose_board: the art fills the top, the text panel
    # the bottom. The panel stays crisp (no dithering) so type and the accent
    # rule read sharply; the art region is dithered to fake extra tones from the
    # four available inks.
    panel_fraction = settings["display"].get("panel_fraction", 0.25)
    art_h = height - round(height * panel_fraction)

    out = Image.new("RGB", (width, height))
    dst = out.load()
    for y in range(height):
        in_panel = y >= art_h
        for x in range(width):
            r, g, b = src[x, y]
            if _is_protected_color(r, g, b):
                # Pure black/red/yellow snap cleanly everywhere.
                dst[x, y] = nd[x, y]
            elif in_panel:
                # Panel: dither only neutral grays, keep everything else crisp.
                dst[x, y] = dd[x, y] if _is_gray_candidate(r, g, b) else nd[x, y]
            else:
                # Art region: dither all non-protected pixels for tonal depth.
                dst[x, y] = dd[x, y]

    out.save(output_path)
    print(str(output_path))


if __name__ == "__main__":
    main()
