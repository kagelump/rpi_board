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


def _rain_window(rows):
    wet = [r for r in rows if r["rain_probability_pct"] >= 40 or r["rain_mm"] >= 0.2]
    if not wet:
        return "Low rain risk"
    start = wet[0]["time"].split("T", 1)[1][:5]
    end = wet[-1]["time"].split("T", 1)[1][:5]
    if start == end:
        return f"Rain likely around {start}"
    return f"Rain likely {start}-{end}"


def _headline(today):
    if today["rain_prob_max_pct"] >= 70:
        return "Wet day ahead. Plan for rain."
    if today["temp_max_c"] >= 30:
        return "Hot daytime conditions."
    if today["temp_min_c"] <= 5:
        return "Cold start, layer up."
    return f"{today['condition']} with mild shifts."


def _subtitle(today, rain_window, tomorrow_daily):
    if today["rain_prob_max_pct"] >= 70:
        return f"{rain_window}. Carry an umbrella."
    if today["temp_max_c"] >= 30:
        return "Hydrate and avoid the hottest afternoon window."
    if today["temp_min_c"] <= 5:
        return "Cold morning. Keep layers ready."
    return f"Tomorrow trends {tomorrow_daily['condition'].lower()}."


def _bullets(today, rain_window):
    bullets = [rain_window, f"High {today['temp_max_c']:.0f}C / Low {today['temp_min_c']:.0f}C"]
    if today["rain_prob_max_pct"] >= 50:
        bullets.append("Carry an umbrella.")
    elif today["temp_max_c"] - today["temp_min_c"] >= 9:
        bullets.append("Big temperature swing. Layering helps.")
    else:
        bullets.append("Comfortable overall; light layer is enough.")
    return bullets[:3]


def build_payload(raw_wrapper):
    raw = raw_wrapper["raw"]
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

    temp_range = f"{math.floor(today_daily['temp_min_c'])}C-{math.ceil(today_daily['temp_max_c'])}C"
    brief = {
        "headline": _headline(today_daily),
        "subtitle": _subtitle(today_daily, rain_window, tomorrow_daily),
        "bullets": _bullets(today_daily, rain_window),
        "rain_window": rain_window,
        "temp_range": temp_range,
        "tomorrow_preview": (
            f"Tomorrow: {tomorrow_daily['condition']}, "
            f"{tomorrow_daily['temp_min_c']:.0f}-{tomorrow_daily['temp_max_c']:.0f}C"
        ),
        "illustration_prompt": (
            f"Minimal weather poster for {today_daily['condition']} with "
            f"rain hint={today_daily['rain_prob_max_pct']}%"
        ),
        "layout_emphasis": {
            "rain": "high" if today_daily["rain_prob_max_pct"] >= 60 else "medium",
            "temperature": "high" if today_daily["temp_max_c"] >= 30 or today_daily["temp_min_c"] <= 5 else "medium",
        },
    }

    return {
        "generated_at_local": now_local.replace(microsecond=0).isoformat(),
        "timezone": tz_name,
        "location": raw_wrapper["location"],
        "today": {"daily_summary": today_daily, "hourly": today_hourly},
        "tomorrow": {"daily_summary": tomorrow_daily},
        "brief": brief,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    settings = load_settings()
    input_path = args.input or settings["runtime"]["payload_file"]
    output_path = args.output or settings["runtime"]["brief_file"]
    payload = read_json(input_path)
    transformed = build_payload(payload)
    write_json(output_path, transformed)
    print(output_path)


if __name__ == "__main__":
    main()
