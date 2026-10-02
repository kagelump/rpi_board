"""Tests for pure helpers in scripts/openrouter/generate_brief.py"""
import io
import json
import time
import urllib.error
from pathlib import Path

import pytest

from scripts.openrouter.generate_brief import (
    _brief_model_attempts,
    _brief_retry_backoff_seconds,
    _brief_schema_violations,
    _brief_total_budget_seconds,
    _brief_violations,
    _call_openrouter,
    _enrich_payload,
    _is_valid_brief,
    _load_recent_history,
    _normalize_brief_punct,
    _renderable_deterministic_brief,
    _request_brief_with_fallback,
    _render_prompt,
    _select_text_model,
    _time_frame,
)
from scripts.openrouter.network import DeadlineExceeded, NetworkRequestError, WallClockDeadline


def _full_brief(**overrides):
    """A concise brief that satisfies the advertised six-key output contract."""
    brief = {
        "headline": "Rain by 3pm, 22C",
        "subtitle": "Bring a coat and an umbrella.",
        "illustration_prompt": "Rain over a city street.",
        "mood": "stormy",
        "accent": "yellow",
        "event_ref": "",
    }
    brief.update(overrides)
    return brief


def _display_settings():
    return {"display": {"width": 960, "height": 640, "panel_fraction": 0.25}}


# Verbatim payload of the archived model response from run
# aa5ea68fa13e43fb9b35a3bebebc4881 / snapshot 08edf1d91b5b4155ac2dae6186c8dea1.
# tests/test_issue6_brief_recovery.py pins the recorded sha256 and byte-size so
# this fixture cannot drift from the archive.
ARCHIVED_RESPONSE = json.loads(
    (Path(__file__).parent / "fixtures" / "issue6_brief_model_response.json").read_text(encoding="utf-8")
)


class TestNormalizeBriefPunct:
    def test_folds_dashes_and_quotes_to_ascii(self):
        brief = {
            "headline": "Hot — 41C peak",
            "subtitle": "Take the ‘river’ walk…",
            "illustration_prompt": "sun – over rooftops",
        }
        out = _normalize_brief_punct(brief)
        assert out["headline"] == "Hot - 41C peak"
        assert out["subtitle"] == "Take the 'river' walk..."
        assert out["illustration_prompt"] == "sun - over rooftops"
        assert out["headline"].isascii() and out["subtitle"].isascii()

    def test_leaves_plain_ascii_untouched(self):
        brief = {"headline": "Rain by 3pm, 22C", "subtitle": "Bring a coat"}
        out = _normalize_brief_punct(dict(brief))
        assert out["headline"] == brief["headline"]
        assert out["subtitle"] == brief["subtitle"]

    def test_non_dict_passthrough(self):
        assert _normalize_brief_punct(None) is None


# ---------------------------------------------------------------------------
# _time_frame (time-of-day framing)
# ---------------------------------------------------------------------------

class TestTimeFrame:
    def test_distinct_per_part_of_day(self):
        frames = {_time_frame(p) for p in ("morning", "midday", "evening", "night")}
        assert len(frames) == 4

    def test_morning_mentions_morning(self):
        assert "Morning" in _time_frame("morning")

    def test_unknown_defaults_to_midday(self):
        assert _time_frame("teatime") == _time_frame("midday")
        assert _time_frame(None) == _time_frame("midday")


# ---------------------------------------------------------------------------
# _select_text_model (online events grounding)
# ---------------------------------------------------------------------------

class TestSelectTextModel:
    def _settings(self, events_mode, model="deepseek/deepseek-v4-flash"):
        return {"openrouter": {"text_model": model}, "context": {"events_mode": events_mode}}

    def test_online_mode_appends_suffix(self):
        assert _select_text_model(self._settings("online_model")) == "deepseek/deepseek-v4-flash:online"

    def test_off_mode_unchanged(self):
        assert _select_text_model(self._settings("off")) == "deepseek/deepseek-v4-flash"

    def test_no_double_suffix(self):
        s = self._settings("online_model", model="deepseek/deepseek-v4-flash:online")
        assert _select_text_model(s) == "deepseek/deepseek-v4-flash:online"

    def test_cli_override_respected(self):
        s = self._settings("online_model")
        assert _select_text_model(s, override="openai/gpt-4o-mini") == "openai/gpt-4o-mini:online"

    def test_missing_events_mode_defaults_off(self):
        s = {"openrouter": {"text_model": "m"}, "context": {}}
        assert _select_text_model(s) == "m"


class TestBriefModelAttempts:
    def _settings(self, events_mode="online_model", retries=2):
        return {
            "openrouter": {"text_model": "provider/model"},
            "context": {"events_mode": events_mode},
            "pipeline": {"brief_offline_retry_count": retries},
        }

    def test_online_then_two_non_online_attempts(self):
        assert _brief_model_attempts(self._settings()) == [
            "provider/model:online",
            "provider/model",
            "provider/model",
        ]

    def test_non_online_primary_can_retry_twice(self):
        assert _brief_model_attempts(self._settings(events_mode="off")) == [
            "provider/model",
            "provider/model",
            "provider/model",
        ]

    def test_retry_count_is_configurable(self):
        assert _brief_model_attempts(self._settings(retries=1)) == [
            "provider/model:online",
            "provider/model",
        ]


class TestRequestBriefWithFallback:
    def _settings(self):
        return {
            "openrouter": {"text_model": "provider/model", "brief_temperature": 0.5},
            "context": {"events_mode": "online_model"},
            "pipeline": {"brief_timeout_seconds": 8, "brief_offline_retry_count": 2},
            "display": {"width": 960, "height": 640, "panel_fraction": 0.25},
        }

    def _valid(self):
        return {
            "headline": "Rain by 3pm",
            "subtitle": "Bring an umbrella.",
            "illustration_prompt": "Rain over a city street.",
            "mood": "stormy",
            "accent": "yellow",
            "event_ref": "",
        }

    def test_primary_success_does_not_retry(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief._call_openrouter",
            lambda settings, prompt, model_override=None, **kwargs: calls.append(model_override) or self._valid(),
        )
        monkeypatch.setattr("scripts.openrouter.generate_brief.record_current_log", lambda *args, **kwargs: None)
        monkeypatch.setattr("scripts.openrouter.generate_brief.record_current_snapshot", lambda *args, **kwargs: None)

        candidate, metadata = _request_brief_with_fallback(self._settings(), "prompt", "sig")

        assert candidate == self._valid()
        assert calls == ["provider/model:online"]
        assert metadata["fallback"] is False

    def test_online_failure_then_two_non_online_attempts(self, monkeypatch):
        calls = []
        outcomes = iter([RuntimeError("online failed"), RuntimeError("offline failed"), self._valid()])

        def fake_call(settings, prompt, model_override=None, **kwargs):
            calls.append(model_override)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        logs = []
        monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", fake_call)
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.record_current_log",
            lambda component, event_type, message, **kwargs: logs.append((event_type, kwargs.get("level"))),
        )
        monkeypatch.setattr("scripts.openrouter.generate_brief.record_current_snapshot", lambda *args, **kwargs: None)

        candidate, metadata = _request_brief_with_fallback(self._settings(), "prompt", "sig")

        assert candidate == self._valid()
        assert calls == ["provider/model:online", "provider/model", "provider/model"]
        assert metadata == {
            "kind": "accepted",
            "model": "provider/model",
            "attempt": 3,
            "attempt_count": 3,
            "fallback": True,
        }
        assert ("brief_request_failed", "error") not in logs
        assert logs.count(("brief_attempt_failed", "warning")) == 2

    def test_exhaustion_records_only_final_failure_as_error(self, monkeypatch):
        calls = []
        logs = []

        def always_fail(settings, prompt, model_override=None, **kwargs):
            calls.append(model_override)
            raise RuntimeError("unavailable")

        monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", always_fail)
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.record_current_log",
            lambda component, event_type, message, **kwargs: logs.append((event_type, kwargs.get("level"))),
        )
        monkeypatch.setattr("scripts.openrouter.generate_brief.record_current_snapshot", lambda *args, **kwargs: None)

        candidate, metadata = _request_brief_with_fallback(self._settings(), "prompt", "sig")

        assert candidate is None
        assert calls == ["provider/model:online", "provider/model", "provider/model"]
        assert metadata["kind"] == "error"
        assert logs.count(("brief_attempt_failed", "warning")) == 2
        assert logs.count(("brief_request_failed", "error")) == 1


# ---------------------------------------------------------------------------
# _enrich_payload / history (anti-repetition plumbing)
# ---------------------------------------------------------------------------

class TestEnrichPayload:
    def _settings(self, tmp_path, history=None):
        history_file = tmp_path / "history.json"
        if history is not None:
            history_file.write_text(json.dumps(history), encoding="utf-8")
        return {
            "runtime": {"history_file": str(history_file)},
            "voice": {"persona": "wry", "history_window": 3},
            "context": {"location_descriptor": "Tokyo"},
        }

    def _payload(self):
        return {"day_context": {"date_iso": "2024-06-15", "part_of_day": "morning"}, "brief": {}}

    def test_adds_expected_keys(self, tmp_path):
        out = _enrich_payload(self._payload(), self._settings(tmp_path))
        for key in (
            "voice", "board_context", "recent_history", "creative_angle",
            "visual_angle", "composition",
        ):
            assert key in out

    def test_does_not_mutate_input(self, tmp_path):
        payload = self._payload()
        _enrich_payload(payload, self._settings(tmp_path))
        assert "voice" not in payload

    def test_creative_angle_deterministic_for_same_day(self, tmp_path):
        settings = self._settings(tmp_path)
        a = _enrich_payload(self._payload(), settings)["creative_angle"]
        b = _enrich_payload(self._payload(), settings)["creative_angle"]
        assert a == b
        assert isinstance(a, str) and a

    def test_visual_direction_advances_for_next_target_day(self, tmp_path):
        settings = self._settings(tmp_path)
        first = self._payload()
        first["day_context"]["target_date_iso"] = "2026-07-30"
        second = self._payload()
        second["day_context"]["target_date_iso"] = "2026-07-31"

        a = _enrich_payload(first, settings)
        b = _enrich_payload(second, settings)

        assert a["visual_angle"] != b["visual_angle"]
        assert a["composition"] != b["composition"]

    def test_history_window_respected(self, tmp_path):
        history = [{"headline": f"h{i}"} for i in range(10)]
        out = _enrich_payload(self._payload(), self._settings(tmp_path, history=history))
        assert len(out["recent_history"]) == 3
        assert out["recent_history"][-1]["headline"] == "h9"

    def test_day_context_extra_merged(self, tmp_path):
        settings = self._settings(tmp_path)
        extra_file = tmp_path / "day_context.json"
        extra_file.write_text(json.dumps({
            "fetched_at": "ignore-me",
            "moon": {"phase": "Full Moon"},
            "holiday_today": "Marine Day",
        }), encoding="utf-8")
        settings["runtime"]["day_context_file"] = str(extra_file)
        payload = {"day_context": {"weekday": "Monday", "season": "summer"}, "brief": {}}
        out = _enrich_payload(payload, settings)
        # Weather-derived fields preserved, extras merged, fetched_at dropped.
        assert out["day_context"]["weekday"] == "Monday"
        assert out["day_context"]["holiday_today"] == "Marine Day"
        assert out["day_context"]["moon"]["phase"] == "Full Moon"
        assert "fetched_at" not in out["day_context"]

    def test_missing_day_context_file_leaves_payload(self, tmp_path):
        settings = self._settings(tmp_path)
        settings["runtime"]["day_context_file"] = str(tmp_path / "nope.json")
        payload = {"day_context": {"weekday": "Monday"}, "brief": {}}
        out = _enrich_payload(payload, settings)
        assert out["day_context"] == {"weekday": "Monday"}

    def test_missing_history_file_is_empty(self, tmp_path):
        assert _load_recent_history(self._settings(tmp_path)) == []

    def test_corrupt_history_file_is_empty(self, tmp_path):
        hist = tmp_path / "history.json"
        hist.write_text("not json{", encoding="utf-8")
        settings = {"runtime": {"history_file": str(hist)}, "voice": {}}
        assert _load_recent_history(settings) == []


# ---------------------------------------------------------------------------
# _is_valid_brief
# ---------------------------------------------------------------------------

class TestIsValidBrief:
    def test_valid_brief(self):
        assert _is_valid_brief(_full_brief()) is True

    def test_extra_keys_rejected(self):
        # The prompt says "exactly this schema": a decision made explicit here so
        # the archived response's extra keys are a recorded violation, not ignored.
        brief = _full_brief(bullets=["No rain"], custom_field={"a": 1})
        assert _is_valid_brief(brief) is False
        violations = _brief_schema_violations(brief)
        assert any("unexpected field 'bullets'" in v for v in violations)
        assert any("unexpected field 'custom_field'" in v for v in violations)

    def test_not_a_dict(self):
        assert _is_valid_brief("some string") is False
        assert _is_valid_brief(None) is False
        assert _is_valid_brief(["headline", "subtitle"]) is False

    @pytest.mark.parametrize(
        "key", ["headline", "subtitle", "illustration_prompt", "mood", "accent", "event_ref"]
    )
    def test_each_required_field_is_required(self, key):
        brief = _full_brief()
        del brief[key]
        assert _is_valid_brief(brief) is False
        assert any(
            f"missing required field '{key}'" in v
            for v in _brief_schema_violations(brief)
        )

    def test_empty_headline(self):
        assert _is_valid_brief(_full_brief(headline="   ")) is False

    def test_empty_subtitle(self):
        assert _is_valid_brief(_full_brief(subtitle="")) is False

    def test_empty_illustration_prompt(self):
        assert _is_valid_brief(_full_brief(illustration_prompt="  ")) is False

    def test_event_ref_may_be_empty(self):
        assert _is_valid_brief(_full_brief(event_ref="")) is True
        assert _is_valid_brief(_full_brief(event_ref="Marine Day")) is True

    def test_non_string_fields(self):
        assert _is_valid_brief(_full_brief(headline=42)) is False
        assert _is_valid_brief(_full_brief(illustration_prompt=None)) is False
        assert _is_valid_brief(_full_brief(event_ref=None)) is False
        assert _is_valid_brief(_full_brief(mood=None)) is False
        assert _is_valid_brief(_full_brief(accent=42)) is False

    def test_whitespace_headline_invalid(self):
        assert _is_valid_brief(_full_brief(headline="\t\n")) is False

    def test_mood_enum_enforced(self):
        for mood in ("calm", "alert", "cozy", "stormy", "festive", "crisp", "muggy"):
            assert _is_valid_brief(_full_brief(mood=mood)) is True
        brief = _full_brief(mood="sunny")
        assert _is_valid_brief(brief) is False
        assert any("mood='sunny'" in v for v in _brief_schema_violations(brief))

    def test_accent_enum_enforced(self):
        for accent in ("red", "yellow", "none"):
            assert _is_valid_brief(_full_brief(accent=accent)) is True
        for accent in ("black", "teal"):
            assert _is_valid_brief(_full_brief(accent=accent)) is False

    def test_headline_length_cap(self):
        assert _is_valid_brief(_full_brief(headline="a" * 52)) is True
        brief = _full_brief(headline="a" * 53)
        assert _is_valid_brief(brief) is False
        assert any("over the 52-character limit" in v for v in _brief_schema_violations(brief))

    def test_subtitle_length_cap(self):
        assert _is_valid_brief(_full_brief(subtitle="a" * 72)) is True
        brief = _full_brief(subtitle="a" * 73)
        assert _is_valid_brief(brief) is False
        assert any("over the 72-character limit" in v for v in _brief_schema_violations(brief))

    @pytest.mark.parametrize("key", ["headline", "subtitle", "illustration_prompt", "event_ref"])
    def test_non_ascii_rejected(self, key):
        brief = _full_brief(**{key: "deg \u00b0 mark"})
        assert _is_valid_brief(brief) is False
        assert any("non-ASCII" in v for v in _brief_schema_violations(brief))


class TestBriefRenderabilityValidation:
    def test_concise_brief_is_renderable(self):
        assert _brief_violations(_full_brief(), _display_settings()) == []

    def test_is_valid_brief_accepts_optional_settings(self):
        assert _is_valid_brief(_full_brief()) is True
        assert _is_valid_brief(_full_brief(), _display_settings()) is True
        assert _is_valid_brief(_full_brief(headline="W" * 52), _display_settings()) is False

    def test_schema_valid_but_unrenderable_is_rejected(self):
        # 52 chars satisfies the character cap but cannot wrap at the readable
        # minimums, so compose_board would raise; upstream validation must reject
        # it through the same font-fitting logic.
        brief = _full_brief(headline="W" * 52)
        assert _brief_schema_violations(brief) == []
        violations = _brief_violations(brief, _display_settings())
        assert any("do not fit the text panel" in v for v in violations)


class TestRenderableDeterministicBrief:
    def test_renderable_copy_is_kept_verbatim(self):
        deterministic = {
            "headline": "Light rain possible today.",
            "subtitle": "Light rain possible 01:00-05:00. A light layer should be enough.",
        }
        assert _renderable_deterministic_brief(_display_settings(), deterministic) is deterministic

    def test_unrenderable_copy_downgrades_to_proven_safe_text(self):
        deterministic = {"headline": "W" * 60, "subtitle": "x" * 200, "rain_level": "light"}
        out = _renderable_deterministic_brief(_display_settings(), deterministic)
        assert out["headline"] == "Weather update"
        assert out["subtitle"] == "Check the forecast before heading out."
        # Other deterministic fields are preserved for provenance/rendering.
        assert out["rain_level"] == "light"

    def test_non_dict_input_still_yields_safe_copy(self):
        out = _renderable_deterministic_brief(_display_settings(), None)
        assert out["headline"] == "Weather update"


class TestArchivedIssue6Response:
    """Exact payload of snapshot 08edf1d91b5b4155ac2dae6186c8dea1 from run
    aa5ea68f: headline "The rain is a morning person" with a 141-char non-ASCII
    subtitle, omitted mood/accent/event_ref, and extra scheduling keys."""

    def _settings(self):
        return _display_settings()

    def test_fixture_is_the_exact_archived_payload(self):
        assert ARCHIVED_RESPONSE["headline"] == "The rain is a morning person"
        assert ARCHIVED_RESPONSE["illustration_prompt"] == (
            "A rain-speckled window looking onto a quiet Tokyo side street in "
            "early autumn; a single clear umbrella crosses wet asphalt; a gingko "
            "tree drops a few yellow leaves; soft grey overcast light; calm and "
            "still; no text."
        )
        assert len(ARCHIVED_RESPONSE["subtitle"]) == 141
        assert ARCHIVED_RESPONSE["subtitle"] == (
            "Damp 19\u00b0C start, 96% chance of drizzle, drying to a mild "
            "22\u00b0C afternoon. Tonight: 10% chance of a sprinkle. Tomorrow: "
            "partly cloudy, 17-24\u00b0C."
        )
        assert ARCHIVED_RESPONSE["date"] == "2026-10-02"
        assert ARCHIVED_RESPONSE["part_of_day"] == "morning"
        assert ARCHIVED_RESPONSE["time_frame"] == (
            "Morning briefing: umbrella for the morning, free hands by lunch."
        )
        assert ARCHIVED_RESPONSE["layout_emphasis"] == {"rain": "high", "temperature": "medium"}
        assert "mood" not in ARCHIVED_RESPONSE
        assert "accent" not in ARCHIVED_RESPONSE
        assert "event_ref" not in ARCHIVED_RESPONSE

    def test_rejected_with_specific_violations(self):
        assert _is_valid_brief(ARCHIVED_RESPONSE) is False
        violations = _brief_violations(ARCHIVED_RESPONSE, self._settings())
        assert len(ARCHIVED_RESPONSE["subtitle"]) == 141
        assert any("subtitle is 141 characters" in v for v in violations)
        assert any("non-ASCII" in v and "subtitle" in v for v in violations)
        for field in ("mood", "accent", "event_ref"):
            assert any(f"missing required field '{field}'" in v for v in violations)
        for extra in ("date", "part_of_day", "time_frame", "layout_emphasis"):
            assert any(f"unexpected field '{extra}'" in v for v in violations)

    def test_invalid_attempts_retry_then_exhaust(self, monkeypatch):
        settings = {
            "openrouter": {"text_model": "provider/model", "brief_temperature": 0.5},
            "context": {"events_mode": "online_model"},
            "pipeline": {"brief_timeout_seconds": 8, "brief_offline_retry_count": 2},
            "display": {"width": 960, "height": 640, "panel_fraction": 0.25},
        }
        calls = []
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief._call_openrouter",
            lambda s, p, model_override=None, **kwargs: calls.append(model_override) or dict(ARCHIVED_RESPONSE),
        )
        events = []
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.record_current_log",
            lambda component, event_type, message, **kwargs: events.append(
                (event_type, kwargs.get("level"), kwargs.get("data", {}))
            ),
        )
        monkeypatch.setattr("scripts.openrouter.generate_brief.record_current_snapshot",
                            lambda *args, **kwargs: None)

        candidate, metadata = _request_brief_with_fallback(settings, "prompt", "sig")

        assert candidate is None
        assert calls == ["provider/model:online", "provider/model", "provider/model"]
        assert metadata["kind"] == "invalid"
        assert any("subtitle is 141 characters" in v for v in metadata["violations"])
        rejected = [entry for entry in events if entry[0] in ("brief_attempt_rejected", "brief_rejected")]
        assert [level for _, level, _ in rejected] == ["warning", "warning", "error"]
        assert rejected[-1][2]["violations"] == metadata["violations"]


# ---------------------------------------------------------------------------
# _render_prompt
# ---------------------------------------------------------------------------

class TestRenderPrompt:
    def _payload(self, ordered_facts=None):
        return {
            "brief_context": {
                "ordered_facts": ordered_facts or [],
            },
            "brief": {
                "headline": "Rain today.",
                "subtitle": "Take an umbrella.",
                "illustration_prompt": "Rain poster.",
            },
        }

    def test_template_appears_first(self):
        result = _render_prompt("MY_TEMPLATE", self._payload())
        assert result.startswith("MY_TEMPLATE")

    def test_ordered_facts_section_present(self):
        facts = [{"id": "x", "source": "yahoo", "text": "Rainy", "value": "Rainy"}]
        result = _render_prompt("TEMPLATE", self._payload(ordered_facts=facts))
        assert "ORDERED_FACTS:" in result
        assert "Rainy" in result

    def test_input_json_section_present(self):
        result = _render_prompt("TEMPLATE", self._payload())
        assert "INPUT_JSON:" in result

    def test_full_payload_serialised(self):
        payload = self._payload()
        result = _render_prompt("TEMPLATE", payload)
        # The whole payload dict should be embedded as JSON
        embedded = json.loads(result.split("INPUT_JSON:\n", 1)[1])
        assert embedded["brief"]["headline"] == "Rain today."

    def test_ordered_facts_not_duplicated_inside_input_json(self):
        facts = [{"id": "x", "source": "yahoo", "text": "Rainy", "value": "Rainy"}]
        payload = self._payload(ordered_facts=facts)
        payload["brief_context"]["conflicts"] = ["temp mismatch"]
        result = _render_prompt("TEMPLATE", payload)
        embedded = json.loads(result.split("INPUT_JSON:\n", 1)[1])
        # ORDERED_FACTS carries the facts; INPUT_JSON keeps the rest.
        assert "ordered_facts" not in embedded["brief_context"]
        assert embedded["brief_context"]["conflicts"] == ["temp mismatch"]
        # And the source payload was not mutated.
        assert payload["brief_context"]["ordered_facts"] == facts

    def test_empty_facts_still_valid_json(self):
        result = _render_prompt("T", self._payload())
        facts_json = result.split("ORDERED_FACTS:\n", 1)[1].split("\n\nINPUT_JSON:")[0]
        assert json.loads(facts_json) == []

    def test_ascii_encoding(self):
        # Non-ASCII in payload must survive round-trip via ensure_ascii=True
        payload = self._payload()
        payload["brief"]["headline"] = "雨が降ります"
        result = _render_prompt("T", payload)
        # Should not contain raw multibyte chars (ensure_ascii encodes them as \\uXXXX)
        assert "雨" not in result
        assert r"\u96e8" in result or "\\u" in result


# ---------------------------------------------------------------------------
# Brief stage wall-clock budget (issue #2)
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self, start=0.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


class TestBriefStageBudget:
    def _valid(self):
        return {
            "headline": "Rain by 3pm",
            "subtitle": "Bring an umbrella.",
            "illustration_prompt": "Rain over a city street.",
            "mood": "stormy",
            "accent": "yellow",
            "event_ref": "",
        }

    def _settings(self, **pipeline):
        base = {
            "brief_timeout_seconds": 8,
            "brief_total_budget_seconds": 12,
            "brief_offline_retry_count": 2,
            "brief_retry_backoff_seconds": 2,
            "brief_retry_backoff_max_seconds": 4,
        }
        base.update(pipeline)
        return {
            "openrouter": {"text_model": "provider/model", "brief_temperature": 0.5},
            "context": {"events_mode": "online_model"},
            "pipeline": base,
            "display": {"width": 960, "height": 640, "panel_fraction": 0.25},
        }

    def _capture_logs(self, monkeypatch):
        events = []
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.record_current_log",
            lambda component, event_type, message, **kwargs: events.append(
                (event_type, kwargs.get("level"), kwargs.get("data", {}))
            ),
        )
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.record_current_snapshot",
            lambda *args, **kwargs: None,
        )
        return events

    def test_dns_failures_back_off_and_respect_total_budget(self, monkeypatch):
        clock = _FakeClock()
        calls = []
        delays = []
        events = self._capture_logs(monkeypatch)

        def fake_call(settings, prompt, model_override=None, **kwargs):
            calls.append(model_override)
            clock.advance(5)
            raise NetworkRequestError("temporary DNS failure", category="dns")

        def fake_sleep(seconds):
            delays.append(seconds)
            clock.advance(seconds)

        monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", fake_call)
        candidate, metadata = _request_brief_with_fallback(
            self._settings(),
            "prompt",
            "sig",
            deadline=WallClockDeadline(12, clock=clock),
            clock=clock,
            sleep_fn=fake_sleep,
        )

        assert candidate is None
        assert calls == ["provider/model:online", "provider/model"]
        # One bounded, positive backoff between attempts; never an immediate retry.
        assert delays == [2.0]
        assert metadata["category"] == "dns"
        assert metadata["budget_exhausted"] is True
        assert metadata["elapsed_seconds"] == 5
        assert clock.now == 12  # never overruns the shared budget
        assert any(event == "brief_retry_backoff" for event, _, _ in events)
        assert any(event == "brief_budget_exhausted" for event, _, _ in events)

    def test_attempt_timeout_is_clamped_to_remaining_budget(self, monkeypatch):
        clock = _FakeClock()
        captured = {}
        self._capture_logs(monkeypatch)

        def fake_call(settings, prompt, model_override=None, timeout=None, deadline=None, **kwargs):
            captured["timeout"] = timeout
            captured["deadline"] = deadline
            return self._valid()

        monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", fake_call)
        candidate, metadata = _request_brief_with_fallback(
            self._settings(brief_total_budget_seconds=3, brief_timeout_seconds=8),
            "prompt",
            "sig",
            clock=clock,
        )

        assert candidate == self._valid()
        assert metadata["kind"] == "accepted"
        assert captured["timeout"] == 3.0
        assert captured["deadline"] == 3.0

    def test_expired_budget_skips_all_attempts(self, monkeypatch):
        clock = _FakeClock()
        calls = []
        events = self._capture_logs(monkeypatch)

        def fake_call(*args, **kwargs):
            calls.append(args)
            return self._valid()

        monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", fake_call)
        deadline = WallClockDeadline(5, clock=clock)
        clock.advance(6)

        candidate, metadata = _request_brief_with_fallback(
            self._settings(), "prompt", "sig", deadline=deadline, clock=clock
        )

        assert candidate is None
        assert calls == []
        assert metadata["category"] == "budget_exhausted"
        assert metadata["budget_exhausted"] is True
        exhausted = [data for event, _, data in events if event == "brief_budget_exhausted"]
        assert len(exhausted) == 1
        assert exhausted[0]["total_elapsed_seconds"] == 6

    def test_read_timeouts_are_categorized_logged_and_backed_off(self, monkeypatch):
        clock = _FakeClock()
        calls = []
        delays = []
        events = self._capture_logs(monkeypatch)

        def fake_call(settings, prompt, model_override=None, **kwargs):
            calls.append(model_override)
            clock.advance(0.2)
            raise NetworkRequestError("The read operation timed out", category="read_timeout")

        def fake_sleep(seconds):
            delays.append(seconds)
            clock.advance(seconds)

        monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", fake_call)
        candidate, metadata = _request_brief_with_fallback(
            self._settings(brief_total_budget_seconds=60),
            "prompt",
            "sig",
            clock=clock,
            sleep_fn=fake_sleep,
        )

        assert candidate is None
        assert calls == ["provider/model:online", "provider/model", "provider/model"]
        assert delays == [2.0, 4.0]  # exponential, capped
        failure_logs = [
            data
            for event, _, data in events
            if event in ("brief_attempt_failed", "brief_request_failed")
        ]
        assert len(failure_logs) == 3
        assert all(data["failure_category"] == "read_timeout" for data in failure_logs)
        assert all(data["attempt_elapsed_seconds"] == 0.2 for data in failure_logs)
        assert failure_logs[0]["will_retry"] is True
        assert failure_logs[-1]["will_retry"] is False

    def test_invalid_briefs_retry_immediately(self, monkeypatch):
        clock = _FakeClock()
        calls = []
        delays = []
        events = self._capture_logs(monkeypatch)

        def fake_call(settings, prompt, model_override=None, **kwargs):
            calls.append(model_override)
            clock.advance(0.1)
            return dict(ARCHIVED_RESPONSE)

        monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", fake_call)
        candidate, metadata = _request_brief_with_fallback(
            self._settings(brief_total_budget_seconds=60),
            "prompt",
            "sig",
            clock=clock,
            sleep_fn=lambda seconds: delays.append(seconds),
        )

        assert candidate is None and metadata["kind"] == "invalid"
        assert calls == ["provider/model:online", "provider/model", "provider/model"]
        # Schema-invalid replies are cheap to re-sample: no backoff delay.
        assert delays == []
        rejected = [
            data for event, _, data in events if event in ("brief_attempt_rejected", "brief_rejected")
        ]
        assert all(data["attempt_elapsed_seconds"] == 0.1 for data in rejected)

    def test_backoff_helpers_are_bounded(self):
        assert _brief_total_budget_seconds({}) == 60.0
        settings = self._settings()
        assert _brief_retry_backoff_seconds(settings, 1) == 2
        assert _brief_retry_backoff_seconds(settings, 2) == 4
        assert _brief_retry_backoff_seconds(settings, 3) == 4  # capped


    def test_per_attempt_timeout_retries_while_stage_budget_remains(self, monkeypatch):
        clock = _FakeClock()
        calls = []
        delays = []
        events = self._capture_logs(monkeypatch)

        def fake_call(settings, prompt, model_override=None, **kwargs):
            calls.append(model_override)
            clock.advance(1.0)
            if len(calls) == 1:
                raise DeadlineExceeded("attempt exceeded its per-attempt timeout", scope="attempt")
            return self._valid()

        def fake_sleep(seconds):
            delays.append(seconds)
            clock.advance(seconds)

        monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", fake_call)
        candidate, metadata = _request_brief_with_fallback(
            self._settings(
                brief_total_budget_seconds=60,
                brief_timeout_seconds=8,
                brief_retry_backoff_seconds=2,
                brief_retry_backoff_max_seconds=4,
            ),
            "prompt",
            "sig",
            clock=clock,
            sleep_fn=fake_sleep,
        )

        assert candidate == self._valid()
        assert metadata["kind"] == "accepted"
        assert calls == ["provider/model:online", "provider/model"]
        assert delays == [2.0]
        failures = [data for event, _, data in events if event == "brief_attempt_failed"]
        assert failures and failures[0]["failure_category"] == "attempt_timeout"
        assert failures[0]["will_retry"] is True
        assert failures[0]["retryable"] is True
        assert failures[0]["budget_exhausted"] is False
        assert all(event != "brief_budget_exhausted" for event, _, _ in events)

    def test_stage_budget_exhaustion_stops_retries_and_returns_fallback(self, monkeypatch):
        clock = _FakeClock()
        calls = []
        delays = []
        events = self._capture_logs(monkeypatch)

        def fake_call(settings, prompt, model_override=None, **kwargs):
            calls.append(model_override)
            clock.advance(10.0)
            raise DeadlineExceeded("the whole stage ran out of time", scope="stage")

        def fake_sleep(seconds):  # pragma: no cover - must never be called
            delays.append(seconds)

        monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", fake_call)
        candidate, metadata = _request_brief_with_fallback(
            self._settings(brief_total_budget_seconds=5, brief_timeout_seconds=8),
            "prompt",
            "sig",
            clock=clock,
            sleep_fn=fake_sleep,
        )

        assert candidate is None
        assert calls == ["provider/model:online"]  # no retries once the budget is gone
        assert delays == []
        assert metadata["category"] == "budget_exhausted"
        assert metadata["budget_exhausted"] is True
        assert metadata["retryable"] is False
        assert any(event == "brief_budget_exhausted" for event, _, _ in events)


class TestCallOpenrouterFailureWrapping:
    def _settings(self):
        return {
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "text_model": "provider/model",
                "brief_temperature": 0.5,
            },
            "pipeline": {"brief_timeout_seconds": 8},
        }

    def test_read_timeout_is_wrapped_with_category(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.get_openrouter_api_key", lambda settings: "test-key"
        )

        def raiser(*args, **kwargs):
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr("scripts.openrouter.generate_brief.fetch_bytes_with_deadline", raiser)
        with pytest.raises(NetworkRequestError) as excinfo:
            _call_openrouter(self._settings(), "prompt")
        assert excinfo.value.category == "read_timeout"
        assert excinfo.value.retryable is True

    def test_stage_deadline_is_wrapped_as_budget_exhaustion(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.get_openrouter_api_key", lambda settings: "test-key"
        )

        def raiser(*args, **kwargs):
            raise DeadlineExceeded("stage too slow", scope="stage")

        monkeypatch.setattr("scripts.openrouter.generate_brief.fetch_bytes_with_deadline", raiser)
        with pytest.raises(NetworkRequestError) as excinfo:
            _call_openrouter(self._settings(), "prompt", deadline=123.0, deadline_scope="stage")
        assert excinfo.value.category == "budget_exhausted"
        assert excinfo.value.retryable is False
        assert "stage budget" in str(excinfo.value)

    def test_attempt_deadline_is_wrapped_as_retryable_timeout(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.get_openrouter_api_key", lambda settings: "test-key"
        )

        def raiser(*args, **kwargs):
            raise DeadlineExceeded("attempt too slow", scope="attempt")

        monkeypatch.setattr("scripts.openrouter.generate_brief.fetch_bytes_with_deadline", raiser)
        with pytest.raises(NetworkRequestError) as excinfo:
            _call_openrouter(
                self._settings(), "prompt", timeout=2.5, deadline=123.0, deadline_scope="attempt"
            )
        assert excinfo.value.category == "attempt_timeout"
        assert excinfo.value.retryable is True
        assert "per-attempt" in str(excinfo.value)

    def test_http_error_body_is_never_read_on_caller_thread(self, monkeypatch):
        """A stalling HTTPError body must not extend the brief deadline."""
        reads = []
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.get_openrouter_api_key", lambda settings: "test-key"
        )

        class StallingBody(io.RawIOBase):
            def readable(self):
                return True

            def read(self, *args, **kwargs):
                reads.append(1)
                time.sleep(2.0)
                return b"late"

        def raiser(*args, **kwargs):
            raise urllib.error.HTTPError(
                url="https://openrouter.ai/api/v1",
                code=502,
                msg="Bad Gateway",
                hdrs=None,
                fp=StallingBody(),
            )

        monkeypatch.setattr("scripts.openrouter.generate_brief.fetch_bytes_with_deadline", raiser)
        start = time.monotonic()
        with pytest.raises(NetworkRequestError) as excinfo:
            _call_openrouter(
                self._settings(), "prompt", timeout=8.0, deadline=time.monotonic() + 1.0
            )
        assert time.monotonic() - start < 1.0
        assert reads == []  # body was never consumed on the caller thread
        assert excinfo.value.category == "http"

    def test_timeout_and_deadline_reach_the_fetch_helper(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.get_openrouter_api_key", lambda settings: "test-key"
        )
        captured = {}

        def fake_fetch(request, *, timeout, settings, deadline=None, **kwargs):
            captured["timeout"] = timeout
            captured["deadline"] = deadline
            return b'{"choices": [{"message": {"content": "{}"}}]}'

        monkeypatch.setattr("scripts.openrouter.generate_brief.fetch_bytes_with_deadline", fake_fetch)
        result = _call_openrouter(self._settings(), "prompt", timeout=2.5, deadline=123.0)
        assert result == {}
        assert captured == {"timeout": 2.5, "deadline": 123.0}


class TestTricklingBriefResponseBudget:
    def test_trickle_cannot_defeat_stage_budget(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

        class TrickleResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                end = time.monotonic() + 2.0
                while time.monotonic() < end:
                    time.sleep(0.01)
                return b"{}"

        monkeypatch.setattr(
            "scripts.openrouter.network.urlopen_with_context", lambda *a, **k: TrickleResponse()
        )
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.record_current_log", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief.record_current_snapshot", lambda *a, **k: None
        )
        settings = {
            "openrouter": {
                "base_url": "https://openrouter.ai/api/v1",
                "text_model": "provider/model",
                "brief_temperature": 0.5,
            },
            "context": {"events_mode": "online_model"},
            "pipeline": {
                "brief_timeout_seconds": 0.4,
                "brief_total_budget_seconds": 0.6,
                "brief_offline_retry_count": 2,
            },
        }
        start = time.monotonic()
        candidate, metadata = _request_brief_with_fallback(settings, "prompt", "sig")
        elapsed = time.monotonic() - start

        assert candidate is None
        assert metadata["budget_exhausted"] is True
        # A per-attempt timeout may be retried, but the shared stage budget then
        # stops further attempts, so the final category is one of these two.
        assert metadata["category"] in ("attempt_timeout", "budget_exhausted")
        assert elapsed < 2.0  # never near the 2s trickle nor unbounded retries
