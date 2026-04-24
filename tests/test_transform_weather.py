"""Tests for scripts/weather/transform_weather.py"""
import pytest

from scripts.weather.transform_weather import (
    WEATHER_LABELS,
    _bullets,
    _daily_summary,
    _headline,
    _hourly_rows,
    _is_ascii_text,
    _rain_rows_by_intensity,
    _rain_window,
    _subtitle,
    build_payload,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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

class TestBuildPayload:
    def test_top_level_keys(self):
        result = build_payload(_CONTEXT)
        for key in ("generated_at_local", "timezone", "location", "today", "tomorrow", "brief_context", "brief"):
            assert key in result, f"Missing key: {key}"

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
