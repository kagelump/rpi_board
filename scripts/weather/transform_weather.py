#!/usr/bin/env python3
import argparse
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import load_settings, read_json, write_json


WEATHER_LABELS = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    80: "Rain showers",
    81: "Moderate showers",
    82: "Violent showers",
    95: "Thunderstorm",
}


def _daily_summary(raw, idx):
    daily = raw["daily"]
    code = daily["weather_code"][idx]
    return {
        "date": daily["time"][idx],
        "condition": WEATHER_LABELS.get(code, f"Code {code}"),
        "weather_code": code,
        "temp_max_c": round(daily["temperature_2m_max"][idx], 1),
        "temp_min_c": round(daily["temperature_2m_min"][idx], 1),
        "rain_prob_max_pct": int(daily["precipitation_probability_max"][idx]),
        "rain_sum_mm": round(daily["precipitation_sum"][idx], 1),
    }


def _hourly_rows(raw, date_str):
    rows = []
    hourly = raw["hourly"]
    for i, stamp in enumerate(hourly["time"]):
        if not stamp.startswith(date_str):
            continue
        rows.append(
            {
                "time": stamp,
                "temp_c": round(hourly["temperature_2m"][i], 1),
                "rain_probability_pct": int(hourly["precipitation_probability"][i]),
                "rain_mm": round(hourly["precipitation"][i], 1),
                "weather_code": int(hourly["weather_code"][i]),
            }
        )
    return rows


def _heavy_rain_rows(rows):
    heavy_codes = {63, 65, 81, 82, 95}
    heavy = []
    for row in rows:
        # Treat heavy rain as either strong measured precip, or strong probability
        # with a weather code indicating moderate/heavy rain.
        if row["rain_mm"] >= 1.0:
            heavy.append(row)
            continue
        if row["rain_probability_pct"] >= 75 and row["weather_code"] in heavy_codes:
            heavy.append(row)
    return heavy


def _rain_window(rows):
    heavy = _heavy_rain_rows(rows)
    if not heavy:
        return "No heavy rain expected"
    start = heavy[0]["time"].split("T", 1)[1][:5]
    end = heavy[-1]["time"].split("T", 1)[1][:5]
    if start == end:
        return f"Heavy rain likely around {start}"
    return f"Heavy rain likely {start}-{end}"


def _headline(today, heavy_rain_expected, yahoo_today, yahoo_alerts):
    if yahoo_alerts:
        first = yahoo_alerts[0]
        return f"{first.get('level', 'Alert')}: {first.get('text', '')}".strip()
    if yahoo_today and yahoo_today.get("condition"):
        return f"{yahoo_today['condition']} expected today."
    if heavy_rain_expected:
        return "Heavy rain window expected today."
    if today["temp_max_c"] >= 30:
        return "Hot daytime conditions."
    if today["temp_min_c"] <= 5:
        return "Cold start, layer up."
    return f"{today['condition']} with mild shifts."


def _subtitle(today, rain_window, tomorrow_daily, heavy_rain_expected, yahoo_today, yahoo_indices):
    umbrella_note = None
    if yahoo_indices:
        for label in ("傘", "umbrella"):
            if label in yahoo_indices:
                umbrella_note = yahoo_indices[label].get("note")
                break
    if umbrella_note:
        return umbrella_note
    if yahoo_today and yahoo_today.get("wind"):
        return f"{rain_window}. Wind: {yahoo_today['wind']}"
    if heavy_rain_expected:
        return f"{rain_window}. Carry an umbrella."
    if today["temp_max_c"] >= 30:
        return "Hydrate and avoid the hottest afternoon window."
    if today["temp_min_c"] <= 5:
        return "Cold morning. Keep layers ready."
    return f"Tomorrow trends {tomorrow_daily['condition'].lower()}."


def _bullets(today, rain_window, yahoo_today, yahoo_indices):
    bullets = [rain_window, f"High {today['temp_max_c']:.0f}C / Low {today['temp_min_c']:.0f}C"]
    if yahoo_today and yahoo_today.get("wave"):
        bullets.append(f"Sea/wave note: {yahoo_today['wave']}")
        return bullets[:3]
    if yahoo_indices:
        for label in ("重ね着", "layering"):
            if label in yahoo_indices:
                bullets.append(yahoo_indices[label].get("note", ""))
                return [item for item in bullets if item][:3]
    if rain_window != "No heavy rain expected":
        bullets.append("Carry an umbrella.")
    elif today["temp_max_c"] - today["temp_min_c"] >= 9:
        bullets.append("Big temperature swing. Layering helps.")
    else:
        bullets.append("Comfortable overall; light layer is enough.")
    return bullets[:3]


def _first_yahoo_today(context):
    yahoo = context.get("sources", {}).get("yahoo", {}).get("payload", {})
    items = yahoo.get("today_tomorrow", [])
    return items[0] if items else {}


def _first_yahoo_tomorrow(context):
    yahoo = context.get("sources", {}).get("yahoo", {}).get("payload", {})
    items = yahoo.get("today_tomorrow", [])
    return items[1] if len(items) > 1 else {}


def _first_yahoo_index_items(context):
    yahoo = context.get("sources", {}).get("yahoo", {}).get("payload", {})
    days = yahoo.get("indices", {}).get("days", [])
    if not days:
        return {}
    return days[0].get("items", {})


def build_payload(context):
    open_meteo_wrapper = context.get("sources", {}).get("open_meteo", {}).get("payload", {})
    raw = open_meteo_wrapper.get("raw")
    if not raw:
        raise RuntimeError("Missing Open-Meteo payload in aggregated context")

    tz_name = raw.get("timezone", "Asia/Tokyo")
    now_local = datetime.now(ZoneInfo(tz_name))
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    today_s = today.isoformat()
    tomorrow_s = tomorrow.isoformat()

    daily_times = raw["daily"]["time"]
    today_idx = daily_times.index(today_s) if today_s in daily_times else 0
    tomorrow_idx = daily_times.index(tomorrow_s) if tomorrow_s in daily_times else min(1, len(daily_times) - 1)

    today_daily = _daily_summary(raw, today_idx)
    tomorrow_daily = _daily_summary(raw, tomorrow_idx)
    today_hourly = _hourly_rows(raw, today_daily["date"])
    rain_window = _rain_window(today_hourly)
    heavy_rain_expected = rain_window != "No heavy rain expected"
    yahoo_today = _first_yahoo_today(context)
    yahoo_tomorrow = _first_yahoo_tomorrow(context)
    yahoo_indices = _first_yahoo_index_items(context)
    yahoo_alerts = context.get("sources", {}).get("yahoo", {}).get("payload", {}).get("alerts", [])

    temp_range = f"{math.floor(today_daily['temp_min_c'])}C-{math.ceil(today_daily['temp_max_c'])}C"
    brief = {
        "headline": _headline(today_daily, heavy_rain_expected, yahoo_today, yahoo_alerts),
        "subtitle": _subtitle(today_daily, rain_window, tomorrow_daily, heavy_rain_expected, yahoo_today, yahoo_indices),
        "bullets": _bullets(today_daily, rain_window, yahoo_today, yahoo_indices),
        "rain_window": rain_window,
        "temp_range": temp_range,
        "tomorrow_preview": (
            f"Tomorrow: {tomorrow_daily['condition']}, "
            f"{tomorrow_daily['temp_min_c']:.0f}-{tomorrow_daily['temp_max_c']:.0f}C"
        ),
        "illustration_prompt": (
            f"Minimal weather poster for {yahoo_today.get('condition', today_daily['condition'])} with "
            f"rain hint={today_daily['rain_prob_max_pct']}% and wind note={yahoo_today.get('wind', 'n/a')}"
        ),
        "layout_emphasis": {
            "rain": "high" if today_daily["rain_prob_max_pct"] >= 60 else "medium",
            "temperature": "high" if today_daily["temp_max_c"] >= 30 or today_daily["temp_min_c"] <= 5 else "medium",
        },
    }

    return {
        "generated_at_local": now_local.replace(microsecond=0).isoformat(),
        "timezone": tz_name,
        "location": open_meteo_wrapper["location"],
        "today": {"daily_summary": today_daily, "hourly": today_hourly, "yahoo_summary": yahoo_today},
        "tomorrow": {"daily_summary": tomorrow_daily, "yahoo_summary": yahoo_tomorrow},
        "brief_context": {
            "source_priority": context.get("source_priority", []),
            "ordered_facts": context.get("ordered_facts", []),
            "conflicts": context.get("conflicts", []),
            "missing_sections": context.get("missing_sections", []),
        },
        "brief": brief,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    settings = load_settings()
    input_path = args.input or settings["runtime"]["brief_context_file"]
    output_path = args.output or settings["runtime"]["brief_file"]
    context = read_json(input_path)
    transformed = build_payload(context)
    write_json(output_path, transformed)
    print(output_path)


if __name__ == "__main__":
    main()
