#!/usr/bin/env python3
"""Decide whether a run should spend money on fresh LLM/image generation.

The board may be driven by a timer that fires more often than the weather (or
the time-of-day frame) actually changes. To avoid paying for near-identical
output, we hash the material inputs into a signature and debounce regeneration:
regenerate when the signature changes, or when enough time has passed; otherwise
reuse the last good brief and artwork.
"""
import hashlib
import json
from datetime import datetime


# Granularity of the "major change" check that decides whether the morning
# (8am) refresh regenerates: temperatures are bucketed so only a swing of about
# this many degrees flips the signature -- a 1C tweak overnight reuses the board.
_TEMP_BUCKET_C = 3


def _temp_bucket(value):
    """Quantise a temperature so only a meaningful (~3C) shift changes the hash."""
    if not isinstance(value, (int, float)):
        return None
    return round(value / _TEMP_BUCKET_C)


def _refresh_key(daypart_role):
    """Collapse the three daily roles into the signature's refresh dimension.

    The evening (primary) and morning (morning_update) runs share a key so the
    8am refresh reuses the 9pm board unless the forecast itself changed. The
    midday (afternoon) run gets its own key so it always re-frames for the rest
    of the day even when nothing about the forecast moved.
    """
    return "afternoon" if daypart_role == "afternoon" else "day"


def compute_signature(payload):
    """Stable short hash of the inputs that should drive a new brief + image.

    Deliberately ignores volatile fields (exact timestamps, hourly noise, and
    sub-3C temperature jitter) so two runs of the same forecast day collapse to
    one signature. The refresh key keeps the afternoon re-frame distinct while
    letting the morning run reuse the evening board on an unchanged forecast.
    """
    today = payload.get("today", {}).get("daily_summary", {})
    tomorrow = payload.get("tomorrow", {}).get("daily_summary", {})
    day_context = payload.get("day_context", {})
    brief = payload.get("brief", {})
    basis = {
        "date": day_context.get("target_date_iso") or day_context.get("date_iso"),
        "refresh": _refresh_key(day_context.get("daypart_role")),
        "holiday": day_context.get("holiday_today"),
        "today_condition": today.get("condition"),
        "today_code": today.get("weather_code"),
        "today_high_bucket": _temp_bucket(today.get("temp_max_c")),
        "today_low_bucket": _temp_bucket(today.get("temp_min_c")),
        "today_rain_level": brief.get("rain_level"),
        "tomorrow_condition": tomorrow.get("condition"),
    }
    blob = json.dumps(basis, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def should_regenerate(last_good, signature, now_iso, min_interval_seconds, force=False, skip_enabled=True):
    """Return True to spend on a fresh generation, False to reuse last good.

    Regenerate when forced, when skipping is off, when there is no prior good
    output, when the signature changed, or when at least min_interval_seconds
    have elapsed. Only a same-signature run inside the interval is skipped.
    """
    if force or not skip_enabled:
        return True
    if not isinstance(last_good, dict):
        return True
    if last_good.get("signature") != signature:
        return True
    last_at = last_good.get("generated_at")
    if not last_at:
        return True
    try:
        elapsed = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(last_at)).total_seconds()
    except (ValueError, TypeError):
        return True
    return elapsed >= min_interval_seconds
