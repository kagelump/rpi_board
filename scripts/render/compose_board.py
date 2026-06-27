#!/usr/bin/env python3
import argparse
import math
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


def _accent_color(accent):
    """Map the brief's mood accent to an on-palette e-ink ink (black/red/yellow)."""
    mapping = {
        "red": (200, 0, 0),
        "yellow": (230, 170, 0),
        "none": (0, 0, 0),
    }
    if not isinstance(accent, str):
        return (0, 0, 0)
    return mapping.get(accent.strip().lower(), (0, 0, 0))


_BLACK = (0, 0, 0)
_YELLOW = (230, 170, 0)
_RED = (200, 0, 0)


def weather_glyph_kind(code):
    """Map an Open-Meteo weather code to a pictogram family."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "cloud"
    if code in (0, 1):
        return "clear"
    if code == 2:
        return "partly"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    return "cloud"


def _draw_sun(draw, cx, cy, radius, rays=True):
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=_YELLOW)
    if rays:
        ray_w = max(2, int(radius * 0.22))
        for i in range(8):
            angle = i * math.pi / 4
            x0 = cx + math.cos(angle) * radius * 1.35
            y0 = cy + math.sin(angle) * radius * 1.35
            x1 = cx + math.cos(angle) * radius * 1.85
            y1 = cy + math.sin(angle) * radius * 1.85
            draw.line([x0, y0, x1, y1], fill=_YELLOW, width=ray_w)


def _draw_cloud(draw, cx, cy, cw, fill=_BLACK):
    ch = cw * 0.6
    left, right = cx - cw / 2, cx + cw / 2
    top, bottom = cy - ch / 2, cy + ch / 2
    draw.ellipse([left, cy - ch * 0.15, left + cw * 0.55, bottom], fill=fill)
    draw.ellipse([right - cw * 0.55, cy - ch * 0.15, right, bottom], fill=fill)
    draw.ellipse([cx - cw * 0.32, top, cx + cw * 0.34, bottom], fill=fill)
    draw.rectangle([left + cw * 0.12, cy, right - cw * 0.12, bottom], fill=fill)


def draw_weather_glyph(draw, box, code):
    """Draw a simple, high-contrast weather pictogram inside box (x0,y0,x1,y1).

    Pure drawing on the four-ink palette; legible at chip size or full-frame.
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    s = min(w, h)
    cx, cy = x0 + w / 2, y0 + h / 2
    kind = weather_glyph_kind(code)

    if kind == "clear":
        _draw_sun(draw, cx, cy, s * 0.26)
        return
    if kind == "partly":
        _draw_sun(draw, cx - s * 0.16, cy - s * 0.16, s * 0.2)
        _draw_cloud(draw, cx + s * 0.08, cy + s * 0.12, s * 0.62)
        return

    # cloud-based families share a cloud body, then add precipitation marks.
    cloud_cy = cy - s * 0.12 if kind in ("rain", "snow", "storm") else cy
    _draw_cloud(draw, cx, cloud_cy, s * 0.72)
    drop_top = cloud_cy + s * 0.26
    if kind == "rain":
        line_w = max(2, int(s * 0.05))
        for dx in (-s * 0.2, 0, s * 0.2):
            draw.line([cx + dx, drop_top, cx + dx - s * 0.06, drop_top + s * 0.22], fill=_RED, width=line_w)
    elif kind == "snow":
        flake_r = max(2, int(s * 0.04))
        for dx in (-s * 0.2, 0, s * 0.2):
            draw.ellipse(
                [cx + dx - flake_r, drop_top - flake_r, cx + dx + flake_r, drop_top + flake_r],
                fill=_BLACK,
            )
    elif kind == "storm":
        bolt = [
            (cx - s * 0.02, drop_top - s * 0.04),
            (cx - s * 0.16, drop_top + s * 0.16),
            (cx - s * 0.02, drop_top + s * 0.12),
            (cx - s * 0.1, drop_top + s * 0.34),
            (cx + s * 0.16, drop_top + s * 0.04),
            (cx + s * 0.02, drop_top + s * 0.06),
        ]
        draw.polygon(bolt, fill=_YELLOW)


def _draw_chip(draw, x1, y1, text, font, accent, height, pad_x=12):
    """Draw a white chip with an accent outline and vertically-centered text.

    Returns the chip's right edge x so chips can be laid out left-to-right.
    """
    text_w = draw.textbbox((0, 0), text, font=font)[2]
    x2 = x1 + text_w + pad_x * 2
    draw.rectangle((x1, y1, x2, y1 + height), fill=(255, 255, 255), outline=accent, width=3)
    draw.text((x1 + pad_x, y1 + (height - font.size) // 2 - 2), text, fill=(0, 0, 0), font=font)
    return x2


def _is_degraded_source(source):
    """True when the brief is canned deterministic text, not LLM-authored.

    "cached" is a previously-generated LLM brief reused by the cost guardrail, so
    it is not degraded.
    """
    return source not in ("openrouter", "cached")


def _ascii_only(text):
    if not isinstance(text, str):
        return ""
    cleaned = "".join(ch if ord(ch) < 128 else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()


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
    # Single source of truth for the split: the art fills the region above the
    # text panel, the panel fills the rest. Image generation targets this same
    # region (see fal image_size), so nothing the model draws is cropped away.
    panel_fraction = settings["display"].get("panel_fraction", 0.25)
    panel_h = round(height * panel_fraction)
    panel_top = height - panel_h
    art_h = panel_top

    accent = _accent_color(brief.get("accent"))

    board = Image.new("RGB", (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(board)

    weather_code = payload["today"]["daily_summary"].get("weather_code")
    hero = _load_hero(settings)
    if hero is not None:
        hero = _cover_crop_top_center(hero, width, art_h)
        board.paste(hero, (0, 0))
    else:
        # No AI art this run: draw a clean code-based pictogram instead of an
        # error string, so the board still reads the weather at a glance.
        glyph_size = int(min(width, art_h) * 0.5)
        gx, gy = (width - glyph_size) // 2, (art_h - glyph_size) // 2
        draw_weather_glyph(draw, (gx, gy, gx + glyph_size, gy + glyph_size), weather_code)

    # Text panel below the art: a clean strip, with a mood-accented divider rule.
    draw.rectangle((0, panel_top, width, height), fill=(255, 255, 255))
    draw.rectangle((0, panel_top, width, panel_top + 5), fill=accent)

    headline = _ascii_only(brief.get("headline", ""))
    subtitle = _ascii_only(brief.get("subtitle", ""))
    if not subtitle:
        subtitle = _ascii_only(brief.get("tomorrow_preview", ""))
    if not subtitle:
        subtitle = "Weather shifts through the day."
    if not headline:
        fallback_condition = payload["today"]["daily_summary"].get("condition", "Weather update")
        headline = _ascii_only(fallback_condition + " expected today.")
    if not headline:
        headline = "Weather update"

    headline_font = _fit_font_size(draw, headline, width - 44, [64, 58, 52, 46, 42, 38])
    subtitle_font = _fit_font_size(draw, subtitle, width - 44, [38, 34, 30, 28, 26])
    headline_line = _fit_single_line(draw, headline, width - 44, headline_font)
    subtitle_line = _fit_single_line(draw, subtitle, width - 44, subtitle_font)
    draw.text((22, panel_top + 18), headline_line, fill=(0, 0, 0), font=headline_font)
    draw.text((24, panel_top + 20 + headline_font.size + 18), subtitle_line, fill=(0, 0, 0), font=subtitle_font)

    # Operational metadata as chips so it reads over any artwork.
    daily = payload["today"]["daily_summary"]
    chip_h = 58
    chip_y1 = 14

    # Date chip, top-left.
    date_text = payload.get("day_context", {}).get("date_pretty") or daily["date"]
    _draw_chip(draw, 18, chip_y1, date_text, _font(28), accent, chip_h)

    # Temp range chip, top-right; low-high outlined in the mood accent.
    high_c = int(round(daily["temp_max_c"]))
    low_c = int(round(daily["temp_min_c"]))
    temp_label = f"{low_c}-{high_c}C"
    chip_font = _font(40)
    temp_w = draw.textbbox((0, 0), temp_label, font=chip_font)[2] + 24
    chip_x1 = width - temp_w - 18
    _draw_chip(draw, chip_x1, chip_y1, temp_label, chip_font, accent, chip_h)

    # Glanceable weather pictogram chip, just left of the temp chip.
    glyph_x1 = chip_x1 - chip_h - 12
    draw.rectangle((glyph_x1, chip_y1, glyph_x1 + chip_h, chip_y1 + chip_h), fill=(255, 255, 255), outline=accent, width=3)
    pad = 9
    draw_weather_glyph(draw, (glyph_x1 + pad, chip_y1 + pad, glyph_x1 + chip_h - pad, chip_y1 + chip_h - pad), weather_code)

    # Unobtrusive degraded marker: a small red dot only when the LLM brief fell
    # back to the deterministic text, so a glance tells you the words are canned.
    if _is_degraded_source(payload.get("brief_source")):
        draw.ellipse((6, 6, 20, 20), fill=(200, 0, 0))

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
