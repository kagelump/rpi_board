"""Tests for scripts/weather/aggregate_weather_sources.py"""
import pytest

from scripts.weather.aggregate_weather_sources import (
    MULTISOURCE_SCHEMA_VERSION,
    _detect_conflicts,
    _open_meteo_facts,
    _source_entry,
    _yahoo_facts,
    build_aggregated_context,
)

# ---------------------------------------------------------------------------
# Minimal fixture data
# ---------------------------------------------------------------------------

_SETTINGS = {
    "location": {
        "latitude": 35.6762,
        "longitude": 139.6503,
        "timezone": "Asia/Tokyo",
    },
    "pipeline": {
        "source_order": ["yahoo", "open_meteo"],
    },
}

_OM_PAYLOAD = {
    "fetched_at": "2024-01-10T00:00:00+00:00",
    "location": {"latitude": 35.68, "longitude": 139.69, "timezone": "Asia/Tokyo"},
    "source": "open-meteo",
    "raw": {
        "timezone": "Asia/Tokyo",
        "daily": {
            "time": ["2024-01-10", "2024-01-11"],
            "weather_code": [61, 0],
            "temperature_2m_max": [18.0, 15.0],
            "temperature_2m_min": [10.0, 8.0],
            "precipitation_probability_max": [70, 5],
            "precipitation_sum": [4.0, 0.0],
        },
        "hourly": {
            "time": ["2024-01-10T06:00", "2024-01-10T09:00"],
            "temperature_2m": [12.0, 15.0],
            "precipitation_probability": [60, 80],
            "precipitation": [0.4, 1.2],
            "weather_code": [61, 65],
        },
    },
}

_YAHOO_PAYLOAD = {
    "fetched_at": "2024-01-10T00:00:00+00:00",
    "today_tomorrow": [
        {
            "date_label": "Today",
            "weekday": "Wed",
            "condition": "Rainy",
            "temp_min_c": 10,
            "temp_max_c": 19,
            "precipitation_windows": [
                {"period": "morning", "raw": "60%", "precipitation_probability_pct": 60}
            ],
            "wind": "NE 3m/s",
        },
        {
            "date_label": "Tomorrow",
            "weekday": "Thu",
            "condition": "Cloudy",
            "temp_min_c": 8,
            "temp_max_c": 16,
            "precipitation_windows": [],
        },
    ],
    "alerts": [],
    "indices": {
        "days": [
            {
                "date_label": "Today",
                "items": {
                    "umbrella": {"score_text": "Needed", "note": "Carry an umbrella"},
                }
            }
        ]
    },
    "missing_sections": [],
}


# ---------------------------------------------------------------------------
# _source_entry
# ---------------------------------------------------------------------------

class TestSourceEntry:
    def test_basic_fields(self):
        entry = _source_entry("open_meteo", True, {"x": 1}, [])
        assert entry["source_name"] == "open_meteo"
        assert entry["available"] is True
        assert entry["payload"] == {"x": 1}
        assert entry["missing_sections"] == []

    def test_missing_sections_sorted_deduplicated(self):
        entry = _source_entry("yahoo", False, {}, ["c_section", "a_section", "c_section"])
        assert entry["missing_sections"] == ["a_section", "c_section"]

    def test_unavailable_source(self):
        entry = _source_entry("foo", False, None, ["all"])
        assert entry["available"] is False
        assert entry["missing_sections"] == ["all"]


# ---------------------------------------------------------------------------
# _yahoo_facts
# ---------------------------------------------------------------------------

class TestYahooFacts:
    def test_generates_condition_and_temp_facts(self):
        facts = _yahoo_facts(_YAHOO_PAYLOAD)
        ids = [f["id"] for f in facts]
        assert "yahoo.today_tomorrow.0.condition" in ids
        assert "yahoo.today_tomorrow.0.temperature" in ids

    def test_generates_precipitation_facts(self):
        facts = _yahoo_facts(_YAHOO_PAYLOAD)
        ids = [f["id"] for f in facts]
        assert "yahoo.today_tomorrow.0.precip.morning" in ids

    def test_generates_index_facts(self):
        facts = _yahoo_facts(_YAHOO_PAYLOAD)
        ids = [f["id"] for f in facts]
        assert any("umbrella" in fid for fid in ids)

    def test_all_facts_have_required_fields(self):
        for fact in _yahoo_facts(_YAHOO_PAYLOAD):
            assert "id" in fact
            assert "source" in fact
            assert "text" in fact
            assert "value" in fact
            assert fact["source"] == "yahoo"

    def test_empty_payload(self):
        assert _yahoo_facts({}) == []

    def test_alert_fact(self):
        payload_with_alert = {
            **_YAHOO_PAYLOAD,
            "alerts": [{"level": "Warning", "text": "Heavy rain"}],
        }
        facts = _yahoo_facts(payload_with_alert)
        ids = [f["id"] for f in facts]
        assert "yahoo.alerts.0" in ids

    def test_multiple_days(self):
        facts = _yahoo_facts(_YAHOO_PAYLOAD)
        day1_ids = [f["id"] for f in facts if "today_tomorrow.1" in f["id"]]
        assert len(day1_ids) >= 2  # at least condition + temperature


# ---------------------------------------------------------------------------
# _open_meteo_facts
# ---------------------------------------------------------------------------

class TestOpenMeteoFacts:
    def test_generates_one_fact_per_day(self):
        facts = _open_meteo_facts(_OM_PAYLOAD)
        assert len(facts) == 2

    def test_fact_ids(self):
        facts = _open_meteo_facts(_OM_PAYLOAD)
        assert facts[0]["id"] == "open_meteo.daily.0"
        assert facts[1]["id"] == "open_meteo.daily.1"

    def test_fact_values_present(self):
        fact = _open_meteo_facts(_OM_PAYLOAD)[0]
        assert fact["value"]["date"] == "2024-01-10"
        assert fact["value"]["temp_max_c"] == 18.0
        assert fact["value"]["temp_min_c"] == 10.0

    def test_all_facts_source_is_open_meteo(self):
        for fact in _open_meteo_facts(_OM_PAYLOAD):
            assert fact["source"] == "open_meteo"

    def test_empty_payload(self):
        assert _open_meteo_facts({}) == []


# ---------------------------------------------------------------------------
# _detect_conflicts
# ---------------------------------------------------------------------------

class TestDetectConflicts:
    def test_no_conflict_within_threshold(self):
        # Delta of 2°C, threshold is 3°C
        yahoo = {"today_tomorrow": [{"temp_max_c": 20}]}
        om = {"raw": {"daily": {"temperature_2m_max": [21.9]}}}
        assert _detect_conflicts(yahoo, om) == []

    def test_conflict_exceeds_threshold(self):
        yahoo = {"today_tomorrow": [{"temp_max_c": 15}]}
        om = {"raw": {"daily": {"temperature_2m_max": [20.0]}}}
        conflicts = _detect_conflicts(yahoo, om)
        assert len(conflicts) == 1
        assert conflicts[0]["metric"] == "today.temp_max_c"
        assert conflicts[0]["delta"] == 5.0

    def test_missing_yahoo_temp_no_conflict(self):
        yahoo = {"today_tomorrow": [{}]}  # no temp_max_c key
        om = {"raw": {"daily": {"temperature_2m_max": [20.0]}}}
        assert _detect_conflicts(yahoo, om) == []

    def test_empty_sources(self):
        assert _detect_conflicts({}, {}) == []

    def test_exact_threshold_no_conflict(self):
        # Delta exactly 3 is NOT >= 3... wait, the code says `if delta >= 3`, so exactly 3 IS a conflict
        yahoo = {"today_tomorrow": [{"temp_max_c": 17.0}]}
        om = {"raw": {"daily": {"temperature_2m_max": [20.0]}}}
        conflicts = _detect_conflicts(yahoo, om)
        assert len(conflicts) == 1
        assert conflicts[0]["delta"] == 3.0


# ---------------------------------------------------------------------------
# build_aggregated_context (integration)
# ---------------------------------------------------------------------------

class TestBuildAggregatedContext:
    def test_schema_version(self):
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, _YAHOO_PAYLOAD, {})
        assert result["schema_version"] == MULTISOURCE_SCHEMA_VERSION

    def test_top_level_keys(self):
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, _YAHOO_PAYLOAD, {})
        for key in ("schema_version", "generated_at_local", "timezone", "location",
                    "source_priority", "sources", "ordered_facts", "conflicts", "missing_sections"):
            assert key in result, f"Missing key: {key}"

    def test_timezone(self):
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, {}, {})
        assert result["timezone"] == "Asia/Tokyo"

    def test_sources_present(self):
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, _YAHOO_PAYLOAD, {})
        assert "open_meteo" in result["sources"]
        assert "yahoo" in result["sources"]

    def test_source_availability(self):
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, _YAHOO_PAYLOAD, {})
        assert result["sources"]["open_meteo"]["available"] is True
        assert result["sources"]["yahoo"]["available"] is True

    def test_missing_yahoo_marks_unavailable(self):
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, {}, {})
        assert result["sources"]["yahoo"]["available"] is False

    def test_ordered_facts_contains_yahoo_then_open_meteo(self):
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, _YAHOO_PAYLOAD, {})
        sources = [f["source"] for f in result["ordered_facts"]]
        yahoo_indices = [i for i, s in enumerate(sources) if s == "yahoo"]
        om_indices = [i for i, s in enumerate(sources) if s == "open_meteo"]
        assert yahoo_indices, "No yahoo facts found"
        assert om_indices, "No open_meteo facts found"
        assert min(yahoo_indices) < min(om_indices), "Yahoo facts should appear before open_meteo"

    def test_conflict_detected_between_sources(self):
        # Force a large temperature discrepancy
        yahoo_with_different_temp = {
            **_YAHOO_PAYLOAD,
            "today_tomorrow": [
                {**_YAHOO_PAYLOAD["today_tomorrow"][0], "temp_max_c": 5},  # delta = 13°C vs OM's 18
            ],
        }
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, yahoo_with_different_temp, {})
        assert len(result["conflicts"]) == 1

    def test_extra_sources_included(self):
        extras = {"my_source": {"some": "data"}}
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, {}, extras)
        assert "my_source" in result["sources"]

    def test_missing_sections_format(self):
        result = build_aggregated_context(_SETTINGS, _OM_PAYLOAD, {}, {})
        # Yahoo is missing, so "yahoo:all" should appear
        assert any("yahoo" in s for s in result["missing_sections"])
