"""Tests for pure helpers in scripts/weather/fetch_context.py"""
from datetime import date

from scripts.weather.fetch_context import (
    moon_phase,
    parse_ics_events,
    select_holidays,
)


# ---------------------------------------------------------------------------
# moon_phase
# ---------------------------------------------------------------------------

class TestMoonPhase:
    def test_keys_present(self):
        m = moon_phase(date(2024, 6, 15))
        assert set(m) == {"phase", "age_days", "illumination_pct"}

    def test_illumination_in_range(self):
        for day in range(1, 30):
            m = moon_phase(date(2024, 6, day))
            assert 0 <= m["illumination_pct"] <= 100

    def test_age_within_synodic_month(self):
        m = moon_phase(date(2024, 1, 1))
        assert 0 <= m["age_days"] < 29.6

    def test_known_full_moon_is_bright(self):
        # 2024-04-23 was a full moon; illumination should be near 100%.
        m = moon_phase(date(2024, 4, 23))
        assert m["illumination_pct"] >= 95
        assert m["phase"] == "Full Moon"

    def test_known_new_moon_is_dark(self):
        # 2024-04-08 was a new moon (the total-eclipse one).
        m = moon_phase(date(2024, 4, 8))
        assert m["illumination_pct"] <= 5
        assert m["phase"] == "New Moon"

    def test_phase_name_is_known(self):
        names = {
            "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
            "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent",
        }
        assert moon_phase(date(2024, 6, 15))["phase"] in names


# ---------------------------------------------------------------------------
# select_holidays
# ---------------------------------------------------------------------------

_HOLIDAYS = [
    {"date": "2026-07-20", "name": "Marine Day", "localName": "海の日"},
    {"date": "2026-07-23", "name": "Future Day", "localName": "未来の日"},
    {"date": "2026-01-01", "name": "New Year's Day", "localName": "元日"},
]


class TestSelectHolidays:
    def test_today_matches(self):
        today, upcoming = select_holidays(_HOLIDAYS, "2026-07-20", 10)
        assert today == "Marine Day"
        assert upcoming == {"name": "Future Day", "date": "2026-07-23", "days_away": 3}

    def test_no_today_but_upcoming(self):
        today, upcoming = select_holidays(_HOLIDAYS, "2026-07-18", 10)
        assert today is None
        assert upcoming["name"] == "Marine Day"
        assert upcoming["days_away"] == 2

    def test_upcoming_outside_window_ignored(self):
        today, upcoming = select_holidays(_HOLIDAYS, "2026-07-01", 5)
        assert today is None
        assert upcoming is None

    def test_picks_nearest_upcoming(self):
        _, upcoming = select_holidays(_HOLIDAYS, "2026-07-19", 30)
        assert upcoming["name"] == "Marine Day"  # nearest, not Future Day

    def test_empty_list(self):
        assert select_holidays([], "2026-07-20", 10) == (None, None)

    def test_bad_today_iso(self):
        assert select_holidays(_HOLIDAYS, "not-a-date", 10) == (None, None)

    def test_skips_malformed_entries(self):
        items = [{"date": "bad"}, {"name": "no date"}, "string", None]
        assert select_holidays(items, "2026-07-20", 10) == (None, None)


# ---------------------------------------------------------------------------
# parse_ics_events
# ---------------------------------------------------------------------------

_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260627
SUMMARY:All day picnic
END:VEVENT
BEGIN:VEVENT
DTSTART:20260627T090000Z
SUMMARY:Morning standup
END:VEVENT
BEGIN:VEVENT
DTSTART:20260628T100000Z
SUMMARY:Tomorrow thing
END:VEVENT
END:VCALENDAR
"""


class TestParseIcsEvents:
    def test_matches_all_day_and_timed(self):
        events = parse_ics_events(_ICS, "20260627")
        assert "All day picnic" in events
        assert "Morning standup" in events

    def test_excludes_other_days(self):
        events = parse_ics_events(_ICS, "20260627")
        assert "Tomorrow thing" not in events

    def test_no_match_returns_empty(self):
        assert parse_ics_events(_ICS, "20991231") == []

    def test_handles_folded_summary(self):
        ics = (
            "BEGIN:VEVENT\n"
            "DTSTART;VALUE=DATE:20260627\n"
            "SUMMARY:A very long event title that\n  continues on the next line\n"
            "END:VEVENT\n"
        )
        events = parse_ics_events(ics, "20260627")
        assert events == ["A very long event title that continues on the next line"]

    def test_empty_calendar(self):
        assert parse_ics_events("BEGIN:VCALENDAR\nEND:VCALENDAR\n", "20260627") == []
