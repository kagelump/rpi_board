#!/usr/bin/env python3
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import load_settings, write_json
from scripts.openrouter.network import describe_network_error, urlopen_with_context


def _compact_text(fragment):
    no_tags = re.sub(r"<[^>]+>", " ", fragment, flags=re.S)
    clean = unescape(no_tags)
    clean = clean.replace("\u3000", " ")
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip()


def _to_int(value):
    if value is None:
        return None
    digits = re.sub(r"[^\d\-]", "", value)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _section_issue_time(html, marker):
    idx = html.find(marker)
    if idx < 0:
        return None
    snippet = html[idx : idx + 1200]
    match = re.search(r'<p class="yjSt yjw_note_h2">([^<]+)</p>', snippet, flags=re.S)
    if not match:
        return None
    return _compact_text(match.group(1))


def _extract_today_tomorrow(html):
    missing = []
    section_start = html.find('<div class="forecastCity">')
    section_end = html.find("<!-- 警報・注意報 -->", section_start if section_start >= 0 else 0)
    if section_start < 0 or section_end < 0:
        return [], ["today_tomorrow"]
    block = html[section_start:section_end]

    day_pattern = re.compile(
        r"<p class=\"date\">\s*<span[^>]*>(?P<date>[^<]+)</span>\((?:<span[^>]*>)?(?P<weekday>[^<]+)(?:</span>)?\)\s*</p>"
        r".*?<p class=\"pict\">.*?alt=\"(?P<alt>[^\"]*)\">(?P<condition>.*?)</p>"
        r".*?<li class=\"high\"><em>(?P<high>-?\d+)</em>℃\[(?P<high_delta>[^\]]+)\]</li>"
        r".*?<li class=\"low\"><em>(?P<low>-?\d+)</em>℃\[(?P<low_delta>[^\]]+)\]</li>"
        r".*?<tr class=\"time\">(?P<time_row>.*?)</tr>"
        r".*?<tr class=\"precip\">(?P<precip_row>.*?)</tr>"
        r".*?<dt>風：</dt>\s*<dd>(?P<wind>.*?)</dd>"
        r".*?<dt>波：</dt>\s*<dd>(?P<wave>.*?)</dd>",
        flags=re.S,
    )

    days = []
    for match in day_pattern.finditer(block):
        times = [_compact_text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", match.group("time_row"), flags=re.S)]
        precip_raw = [_compact_text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", match.group("precip_row"), flags=re.S)]
        precip_pairs = []
        for period, value in zip(times, precip_raw):
            precip_pairs.append(
                {
                    "period": period,
                    "precipitation_probability_pct": _to_int(value) if value != "---" else None,
                    "raw": value,
                }
            )

        condition_text = _compact_text(match.group("condition")) or _compact_text(match.group("alt"))
        days.append(
            {
                "date_label": _compact_text(match.group("date")),
                "weekday": _compact_text(match.group("weekday")),
                "condition": condition_text,
                "temp_max_c": _to_int(match.group("high")),
                "temp_min_c": _to_int(match.group("low")),
                "temp_max_delta": _compact_text(match.group("high_delta")),
                "temp_min_delta": _compact_text(match.group("low_delta")),
                "precipitation_windows": precip_pairs,
                "wind": _compact_text(match.group("wind")),
                "wave": _compact_text(match.group("wave")),
            }
        )

    if len(days) < 2:
        missing.append("today_tomorrow")
    return days, missing


def _extract_alerts(html):
    section_start = html.find('<div id="wrnrpt"')
    section_end = html.find("<!-- 花粉モジュール -->", section_start if section_start >= 0 else 0)
    if section_start < 0 or section_end < 0:
        return [], ["alerts"]
    block = html[section_start:section_end]
    alerts = []
    for match in re.finditer(
        r"<dt><span class=\"ico[^\"]*\"><span>(?P<level>[^<]+)</span></span></dt>\s*<dd>(?P<text>[^<]+)</dd>",
        block,
        flags=re.S,
    ):
        alerts.append({"level": _compact_text(match.group("level")), "text": _compact_text(match.group("text"))})
    if not alerts:
        return [], ["alerts"]
    return alerts, []


def _extract_weekly(html):
    section_start = html.find('<div id="yjw_week"')
    section_end = html.find("<!--指数情報-->", section_start if section_start >= 0 else 0)
    if section_start < 0 or section_end < 0:
        return [], ["weekly"]
    block = html[section_start:section_end]

    table_match = re.search(r"<tbody>(?P<body>.*?)</tbody>", block, flags=re.S)
    if not table_match:
        return [], ["weekly"]
    tbody = table_match.group("body")
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbody, flags=re.S)
    if len(rows) < 4:
        return [], ["weekly"]

    header_cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[0], flags=re.S)
    date_meta = []
    for cell in header_cells[1:]:
        date_match = re.search(r"(\d{1,2}月\d{1,2}日)", cell)
        weekday_match = re.search(r"\(([^)]+)\)", _compact_text(cell))
        date_meta.append(
            {
                "date_label": date_match.group(1) if date_match else _compact_text(cell),
                "weekday": weekday_match.group(1) if weekday_match else None,
            }
        )

    weather_cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[1], flags=re.S)[1:]
    temp_cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[2], flags=re.S)[1:]
    precip_cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[3], flags=re.S)[1:]

    weekly = []
    for idx, meta in enumerate(date_meta):
        weather_raw = weather_cells[idx] if idx < len(weather_cells) else ""
        condition = _compact_text(weather_raw)
        alt_match = re.search(r'alt=\"([^\"]+)\"', weather_raw)
        if alt_match:
            condition = _compact_text(alt_match.group(1))

        temp_text = _compact_text(temp_cells[idx]) if idx < len(temp_cells) else ""
        temp_numbers = [int(x) for x in re.findall(r"-?\d+", temp_text)]
        high = temp_numbers[0] if len(temp_numbers) > 0 else None
        low = temp_numbers[1] if len(temp_numbers) > 1 else None

        precip_text = _compact_text(precip_cells[idx]) if idx < len(precip_cells) else ""
        weekly.append(
            {
                "date_label": meta["date_label"],
                "weekday": meta["weekday"],
                "condition": condition,
                "temp_max_c": high,
                "temp_min_c": low,
                "precipitation_probability_pct": _to_int(precip_text),
            }
        )

    if not weekly:
        return [], ["weekly"]
    return weekly, []


def _extract_pollen(html):
    section_start = html.find('<div class="pollenInduction')
    section_end = html.find("<!--/ 花粉モジュール -->", section_start if section_start >= 0 else 0)
    if section_start < 0 or section_end < 0:
        return [], ["pollen"]
    block = html[section_start:section_end]
    rows = []
    for match in re.finditer(
        r'<p class="date">(?P<date>.*?)</p>\s*<p class="flying">.*?<span class="type">(?P<level>[^<]+)</span>',
        block,
        flags=re.S,
    ):
        rows.append({"date_label": _compact_text(match.group("date")), "level": _compact_text(match.group("level"))})
    if not rows:
        return [], ["pollen"]
    return rows, []


def _extract_heatstroke(html):
    section_start = html.find('<div class="mdheatstrokeInduction">')
    section_end = html.find("<!--週間の天気-->", section_start if section_start >= 0 else 0)
    if section_start < 0 or section_end < 0:
        return [], ["heatstroke"]
    block = html[section_start:section_end]
    rows = []
    for match in re.finditer(
        r'<p class="day">(?P<date>.*?)</p>.*?<p class="label"><span>(?P<label>[^<]+)</span></p>\s*<p class="comment">(?P<note>[^<]+)</p>',
        block,
        flags=re.S,
    ):
        rows.append(
            {
                "date_label": _compact_text(match.group("date")),
                "level": _compact_text(match.group("label")),
                "note": _compact_text(match.group("note")),
            }
        )
    if not rows:
        return [], ["heatstroke"]
    return rows, []


def _extract_indices(html):
    section_start = html.find('<div class="indexList">')
    section_end = html.find("<!--/指数情報-->", section_start if section_start >= 0 else 0)
    if section_end < 0:
        section_end = html.find("<!-- /指数情報 -->", section_start if section_start >= 0 else 0)
    if section_start < 0 or section_end < 0:
        return {"day_labels": [], "days": []}, ["indices"]
    block = html[section_start:section_end]

    day_labels = [_compact_text(x) for x in re.findall(r'<li class="tabView_item"><a [^>]*>(.*?)</a></li>', block, flags=re.S)]
    day_blocks = list(
        re.finditer(
            r'<div class="tabView_content[^"]*" id="(?P<id>index-\d+)">(?P<body>.*?)(?=<div class="tabView_content|<!--/指数情報-->|<!-- /指数情報 -->)',
            block,
            flags=re.S,
        )
    )
    days = []
    for idx, day_block in enumerate(day_blocks):
        body = day_block.group("body")
        items = {}
        for item in re.finditer(
            r'<dl class="indexList_item[^"]*">\s*<dt>(?P<name>[^<]+)</dt>\s*<dd>\s*<p class="index_value[^"]*"><span>(?P<score>[^<]+)</span></p>\s*<p class="index_text">(?P<text>[^<]+)</p>',
            body,
            flags=re.S,
        ):
            label = _compact_text(item.group("name"))
            items[label] = {
                "score_text": _compact_text(item.group("score")),
                "score": _to_int(item.group("score")),
                "note": _compact_text(item.group("text")),
            }
        days.append(
            {
                "date_label": day_labels[idx] if idx < len(day_labels) else day_block.group("id"),
                "items": items,
            }
        )

    if not days:
        return {"day_labels": day_labels, "days": []}, ["indices"]
    return {"day_labels": day_labels, "days": days}, []


def parse_yahoo_weather_html(html, page_url):
    missing_sections = []
    today_tomorrow, missing = _extract_today_tomorrow(html)
    missing_sections.extend(missing)
    alerts, missing = _extract_alerts(html)
    missing_sections.extend(missing)
    weekly, missing = _extract_weekly(html)
    missing_sections.extend(missing)
    pollen, missing = _extract_pollen(html)
    missing_sections.extend(missing)
    heatstroke, missing = _extract_heatstroke(html)
    missing_sections.extend(missing)
    indices, missing = _extract_indices(html)
    missing_sections.extend(missing)

    section_timestamps = {
        "today_tomorrow": _section_issue_time(html, "今日明日の天気"),
        "weekly": _section_issue_time(html, '<div id="week" class="yjw_title_h2 yjw_clr">'),
        "pollen": _section_issue_time(html, '<div class="pollenInduction target_modules" id="kafunnav">'),
        "indices": _section_issue_time(html, '<div class="yjw_title_h2" id="index2days">'),
    }

    return {
        "source": "yahoo-weather",
        "page_url": page_url,
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "section_timestamps": section_timestamps,
        "today_tomorrow": today_tomorrow,
        "alerts": alerts,
        "weekly": weekly,
        "pollen": pollen,
        "heatstroke": heatstroke,
        "indices": indices,
        "missing_sections": sorted(set(missing_sections)),
    }


def _fetch_html(url, timeout, retries, settings):
    last_error = None
    request = urllib.request.Request(
        url=url,
        method="GET",
        headers={"User-Agent": "Mozilla/5.0 (compatible; rpi-board-weather/1.0)"},
    )
    for attempt in range(retries + 1):
        try:
            with urlopen_with_context(request, timeout=timeout, settings=settings) as response:
                return response.read().decode("utf-8", "ignore")
        except urllib.error.URLError as error:
            last_error = error
            if attempt < retries:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"failed to fetch Yahoo weather page: {describe_network_error(last_error)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    parser.add_argument("--html-input", default=None, help="Optional local HTML file for parser testing")
    args = parser.parse_args()

    settings = load_settings()
    output_path = args.output or settings["runtime"]["yahoo_weather_file"]
    page_url = settings["pipeline"]["yahoo_weather_url"]

    if args.html_input:
        html = Path(args.html_input).read_text(encoding="utf-8")
    else:
        html = _fetch_html(
            page_url,
            timeout=settings["pipeline"]["yahoo_timeout_seconds"],
            retries=settings["pipeline"]["yahoo_retry_count"],
            settings=settings,
        )
    payload = parse_yahoo_weather_html(html, page_url=page_url)
    write_json(output_path, payload)
    print(output_path)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"fetch_yahoo_weather.py error: {exc}", file=sys.stderr)
        raise
