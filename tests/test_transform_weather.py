"""Tests for scripts/weather/transform_weather.py"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.weather.transform_weather import (
    WEATHER_LABELS,
    _DETERMINISTIC_SCENES,
    _bullets,
    _deterministic_illustration_prompt,
    _daily_summary,
    _day_context,
    _daypart_role,
    _headline,
    _hourly_rows,
    _is_ascii_text,
    _rain_rows_by_intensity,
    _rain_window,
    _remaining_hourly_rows,
    _remaining_summary,
    _subtitle,
    _target_offset_days,
    build_payload,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _has_caption_material(text):
    """True when a deterministic subject reads like a caption/label."""
    lowered = text.lower()
    return (
        any(ch.isdigit() for ch in text)
        or "%" in text
        or "poster" in lowered
        or "hint" in lowered
        or "forecast" in lowered
        or "report" in lowered
    )


# Minimal Open-Meteo "raw" payload using a safely-past date so
# build_payload always falls back to today_idx=0 / tomorrow_idx=1.
_RAW = {
    "timezone": "Asia/Tokyo",
    "daily": {
        "time": ["2024-01-10", "2024-01-11", "2024-01-12"],
        "weather_code": [61, 63, 0],
        "temperature_2m_max": [18.5, 20.0, 15.0],
        "temperature_2m_min": [10.0, 12.0, 8.0],
        "precipitation_probability_max": [75, 40, 10],
        "precipitation_sum": [5.0, 1.0, 0.0],
    },
    "hourly": {
        "time": [
            "2024-01-10T06:00",
            "2024-01-10T09:00",
            "2024-01-10T12:00",
            "2024-01-11T06:00",
        ],
        "temperature_2m": [12.0, 15.0, 17.0, 13.0],
        "precipitation_probability": [70, 80, 60, 20],
        "precipitation": [0.5, 1.8, 0.3, 0.0],
        "weather_code": [61, 65, 61, 0],
    },
}

_CONTEXT = {
    "sources": {
        "open_meteo": {
            "source_name": "open_meteo",
            "available": True,
            "missing_sections": [],
            "payload": {
                "fetched_at": "2024-01-10T00:00:00+00:00",
                "location": {
                    "latitude": 35.6762,
                    "longitude": 139.6503,
                    "timezone": "Asia/Tokyo",
                },
                "source": "open-meteo",
                "raw": _RAW,
            },
        },
        "yahoo": {
            "source_name": "yahoo",
            "available": False,
            "missing_sections": ["all"],
            "payload": {},
        },
    },
    "source_priority": ["yahoo", "open_meteo"],
    "ordered_facts": [],
    "conflicts": [],
    "missing_sections": [],
}


# ---------------------------------------------------------------------------
# _is_ascii_text
# ---------------------------------------------------------------------------

class TestIsAsciiText:
    def test_pure_ascii(self):
        assert _is_ascii_text("Hello world") is True

    def test_empty_string(self):
        assert _is_ascii_text("") is True

    def test_non_string(self):
        assert _is_ascii_text(None) is False
        assert _is_ascii_text(42) is False

    def test_contains_multibyte(self):
        assert _is_ascii_text("Rain 雨 today") is False
        assert _is_ascii_text("傘") is False

    def test_punctuation_and_numbers(self):
        assert _is_ascii_text("Temp 18.5C / Low 10.0C") is True


# ---------------------------------------------------------------------------
# _daily_summary
# ---------------------------------------------------------------------------

class TestDailySummary:
    def test_known_weather_code(self):
        summary = _daily_summary(_RAW, 0)
        assert summary["date"] == "2024-01-10"
        assert summary["condition"] == "Slight rain"
        assert summary["weather_code"] == 61
        assert summary["temp_max_c"] == 18.5
        assert summary["temp_min_c"] == 10.0
        assert summary["rain_prob_max_pct"] == 75
        assert summary["rain_sum_mm"] == 5.0

    def test_clear_sky_code(self):
        summary = _daily_summary(_RAW, 2)
        assert summary["condition"] == "Clear sky"

    def test_unknown_code_fallback(self):
        raw = {
            "daily": {
                "time": ["2024-01-10"],
                "weather_code": [999],
                "temperature_2m_max": [20.0],
                "temperature_2m_min": [10.0],
                "precipitation_probability_max": [0],
                "precipitation_sum": [0.0],
            }
        }
        summary = _daily_summary(raw, 0)
        assert summary["condition"] == "Code 999"

    def test_rounding(self):
        raw = {
            "daily": {
                "time": ["2024-01-10"],
                "weather_code": [0],
                "temperature_2m_max": [22.349],
                "temperature_2m_min": [9.851],
                "precipitation_probability_max": [33],
                "precipitation_sum": [0.123],
            }
        }
        summary = _daily_summary(raw, 0)
        assert summary["temp_max_c"] == 22.3
        assert summary["temp_min_c"] == 9.9
        assert summary["rain_sum_mm"] == 0.1


# ---------------------------------------------------------------------------
# _hourly_rows
# ---------------------------------------------------------------------------

class TestHourlyRows:
    def test_filters_by_date(self):
        rows = _hourly_rows(_RAW, "2024-01-10")
        assert len(rows) == 3
        assert all(r["time"].startswith("2024-01-10") for r in rows)

    def test_other_date(self):
        rows = _hourly_rows(_RAW, "2024-01-11")
        assert len(rows) == 1
        assert rows[0]["time"] == "2024-01-11T06:00"

    def test_no_match(self):
        rows = _hourly_rows(_RAW, "2024-01-15")
        assert rows == []

    def test_row_fields(self):
        rows = _hourly_rows(_RAW, "2024-01-10")
        row = rows[0]
        assert "time" in row
        assert "temp_c" in row
        assert "rain_probability_pct" in row
        assert "rain_mm" in row
        assert "weather_code" in row
        assert isinstance(row["rain_probability_pct"], int)
        assert isinstance(row["weather_code"], int)


# ---------------------------------------------------------------------------
# _rain_rows_by_intensity
# ---------------------------------------------------------------------------

def _row(time, rain_mm, rain_prob, code):
    return {"time": time, "rain_mm": rain_mm, "rain_probability_pct": rain_prob, "weather_code": code}


class TestRainRowsByIntensity:
    def test_empty_input(self):
        result = _rain_rows_by_intensity([])
        assert result == {"light": [], "regular": [], "heavy": []}

    def test_heavy_by_rain_mm(self):
        result = _rain_rows_by_intensity([_row("T09:00", 1.5, 0, 0)])
        assert len(result["heavy"]) == 1
        assert result["light"] == result["regular"] == []

    def test_regular_by_rain_mm(self):
        result = _rain_rows_by_intensity([_row("T09:00", 0.6, 0, 0)])
        assert len(result["regular"]) == 1

    def test_light_by_rain_mm(self):
        result = _rain_rows_by_intensity([_row("T09:00", 0.1, 0, 0)])
        assert len(result["light"]) == 1

    def test_dry_row(self):
        result = _rain_rows_by_intensity([_row("T09:00", 0.0, 0, 0)])
        assert result == {"light": [], "regular": [], "heavy": []}

    def test_heavy_by_prob_and_code(self):
        # rain_mm < 0.1 but high prob + heavy code (82)
        result = _rain_rows_by_intensity([_row("T09:00", 0.0, 75, 82)])
        assert len(result["heavy"]) == 1

    def test_regular_by_prob_and_code(self):
        result = _rain_rows_by_intensity([_row("T09:00", 0.0, 55, 63)])
        assert len(result["regular"]) == 1

    def test_light_by_prob_and_code(self):
        result = _rain_rows_by_intensity([_row("T09:00", 0.0, 35, 61)])
        assert len(result["light"]) == 1

    def test_rain_mm_takes_priority_over_prob(self):
        # rain_mm >= 1.5 → heavy, regardless of code being a light code
        result = _rain_rows_by_intensity([_row("T09:00", 2.0, 10, 51)])
        assert len(result["heavy"]) == 1

    def test_multiple_rows_mixed(self):
        rows = [
            _row("T06:00", 0.05, 20, 0),  # dry
            _row("T09:00", 0.2, 50, 61),  # light by mm
            _row("T12:00", 2.0, 80, 65),  # heavy by mm
        ]
        result = _rain_rows_by_intensity(rows)
        assert len(result["heavy"]) == 1
        assert len(result["light"]) == 1
        assert len(result["regular"]) == 0


# ---------------------------------------------------------------------------
# _rain_window
# ---------------------------------------------------------------------------

class TestRainWindow:
    def test_no_rain(self):
        level, msg = _rain_window([])
        assert level == "none"
        assert msg == "No rain expected"

    def test_single_light_row(self):
        rows = [_row("2024-01-10T09:00", 0.1, 0, 61)]
        level, msg = _rain_window(rows)
        assert level == "light"
        assert "around 09:00" in msg
        assert "Light rain possible" in msg

    def test_range_heavy_rows(self):
        rows = [
            _row("2024-01-10T09:00", 2.0, 0, 65),
            _row("2024-01-10T11:00", 1.8, 0, 65),
        ]
        level, msg = _rain_window(rows)
        assert level == "heavy"
        assert "09:00" in msg
        assert "11:00" in msg
        assert "Heavy rain likely" in msg

    def test_heavy_takes_priority_over_light(self):
        rows = [
            _row("2024-01-10T06:00", 0.1, 0, 51),  # light
            _row("2024-01-10T09:00", 2.0, 0, 65),  # heavy
        ]
        level, _ = _rain_window(rows)
        assert level == "heavy"


# ---------------------------------------------------------------------------
# _headline
# ---------------------------------------------------------------------------

def _today(max_c=22.0, min_c=12.0, condition="Partly cloudy"):
    return {"temp_max_c": max_c, "temp_min_c": min_c, "condition": condition}


class TestHeadline:
    def test_yahoo_alert_ascii(self):
        alerts = [{"level": "Warning", "text": "Heavy rain advisory"}]
        h = _headline(_today(), "none", {}, alerts)
        assert "Warning" in h
        assert "Heavy rain advisory" in h

    def test_yahoo_alert_non_ascii_skipped(self):
        alerts = [{"level": "注意", "text": "大雨注意報"}]
        h = _headline(_today(), "none", {}, alerts)
        # Falls through to condition-based logic
        assert "注意" not in h

    def test_yahoo_condition_ascii(self):
        h = _headline(_today(), "none", {"condition": "Sunny with clouds"}, [])
        assert "Sunny with clouds" in h

    def test_yahoo_condition_non_ascii_skipped(self):
        h = _headline(_today(), "none", {"condition": "晴れ"}, [])
        assert "晴れ" not in h

    def test_heavy_rain(self):
        h = _headline(_today(), "heavy", {}, [])
        assert "Heavy rain" in h

    def test_regular_rain(self):
        h = _headline(_today(), "regular", {}, [])
        assert "Rain likely" in h

    def test_light_rain(self):
        h = _headline(_today(), "light", {}, [])
        assert "Light rain" in h

    def test_hot_day(self):
        h = _headline(_today(max_c=31.0), "none", {}, [])
        assert "Hot" in h

    def test_cold_start(self):
        h = _headline(_today(min_c=4.0), "none", {}, [])
        assert "Cold" in h

    def test_default_mild(self):
        h = _headline(_today(max_c=22.0, min_c=14.0, condition="Overcast"), "none", {}, [])
        assert "Overcast" in h
        assert "mild" in h.lower()


# ---------------------------------------------------------------------------
# _subtitle
# ---------------------------------------------------------------------------

class TestSubtitle:
    def _tomorrow(self, condition="Mainly clear"):
        return {"condition": condition}

    def test_heavy_rain(self):
        s = _subtitle(_today(), "Heavy rain likely 09:00-11:00", self._tomorrow(), "heavy", {}, {})
        assert "umbrella" in s.lower()

    def test_regular_rain(self):
        s = _subtitle(_today(), "Rain likely 09:00-11:00", self._tomorrow(), "regular", {}, {})
        assert "umbrella" in s.lower()

    def test_light_rain(self):
        s = _subtitle(_today(), "Light rain possible around 09:00", self._tomorrow(), "light", {}, {})
        assert "layer" in s.lower() or "umbrella" in s.lower()

    def test_hot_day(self):
        s = _subtitle(_today(max_c=31.0), "No rain expected", self._tomorrow(), "none", {}, {})
        assert "hydrate" in s.lower() or "afternoon" in s.lower()

    def test_cold_start(self):
        s = _subtitle(_today(min_c=3.0), "No rain expected", self._tomorrow(), "none", {}, {})
        assert "cold" in s.lower()

    def test_yahoo_umbrella_index_ascii(self):
        indices = {"umbrella": {"note": "Carry an umbrella today"}}
        s = _subtitle(_today(), "No rain expected", self._tomorrow(), "none", {}, indices)
        assert "umbrella" in s.lower()

    def test_yahoo_umbrella_index_non_ascii_skipped(self):
        indices = {"傘": {"note": "折り畳み傘を忘れずに"}}
        s = _subtitle(_today(), "No rain expected", self._tomorrow(), "none", {}, indices)
        # Falls through to non-Yahoo logic
        assert "折り畳み" not in s

    def test_tomorrow_preview(self):
        s = _subtitle(_today(), "No rain expected", self._tomorrow("Rain showers"), "none", {}, {})
        assert "rain showers" in s.lower()


# ---------------------------------------------------------------------------
# _bullets
# ---------------------------------------------------------------------------

class TestBullets:
    def test_always_returns_three_items(self):
        bullets = _bullets(_today(), "No rain expected", "none", {}, {})
        assert len(bullets) == 3

    def test_first_bullet_is_rain_window(self):
        bullets = _bullets(_today(), "No rain expected", "none", {}, {})
        assert bullets[0] == "No rain expected"

    def test_second_bullet_has_temperatures(self):
        bullets = _bullets(_today(max_c=22.0, min_c=10.0), "No rain expected", "none", {}, {})
        assert "22" in bullets[1] and "10" in bullets[1]

    def test_heavy_rain_umbrella_hint(self):
        bullets = _bullets(_today(), "Heavy rain likely", "heavy", {}, {})
        assert any("umbrella" in b.lower() for b in bullets)

    def test_light_rain_hint(self):
        bullets = _bullets(_today(), "Light rain possible", "light", {}, {})
        assert any("light rain" in b.lower() for b in bullets)

    def test_big_swing_hint(self):
        bullets = _bullets(_today(max_c=28.0, min_c=14.0), "No rain expected", "none", {}, {})
        assert any("swing" in b.lower() or "layer" in b.lower() for b in bullets)


# ---------------------------------------------------------------------------
# build_payload (integration)
# ---------------------------------------------------------------------------

class TestDayContext:
    def _dt(self, year, month, day, hour=9):
        return datetime(year, month, day, hour, tzinfo=ZoneInfo("Asia/Tokyo"))

    def test_keys_present(self):
        ctx = _day_context(self._dt(2024, 6, 15))
        for key in ("date_iso", "date_pretty", "weekday", "is_weekend", "month_name", "season", "part_of_day"):
            assert key in ctx

    def test_weekend_detection(self):
        assert _day_context(self._dt(2024, 6, 15))["is_weekend"] is True   # Saturday
        assert _day_context(self._dt(2024, 6, 17))["is_weekend"] is False  # Monday

    def test_season_mapping(self):
        assert _day_context(self._dt(2024, 1, 10))["season"] == "winter"
        assert _day_context(self._dt(2024, 4, 10))["season"] == "spring"
        assert _day_context(self._dt(2024, 7, 10))["season"] == "summer"
        assert _day_context(self._dt(2024, 10, 10))["season"] == "autumn"

    def test_part_of_day(self):
        assert _day_context(self._dt(2024, 6, 15, 8))["part_of_day"] == "morning"
        assert _day_context(self._dt(2024, 6, 15, 13))["part_of_day"] == "midday"
        assert _day_context(self._dt(2024, 6, 15, 19))["part_of_day"] == "evening"
        assert _day_context(self._dt(2024, 6, 15, 23))["part_of_day"] == "night"

    def test_date_iso_format(self):
        assert _day_context(self._dt(2024, 6, 15))["date_iso"] == "2024-06-15"


class TestTargetDate:
    def _dt(self, hour):
        return datetime(2024, 6, 15, hour, tzinfo=ZoneInfo("Asia/Tokyo"))

    def test_morning_and_midday_target_today(self):
        assert _target_offset_days(self._dt(8)) == 0   # 8am refresh
        assert _target_offset_days(self._dt(13)) == 0  # 1pm refresh

    def test_evening_targets_tomorrow(self):
        assert _target_offset_days(self._dt(21)) == 1  # 9pm builds tomorrow
        assert _target_offset_days(self._dt(18)) == 1  # cutoff is 18:00

    def test_daypart_roles(self):
        assert _daypart_role(self._dt(8)) == "morning_update"
        assert _daypart_role(self._dt(13)) == "afternoon"
        assert _daypart_role(self._dt(21)) == "primary"


class TestBuildPayload:
    def test_top_level_keys(self):
        result = build_payload(_CONTEXT)
        for key in ("generated_at_local", "timezone", "day_context", "location", "today", "tomorrow", "brief_context", "brief"):
            assert key in result, f"Missing key: {key}"

    def test_day_context_attached(self):
        ctx = build_payload(_CONTEXT)["day_context"]
        assert ctx["season"] in ("winter", "spring", "summer", "autumn")
        assert isinstance(ctx["is_weekend"], bool)

    def test_day_context_has_target_fields(self):
        # The forecast-day anchor (theme lock) and run-role must be exposed.
        ctx = build_payload(_CONTEXT)["day_context"]
        assert ctx["target_date_iso"] == ctx["date_iso"]
        assert ctx["daypart_role"] in ("primary", "morning_update", "afternoon")
        assert "run_date_iso" in ctx

    def test_timezone(self):
        assert build_payload(_CONTEXT)["timezone"] == "Asia/Tokyo"

    def test_brief_has_required_fields(self):
        brief = build_payload(_CONTEXT)["brief"]
        for key in ("headline", "subtitle", "bullets", "rain_window", "rain_level", "temp_range", "tomorrow_preview", "illustration_prompt", "layout_emphasis"):
            assert key in brief, f"Missing brief key: {key}"

    def test_brief_headline_is_str(self):
        brief = build_payload(_CONTEXT)["brief"]
        assert isinstance(brief["headline"], str)
        assert len(brief["headline"]) > 0

    def test_today_daily_summary_present(self):
        result = build_payload(_CONTEXT)
        assert "daily_summary" in result["today"]
        assert "hourly" in result["today"]

    def test_tomorrow_daily_summary_present(self):
        result = build_payload(_CONTEXT)
        assert "daily_summary" in result["tomorrow"]

    def test_temp_range_format(self):
        result = build_payload(_CONTEXT)
        temp_range = result["brief"]["temp_range"]
        assert "C" in temp_range and "-" in temp_range

    def test_missing_open_meteo_raises(self):
        bad_context = {"sources": {}, "source_priority": []}
        with pytest.raises(RuntimeError, match="Missing Open-Meteo payload"):
            build_payload(bad_context)

    def test_layout_emphasis_keys(self):
        emphasis = build_payload(_CONTEXT)["brief"]["layout_emphasis"]
        assert "rain" in emphasis
        assert "temperature" in emphasis
        assert emphasis["rain"] in ("high", "medium")
        assert emphasis["temperature"] in ("high", "medium")


# ---------------------------------------------------------------------------
# Same-day fallback rain window (issue #1)
# ---------------------------------------------------------------------------

_TZ = ZoneInfo("Asia/Tokyo")


def _hourly_series(day, rain_hours=(), code=0, rain_mm=0.0, rain_prob=90):
    hours = list(range(24))
    return {
        "time": [f"{day}T{hour:02d}:00" for hour in hours],
        "temperature_2m": [20.0 for _ in hours],
        "precipitation_probability": [rain_prob if hour in rain_hours else 5 for hour in hours],
        "precipitation": [rain_mm if hour in rain_hours else 0.0 for hour in hours],
        "weather_code": [code if hour in rain_hours else 0 for hour in hours],
    }


def _replay_context(day1_rain=(), day1_code=61, day1_mm=0.8,
                    day2_rain=(), day2_code=65, day2_mm=1.8,
                    day1_daily_code=61, day2_daily_code=0):
    """Context mirroring the recorded morning/afternoon replay inputs.

    Both target days carry a full 24h of hourly rows so the same-day filter and
    the untouched next-day window can be exercised from one fixture.
    """
    day1 = _hourly_series("2026-10-01", day1_rain, day1_code, day1_mm)
    day2 = _hourly_series("2026-10-02", day2_rain, day2_code, day2_mm)
    raw = {
        "timezone": "Asia/Tokyo",
        "daily": {
            "time": ["2026-10-01", "2026-10-02", "2026-10-03"],
            "weather_code": [day1_daily_code, day2_daily_code, 2],
            "temperature_2m_max": [25.0, 26.0, 24.0],
            "temperature_2m_min": [18.0, 17.0, 16.0],
            "precipitation_probability_max": [80, 40, 20],
            "precipitation_sum": [5.0, 1.0, 0.0],
        },
        "hourly": {
            field: day1[field] + day2[field]
            for field in day1
        },
    }
    return {
        "sources": {
            "open_meteo": {
                "payload": {
                    "location": {"timezone": "Asia/Tokyo", "latitude": 35.658, "longitude": 139.6835},
                    "raw": raw,
                }
            }
        },
        "source_priority": ["yahoo", "open_meteo"],
        "ordered_facts": [],
        "conflicts": [],
        "missing_sections": [],
    }


class TestRemainingHourlyRows:
    def test_drops_elapsed_hours_and_keeps_current_hour(self):
        rows = [_row(f"2026-10-01T{hour:02d}:00", 0.0, 0, 0) for hour in (11, 12, 13, 14)]
        now = datetime(2026, 10, 1, 13, 1, tzinfo=_TZ)
        kept = _remaining_hourly_rows(rows, now)
        assert [row["time"][11:13] for row in kept] == ["13", "14"]

    def test_naive_stamps_are_compared_in_the_run_timezone(self):
        rows = [_row("2026-10-01T13:00", 0.0, 0, 0)]
        now = datetime(2026, 10, 1, 13, 1, tzinfo=ZoneInfo("UTC"))
        # 13:00 naive is interpreted as 13:00 UTC, which is >= the 13:00 cutoff.
        assert len(_remaining_hourly_rows(rows, now)) == 1


class TestRemainingSummary:
    def _daily(self):
        return {
            "date": "2026-10-01",
            "condition": "Slight rain",
            "weather_code": 61,
            "temp_max_c": 25.0,
            "temp_min_c": 18.0,
            "rain_prob_max_pct": 80,
            "rain_sum_mm": 5.0,
        }

    def test_temperature_extrema_are_preserved(self):
        summary = _remaining_summary(self._daily(), [_row("2026-10-01T13:00", 0.0, 5, 0)])
        assert summary["temp_max_c"] == 25.0
        assert summary["temp_min_c"] == 18.0

    def test_rain_fields_describe_only_remaining_rows(self):
        rows = [
            _row("2026-10-01T13:00", 0.0, 5, 0),
            _row("2026-10-01T14:00", 0.4, 10, 3),
        ]
        summary = _remaining_summary(self._daily(), rows)
        assert summary["weather_code"] == 3
        assert summary["condition"] == "Overcast"
        assert summary["rain_prob_max_pct"] == 10
        assert summary["rain_sum_mm"] == 0.4

    def test_empty_window_is_dry(self):
        summary = _remaining_summary(self._daily(), [])
        assert summary["weather_code"] == 0
        assert summary["condition"] == "Clear sky"
        assert summary["rain_prob_max_pct"] == 0
        assert summary["rain_sum_mm"] == 0.0


class TestSameDayFallbackRain:
    NOW_MIDDAY = datetime(2026, 10, 1, 13, 1, tzinfo=_TZ)

    @pytest.fixture(autouse=True)
    def _no_forced_tomorrow(self, monkeypatch):
        monkeypatch.delenv("FORECAST_TARGET", raising=False)

    def test_overnight_rain_is_not_presented_as_upcoming_at_midday(self):
        ctx = _replay_context(day1_rain=range(1, 6), day1_mm=0.8)
        result = build_payload(ctx, now_local=self.NOW_MIDDAY)
        brief = result["brief"]

        assert brief["rain_level"] == "none"
        assert brief["rain_window"] == "No rain expected"
        assert "01:00" not in brief["rain_window"]
        assert "rain" not in brief["headline"].lower()
        # Full-day temperature extrema survive even though the min/max rows
        # themselves may already have elapsed.
        assert brief["temp_range"] == "18C-25C"

    def test_refresh_reports_dry_remainder_of_the_day(self):
        ctx = _replay_context(day1_rain=range(1, 6))
        result = build_payload(ctx, now_local=self.NOW_MIDDAY)
        summary = result["today"]["daily_summary"]

        assert summary["weather_code"] == 0
        assert summary["condition"] == "Clear sky"
        assert summary["rain_prob_max_pct"] == 5
        subject = result["brief"]["illustration_prompt"]
        assert subject == _DETERMINISTIC_SCENES[0]
        # The fallback image subject is a scene, never caption material:
        # no numbers/percentages and no poster/forecast wording.
        assert _has_caption_material(subject) is False
        assert result["brief"]["layout_emphasis"]["rain"] == "medium"

    def test_rain_in_the_current_hour_is_retained(self):
        # Rain only in the 13:00 row (an ongoing/current interval at 13:01)
        # must survive the same-day filter even though earlier rows are gone.
        ctx = _replay_context(day1_rain={13}, day1_code=61, day1_mm=0.2)
        result = build_payload(ctx, now_local=self.NOW_MIDDAY)

        assert result["brief"]["rain_level"] == "light"
        assert result["brief"]["rain_window"] == "Light rain possible around 13:00"

    def test_ongoing_rain_interval_is_retained(self):
        # Rain spans 12:00-15:00; the 13:00 run must keep what is happening now
        # without reporting the already-finished 12:00 hour as upcoming.
        ctx = _replay_context(day1_rain=range(12, 16), day1_code=63, day1_mm=0.8)
        result = build_payload(ctx, now_local=self.NOW_MIDDAY)
        brief = result["brief"]

        assert brief["rain_level"] == "regular"
        assert brief["rain_window"] == "Rain likely 13:00-15:00"
        assert "12:00" not in brief["rain_window"]

    def test_future_rain_is_reported(self):
        ctx = _replay_context(day1_rain=range(16, 19), day1_code=61, day1_mm=0.2)
        now = datetime(2026, 10, 1, 8, 0, tzinfo=_TZ)
        result = build_payload(ctx, now_local=now)

        assert result["brief"]["rain_level"] == "light"
        assert result["brief"]["rain_window"] == "Light rain possible 16:00-18:00"

    def test_morning_update_drops_only_elapsed_overnight_rain(self):
        ctx = _replay_context(day1_rain=range(1, 6))
        now = datetime(2026, 10, 1, 8, 0, tzinfo=_TZ)
        result = build_payload(ctx, now_local=now)

        assert result["brief"]["rain_window"] == "No rain expected"

    def test_evening_run_preserves_full_next_day_window(self):
        ctx = _replay_context(day2_rain=range(1, 6), day2_code=65, day2_mm=1.8,
                              day2_daily_code=65)
        now = datetime(2026, 10, 1, 21, 0, tzinfo=_TZ)
        result = build_payload(ctx, now_local=now)

        assert result["today"]["daily_summary"]["date"] == "2026-10-02"
        assert result["brief"]["rain_level"] == "heavy"
        assert result["brief"]["rain_window"] == "Heavy rain likely 01:00-05:00"
        assert result["brief"]["temp_range"] == "17C-26C"

    def test_utc_clock_matches_local_run_time(self):
        ctx = _replay_context(day1_rain=range(1, 6))
        local = build_payload(ctx, now_local=self.NOW_MIDDAY)
        utc = build_payload(ctx, now_local=datetime(2026, 10, 1, 4, 1, tzinfo=ZoneInfo("UTC")))

        assert local["brief"]["rain_window"] == "No rain expected"
        assert utc["brief"]["rain_window"] == local["brief"]["rain_window"]
        assert utc["day_context"]["run_date_iso"] == "2026-10-01"


# ---------------------------------------------------------------------------
# Deterministic fallback image subject (issue #16, AC3)
# ---------------------------------------------------------------------------

class TestDeterministicIllustrationPrompt:
    """The fallback brief feeds image generation when every model fails."""

    def test_every_mapped_condition_gets_a_caption_free_scene(self):
        for code, scene in _DETERMINISTIC_SCENES.items():
            condition = WEATHER_LABELS.get(code, f"Code {code}")
            subject = _deterministic_illustration_prompt(
                {"weather_code": code, "condition": condition}
            )
            assert subject == scene
            assert subject.isascii()
            assert _has_caption_material(subject) is False

    def test_unknown_numeric_label_does_not_leak_digits(self):
        # Open-Meteo can report a code with no label; never echo "Code 1234"
        # into the art subject, which would hand the image model lettering.
        subject = _deterministic_illustration_prompt(
            {"weather_code": 1234, "condition": "Code 1234"}
        )
        assert subject == "the day's weather over a quiet city skyline"
        assert _has_caption_material(subject) is False

    def test_unknown_text_label_still_yields_a_scene(self):
        subject = _deterministic_illustration_prompt(
            {"weather_code": 1234, "condition": "Wild Weather"}
        )
        assert subject == "the day's wild weather over a quiet city skyline"
        assert _has_caption_material(subject) is False
