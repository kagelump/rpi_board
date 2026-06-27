#!/usr/bin/env python3
"""Gather non-weather "what's happening today" context for the creative director.

Produces runtime/day_context.json with:
  - moon phase (computed locally, always available, no network)
  - today's public holiday + next upcoming holiday (Nager.Date, cached, optional)
  - today's calendar events from configured ICS URLs (optional)

Every network source is best-effort: failures degrade to empty fields and never
break the pipeline. The whole step is also guarded by `|| true` in the shell.
"""
import json
import math
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import load_settings, write_json
from scripts.openrouter.network import urlopen_with_context

_SYNODIC_MONTH = 29.530588853
# A well-known new moon epoch (2000-01-06 18:14 UTC) anchors the phase math.
_NEW_MOON_EPOCH = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

_PHASE_NAMES = [
    (0.03, "New Moon"),
    (0.22, "Waxing Crescent"),
    (0.28, "First Quarter"),
    (0.47, "Waxing Gibbous"),
    (0.53, "Full Moon"),
    (0.72, "Waning Gibbous"),
    (0.78, "Last Quarter"),
    (0.97, "Waning Crescent"),
    (1.01, "New Moon"),
]


def moon_phase(on_date):
    """Approximate moon phase for a date. Pure / offline.

    Returns phase name, age in days, and illuminated fraction as a percent.
    """
    noon = datetime(on_date.year, on_date.month, on_date.day, 12, tzinfo=timezone.utc)
    days = (noon - _NEW_MOON_EPOCH).total_seconds() / 86400.0
    age = days % _SYNODIC_MONTH
    if age < 0:
        age += _SYNODIC_MONTH
    fraction = age / _SYNODIC_MONTH  # 0 = new, 0.5 = full
    illumination = (1 - math.cos(2 * math.pi * fraction)) / 2
    name = next(label for threshold, label in _PHASE_NAMES if fraction < threshold)
    return {
        "phase": name,
        "age_days": round(age, 1),
        "illumination_pct": round(illumination * 100),
    }


def select_holidays(holiday_list, today_iso, window_days):
    """From a Nager.Date list, pick today's holiday and the next upcoming one.

    Pure: takes the already-fetched list so it is trivially testable.
    """
    today_holiday = None
    upcoming = None
    try:
        today = date.fromisoformat(today_iso)
    except (ValueError, TypeError):
        return None, None

    best_delta = None
    for item in holiday_list:
        if not isinstance(item, dict):
            continue
        raw_date = item.get("date")
        name = item.get("name") or item.get("localName")
        if not raw_date or not name:
            continue
        try:
            hol_date = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if hol_date == today:
            today_holiday = name
            continue
        delta = (hol_date - today).days
        if 0 < delta <= window_days and (best_delta is None or delta < best_delta):
            best_delta = delta
            upcoming = {"name": name, "date": raw_date, "days_away": delta}
    return today_holiday, upcoming


def _unfold_ics(text):
    # RFC 5545: a leading space/tab continues the previous logical line.
    out = []
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def parse_ics_events(ics_text, target_yyyymmdd):
    """Return SUMMARYs of VEVENTs whose start date matches target_yyyymmdd.

    Pure. Handles all-day (VALUE=DATE) and timestamped DTSTART; does not expand
    recurrence rules (RRULE), which would need a full calendar engine.
    """
    events = []
    summary = None
    start_date = None
    in_event = False
    for line in _unfold_ics(ics_text):
        if line.startswith("BEGIN:VEVENT"):
            in_event, summary, start_date = True, None, None
        elif line.startswith("END:VEVENT"):
            if in_event and start_date == target_yyyymmdd and summary:
                events.append(summary)
            in_event = False
        elif not in_event:
            continue
        elif line.startswith("SUMMARY"):
            summary = line.split(":", 1)[1].strip() if ":" in line else None
        elif line.startswith("DTSTART"):
            value = line.split(":", 1)[1].strip() if ":" in line else ""
            digits = "".join(ch for ch in value if ch.isdigit())
            if len(digits) >= 8:
                start_date = digits[:8]
    return events


def _fetch_json(url, settings, timeout):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urlopen_with_context(request, timeout=timeout, settings=settings) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_holidays(settings, year):
    """Fetch a country's holidays for a year, caching per country-year on disk."""
    context = settings.get("context", {})
    country = context.get("holiday_country")
    api_url = context.get("holidays_api_url")
    if not country or not api_url:
        return []

    cache_path = settings["runtime"].get("holiday_cache_file")
    cache_key = f"{country}-{year}"
    cache = {}
    if cache_path:
        cache_file = Path(cache_path)
        if not cache_file.is_absolute():
            cache_file = Path(__file__).resolve().parents[2] / cache_file
        if cache_file.exists():
            try:
                cache = json.loads(cache_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                cache = {}
        if isinstance(cache, dict) and cache_key in cache:
            return cache[cache_key]

    timeout = context.get("context_timeout_seconds", 8)
    data = _fetch_json(f"{api_url.rstrip('/')}/{year}/{country}", settings, timeout)
    if not isinstance(data, list):
        return []
    if cache_path:
        if not isinstance(cache, dict):
            cache = {}
        cache[cache_key] = data
        write_json(cache_path, cache)
    return data


def fetch_calendar_events(settings, target_date):
    context = settings.get("context", {})
    urls = context.get("calendar_ics_urls", []) or []
    if not urls:
        return []
    timeout = context.get("context_timeout_seconds", 8)
    target = target_date.strftime("%Y%m%d")
    events = []
    for url in urls:
        try:
            request = urllib.request.Request(url, headers={"Accept": "text/calendar"})
            with urlopen_with_context(request, timeout=timeout, settings=settings) as response:
                text = response.read().decode("utf-8", errors="replace")
            events.extend(parse_ics_events(text, target))
        except (urllib.error.URLError, TimeoutError, ValueError):
            continue
    return events[:5]


def build_day_context_extra(settings, now_local):
    context = settings.get("context", {})
    extra = {
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "events_mode": context.get("events_mode", "off"),
        "moon": moon_phase(now_local.date()),
        "holiday_today": None,
        "upcoming_holiday": None,
        "calendar_events": [],
    }
    if not context.get("enable_events", False):
        return extra

    window = context.get("upcoming_holiday_window_days", 10)
    try:
        holidays = fetch_holidays(settings, now_local.year)
        today_holiday, upcoming = select_holidays(holidays, now_local.date().isoformat(), window)
        extra["holiday_today"] = today_holiday
        extra["upcoming_holiday"] = upcoming
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as error:
        print(f"[context] holiday lookup skipped: {type(error).__name__}: {error}")

    try:
        extra["calendar_events"] = fetch_calendar_events(settings, now_local)
    except Exception as error:  # calendar parsing is best-effort
        print(f"[context] calendar lookup skipped: {type(error).__name__}: {error}")

    return extra


def main():
    settings = load_settings()
    tz_name = settings["location"]["timezone"]
    now_local = datetime.now(ZoneInfo(tz_name))
    extra = build_day_context_extra(settings, now_local)
    output_path = settings["runtime"]["day_context_file"]
    write_json(output_path, extra)
    print(output_path)


if __name__ == "__main__":
    main()
