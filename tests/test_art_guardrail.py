"""Tests for the art guardrail (status separation + bounded retry)."""
import io
import json

import pytest

from PIL import Image

import scripts.openrouter.art_guardrail as ag
import scripts.openrouter.generate_image as gi


def _png(rgb, size=(64, 64)):
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mock_guardrail_response(monkeypatch, verdict):
    monkeypatch.setattr(ag, "get_openrouter_api_key", lambda settings: "k")
    payload = {"choices": [{"message": {"content": json.dumps(verdict)}}]}
    monkeypatch.setattr(
        ag, "urlopen_with_context",
        lambda request, **kwargs: _FakeResponse(payload),
    )


class TestInspectArtStatus:
    def test_relaxed_mode_allows_natural_sign_but_audits_mark_check(
            self, monkeypatch):
        requests = []
        monkeypatch.setattr(ag, "get_openrouter_api_key", lambda settings: "k")

        def response(request, **kwargs):
            requests.append(json.loads(request.data))
            # Broad text is ignored in this mode, but the narrow mark verdict
            # must be explicitly false for the image to be accepted.
            return _FakeResponse({"choices": [{"message": {"content": json.dumps({
                "has_text": True,
                "has_watermark_or_signature": False,
                "is_collage": False,
                "note": "natural street sign",
            })}}]})

        monkeypatch.setattr(ag, "urlopen_with_context", response)
        settings = {
            "openrouter": {"base_url": "https://x", "image_tool_model": "m"},
            "pipeline": {"image_guardrail_check_text": False},
        }
        verdict = ag.inspect_art(b"x", settings)

        assert len(requests) == 1
        assert verdict["ok"] is True
        assert verdict["status"] == "accepted"
        assert verdict["has_text"] is None
        assert verdict["text_check_enabled"] is False
        assert verdict["has_watermark_or_signature"] is False
        assert verdict["is_collage"] is False
        prompt = requests[0]["messages"][0]["content"][0]["text"]
        assert '"has_text"' not in prompt
        assert '"has_watermark_or_signature"' in prompt
        assert "Ordinary environmental lettering" in prompt
        assert "Do not reject on broad text alone" in prompt

    @pytest.mark.parametrize("mark_note", [
        "handwritten artist signature",
        "watermark",
        "prominent creator/brand logo",
    ])
    def test_relaxed_mode_rejects_creator_marks(
            self, monkeypatch, mark_note):
        requests = []
        monkeypatch.setattr(ag, "get_openrouter_api_key", lambda settings: "k")

        def response(request, **kwargs):
            requests.append(json.loads(request.data))
            return _FakeResponse({"choices": [{"message": {"content": json.dumps({
                "has_text": False,
                "has_watermark_or_signature": True,
                "is_collage": False,
                "note": mark_note,
            })}}]})

        monkeypatch.setattr(ag, "urlopen_with_context", response)
        settings = {
            "openrouter": {"base_url": "https://x", "image_tool_model": "m"},
            "pipeline": {"image_guardrail_check_text": False},
        }
        verdict = ag.inspect_art(b"x", settings)

        assert len(requests) == 1
        assert verdict["ok"] is False
        assert verdict["status"] == "rejected"
        assert verdict["has_text"] is None
        assert verdict["text_check_enabled"] is False
        assert verdict["has_watermark_or_signature"] is True

    def test_relaxed_mode_rejects_collage_independently(self, monkeypatch):
        _mock_guardrail_response(monkeypatch, {
            "has_watermark_or_signature": False,
            "is_collage": True,
            "note": "framed photo",
        })
        settings = {
            "openrouter": {"base_url": "https://x", "image_tool_model": "m"},
            "pipeline": {"image_guardrail_check_text": False},
        }
        verdict = ag.inspect_art(b"x", settings)
        assert verdict["ok"] is False
        assert verdict["status"] == "rejected"
        assert verdict["has_text"] is None
        assert verdict["has_watermark_or_signature"] is False
        assert verdict["is_collage"] is True

    def test_relaxed_mode_clean_art_is_accepted(self, monkeypatch):
        _mock_guardrail_response(monkeypatch, {
            "has_watermark_or_signature": False,
            "is_collage": False,
            "note": "clean illustration",
        })
        settings = {
            "openrouter": {"base_url": "https://x", "image_tool_model": "m"},
            "pipeline": {"image_guardrail_check_text": False},
        }
        verdict = ag.inspect_art(b"x", settings)
        assert verdict["ok"] is True
        assert verdict["status"] == "accepted"
        assert verdict["has_text"] is None
        assert verdict["text_check_enabled"] is False
        assert verdict["has_watermark_or_signature"] is False

    def test_incomplete_relaxed_verdict_is_unverified(self, monkeypatch):
        _mock_guardrail_response(monkeypatch, {
            "is_collage": False,
            "note": "model omitted mark verdict",
        })
        settings = {
            "openrouter": {"base_url": "https://x", "image_tool_model": "m"},
            "pipeline": {"image_guardrail_check_text": False},
        }
        verdict = ag.inspect_art(b"x", settings)
        assert verdict["ok"] is True
        assert verdict["status"] == "unverified"
        assert verdict["has_watermark_or_signature"] is None
        assert "has_watermark_or_signature" in verdict["error"]

    @pytest.mark.parametrize("model,temperature_supported", [
        ("openai/gpt-6-luna", False), ("openai/gpt-5.2", True)])
    def test_model_request_omits_unsupported_temperature(
            self, monkeypatch, model, temperature_supported):
        monkeypatch.setattr(ag, "get_openrouter_api_key", lambda s: "k")
        requests = []

        def response(request, **kwargs):
            requests.append(json.loads(request.data))
            return _FakeResponse({"choices": [{"message": {"content": json.dumps({
                "has_text": False,
                "has_watermark_or_signature": False,
                "is_collage": False,
                "note": "clean",
            })}}]})

        monkeypatch.setattr(ag, "urlopen_with_context", response)
        settings = {"openrouter": {"base_url": "https://x", "image_tool_model": model}}
        assert ag.inspect_art(_png((0, 0, 0)), settings)["status"] == "accepted"
        assert ("temperature" in requests[0]) is temperature_supported
        assert requests[0]["model"] == model
        assert requests[0]["response_format"] == {"type": "json_object"}

    def test_no_key_marks_unverified(self, monkeypatch):
        monkeypatch.setattr(ag, "get_openrouter_api_key", lambda settings: None)
        verdict = ag.inspect_art(b"x", {"openrouter": {"text_model": "m"}, "pipeline": {}})
        assert verdict["ok"] is True
        assert verdict["status"] == "unverified"

    def test_network_error_marks_unverified(self, monkeypatch):
        monkeypatch.setattr(ag, "get_openrouter_api_key", lambda settings: "k")

        def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(ag, "urlopen_with_context", boom)
        settings = {"openrouter": {"base_url": "https://x", "image_tool_model": "m"}, "pipeline": {}}
        verdict = ag.inspect_art(b"x", settings)
        assert verdict["ok"] is True
        assert verdict["status"] == "unverified"
        assert "error" in verdict

    def test_strict_mode_still_rejects_any_text(self, monkeypatch):
        _mock_guardrail_response(monkeypatch, {
            "has_text": True,
            "has_watermark_or_signature": False,
            "is_collage": False,
            "note": "caption",
        })
        settings = {"openrouter": {"base_url": "https://x", "image_tool_model": "m"}, "pipeline": {}}
        verdict = ag.inspect_art(b"x", settings)
        assert verdict["ok"] is False
        assert verdict["status"] == "rejected"
        assert verdict["has_text"] is True
        assert verdict["text_check_enabled"] is True
        assert verdict["has_watermark_or_signature"] is False
        assert verdict["is_collage"] is False

    def test_strict_mode_rejects_creator_mark_even_without_broad_text(
            self, monkeypatch):
        _mock_guardrail_response(monkeypatch, {
            "has_text": False,
            "has_watermark_or_signature": True,
            "is_collage": False,
            "note": "signature",
        })
        settings = {"openrouter": {"base_url": "https://x", "image_tool_model": "m"}, "pipeline": {}}
        verdict = ag.inspect_art(b"x", settings)
        assert verdict["status"] == "rejected"
        assert verdict["has_watermark_or_signature"] is True

    def test_strict_mode_clean_art_is_accepted(self, monkeypatch):
        _mock_guardrail_response(monkeypatch, {
            "has_text": False,
            "has_watermark_or_signature": False,
            "is_collage": False,
            "note": "clean",
        })
        settings = {"openrouter": {"base_url": "https://x", "image_tool_model": "m"}, "pipeline": {}}
        verdict = ag.inspect_art(b"x", settings)
        assert verdict["ok"] is True
        assert verdict["status"] == "accepted"
        assert verdict["has_text"] is False
        assert verdict["text_check_enabled"] is True
        assert verdict["has_watermark_or_signature"] is False

    def test_strict_mode_legacy_verdict_without_narrow_field_is_accepted(
            self, monkeypatch):
        # Strict mode's established contract is broad text rejection. Keep
        # accepting an older/partial response when that check explicitly passed.
        _mock_guardrail_response(monkeypatch, {
            "has_text": False,
            "is_collage": False,
            "note": "clean",
        })
        settings = {"openrouter": {"base_url": "https://x", "image_tool_model": "m"}, "pipeline": {}}
        verdict = ag.inspect_art(b"x", settings)
        assert verdict["ok"] is True
        assert verdict["status"] == "accepted"
        assert verdict["has_text"] is False
        assert verdict["text_check_enabled"] is True
        assert verdict["has_watermark_or_signature"] is None


class TestGenerateWithGuardrail:
    def _settings(self, **pipeline):
        base = {"enable_image_guardrail": True, "image_guardrail_max_retries": 1}
        base.update(pipeline)
        return {"pipeline": base}

    def test_disabled_calls_once(self, monkeypatch):
        calls = {"n": 0}

        def fake_call(settings, prompt, provider):
            calls["n"] += 1
            return b"img"

        monkeypatch.setattr(gi, "_call_image_api", fake_call)
        monkeypatch.setattr(gi, "inspect_art",
                            lambda b, s: (_ for _ in ()).throw(AssertionError("should not inspect")))
        out = gi._generate_with_guardrail(self._settings(enable_image_guardrail=False), "p", "fal")
        assert out["status"] == "disabled"
        assert out["image_bytes"] == b"img"
        assert calls["n"] == 1

    def test_passes_first_attempt(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(gi, "_call_image_api",
                            lambda s, p, pr: (calls.__setitem__("n", calls["n"] + 1), b"good")[1])
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        out = gi._generate_with_guardrail(self._settings(), "p", "fal")
        assert out["status"] == "accepted"
        assert out["image_bytes"] == b"good"
        assert calls["n"] == 1

    def test_retries_then_passes_with_correction(self, monkeypatch):
        seq = [b"bad", b"good"]
        prompts = []

        def fake_call(s, p, pr):
            prompts.append(p)
            return seq.pop(0)

        monkeypatch.setattr(gi, "_call_image_api", fake_call)
        verdicts = [
            {"ok": False, "status": "rejected", "has_text": True, "note": "text"},
            {"ok": True, "status": "accepted"},
        ]
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: verdicts.pop(0))
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        out = gi._generate_with_guardrail(self._settings(image_guardrail_max_retries=1), "p", "fal")
        assert out["status"] == "accepted"
        assert out["image_bytes"] == b"good"
        # The retry prompt addresses the actual rejection rather than repeating it.
        assert "NO text" in prompts[1]

    def test_retry_correction_covers_collage_then_text(self, monkeypatch):
        seq = [b"bad1", b"bad2", b"good"]
        prompts = []

        def fake_call(s, p, pr):
            prompts.append(p)
            return seq.pop(0)

        monkeypatch.setattr(gi, "_call_image_api", fake_call)
        verdicts = [
            {"ok": False, "status": "rejected", "is_collage": True, "note": "frame"},
            {"ok": False, "status": "rejected", "has_text": True, "note": "text"},
            {"ok": True, "status": "accepted"},
        ]
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: verdicts.pop(0))
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        out = gi._generate_with_guardrail(
            self._settings(image_guardrail_max_retries=2), "p", "fal")
        assert out["status"] == "accepted"
        assert "ONE unified illustration" in prompts[1]
        assert "NO text" in prompts[2]

    def test_retry_correction_covers_creator_mark(self, monkeypatch):
        seq = [b"bad", b"good"]
        prompts = []

        def fake_call(s, p, pr):
            prompts.append(p)
            return seq.pop(0)

        monkeypatch.setattr(gi, "_call_image_api", fake_call)
        verdicts = [
            {
                "ok": False,
                "status": "rejected",
                "has_watermark_or_signature": True,
                "note": "artist signature",
            },
            {"ok": True, "status": "accepted"},
        ]
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: verdicts.pop(0))
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        out = gi._generate_with_guardrail(
            self._settings(image_guardrail_max_retries=1), "p", "fal")

        assert out["status"] == "accepted"
        assert out["image_bytes"] == b"good"
        assert out["attempts"][0]["reasons"] == ["has_watermark_or_signature"]
        assert "Draw NO signature" in prompts[1]
        assert "Ordinary environmental signage" in prompts[1]

    def test_exhausts_retries_returns_rejected(self, monkeypatch):
        produced = [b"bad1", b"bad2"]
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: produced.pop(0))
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {
            "ok": False, "status": "rejected", "is_collage": True, "note": "frame"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        out = gi._generate_with_guardrail(self._settings(image_guardrail_max_retries=1), "p", "fal")
        assert out["status"] == "rejected"
        assert out["image_bytes"] is None
        assert "is_collage" in out["reasons"]
        assert len(out["attempts"]) == 2
        assert all(attempt["status"] == "rejected" for attempt in out["attempts"])

    def test_unavailable_validation_is_unverified_not_rejected(self, monkeypatch):
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"img")
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {
            "ok": True, "status": "unverified", "error": "network down"})
        out = gi._generate_with_guardrail(self._settings(), "p", "fal")
        assert out["status"] == "unverified"
        assert out["image_bytes"] == b"img"
        assert out["reasons"] == []

    def test_legacy_unavailable_verdict_is_unverified(self, monkeypatch):
        # Verdict dicts predating the ``status`` field carry ``skipped``/``error``
        # instead; they must still mean unverified, never accepted.
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"img")
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "skipped": "no key"})
        out = gi._generate_with_guardrail(self._settings(), "p", "fal")
        assert out["status"] == "unverified"
        assert out["image_bytes"] == b"img"

    def test_palette_analysis_error_is_unverified_not_accepted(self, monkeypatch):
        # Issue #9: a palette analyzer exception must not fall through to an
        # accepted, reusable hero even when the vision check passes.
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"img")
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})

        def boom(image):
            raise RuntimeError("analyzer exploded")

        monkeypatch.setattr(gi, "analyze_palette", boom)
        out = gi._generate_with_guardrail(self._settings(), "p", "fal")
        assert out["status"] == "unverified"
        assert out["image_bytes"] == b"img"
        assert out["reasons"] == []
        assert out["attempts"][0]["palette_status"] == "unavailable"
        assert out["attempts"][0]["off_palette_pct"] is None

    def test_off_palette_retry_correction_mentions_palette(self, monkeypatch):
        blue, red = _png((0, 0, 255)), _png((220, 0, 0))
        seq = [blue, red]
        prompts = []

        def fake_call(s, p, pr):
            prompts.append(p)
            return seq.pop(0)

        monkeypatch.setattr(gi, "_call_image_api", fake_call)
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        out = gi._generate_with_guardrail(
            self._settings(image_guardrail_max_retries=1, image_guardrail_max_off_palette_pct=0.15),
            "p", "fal")
        assert out["status"] == "accepted"
        assert all(ink in prompts[1] for ink in ("white", "black", "red", "yellow"))

    def test_off_palette_rejected_then_passes(self, monkeypatch):
        # First image is mostly blue (unrenderable), second is on-palette red.
        blue, red = _png((0, 0, 255)), _png((220, 0, 0))
        seq = [blue, red]
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: seq.pop(0))
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        out = gi._generate_with_guardrail(
            self._settings(image_guardrail_max_retries=1, image_guardrail_max_off_palette_pct=0.15),
            "p", "fal")
        assert out["status"] == "accepted"
        assert out["image_bytes"] == red

    def test_off_palette_within_budget_accepted(self, monkeypatch):
        red = _png((220, 0, 0))
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: red)
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        out = gi._generate_with_guardrail(
            self._settings(image_guardrail_max_off_palette_pct=0.15), "p", "fal")
        assert out["status"] == "accepted"
        assert out["image_bytes"] == red

    @pytest.mark.parametrize("fraction,status", [
        (0.1926, "accepted"), (0.40, "accepted"), (0.4001, "rejected"),
    ])
    def test_shipped_forty_percent_palette_limit(self, monkeypatch, fraction, status):
        settings = gi.load_settings()
        settings["pipeline"]["image_guardrail_max_retries"] = 0
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"art")
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: fraction)
        result = gi._generate_with_guardrail(settings, "prompt", "fal")
        assert result["status"] == status
        assert result["image_bytes"] == (b"art" if status == "accepted" else None)

    def test_exhausted_off_palette_returns_rejected(self, monkeypatch):
        blue = _png((0, 0, 255))
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: blue)
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        out = gi._generate_with_guardrail(
            self._settings(image_guardrail_max_retries=1, image_guardrail_max_off_palette_pct=0.15),
            "p", "fal")
        assert out["status"] == "rejected"
        assert out["image_bytes"] is None
        assert any(reason.startswith("off_palette=") for reason in out["reasons"])
