"""Tests for scripts/ops/render_gate.py"""
from scripts.ops.render_gate import compute_signature, should_regenerate


def _payload(part="morning", condition="Slight rain", high=22.0, code=61, holiday=None):
    return {
        "day_context": {"date_iso": "2026-06-27", "part_of_day": part, "holiday_today": holiday},
        "today": {"daily_summary": {
            "condition": condition, "weather_code": code, "temp_max_c": high,
            "temp_min_c": 18.0, "rain_prob_max_pct": 80,
        }},
        "tomorrow": {"daily_summary": {"condition": "Clear sky"}},
    }


class TestComputeSignature:
    def test_deterministic(self):
        assert compute_signature(_payload()) == compute_signature(_payload())

    def test_changes_with_part_of_day(self):
        assert compute_signature(_payload(part="morning")) != compute_signature(_payload(part="evening"))

    def test_changes_with_condition(self):
        assert compute_signature(_payload(condition="Heavy rain")) != compute_signature(_payload())

    def test_changes_with_temperature(self):
        assert compute_signature(_payload(high=30.0)) != compute_signature(_payload())

    def test_changes_with_holiday(self):
        assert compute_signature(_payload(holiday="Marine Day")) != compute_signature(_payload())

    def test_short_hex(self):
        sig = compute_signature(_payload())
        assert len(sig) == 16 and all(c in "0123456789abcdef" for c in sig)

    def test_handles_missing_sections(self):
        assert isinstance(compute_signature({}), str)


_NOW = "2026-06-27T12:00:00+09:00"


class TestShouldRegenerate:
    def _last(self, sig="abc", at="2026-06-27T11:00:00+09:00"):
        return {"signature": sig, "generated_at": at, "brief": {"headline": "x"}}

    def test_same_signature_within_interval_skips(self):
        # 1h elapsed, interval 1.5h -> reuse.
        assert should_regenerate(self._last(), "abc", _NOW, 5400) is False

    def test_same_signature_past_interval_regenerates(self):
        # 1h elapsed, interval 0.5h -> refresh for variety.
        assert should_regenerate(self._last(), "abc", _NOW, 1800) is True

    def test_changed_signature_always_regenerates(self):
        assert should_regenerate(self._last(sig="zzz"), "abc", _NOW, 999999) is True

    def test_no_last_good_regenerates(self):
        assert should_regenerate({}, "abc", _NOW, 5400) is True

    def test_force_regenerates(self):
        assert should_regenerate(self._last(), "abc", _NOW, 999999, force=True) is True

    def test_skip_disabled_regenerates(self):
        assert should_regenerate(self._last(), "abc", _NOW, 999999, skip_enabled=False) is True

    def test_missing_generated_at_regenerates(self):
        assert should_regenerate({"signature": "abc"}, "abc", _NOW, 5400) is True

    def test_bad_timestamp_regenerates(self):
        assert should_regenerate(self._last(at="not-a-time"), "abc", _NOW, 5400) is True
