#!/usr/bin/env python3
import argparse
import math
import os
import re
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import load_settings, read_json, write_json
# Yahoo date parsing is shared with the aggregator; re-exported here for the
# transform tests that exercise it directly.
from scripts.weather.date_utils import (
    normalize_yahoo_date as _normalize_yahoo_date,
    parse_yahoo_month_day as _parse_yahoo_month_day,
)


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


# Caption-free deterministic art subjects. When every model attempt fails the
# deterministic brief is handed straight to image generation, so its subject
# must describe a scene, never lettering material: no numbers, no percentages,
# and no poster/forecast wording that invites a baked-in caption. The
# downstream no-text guardrail and retry correction still apply.
_DETERMINISTIC_SCENES = {
    0: "a wide clear sky with low sunlight raking across quiet rooftops",
    1: "thin bright clouds drifting over a calm city skyline",
    2: "ragged clouds crossing a pale sky above a street",
    3: "layered clouds pressing over a still street",
    45: "fog swallowing the far end of a riverside path",
    48: "frost-fog curling along a quiet morning street",
    51: "fine drizzle beading on a leaf and a wet railing",
    53: "steady drizzle drawing thin streaks down a window",
    55: "dense drizzle blurring a line of streetlights",
    56: "freezing drizzle glazing a bare branch",
    57: "freezing drizzle coating a cold railing",
    61: "light rain dimpling puddles along a narrow lane",
    63: "steady rain sweeping across an empty crossing",
    65: "heavy rain hammering a flooded street under dark clouds",
    66: "freezing rain sheening a slick street",
    67: "freezing rain glazing a dark road",
    71: "soft snow settling over a hushed side street",
    73: "steady snow drifting past a row of shopfronts",
    75: "thick snow piling on a bare tree at a quiet corner",
    77: "snow grains skittering across a quiet lane",
    80: "a sudden shower breaking over a startled city street",
    81: "quick showers swept sideways across a park path",
    82: "violent rain lashing a bent tree beside the road",
    85: "snow showers drifting over a low skyline",
    86: "heavy snow showers burying a park bench",
    95: "a black storm sky split by one sharp bolt over the skyline",
    96: "hail stinging a street beneath a bruised sky",
    99: "violent hail hammering a torn skyline",
}


def _deterministic_illustration_prompt(daily):
    """Return a scene-only subject for the deterministic fallback brief.

    The fallback has no creative director, so it maps the local weather code
    to a plain visual scene. It never emits numbers, percentages, or poster
    wording, so a deterministic failure cannot feed caption material into the
    primary image subject. Unknown or numeric condition labels fall back to a
    generic scene rather than echoing the label.
    """
    code = daily.get("weather_code")
    if code in _DETERMINISTIC_SCENES:
        return _DETERMINISTIC_SCENES[code]
    condition = str(daily.get("condition") or "").strip().lower()
    if condition and not any(ch.isdigit() for ch in condition):
        return f"the day's {condition} over a quiet city skyline"
    return "the day's weather over a quiet city skyline"


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


# Open-Meteo weather codes ordered from calmest to most disruptive. Used to
# pick the representative condition for the remaining part of a same-day run.
_CONDITION_SEVERITY = {
    0: 0, 1: 1, 2: 2, 3: 3, 45: 3, 48: 3,
    51: 4, 56: 4, 61: 4, 71: 4, 80: 4,
    53: 5, 63: 5, 66: 5, 73: 5, 77: 5, 81: 5, 85: 5,
    55: 6, 57: 6, 65: 6, 67: 6, 75: 6, 82: 6, 86: 6,
    95: 7, 96: 7, 99: 7,
}


def _row_start(stamp):
    """Parse an Open-Meteo local timestamp into a datetime (may be naive)."""
    try:
        return datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None


def _remaining_hourly_rows(rows, now_local):
    """Rows still relevant to a same-day update.

    Each row is keyed by the hour it starts, so the row for the run's current
    hour counts as ongoing/future while earlier rows have elapsed. This keeps
    overnight rain from resurfacing in a midday briefing without dropping rain
    that is happening right now.
    """
    cutoff = now_local.replace(minute=0, second=0, microsecond=0)
    kept = []
    for row in rows:
        start = _row_start(row["time"])
        if start is None:
            kept.append(row)
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=now_local.tzinfo)
        if start >= cutoff:
            kept.append(row)
    return kept


def _remaining_summary(today_daily, rows):
    """Fold the remaining hours into the summary that drives advice text.

    Full-day temperature extrema are preserved, but condition and rain fields
    describe only the still-open window so the rain copy, icon, and
    illustration all reflect what is actually ahead.
    """
    summary = dict(today_daily)
    if not rows:
        summary.update(
            condition=WEATHER_LABELS[0],
            weather_code=0,
            rain_prob_max_pct=0,
            rain_sum_mm=0.0,
        )
        return summary

    representative = max(
        rows,
        key=lambda row: _CONDITION_SEVERITY.get(row["weather_code"], 3),
    )
    code = representative["weather_code"]
    summary.update(
        condition=WEATHER_LABELS.get(code, f"Code {code}"),
        weather_code=code,
        rain_prob_max_pct=max(int(row["rain_probability_pct"]) for row in rows),
        rain_sum_mm=round(sum(float(row["rain_mm"]) for row in rows), 1),
    )
    return summary


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


_YAHOO_FORECAST_FACT_ID = re.compile(r"^yahoo\.today_tomorrow\.(\d+)\.")
_YAHOO_INDEX_FACT_ID = re.compile(r"^yahoo\.indices\.(\d+)\.")


def _yahoo_payload(context):
    payload = context.get("sources", {}).get("yahoo", {}).get("payload", {})
    return payload if isinstance(payload, dict) else {}


def _annotate_yahoo_item(item, reference_date):
    """Return a copy of a Yahoo row carrying its resolved ``date_iso``."""
    if not isinstance(item, dict) or not item:
        return {}
    enriched = dict(item)
    date_iso = _normalize_yahoo_date(item, reference_date)
    if date_iso:
        enriched["date_iso"] = date_iso
    return enriched


def _select_yahoo_forecasts(context, target_s, following_s, reference_date, allow_positional_fallback):
    """Select the Yahoo forecast rows for the target and following dates.

    Rows are chosen by their resolved date whenever any row carries a usable
    date. Row position is used only for same-day boards whose labels cannot be
    parsed, where Yahoo's today/tomorrow tabs are unambiguous by definition. A
    next-day board never guesses by position: an unresolvable or absent row is
    returned as ``{}``.
    """
    items = _yahoo_payload(context).get("today_tomorrow", [])
    if not isinstance(items, list):
        items = []
    resolved = [(item, _normalize_yahoo_date(item, reference_date)) for item in items]
    resolved = [(item, date_iso) for item, date_iso in resolved if date_iso is not None]

    today = {}
    tomorrow = {}
    if resolved:
        for item, date_iso in resolved:
            if date_iso == target_s and not today:
                today = item
            elif date_iso == following_s and not tomorrow:
                tomorrow = item
    elif allow_positional_fallback:
        if items:
            today = items[0]
        if len(items) > 1:
            tomorrow = items[1]
    return _annotate_yahoo_item(today, reference_date), _annotate_yahoo_item(tomorrow, reference_date)


def _select_yahoo_index_items(context, target_s, reference_date, allow_positional_fallback):
    """Return the Yahoo index items whose date matches the board target.

    Run-day-only index rows are dropped for a next-day board. As with the
    forecast rows, a same-day board may fall back to the first tab when labels
    are unparseable, but a next-day board never guesses by position.
    """
    days = _yahoo_payload(context).get("indices", {}).get("days", [])
    if not isinstance(days, list):
        days = []
    resolved = [(day, _normalize_yahoo_date(day, reference_date)) for day in days]
    resolved = [(day, date_iso) for day, date_iso in resolved if date_iso is not None]
    if resolved:
        for day, date_iso in resolved:
            if date_iso == target_s:
                items = day.get("items", {})
                return dict(items) if isinstance(items, dict) else {}
        return {}
    if allow_positional_fallback and days:
        items = days[0].get("items", {})
        return dict(items) if isinstance(items, dict) else {}
    return {}


def _fact_with_date(fact, date_iso):
    enriched = dict(fact)
    enriched["date_iso"] = date_iso
    return enriched


def _filter_ordered_facts(ordered_facts, context, target_s, following_s, reference_date, allow_positional_fallback):
    """Drop Yahoo facts whose resolved date is outside the board's window.

    The brief prompt treats ORDERED_FACTS as ground truth. An evening board must
    therefore not carry the run day's Yahoo forecast or run-day-only index rows.
    Facts with an unresolvable date are kept only for same-day boards, where
    Yahoo's fixed tabs are unambiguous; a next-day board drops them rather than
    present run-day data as target-day context.
    """
    if not ordered_facts:
        return []
    items = _yahoo_payload(context).get("today_tomorrow", [])
    if not isinstance(items, list):
        items = []
    index_days = _yahoo_payload(context).get("indices", {}).get("days", [])
    if not isinstance(index_days, list):
        index_days = []
    forecast_dates = {idx: _normalize_yahoo_date(item, reference_date) for idx, item in enumerate(items)}
    index_dates = {idx: _normalize_yahoo_date(day, reference_date) for idx, day in enumerate(index_days)}

    kept = []
    for fact in ordered_facts:
        if not isinstance(fact, dict) or fact.get("source") != "yahoo":
            kept.append(fact)
            continue
        fact_id = fact.get("id", "")
        if not isinstance(fact_id, str):
            kept.append(fact)
            continue
        forecast_match = _YAHOO_FORECAST_FACT_ID.match(fact_id)
        if forecast_match:
            date_iso = forecast_dates.get(int(forecast_match.group(1)))
            if date_iso is None:
                if allow_positional_fallback:
                    kept.append(fact)
            elif date_iso in (target_s, following_s):
                kept.append(_fact_with_date(fact, date_iso))
            continue
        index_match = _YAHOO_INDEX_FACT_ID.match(fact_id)
        if index_match:
            day_idx = int(index_match.group(1))
            date_iso = index_dates.get(day_idx)
            if date_iso is None:
                if allow_positional_fallback and day_idx == 0:
                    kept.append(fact)
            elif date_iso == target_s:
                kept.append(_fact_with_date(fact, date_iso))
            continue
        # Alerts and any other Yahoo facts are not date-scoped; keep them.
        kept.append(fact)
    return kept


def _filter_conflicts(conflicts, target_s, same_day):
    """Keep only date-safe conflicts for the board's target day.

    ``aggregate_weather_sources._detect_conflicts`` compares Yahoo's run-day
    forecast tab with Open-Meteo row 0. That comparison is only meaningful for
    a same-day board: an evening board must never serialize the run-day
    comparison under a target-day ``today`` label. Conflicts that carry an
    explicit date are kept only when that date is the target; undated legacy
    conflicts are kept only for a same-day board, where the run day *is* the
    target.
    """
    if not conflicts:
        return []
    kept = []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            # Unknown shape: only a same-day board can trust the legacy run-day
            # comparison, so a next-day board drops it rather than guess.
            if same_day:
                kept.append(conflict)
            continue
        date_iso = conflict.get("date") or conflict.get("date_iso")
        if date_iso:
            if str(date_iso)[:10] == target_s:
                kept.append(conflict)
            continue
        if same_day:
            kept.append(conflict)
    return kept


def build_payload(context, now_local=None):
    open_meteo_wrapper = context.get("sources", {}).get("open_meteo", {}).get("payload", {})
    raw = open_meteo_wrapper.get("raw")
    if not raw:
        raise RuntimeError("Missing Open-Meteo payload in aggregated context")

    tz_name = raw.get("timezone", "Asia/Tokyo")
    tz = ZoneInfo(tz_name)
    # ``now_local`` is injectable so a replay can pin the run's local clock.
    if now_local is None:
        now_local = datetime.now(tz)
    elif now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=tz)
    else:
        now_local = now_local.astimezone(tz)
    # FORECAST_TARGET=tomorrow forces the 9pm (primary) role at any hour.
    force_tomorrow = os.environ.get("FORECAST_TARGET", "").strip().lower() == "tomorrow"
    offset = 1 if force_tomorrow else _target_offset_days(now_local)
    target = now_local.date() + timedelta(days=offset)
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

    # Morning and afternoon refreshes brief only the hours still open on the
    # target day, so elapsed overnight rain is never recycled as upcoming
    # advice. The evening run builds tomorrow's board from the full day.
    same_day = offset == 0 and today_daily["date"] == now_local.date().isoformat()
    if same_day:
        advising_hourly = _remaining_hourly_rows(today_hourly, now_local)
        advice_daily = _remaining_summary(today_daily, advising_hourly)
    else:
        advising_hourly = today_hourly
        advice_daily = today_daily
    rain_level, rain_window = _rain_window(advising_hourly)
    # Same-day boards may keep Yahoo's fixed today/tomorrow tabs when labels are
    # unparseable; a next-day board must never guess by position.
    reference_date = now_local.date()
    allow_positional_fallback = offset == 0
    yahoo_today, yahoo_tomorrow = _select_yahoo_forecasts(
        context, target_s, following_s, reference_date, allow_positional_fallback
    )
    yahoo_indices = _select_yahoo_index_items(
        context, target_s, reference_date, allow_positional_fallback
    )
    ordered_facts = _filter_ordered_facts(
        context.get("ordered_facts", []),
        context,
        target_s,
        following_s,
        reference_date,
        allow_positional_fallback,
    )
    yahoo_alerts = _yahoo_payload(context).get("alerts", [])
    conflicts = _filter_conflicts(context.get("conflicts", []), target_s, same_day)

    # Calendar context (date label, weekday, season) describes the *forecast*
    # day, but part_of_day stays tied to the actual run time so each refresh can
    # frame the same forecast differently (morning vs afternoon vs evening).
    day_context = _day_context(datetime.combine(target, dtime(12), tzinfo=tz))
    day_context["part_of_day"] = _day_context(now_local)["part_of_day"]
    day_context["target_date_iso"] = target_s
    day_context["run_date_iso"] = now_local.date().isoformat()
    day_context["daypart_role"] = "primary" if force_tomorrow else _daypart_role(now_local)
    day_context.update(_sun_times(raw, target_idx))

    temp_range = f"{math.floor(today_daily['temp_min_c'])}C-{math.ceil(today_daily['temp_max_c'])}C"
    brief = {
        "headline": _headline(advice_daily, rain_level, yahoo_today, yahoo_alerts),
        "subtitle": _subtitle(advice_daily, rain_window, tomorrow_daily, rain_level, yahoo_today, yahoo_indices),
        "bullets": _bullets(advice_daily, rain_window, rain_level, yahoo_today, yahoo_indices),
        "rain_window": rain_window,
        "rain_level": rain_level,
        "temp_range": temp_range,
        "tomorrow_preview": (
            f"Tomorrow: {tomorrow_daily['condition']}, "
            f"{tomorrow_daily['temp_min_c']:.0f}-{tomorrow_daily['temp_max_c']:.0f}C"
        ),
        "illustration_prompt": _deterministic_illustration_prompt(advice_daily),
        "layout_emphasis": {
            "rain": "high" if advice_daily["rain_prob_max_pct"] >= 60 else "medium",
            "temperature": "high" if today_daily["temp_max_c"] >= 30 or today_daily["temp_min_c"] <= 5 else "medium",
        },
    }

    return {
        "generated_at_local": now_local.replace(microsecond=0).isoformat(),
        "timezone": tz_name,
        "day_context": day_context,
        "location": open_meteo_wrapper["location"],
        "today": {"daily_summary": advice_daily, "hourly": today_hourly, "yahoo_summary": yahoo_today},
        "tomorrow": {"daily_summary": tomorrow_daily, "yahoo_summary": yahoo_tomorrow},
        "brief_context": {
            "target_date_iso": target_s,
            "source_priority": context.get("source_priority", []),
            "ordered_facts": ordered_facts,
            "conflicts": conflicts,
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
