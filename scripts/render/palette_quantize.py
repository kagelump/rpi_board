#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from PIL import Image

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import absolute_path, load_settings


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
    # Use nearest-color mapping with no dithering so large areas stay solid.
    quantized = image.quantize(palette=palette, dither=Image.NONE).convert("RGB")
    quantized.save(output_path)
    print(str(output_path))


if __name__ == "__main__":
    main()
