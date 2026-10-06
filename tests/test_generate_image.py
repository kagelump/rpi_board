"""Tests for image style/palette rotation and guardrail/fallback/reuse policy
(issues #3, #9, and #11)."""
import hashlib
import io
import json
import sys
import urllib.error

from PIL import Image

from scripts.common import ROOT
from scripts.history.store import GenerationStore
from scripts.openrouter import generate_image as gi
from scripts.openrouter.generate_image import (
    ART_STYLE_POOL,
    PALETTE_STRATEGY_POOL,
    _inject_style_prompt,
    _pick_art_style,
    _pick_palette_strategy,
)


def test_style_pool_has_broad_rotation():
    names = [style["name"] for style in ART_STYLE_POOL]
    assert len(names) >= 20
    assert len(names) == len(set(names))


def test_style_rotation_does_not_repeat_before_pool_exhaustion():
    state = {}
    selected = []
    for index in range(len(ART_STYLE_POOL)):
        selected.append(_pick_art_style({}, state, f"day-{index}")["name"])
    assert len(set(selected)) == len(ART_STYLE_POOL)


def test_palette_is_locked_for_day_and_advances_on_new_day():
    state = {}
    first = _pick_palette_strategy(state, new_target_day=True)
    same_day = _pick_palette_strategy(state, new_target_day=False)
    next_day = _pick_palette_strategy(state, new_target_day=True)

    assert same_day == first
    assert next_day["name"] != first["name"]
    assert len(PALETTE_STRATEGY_POOL) >= 6


def test_prompt_injects_subject_style_and_palette():
    template = "SUBJECT={{IMAGE_PROMPT}}\nPALETTE={{PALETTE_GUIDANCE}}\nSTYLE={{STYLE_GUIDANCE}}"
    style = {"name": "Linocut", "prompt": "carved marks"}
    palette = {"name": "Red Signal", "prompt": "red focal accent"}

    prompt = _inject_style_prompt(template, "wind in trees", style, palette)

    assert "wind in trees" in prompt
    assert "Linocut" in prompt and "carved marks" in prompt
    assert "Red Signal" in prompt and "red focal accent" in prompt
    assert "{{" not in prompt


def test_deterministic_fallback_subject_stays_caption_free_downstream():
    """Issue #16: the fallback art subject must not smuggle weather numbers or
    poster wording into the image prompt; the no-text guardrail still applies."""
    from scripts.weather.transform_weather import _deterministic_illustration_prompt

    subject = _deterministic_illustration_prompt(
        {"weather_code": 61, "condition": "Slight rain"}
    )
    assert not any(ch.isdigit() for ch in subject)
    assert "poster" not in subject.lower() and "hint" not in subject.lower()

    template = (
        ROOT / "config" / "prompt_templates" / "weather_image.txt"
    ).read_text(encoding="utf-8")
    prompt = _inject_style_prompt(
        template, subject, ART_STYLE_POOL[0], PALETTE_STRATEGY_POOL[0]
    )

    assert subject in prompt
    assert "Absolutely NO TEXT" in prompt
    assert "hint=" not in prompt


# ---------------------------------------------------------------------------
# Issue #3: rejected artwork must never be published or reused
# ---------------------------------------------------------------------------


def _png_bytes(rgb=(17, 34, 51), size=(32, 32)):
    buf = io.BytesIO()
    Image.new("RGB", size, rgb).save(buf, format="PNG")
    return buf.getvalue()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _main_settings(tmp_path, *, guardrail=True, max_retries=1):
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return {
        "runtime": {
            "brief_file": str(runtime / "last_brief.json"),
            "hero_file": str(runtime / "hero.png"),
            "image_style_state_file": str(runtime / "image_style_state.json"),
            "art_recipe_ledger_file": str(runtime / "art_recipe_ledger.jsonl"),
        },
        "pipeline": {
            "enable_openrouter_image": True,
            "enable_image_guardrail": guardrail,
            "image_guardrail_max_retries": max_retries,
            "image_provider": "fal",
        },
        "openrouter": {"base_url": "https://x", "text_model": "m", "image_tool_model": "m"},
    }


def _write_brief(settings, *, brief_source="openrouter", daypart_role="primary",
                 target_date="2026-10-01", prompt="wind over rooftops"):
    payload = {
        "brief_source": brief_source,
        "brief": {"illustration_prompt": prompt},
        "day_context": {"target_date_iso": target_date, "daypart_role": daypart_role},
    }
    with open(settings["runtime"]["brief_file"], "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _seed_style_state(settings, *, status, target_date="2026-10-01", hero_bytes=None,
                      style="Bauhaus", palette="Red Signal", sha=None, prompt="wind over rooftops"):
    state = {
        "last_selected": style,
        "target_date": target_date,
        "hero_prompt": prompt,
        "last_palette": palette,
        "hero_validation": {
            "status": status,
            "target_date": target_date,
            "style": style,
            "palette": palette,
            "provider": "fal",
            "image_sha256": sha if sha is not None else (_sha(hero_bytes) if hero_bytes else None),
            "image_byte_size": len(hero_bytes) if hero_bytes else None,
            "reasons": [],
        },
    }
    with open(settings["runtime"]["image_style_state_file"], "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def _run_main(monkeypatch, settings, logs):
    monkeypatch.setattr(gi, "load_settings", lambda: settings)
    monkeypatch.setattr(
        gi, "record_current_log",
        lambda component, event_type, message=None, *, level="info", data=None: logs.append({
            "component": component, "event_type": event_type, "message": message,
            "level": level, "data": data or {},
        }),
    )
    monkeypatch.setattr(gi, "record_current_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["generate_image.py"])
    gi.main()


def _hero_path(settings):
    from pathlib import Path
    return Path(settings["runtime"]["hero_file"])


def _state_path(settings):
    from pathlib import Path
    return Path(settings["runtime"]["image_style_state_file"])


class TestHeroReuseEligibility:
    def _record(self, status, hero_bytes, sha=None, target_date="2026-10-01"):
        record = {
            "status": status,
            "target_date": target_date,
            "image_sha256": sha if sha is not None else _sha(hero_bytes),
        }
        return {"hero_validation": record}

    def test_accepted_with_matching_hash_is_eligible(self, tmp_path):
        hero = tmp_path / "hero.png"
        data = _png_bytes()
        hero.write_bytes(data)
        assert gi._hero_reuse_eligible(self._record("accepted", data), "2026-10-01", hero) is True

    def test_rejected_is_not_eligible(self, tmp_path):
        hero = tmp_path / "hero.png"
        data = _png_bytes()
        hero.write_bytes(data)
        record = self._record("rejected", data)
        for guardrail_enabled in (True, False):
            assert gi._hero_reuse_eligible(
                record, "2026-10-01", hero,
                guardrail_enabled=guardrail_enabled) is False

    def test_unverified_is_not_eligible(self, tmp_path):
        hero = tmp_path / "hero.png"
        data = _png_bytes()
        hero.write_bytes(data)
        record = self._record("unverified", data)
        for guardrail_enabled in (True, False):
            assert gi._hero_reuse_eligible(
                record, "2026-10-01", hero,
                guardrail_enabled=guardrail_enabled) is False

    def test_legacy_state_without_validation_is_not_eligible(self, tmp_path):
        hero = tmp_path / "hero.png"
        hero.write_bytes(_png_bytes())
        for guardrail_enabled in (True, False):
            assert gi._hero_reuse_eligible(
                {"hero_prompt": "old"}, "2026-10-01", hero,
                guardrail_enabled=guardrail_enabled) is False

    def test_hash_mismatch_is_not_eligible(self, tmp_path):
        hero = tmp_path / "hero.png"
        hero.write_bytes(_png_bytes())
        record = self._record("accepted", b"different", sha=_sha(b"different"))
        assert gi._hero_reuse_eligible(record, "2026-10-01", hero) is False

    def test_stale_target_date_is_not_eligible(self, tmp_path):
        hero = tmp_path / "hero.png"
        data = _png_bytes()
        hero.write_bytes(data)
        record = self._record("accepted", data, target_date="2026-09-30")
        assert gi._hero_reuse_eligible(record, "2026-10-01", hero) is False

    def test_disabled_is_eligible_while_guardrail_stays_disabled(self, tmp_path):
        # Issue #11: a disabled verdict never passed validation, but while the
        # guardrail is still off the unchanged art may be reused instead of
        # paying for a fresh generation on every cached refresh.
        hero = tmp_path / "hero.png"
        data = _png_bytes()
        hero.write_bytes(data)
        assert gi._hero_reuse_eligible(
            self._record("disabled", data), "2026-10-01", hero,
            guardrail_enabled=False) is True

    def test_disabled_is_not_eligible_once_guardrail_is_enabled(self, tmp_path):
        # Issue #11: turning validation back on invalidates unvalidated art so
        # it is regenerated and actually validated before it can be trusted.
        hero = tmp_path / "hero.png"
        data = _png_bytes()
        hero.write_bytes(data)
        assert gi._hero_reuse_eligible(
            self._record("disabled", data), "2026-10-01", hero,
            guardrail_enabled=True) is False

    def test_disabled_with_stale_date_or_hash_is_not_eligible(self, tmp_path):
        hero = tmp_path / "hero.png"
        data = _png_bytes()
        hero.write_bytes(data)
        stale = self._record("disabled", data, target_date="2026-09-30")
        assert gi._hero_reuse_eligible(
            stale, "2026-10-01", hero, guardrail_enabled=False) is False
        mismatched = self._record("disabled", b"other", sha=_sha(b"other"))
        assert gi._hero_reuse_eligible(
            mismatched, "2026-10-01", hero, guardrail_enabled=False) is False


class TestDisabledGuardrailMain:
    def test_disabled_guardrail_publishes_image_and_records_disabled(
            self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path, guardrail=False)
        _write_brief(settings)
        image_bytes = _png_bytes((10, 20, 30))
        calls = {"n": 0}

        def fake_call(s, p, pr):
            calls["n"] += 1
            return image_bytes

        monkeypatch.setattr(gi, "_call_image_api", fake_call)
        monkeypatch.setattr(
            gi, "inspect_art",
            lambda b, s: (_ for _ in ()).throw(
                AssertionError("disabled guardrail must not inspect")))
        logs = []
        _run_main(monkeypatch, settings, logs)

        # The configured behaviour for the current generation is retained: the
        # unchecked image is still written, but its provenance says "disabled".
        assert calls["n"] == 1
        assert _hero_path(settings).read_bytes() == image_bytes
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        assert state["hero_validation"]["status"] == "disabled"
        generated = [entry for entry in logs if entry["event_type"] == "image_generated"]
        assert generated and generated[0]["level"] == "info"
        assert generated[0]["data"]["validation_status"] == "disabled"
        assert "disabled" in generated[0]["message"]

    def test_cached_brief_reuses_disabled_hero_while_guardrail_disabled(
            self, tmp_path, monkeypatch):
        """Issue #11: while the guardrail is off, a same-date, hash-matched
        ``disabled`` hero is reused on a cached refresh instead of paying to
        regenerate unchanged art."""
        settings = _main_settings(tmp_path, guardrail=False)
        _write_brief(settings, brief_source="cached")
        disabled_bytes = _png_bytes((11, 22, 33))
        _hero_path(settings).write_bytes(disabled_bytes)
        _seed_style_state(settings, status="disabled", hero_bytes=disabled_bytes)

        def fake_call(s, p, pr):
            raise AssertionError("reusable disabled hero must not regenerate")

        monkeypatch.setattr(gi, "_call_image_api", fake_call)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert _hero_path(settings).read_bytes() == disabled_bytes
        assert any(entry["event_type"] == "hero_reused" for entry in logs)
        assert not any(entry["event_type"] == "hero_reuse_rejected" for entry in logs)
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        assert state["hero_validation"]["status"] == "disabled"

    def test_enabling_guardrail_regenerates_prior_disabled_hero(
            self, tmp_path, monkeypatch):
        """Issue #11: a ``disabled`` hero is not reusable once the guardrail is
        enabled, so it must be regenerated and actually validated."""
        settings = _main_settings(tmp_path, guardrail=True)
        _write_brief(settings, brief_source="cached")
        disabled_bytes = _png_bytes((11, 22, 33))
        _hero_path(settings).write_bytes(disabled_bytes)
        _seed_style_state(settings, status="disabled", hero_bytes=disabled_bytes)

        validated_bytes = _png_bytes((44, 55, 66))
        calls = {"n": 0}

        def fake_call(s, p, pr):
            calls["n"] += 1
            return validated_bytes

        monkeypatch.setattr(gi, "_call_image_api", fake_call)
        monkeypatch.setattr(gi, "inspect_art",
                            lambda b, s: {"ok": True, "status": "accepted"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert calls["n"] == 1
        assert _hero_path(settings).read_bytes() == validated_bytes
        assert any(entry["event_type"] == "hero_reuse_rejected" for entry in logs)
        assert not any(entry["event_type"] == "hero_reused" for entry in logs)
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        assert state["hero_validation"]["status"] == "accepted"


class TestExhaustedRejectionMain:
    def test_exhaustion_never_writes_rejected_candidate_and_uses_pictogram(
            self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings)
        produced = [b"rejected-one", b"rejected-two"]
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: produced.pop(0))
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {
            "ok": False, "status": "rejected", "has_text": True, "note": "caption"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert not _hero_path(settings).exists()
        exhausted = [entry for entry in logs if entry["event_type"] == "image_guardrail_exhausted"]
        assert exhausted and exhausted[0]["level"] == "error"
        assert "has_text" in exhausted[0]["data"]["reasons"]
        assert exhausted[0]["data"]["fallback"] == "deterministic_pictogram"
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        assert state["hero_validation"]["status"] == "rejected"

    def test_exhaustion_keeps_prior_accepted_hero(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings)
        prior = _png_bytes((9, 9, 9))
        _hero_path(settings).write_bytes(prior)
        _seed_style_state(settings, status="accepted", hero_bytes=prior)

        produced = [b"rejected-one", b"rejected-two"]
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: produced.pop(0))
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {
            "ok": False, "status": "rejected", "is_collage": True, "note": "frame"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert _hero_path(settings).read_bytes() == prior
        exhausted = [entry for entry in logs if entry["event_type"] == "image_guardrail_exhausted"]
        assert exhausted and exhausted[0]["data"]["fallback"] == "previous_accepted_hero"

    def test_exhaustion_does_not_keep_unvalidated_prior_hero(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings)
        prior = _png_bytes((4, 4, 4))
        _hero_path(settings).write_bytes(prior)
        # Legacy state: an on-disk hero with no recorded validation acceptance.
        _state_path(settings).write_text(json.dumps({"hero_prompt": "old"}), encoding="utf-8")

        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"rejected")
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {
            "ok": False, "status": "rejected", "has_text": True, "note": "caption"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert not _hero_path(settings).exists()
        exhausted = [entry for entry in logs if entry["event_type"] == "image_guardrail_exhausted"]
        assert exhausted and exhausted[0]["data"]["fallback"] == "deterministic_pictogram"


class TestAcceptedAndReuseMain:
    def test_accepted_retry_writes_hero_and_records_acceptance(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings)
        seq = [b"bad", b"good"]
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: seq.pop(0))
        verdicts = [
            {"ok": False, "status": "rejected", "has_text": True, "note": "caption"},
            {"ok": True, "status": "accepted"},
        ]
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: verdicts.pop(0))
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert _hero_path(settings).read_bytes() == b"good"
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        assert state["hero_validation"]["status"] == "accepted"
        assert state["hero_validation"]["image_sha256"] == _sha(b"good")
        assert gi._hero_reuse_eligible(state, "2026-10-01", _hero_path(settings)) is True

    def test_cached_brief_reuses_only_accepted_hero(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings, brief_source="cached")
        hero_bytes = _png_bytes((3, 3, 3))
        _hero_path(settings).write_bytes(hero_bytes)
        _seed_style_state(settings, status="accepted", hero_bytes=hero_bytes)

        calls = []
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: calls.append(1) or b"new")
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert calls == []
        assert _hero_path(settings).read_bytes() == hero_bytes
        assert any(entry["event_type"] == "hero_reused" for entry in logs)

    def test_cached_brief_regenerates_rejected_hero(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings, brief_source="cached")
        rejected_bytes = _png_bytes((8, 8, 8))
        _hero_path(settings).write_bytes(rejected_bytes)
        _seed_style_state(settings, status="rejected", hero_bytes=rejected_bytes)

        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"accepted")
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert _hero_path(settings).read_bytes() == b"accepted"
        assert any(entry["event_type"] == "hero_reuse_rejected" for entry in logs)
        assert not any(entry["event_type"] == "hero_reused" for entry in logs)

    def test_accepted_hero_is_reused_on_next_cached_run(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings, brief_source="openrouter")
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"good")
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        first_logs = []
        _run_main(monkeypatch, settings, first_logs)
        assert _hero_path(settings).read_bytes() == b"good"

        # Next refresh with an unchanged (cached) brief keeps the accepted hero.
        _write_brief(settings, brief_source="cached")
        calls = []
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: calls.append(1) or b"new")
        second_logs = []
        _run_main(monkeypatch, settings, second_logs)
        assert calls == []
        assert _hero_path(settings).read_bytes() == b"good"
        assert any(entry["event_type"] == "hero_reused" for entry in second_logs)

    def test_cached_brief_regenerates_unverified_hero(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings, brief_source="cached")
        unverified = _png_bytes((6, 6, 6))
        _hero_path(settings).write_bytes(unverified)
        _seed_style_state(settings, status="unverified", hero_bytes=unverified)

        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"accepted")
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert _hero_path(settings).read_bytes() == b"accepted"
        assert any(entry["event_type"] == "hero_reuse_rejected" for entry in logs)


class TestApiFailureFallbackMain:
    def test_api_failure_keeps_prior_accepted_hero(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings)
        prior = _png_bytes((7, 7, 7))
        _hero_path(settings).write_bytes(prior)
        _seed_style_state(settings, status="accepted", hero_bytes=prior)

        def boom(s, p, pr):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(gi, "_call_image_api", boom)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert _hero_path(settings).read_bytes() == prior
        failures = [entry for entry in logs if entry["event_type"] == "image_generation_failed"]
        assert failures and failures[0]["level"] == "error"

    def test_api_failure_drops_unvalidated_hero(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings)
        _hero_path(settings).write_bytes(_png_bytes((7, 7, 7)))
        # Legacy state: no recorded validation acceptance for the on-disk hero.
        _state_path(settings).write_text(json.dumps({"hero_prompt": "old"}), encoding="utf-8")

        def boom(s, p, pr):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(gi, "_call_image_api", boom)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert not _hero_path(settings).exists()
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        assert state["hero_validation"]["status"] == "unverified"


class TestUnavailableValidationMain:
    def test_unavailable_validation_publishes_unverified_hero(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path)
        _write_brief(settings)
        image_bytes = _png_bytes((5, 5, 5))
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: image_bytes)
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {
            "ok": True, "status": "unverified", "error": "network down"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        logs = []
        _run_main(monkeypatch, settings, logs)

        # Fail-open: the image still publishes, but it is recorded as unverified.
        assert _hero_path(settings).read_bytes() == image_bytes
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        assert state["hero_validation"]["status"] == "unverified"
        generated = [entry for entry in logs if entry["event_type"] == "image_generated"]
        assert generated and generated[0]["level"] == "warning"
        assert not [entry for entry in logs if entry["event_type"] == "image_guardrail_exhausted"]


class TestPaletteUnavailableMain:
    def test_palette_analysis_error_publishes_unverified_and_is_not_reused(
            self, tmp_path, monkeypatch):
        """Issue #9: when ``analyze_palette()`` raises while vision passes, the
        candidate still renders (fail-open) but is recorded unverified and can
        never be reused as accepted art on a later cached refresh."""
        settings = _main_settings(tmp_path)
        _write_brief(settings)
        image_bytes = _png_bytes((12, 34, 56))
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: image_bytes)
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})

        def boom(image):
            raise RuntimeError("palette analyzer unavailable")

        monkeypatch.setattr(gi, "analyze_palette", boom)
        logs = []
        _run_main(monkeypatch, settings, logs)

        # Fail-open: the candidate is still written, but never as accepted.
        assert _hero_path(settings).read_bytes() == image_bytes
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        assert state["hero_validation"]["status"] == "unverified"
        assert state["hero_validation"]["status"] != "accepted"
        assert gi._hero_reuse_eligible(state, "2026-10-01", _hero_path(settings)) is False
        generated = [entry for entry in logs if entry["event_type"] == "image_generated"]
        assert generated and generated[0]["level"] == "warning"
        assert generated[0]["data"]["validation_status"] == "unverified"
        assert not [entry for entry in logs if entry["event_type"] == "image_guardrail_exhausted"]

        # A cached refresh must regenerate rather than reuse the unverified art.
        _write_brief(settings, brief_source="cached")
        calls = []
        monkeypatch.setattr(
            gi, "_call_image_api",
            lambda s, p, pr: calls.append(1) or b"regenerated")
        monkeypatch.setattr(gi, "analyze_palette", lambda b: {"off_palette_pct": 0.0})
        second_logs = []
        _run_main(monkeypatch, settings, second_logs)

        assert calls == [1]
        assert any(entry["event_type"] == "hero_reuse_rejected" for entry in second_logs)
        assert not any(entry["event_type"] == "hero_reused" for entry in second_logs)
        regenerated_state = json.loads(
            _state_path(settings).read_text(encoding="utf-8"))
        assert regenerated_state["hero_validation"]["status"] == "accepted"


class TestRunStatusDegraded:
    def test_exhaustion_marks_generation_run_degraded_with_reasons(
            self, tmp_path, monkeypatch):
        store = GenerationStore(
            record_file=tmp_path / "history.jsonl",
            artifacts_dir=tmp_path / "artifacts",
        )
        run_id = store.start_run()

        settings = _main_settings(tmp_path)
        _write_brief(settings)
        monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"rejected")
        monkeypatch.setattr(gi, "inspect_art", lambda b, s: {
            "ok": False, "status": "rejected", "has_text": True, "note": "caption"})
        monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
        monkeypatch.setattr(gi, "load_settings", lambda: settings)
        monkeypatch.setattr(gi, "record_current_snapshot", lambda *a, **k: None)
        monkeypatch.setattr(
            gi, "record_current_log",
            lambda component, event_type, message=None, *, level="info", data=None: store.log(
                run_id, component=component, event_type=event_type, message=message,
                level=level, data=data),
        )
        monkeypatch.setattr(sys, "argv", ["generate_image.py"])
        gi.main()

        store.finish_run(run_id, status="succeeded")
        run = store.get_run(run_id)
        assert run["status"] == "degraded"
        errors = [log for log in run["logs"] if log["level"] == "error"]
        assert errors
        assert "has_text" in errors[0]["data"]["reasons"]


# ---------------------------------------------------------------------------
# Issue #18: shared artwork recipe selection before image generation
# ---------------------------------------------------------------------------


def test_art_recipe_selected_locked_and_injected_into_prompt(monkeypatch, tmp_path):
    from scripts.openrouter import art_recipes

    settings = _main_settings(tmp_path)
    _write_brief(settings, target_date="2026-10-05")
    captured = {}

    def fake_call(_settings, prompt, _provider):
        captured["prompt"] = prompt
        return b"artwork"

    monkeypatch.setattr(gi, "_call_image_api", fake_call)
    monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
    monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)

    logs = []
    _run_main(monkeypatch, settings, logs)
    state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    recipe = state["art_recipe"]
    assert recipe["status"] == "selected"
    assert recipe["tags"]["subject"]
    assert "Artwork recipe for this forecast day" in captured["prompt"]
    assert recipe["rationale"]
    selected_events = [log for log in logs if log["event_type"] == "art_recipe_selected"]
    assert len(selected_events) == 1

    # A second refresh on the same forecast day keeps the locked recipe.
    second = {}

    def fake_call_two(_settings, prompt, _provider):
        second["prompt"] = prompt
        return b"artwork2"

    monkeypatch.setattr(gi, "_call_image_api", fake_call_two)
    _run_main(monkeypatch, settings, logs)
    state_again = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    assert state_again["art_recipe"]["tags"] == recipe["tags"]
    locked_events = [log for log in logs if log["event_type"] == "art_recipe_locked"]
    assert len(locked_events) >= 1


def test_art_recipe_avoids_recent_published_walker(monkeypatch, tmp_path):
    from scripts.openrouter import art_recipes

    settings = _main_settings(tmp_path)
    art_recipes.record_publication(
        settings,
        recipe={
            "subject": "lone_walker",
            "motifs": ["small red bag"],
            "setting": "residential_lane",
            "viewpoint": "eye_level",
            "composition": "cropped_edge",
        },
        artwork_sha256="a" * 64,
        target_date="2026-10-04",
        mode="pi_display",
    )
    _write_brief(settings, target_date="2026-10-05")

    monkeypatch.setattr(gi, "_call_image_api", lambda s, p, pr: b"artwork")
    monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
    monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)

    _run_main(monkeypatch, settings, [])
    state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    tags = state["art_recipe"]["tags"]
    assert not (tags["subject"] == "lone_walker" and "small red bag" in tags["motifs"])


# ---------------------------------------------------------------------------
# Issue #20: recipe staging must not contradict the accepted dry brief
# ---------------------------------------------------------------------------


ARCHIVED_DRY_FIXTURE = (
    ROOT / "tests" / "fixtures" / "issue20_archived_dry_oct6_brief.json"
)


def _archived_dry_payload():
    return json.loads(ARCHIVED_DRY_FIXTURE.read_text(encoding="utf-8"))


def _umbrella_recipe():
    return {
        "status": "selected",
        "rationale": "novelty test",
        "forecast_weather": "dry",
        "recipe_weather": "wet",
        "tags": {
            "subject": "umbrella_crowd",
            "subject_text": "a small cluster of commuters under umbrellas",
            "motif": "umbrellas",
            "motifs": ["umbrellas"],
            "setting": "rooftop_terrace",
            "setting_text": "a rooftop terrace",
            "viewpoint": "close_crop",
            "viewpoint_text": "tight crop on the main motif",
            "composition": "upward",
            "composition_text": "upward view through the scene toward the sky",
        },
    }


def _neutral_recipe():
    return {
        "status": "selected",
        "tags": {
            "subject": "school_children",
            "subject_text": "children hurrying through a school gate",
            "motif": "backpacks",
            "motifs": ["backpacks"],
            "setting": "school_gate",
            "setting_text": "a school gate",
            "viewpoint": "eye_level",
            "viewpoint_text": "eye-level medium shot",
            "composition": "emblem",
            "composition_text": "near-symmetrical emblem with one deliberate disruption",
        },
    }


def test_inject_style_prompt_suppresses_conflicting_recipe_on_dry_brief():
    payload = _archived_dry_payload()
    template = (
        ROOT / "config" / "prompt_templates" / "weather_image.txt"
    ).read_text(encoding="utf-8")

    prompt = _inject_style_prompt(
        template, payload["brief"]["illustration_prompt"],
        ART_STYLE_POOL[0], PALETTE_STRATEGY_POOL[0],
        recipe=_umbrella_recipe(), forecast=payload,
    )

    lowered = prompt.lower()
    assert "artwork recipe" not in lowered
    assert "umbrella" not in lowered
    assert "ginkgo" in lowered  # the accepted brief's SUBJECT still drives the art


def test_inject_style_prompt_keeps_compatible_recipe_on_dry_brief():
    payload = _archived_dry_payload()
    template = (
        ROOT / "config" / "prompt_templates" / "weather_image.txt"
    ).read_text(encoding="utf-8")

    prompt = _inject_style_prompt(
        template, payload["brief"]["illustration_prompt"],
        ART_STYLE_POOL[0], PALETTE_STRATEGY_POOL[0],
        recipe=_neutral_recipe(), forecast=payload,
    )

    assert "Artwork recipe for this forecast day" in prompt
    assert "backpacks" in prompt


def test_main_suppresses_locked_conflicting_recipe_and_logs_decision(monkeypatch, tmp_path):
    settings = _main_settings(tmp_path)
    payload = _archived_dry_payload()
    with open(settings["runtime"]["brief_file"], "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    # A recipe locked from an earlier refresh of the same target day can predate
    # the weather filter, so the prompt path must still refuse contradictory art.
    state = {
        "target_date": "2026-10-06",
        "art_recipe_target_date": "2026-10-06",
        "art_recipe": _umbrella_recipe(),
        "last_selected": ART_STYLE_POOL[0]["name"],
        "last_palette": PALETTE_STRATEGY_POOL[0]["name"],
    }
    _state_path(settings).write_text(json.dumps(state), encoding="utf-8")

    captured = {}

    def fake_call(_settings, prompt, _provider):
        captured["prompt"] = prompt
        return b"artwork"

    monkeypatch.setattr(gi, "_call_image_api", fake_call)
    monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
    monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)

    logs = []
    _run_main(monkeypatch, settings, logs)

    assert "umbrella" not in captured["prompt"].lower()
    assert "ginkgo" in captured["prompt"].lower()
    suppressed = [log for log in logs if log["event_type"] == "art_recipe_suppressed"]
    assert len(suppressed) == 1
    assert suppressed[0]["data"]["recipe_weather"] == "wet"
    assert suppressed[0]["data"]["forecast_weather"] == "dry"
    # The audited selected recipe is still recorded in state.
    state_after = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    assert state_after["art_recipe"]["tags"]["subject"] == "umbrella_crowd"


# ---------------------------------------------------------------------------
# Issue #21: a suppressed recipe is audit-only and never enters the ledger
# ---------------------------------------------------------------------------


class _RecipeHistoryLog:
    def __init__(self):
        self.entries = []

    def log(self, *args, **kwargs):
        self.entries.append(kwargs)


def _issue21_settings(tmp_path):
    settings = _main_settings(tmp_path)
    settings["display"] = {"mode": "pi_display"}
    settings["art_variety"] = {
        "lookback_days": 14,
        "motif_cooldown_days": 7,
        "min_dimension_differences": 2,
    }
    return settings


def _write_archived_dry_brief(settings):
    payload = _archived_dry_payload()
    with open(settings["runtime"]["brief_file"], "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return payload


def _lock_recipe(settings, recipe, target_date="2026-10-06"):
    state = {
        "target_date": target_date,
        "art_recipe_target_date": target_date,
        "art_recipe": recipe,
        "last_selected": ART_STYLE_POOL[0]["name"],
        "last_palette": PALETTE_STRATEGY_POOL[0]["name"],
    }
    _state_path(settings).write_text(json.dumps(state), encoding="utf-8")
    return state


def _accept_generation(monkeypatch, capture=None):
    def fake_call(_settings, prompt, _provider):
        if capture is not None:
            capture["prompt"] = prompt
        return b"artwork"

    monkeypatch.setattr(gi, "_call_image_api", fake_call)
    monkeypatch.setattr(gi, "inspect_art", lambda b, s: {"ok": True, "status": "accepted"})
    monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)


def test_suppressed_recipe_is_not_booked_as_published(monkeypatch, tmp_path):
    from scripts.history import record as record_mod
    from scripts.openrouter import art_recipes as ar

    settings = _issue21_settings(tmp_path)
    _write_archived_dry_brief(settings)
    _lock_recipe(settings, _umbrella_recipe())

    captured = {}
    _accept_generation(monkeypatch, captured)
    logs = []
    _run_main(monkeypatch, settings, logs)

    # #20 behaviour is preserved: the contradictory staging is out of the prompt.
    assert "umbrella" not in captured["prompt"].lower()
    assert "ginkgo" in captured["prompt"].lower()
    assert [log["event_type"] for log in logs].count("art_recipe_suppressed") == 1

    state_after = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    # The selected recipe stays in state for audit...
    assert state_after["art_recipe"]["tags"]["subject"] == "umbrella_crowd"
    # ...but its usage marker says it never guided the hero.
    assert state_after["art_recipe_used"] is None
    assert state_after["art_recipe_suppressed"] is True

    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    monkeypatch.setenv("GENERATION_RUN_ID", "run-issue21-suppressed")
    store = _RecipeHistoryLog()
    result = record_mod._record_published_recipe(store, mode="pi_display")

    assert result["status"] == "skipped_suppressed"
    assert ar.published_recipes(settings) == []
    # No phantom entry means no novelty/cooldown input and no API summary count.
    assert ar.recent_published_recipes(settings) == []
    assert ar.history_summary(settings)["count"] == 0
    assert any(
        entry.get("event_type") == "recipe_skipped_suppressed"
        for entry in store.entries
    )


def test_compatible_recipe_is_booked_after_panel_delivery(monkeypatch, tmp_path):
    from scripts.history import record as record_mod
    from scripts.openrouter import art_recipes as ar

    settings = _issue21_settings(tmp_path)
    _write_archived_dry_brief(settings)
    _lock_recipe(settings, _neutral_recipe())

    captured = {}
    _accept_generation(monkeypatch, captured)
    _run_main(monkeypatch, settings, [])

    assert "backpacks" in captured["prompt"]
    state_after = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    assert state_after["art_recipe_used"]["tags"]["subject"] == "school_children"
    assert state_after["art_recipe_suppressed"] is False

    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    monkeypatch.setenv("GENERATION_RUN_ID", "run-issue21-used")
    result = record_mod._record_published_recipe(_RecipeHistoryLog(), mode="pi_display")

    assert result["status"] == "published"
    entries = ar.published_recipes(settings)
    assert len(entries) == 1
    assert entries[0]["tags"]["subject"] == "school children"


def test_recipe_usage_marker_survives_hero_reuse(monkeypatch, tmp_path):
    from scripts.history import record as record_mod
    from scripts.openrouter import art_recipes as ar

    from pathlib import Path

    settings = _issue21_settings(tmp_path)
    _write_archived_dry_brief(settings)
    _lock_recipe(settings, _neutral_recipe())

    _accept_generation(monkeypatch)
    _run_main(monkeypatch, settings, [])
    first_used = json.loads(_state_path(settings).read_text(encoding="utf-8"))["art_recipe_used"]
    assert first_used["tags"]["subject"] == "school_children"

    payload = _archived_dry_payload()
    payload["brief_source"] = "cached"
    Path(settings["runtime"]["brief_file"]).write_text(json.dumps(payload), encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise AssertionError("reused hero must not call the image provider")

    monkeypatch.setattr(gi, "_call_image_api", boom)
    logs = []
    _run_main(monkeypatch, settings, logs)
    assert any(log["event_type"] == "hero_reused" for log in logs)

    state_after = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    assert state_after["art_recipe_used"] == first_used
    assert state_after["art_recipe_suppressed"] is False

    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    monkeypatch.setenv("GENERATION_RUN_ID", "run-issue21-reuse")
    result = record_mod._record_published_recipe(_RecipeHistoryLog(), mode="pi_display")
    assert result["status"] == "published"
    assert len(ar.published_recipes(settings)) == 1


def test_same_day_locked_recipe_keeps_usage_marker(monkeypatch, tmp_path):
    settings = _issue21_settings(tmp_path)
    _write_archived_dry_brief(settings)
    _lock_recipe(settings, _neutral_recipe())

    _accept_generation(monkeypatch)
    _run_main(monkeypatch, settings, [])

    # A later non-cached refresh of the same forecast day regenerates the hero
    # but keeps the locked recipe (no new selection).
    logs = []
    _accept_generation(monkeypatch)
    _run_main(monkeypatch, settings, logs)

    assert any(log["event_type"] == "art_recipe_locked" for log in logs)
    assert not any(log["event_type"] == "art_recipe_selected" for log in logs)
    state_after = json.loads(_state_path(settings).read_text(encoding="utf-8"))
    assert state_after["art_recipe_used"]["tags"]["subject"] == "school_children"
    assert state_after["art_recipe_suppressed"] is False


def test_deterministic_fallback_does_not_book_a_recipe(monkeypatch, tmp_path):
    from scripts.history import record as record_mod
    from scripts.openrouter import art_recipes as ar

    settings = _issue21_settings(tmp_path)
    _write_archived_dry_brief(settings)

    monkeypatch.setattr(gi, "_call_image_api", lambda *a, **k: b"artwork")
    monkeypatch.setattr(
        gi, "inspect_art", lambda b, s: {"status": "rejected", "has_text": True})
    monkeypatch.setattr(gi, "_off_palette_pct", lambda b: 0.0)
    logs = []
    _run_main(monkeypatch, settings, logs)

    assert not _hero_path(settings).exists()
    assert any(log["event_type"] == "hero_fallback_pictogram" for log in logs)

    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    monkeypatch.setenv("GENERATION_RUN_ID", "run-issue21-fallback")
    result = record_mod._record_published_recipe(_RecipeHistoryLog(), mode="pi_display")

    assert result["status"] == "skipped"
    assert ar.published_recipes(settings) == []


def test_archived_dry_oct6_eight_and_one_deliveries_skip_ledger(monkeypatch, tmp_path):
    """Both archived dry Oct 6 deliveries suppress the locked recipe and skip it."""
    from pathlib import Path

    from scripts.history import record as record_mod
    from scripts.openrouter import art_recipes as ar

    settings = _issue21_settings(tmp_path)
    _write_archived_dry_brief(settings)
    _lock_recipe(settings, _umbrella_recipe())

    # 08:00 delivery: the locked wet recipe contradicts the dry accepted brief.
    _accept_generation(monkeypatch)
    logs_0800 = []
    _run_main(monkeypatch, settings, logs_0800)
    assert any(log["event_type"] == "art_recipe_suppressed" for log in logs_0800)

    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    monkeypatch.setenv("GENERATION_RUN_ID", "run-issue21-0800")
    first = record_mod._record_published_recipe(_RecipeHistoryLog(), mode="pi_display")
    assert first["status"] == "skipped_suppressed"

    # 13:00 refresh: same locked recipe, re-rendered face-in-clearing-sun brief.
    payload = _archived_dry_payload()
    payload["day_context"]["daypart_role"] = "afternoon"
    payload["brief"]["illustration_prompt"] = (
        "A close-up of a person's face in clearing sun. No umbrella needed."
    )
    Path(settings["runtime"]["brief_file"]).write_text(json.dumps(payload), encoding="utf-8")

    _accept_generation(monkeypatch)
    logs_1300 = []
    _run_main(monkeypatch, settings, logs_1300)
    assert any(log["event_type"] == "art_recipe_suppressed" for log in logs_1300)

    monkeypatch.setenv("GENERATION_RUN_ID", "run-issue21-1300")
    second = record_mod._record_published_recipe(_RecipeHistoryLog(), mode="pi_display")
    assert second["status"] == "skipped_suppressed"

    assert ar.published_recipes(settings) == []
    assert ar.history_summary(settings)["count"] == 0
