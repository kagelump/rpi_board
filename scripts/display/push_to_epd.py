#!/usr/bin/env python3
import argparse
import importlib
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import absolute_path, load_settings


def _load_epd_module(candidates):
    for name in candidates:
        try:
            return importlib.import_module(name), name
        except ImportError:
            continue
    raise RuntimeError(f"Unable to import any waveshare module from: {candidates}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--mode", choices=["local_preview", "pi_display"], default=None)
    args = parser.parse_args()

    settings = load_settings()
    image_path = absolute_path(args.input or settings["runtime"]["final_file"])
    mode = args.mode or settings["display"]["mode"]

    if mode != "pi_display":
        print(f"Skipping hardware display in mode={mode}. Image: {image_path}")
        return

    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError("Pillow must be installed to push image to EPD") from error

    epd_module, module_name = _load_epd_module(settings["display"]["waveshare_module_candidates"])
    epd = epd_module.EPD()
    epd.init()
    image = Image.open(image_path).convert("RGB")
    epd.display(epd.getbuffer(image))
    epd.sleep()
    print(f"Pushed image via {module_name}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"push_to_epd.py error: {exc}", file=sys.stderr)
        raise
