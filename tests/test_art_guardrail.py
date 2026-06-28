"""Tests for the art guardrail (text/collage rejection + bounded retry)."""
import scripts.openrouter.art_guardrail as ag
import scripts.openrouter.generate_image as gi


class TestInspectArtFailOpen:
    def test_no_key_skips_and_passes(self, monkeypatch):
        monkeypatch.setattr(ag, "get_openrouter_api_key", lambda settings: None)
        verdict = ag.inspect_art(b"x", {"openrouter": {"text_model": "m"}, "pipeline": {}})
        assert verdict["ok"] is True

    def test_network_error_fails_open(self, monkeypatch):
        monkeypatch.setattr(ag, "get_openrouter_api_key", lambda settings: "k")

        def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr(ag, "urlopen_with_context", boom)
        settings = {"openrouter": {"base_url": "https://x", "image_tool_model": "m"}, "pipeline": {}}
        verdict = ag.inspect_art(b"x", settings)
        assert verdict["ok"] is True
        assert "error" in verdict


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
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: (_ for _ in ()).throw(AssertionError("should not inspect")))
        out = gi._generate_with_guardrail(self._settings(enable_image_guardrail=False), "p", "fal")
        assert out == b"img"
        assert calls["n"] == 1

    def test_passes_first_attempt(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: (calls.__setitem__("n", calls["n"] + 1), b"good")[1])
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True})
        out = gi._generate_with_guardrail(self._settings(), "p", "fal")
        assert out == b"good"
        assert calls["n"] == 1

    def test_retries_then_passes(self, monkeypatch):
        seq = [b"bad", b"good"]
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: seq.pop(0))
        verdicts = [{"ok": False, "has_text": True, "note": "text"}, {"ok": True}]
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: verdicts.pop(0))
        out = gi._generate_with_guardrail(self._settings(image_guardrail_max_retries=1), "p", "fal")
        assert out == b"good"

    def test_exhausts_retries_keeps_last(self, monkeypatch):
        produced = [b"bad1", b"bad2"]
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: produced.pop(0))
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": False, "is_collage": True, "note": "frame"})
        out = gi._generate_with_guardrail(self._settings(image_guardrail_max_retries=1), "p", "fal")
        assert out == b"bad2"  # last attempt kept, never a blank board
