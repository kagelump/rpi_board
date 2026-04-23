#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import absolute_path, load_settings, read_json, write_json


def _font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    ]
    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_wrapped(draw, text, xy, width, font, fill, line_spacing=8, max_lines=2):
    words = text.split()
    lines = []
    current = []
    for word in words:
        trial = " ".join(current + [word])
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] <= width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    lines = lines[:max_lines]
    x, y = xy
    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += font.size + line_spacing
    return y


def _fit_single_line(draw, text, width, font):
    text = text.strip()
    if draw.textbbox((0, 0), text, font=font)[2] <= width:
        return text
    words = text.split()
    if not words:
        return ""
    current = []
    for word in words:
        trial = " ".join(current + [word]).strip()
        with_ellipsis = f"{trial}..."
        if draw.textbbox((0, 0), with_ellipsis, font=font)[2] <= width:
            current.append(word)
        else:
            break
    if not current:
        # Fallback for a very long single token.
        return text[: max(1, len(text) // 2)] + "..."
    return " ".join(current).strip() + "..."


def _draw_text_with_stroke(draw, xy, text, font, fill=(0, 0, 0), stroke_fill=(255, 255, 255), stroke_width=3):
    draw.text(xy, text, fill=fill, font=font, stroke_fill=stroke_fill, stroke_width=stroke_width)


def _fit_font_size(draw, text, max_width, sizes):
    for size in sizes:
        font = _font(size)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return _font(sizes[-1])


def _load_hero(settings):
    hero_path = absolute_path(settings["runtime"]["hero_file"])
    if not hero_path.exists():
        return None
    try:
        return Image.open(hero_path).convert("RGB")
    except OSError:
        return None


def _cover_crop_top_center(image, target_width, target_height):
    src_w, src_h = image.size
    src_ratio = src_w / src_h
    target_ratio = target_width / target_height

    if src_ratio > target_ratio:
        # Source is wider: crop horizontally, centered.
        crop_h = src_h
        crop_w = int(round(crop_h * target_ratio))
        left = max(0, (src_w - crop_w) // 2)
        top = 0
    else:
        # Source is taller: crop vertically, anchored to top.
        crop_w = src_w
        crop_h = int(round(crop_w / target_ratio))
        left = 0
        top = 0

    cropped = image.crop((left, top, left + crop_w, top + crop_h))
    return cropped.resize((target_width, target_height), Image.Resampling.LANCZOS)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    settings = load_settings()
    input_path = args.input or settings["runtime"]["brief_file"]
    output_path = args.output or settings["runtime"]["final_file"]
    preview_path = settings["runtime"]["preview_file"]

    payload = read_json(input_path)
    brief = payload["brief"]
    width = settings["display"]["width"]
    height = settings["display"]["height"]
    board = Image.new("RGB", (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(board)

    hero = _load_hero(settings)
    if hero is not None:
        hero = _cover_crop_top_center(hero, width, height)
        board.paste(hero, (0, 0))
    else:
        draw.rectangle((0, 0, width - 1, height - 1), outline=(0, 0, 0), width=3)
        _draw_text_with_stroke(
            draw,
            (26, height // 2 - 18),
            "Illustration unavailable",
            _font(40),
            fill=(0, 0, 0),
            stroke_fill=(255, 255, 255),
            stroke_width=2,
        )

    # Overlay zone: minimal text panel for headline/subtitle.
    panel_h = int(height * 0.24)
    panel_top = height - panel_h
    draw.rectangle((0, panel_top, width, height), fill=(255, 255, 255))
    draw.line((0, panel_top, width, panel_top), fill=(0, 0, 0), width=2)

    headline = brief.get("headline", "").strip()
    subtitle = brief.get("subtitle", "").strip()
    if not subtitle:
        subtitle = brief.get("tomorrow_preview", "").strip()
    if not subtitle:
        subtitle = "Weather shifts through the day."

    headline_font = _fit_font_size(draw, headline, width - 44, [64, 58, 52, 46, 42, 38])
    subtitle_font = _fit_font_size(draw, subtitle, width - 44, [38, 34, 30, 28, 26])
    headline_line = _fit_single_line(draw, headline, width - 44, headline_font)
    subtitle_line = _fit_single_line(draw, subtitle, width - 44, subtitle_font)
    draw.text((22, panel_top + 18), headline_line, fill=(0, 0, 0), font=headline_font)
    draw.text((24, panel_top + 20 + headline_font.size + 18), subtitle_line, fill=(0, 0, 0), font=subtitle_font)

    # Keep tiny operational metadata and high-temp corner chip.
    date_text = payload["today"]["daily_summary"]["date"]
    draw.text((22, 14), date_text, fill=(0, 0, 0), font=_font(28))
    high_c = int(round(payload["today"]["daily_summary"]["temp_max_c"]))
    high_label = f"{high_c}C"
    chip_w = draw.textbbox((0, 0), high_label, font=_font(40))[2] + 24
    chip_h = 58
    chip_x1 = width - chip_w - 18
    chip_y1 = 14
    draw.rectangle((chip_x1, chip_y1, chip_x1 + chip_w, chip_y1 + chip_h), fill=(255, 255, 255), outline=(0, 0, 0), width=2)
    draw.text((chip_x1 + 12, chip_y1 + 8), high_label, fill=(0, 0, 0), font=_font(40))

    board.save(absolute_path(output_path))
    board.resize((width // 2, height // 2)).save(absolute_path(preview_path))

    status = {
        "last_success_at": payload["generated_at_local"],
        "brief_source": payload.get("brief_source", "deterministic"),
    }
    write_json(settings["runtime"]["stale_file"], status)
    print(output_path)


if __name__ == "__main__":
    main()
