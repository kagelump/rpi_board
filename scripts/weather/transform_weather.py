#!/usr/bin/env python3
import argparse
import math
import sys
from datetime import datetime, time as dtime, timedelta
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


def _is_ascii_text(text):
    if not isinstance(text, str):
        return False
    return all(ord(ch) < 128 for ch in text)


_SEASONS = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}


def _sun_times(raw, idx):
    """Pull today's sunrise/sunset as HH:MM if Open-Meteo provided them.

    The sample fallback payload omits these arrays, so every access is guarded.
    """
    daily = raw.get("daily", {})
    out = {}
    for key, label in (("sunrise", "sunrise"), ("sunset", "sunset")):
        series = daily.get(key)
        if isinstance(series, list) and idx < len(series) and isinstance(series[idx], str):
            stamp = series[idx]
            out[label] = stamp.split("T", 1)[1][:5] if "T" in stamp else stamp
    return out


def _day_context(now_local):
    """Temporal facts the creative director can lean on for non-weather flavour.

    Northern-hemisphere seasons (this board lives in Tokyo). Computed locally so
    it never depends on the network and is always present even in fallback mode.
    """
    weekday = now_local.strftime("%A")
    month = now_local.month
    return {
        "date_iso": now_local.date().isoformat(),
        "date_pretty": now_local.strftime("%A, %B %-d"),
        "weekday": weekday,
        "is_weekend": weekday in ("Saturday", "Sunday"),
        "month_name": now_local.strftime("%B"),
        "season": _SEASONS.get(month, "unknown"),
        "part_of_day": (
            "morning" if now_local.hour < 11
            else "midday" if now_local.hour < 16
            else "evening" if now_local.hour < 21
            else "night"
        ),
    }


# From this hour onward a run is treated as "tomorrow's" board: the 21:00
# (9pm) refresh prepares the next day, while the 08:00 and 13:00 refreshes still
# target the current day. All three runs for one forecast day therefore resolve
# to the same target date, which is what anchors the fixed daily theme.
_EVENING_TARGET_HOUR = 18


def _target_offset_days(now_local):
    """0 if this run forecasts today, 1 if it forecasts tomorrow (evening/night)."""
    return 1 if now_local.hour >= _EVENING_TARGET_HOUR else 0


def _daypart_role(now_local):
    """Which of the three daily refreshes this run is.

    primary        -- evening/night (9pm): builds tomorrow's board from scratch.
    morning_update -- before midday (8am): refresh today only on a major change.
    afternoon      -- midday onward (1pm): re-frame for the afternoon/evening.
    """
    hour = now_local.hour
    if hour >= _EVENING_TARGET_HOUR:
        return "primary"
    if hour < 11:
        return "morning_update"
    return "afternoon"


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


def _rain_rows_by_intensity(rows):
    # Open-Meteo weather_code groups:
    # light: drizzle/slight rain/showers, regular: moderate rain/showers,
    # heavy: heavy rain/violent showers/thunderstorm.
    light_codes = {51, 53, 55, 61, 80}
    regular_codes = {63, 81}
    heavy_codes = {65, 82, 95}
    light = []
    regular = []
    heavy = []
    for row in rows:
        code = row["weather_code"]
        rain_mm = row["rain_mm"]
        rain_prob = row["rain_probability_pct"]

        if rain_mm >= 1.5:
            heavy.append(row)
            continue
        if rain_mm >= 0.6:
            regular.append(row)
            continue
        if rain_mm >= 0.1:
            light.append(row)
            continue

        if rain_prob >= 75 and code in (regular_codes | heavy_codes):
            heavy.append(row)
            continue
        if rain_prob >= 55 and code in (light_codes | regular_codes | heavy_codes):
            regular.append(row)
            continue
        if rain_prob >= 35 and code in (light_codes | regular_codes | heavy_codes):
            light.append(row)
            continue

        if code in heavy_codes:
            heavy.append(row)
        elif code in regular_codes:
            regular.append(row)
        elif code in light_codes:
            light.append(row)

    return {"light": light, "regular": regular, "heavy": heavy}


def _rain_window(rows):
    rain_rows = _rain_rows_by_intensity(rows)
    if rain_rows["heavy"]:
        level = "heavy"
        target = rain_rows["heavy"]
    elif rain_rows["regular"]:
        level = "regular"
        target = rain_rows["regular"]
    elif rain_rows["light"]:
        level = "light"
        target = rain_rows["light"]
    else:
        return "none", "No rain expected"

    start = target[0]["time"].split("T", 1)[1][:5]
    end = target[-1]["time"].split("T", 1)[1][:5]
    prefix = {
        "light": "Light rain possible",
        "regular": "Rain likely",
        "heavy": "Heavy rain likely",
    }[level]
    if start == end:
        return level, f"{prefix} around {start}"
    return level, f"{prefix} {start}-{end}"


def _headline(today, rain_level, yahoo_today, yahoo_alerts):
    if yahoo_alerts:
        first = yahoo_alerts[0]
        candidate = f"{first.get('level', 'Alert')}: {first.get('text', '')}".strip()
        if _is_ascii_text(candidate):
            return candidate
    if yahoo_today and yahoo_today.get("condition"):
        candidate = f"{yahoo_today['condition']} expected today."
        if _is_ascii_text(candidate):
            return candidate
    if rain_level == "heavy":
        return "Heavy rain window expected today."
    if rain_level == "regular":
        return "Rain likely through parts of today."
    if rain_level == "light":
        return "Light rain possible today."
    if today["temp_max_c"] >= 30:
        return "Hot daytime conditions."
    if today["temp_min_c"] <= 5:
        return "Cold start, layer up."
    return f"{today['condition']} with mild shifts."


def _subtitle(today, rain_window, tomorrow_daily, rain_level, yahoo_today, yahoo_indices):
    umbrella_note = None
    if yahoo_indices:
        for label in ("傘", "umbrella"):
            if label in yahoo_indices:
                umbrella_note = yahoo_indices[label].get("note")
                break
    if umbrella_note:
        if _is_ascii_text(umbrella_note):
            return umbrella_note
    if yahoo_today and yahoo_today.get("wind"):
        candidate = f"{rain_window}. Wind: {yahoo_today['wind']}"
        if _is_ascii_text(candidate):
            return candidate
    if rain_level == "heavy":
        return f"{rain_window}. Carry an umbrella."
    if rain_level == "regular":
        return f"{rain_window}. Umbrella recommended."
    if rain_level == "light":
        return f"{rain_window}. A light layer should be enough."
    if today["temp_max_c"] >= 30:
        return "Hydrate and avoid the hottest afternoon window."
    if today["temp_min_c"] <= 5:
        return "Cold morning. Keep layers ready."
    return f"Tomorrow trends {tomorrow_daily['condition'].lower()}."


def _bullets(today, rain_window, rain_level, yahoo_today, yahoo_indices):
    bullets = [rain_window, f"High {today['temp_max_c']:.0f}C / Low {today['temp_min_c']:.0f}C"]
    if yahoo_today and yahoo_today.get("wave"):
        wave_line = f"Sea/wave note: {yahoo_today['wave']}"
        if _is_ascii_text(wave_line):
            bullets.append(wave_line)
            return bullets[:3]
    if yahoo_indices:
        for label in ("重ね着", "layering"):
            if label in yahoo_indices:
                line = yahoo_indices[label].get("note", "")
                if _is_ascii_text(line):
                    bullets.append(line)
                    return [item for item in bullets if item][:3]
    if rain_level in {"heavy", "regular"}:
        bullets.append("Carry an umbrella.")
    elif rain_level == "light":
        bullets.append("Only light rain risk.")
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
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    # The forecast day this board is about: today, or tomorrow for evening runs.
    target = now_local.date() + timedelta(days=_target_offset_days(now_local))
    following = target + timedelta(days=1)
    target_s = target.isoformat()
    following_s = following.isoformat()

    daily_times = raw["daily"]["time"]
    target_idx = daily_times.index(target_s) if target_s in daily_times else 0
    following_idx = (
        daily_times.index(following_s)
        if following_s in daily_times
        else min(target_idx + 1, len(daily_times) - 1)
    )

    today_daily = _daily_summary(raw, target_idx)
    tomorrow_daily = _daily_summary(raw, following_idx)
    today_hourly = _hourly_rows(raw, today_daily["date"])
    rain_level, rain_window = _rain_window(today_hourly)
    yahoo_today = _first_yahoo_today(context)
    yahoo_tomorrow = _first_yahoo_tomorrow(context)
    yahoo_indices = _first_yahoo_index_items(context)
    yahoo_alerts = context.get("sources", {}).get("yahoo", {}).get("payload", {}).get("alerts", [])

    # Calendar context (date label, weekday, season) describes the *forecast*
    # day, but part_of_day stays tied to the actual run time so each refresh can
    # frame the same forecast differently (morning vs afternoon vs evening).
    day_context = _day_context(datetime.combine(target, dtime(12), tzinfo=tz))
    day_context["part_of_day"] = _day_context(now_local)["part_of_day"]
    day_context["target_date_iso"] = target_s
    day_context["run_date_iso"] = now_local.date().isoformat()
    day_context["daypart_role"] = _daypart_role(now_local)
    day_context.update(_sun_times(raw, target_idx))

    temp_range = f"{math.floor(today_daily['temp_min_c'])}C-{math.ceil(today_daily['temp_max_c'])}C"
    brief = {
        "headline": _headline(today_daily, rain_level, yahoo_today, yahoo_alerts),
        "subtitle": _subtitle(today_daily, rain_window, tomorrow_daily, rain_level, yahoo_today, yahoo_indices),
        "bullets": _bullets(today_daily, rain_window, rain_level, yahoo_today, yahoo_indices),
        "rain_window": rain_window,
        "rain_level": rain_level,
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
        "day_context": day_context,
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
