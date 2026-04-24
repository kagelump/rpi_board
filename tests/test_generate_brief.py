"""Tests for pure helpers in scripts/openrouter/generate_brief.py"""
import json

from scripts.openrouter.generate_brief import _is_valid_brief, _render_prompt


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
