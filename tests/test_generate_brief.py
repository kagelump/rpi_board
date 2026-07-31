"""Tests for pure helpers in scripts/openrouter/generate_brief.py"""
import json

from scripts.openrouter.generate_brief import (
    _brief_model_attempts,
    _enrich_payload,
    _is_valid_brief,
    _load_recent_history,
    _normalize_brief_punct,
    _request_brief_with_fallback,
    _render_prompt,
    _select_text_model,
    _time_frame,
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
        }

    def _valid(self):
        return {
            "headline": "Rain by 3pm",
            "subtitle": "Bring an umbrella.",
            "illustration_prompt": "Rain over a city street.",
        }

    def test_primary_success_does_not_retry(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "scripts.openrouter.generate_brief._call_openrouter",
            lambda settings, prompt, model_override=None: calls.append(model_override) or self._valid(),
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

        def fake_call(settings, prompt, model_override=None):
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

        def always_fail(settings, prompt, model_override=None):
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
        for key in ("voice", "board_context", "recent_history", "creative_angle"):
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
        brief = {
            "headline": "Heavy rain expected today.",
            "subtitle": "Carry an umbrella.",
            "illustration_prompt": "Dark rain clouds over city.",
        }
        assert _is_valid_brief(brief) is True

    def test_extra_keys_allowed(self):
        brief = {
            "headline": "Sunny skies.",
            "subtitle": "Comfortable afternoon.",
            "illustration_prompt": "Bright sun, minimal poster.",
            "bullets": ["No rain", "High 22C"],
        }
        assert _is_valid_brief(brief) is True

    def test_not_a_dict(self):
        assert _is_valid_brief("some string") is False
        assert _is_valid_brief(None) is False
        assert _is_valid_brief(["headline", "subtitle"]) is False

    def test_missing_headline(self):
        brief = {
            "subtitle": "Carry an umbrella.",
            "illustration_prompt": "Rain clouds.",
        }
        assert _is_valid_brief(brief) is False

    def test_missing_subtitle(self):
        brief = {
            "headline": "Rain today.",
            "illustration_prompt": "Rain clouds.",
        }
        assert _is_valid_brief(brief) is False

    def test_missing_illustration_prompt(self):
        brief = {
            "headline": "Rain today.",
            "subtitle": "Carry an umbrella.",
        }
        assert _is_valid_brief(brief) is False

    def test_empty_headline(self):
        brief = {
            "headline": "   ",
            "subtitle": "Carry an umbrella.",
            "illustration_prompt": "Rain clouds.",
        }
        assert _is_valid_brief(brief) is False

    def test_empty_subtitle(self):
        brief = {
            "headline": "Rain today.",
            "subtitle": "",
            "illustration_prompt": "Rain clouds.",
        }
        assert _is_valid_brief(brief) is False

    def test_non_string_headline(self):
        brief = {
            "headline": 42,
            "subtitle": "Carry an umbrella.",
            "illustration_prompt": "Rain clouds.",
        }
        assert _is_valid_brief(brief) is False

    def test_non_string_illustration_prompt(self):
        brief = {
            "headline": "Rain today.",
            "subtitle": "Carry an umbrella.",
            "illustration_prompt": None,
        }
        assert _is_valid_brief(brief) is False

    def test_whitespace_headline_invalid(self):
        brief = {
            "headline": "\t\n",
            "subtitle": "Fine.",
            "illustration_prompt": "Poster.",
        }
        assert _is_valid_brief(brief) is False


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
