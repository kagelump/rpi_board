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


def compute_signature(payload):
    """Stable short hash of the inputs that should drive a new brief + image.

    Deliberately ignores volatile fields (exact timestamps, hourly noise) so two
    runs in the same part of the day with the same forecast collapse to one
    signature. part_of_day is included so each daypart still refreshes.
    """
    today = payload.get("today", {}).get("daily_summary", {})
    tomorrow = payload.get("tomorrow", {}).get("daily_summary", {})
    day_context = payload.get("day_context", {})
    basis = {
        "date": day_context.get("date_iso"),
        "part_of_day": day_context.get("part_of_day"),
        "holiday": day_context.get("holiday_today"),
        "today_condition": today.get("condition"),
        "today_code": today.get("weather_code"),
        "today_high": today.get("temp_max_c"),
        "today_low": today.get("temp_min_c"),
        "today_rain": today.get("rain_prob_max_pct"),
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
