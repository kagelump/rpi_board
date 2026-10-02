"""Tests for image style/palette rotation and issue #3 guardrail/fallback behavior."""
import hashlib
import io
import json
import sys
import urllib.error

from PIL import Image

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
        assert gi._hero_reuse_eligible(self._record("rejected", data), "2026-10-01", hero) is False

    def test_unverified_is_not_eligible(self, tmp_path):
        hero = tmp_path / "hero.png"
        data = _png_bytes()
        hero.write_bytes(data)
        assert gi._hero_reuse_eligible(self._record("unverified", data), "2026-10-01", hero) is False

    def test_legacy_state_without_validation_is_not_eligible(self, tmp_path):
        hero = tmp_path / "hero.png"
        hero.write_bytes(_png_bytes())
        assert gi._hero_reuse_eligible({"hero_prompt": "old"}, "2026-10-01", hero) is False

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

    def test_disabled_guardrail_is_not_eligible(self, tmp_path):
        # A disabled guardrail never validated the image, so it must not be
        # treated as previously accepted artwork for reuse.
        hero = tmp_path / "hero.png"
        data = _png_bytes()
        hero.write_bytes(data)
        assert gi._hero_reuse_eligible(self._record("disabled", data), "2026-10-01", hero) is False


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

    def test_cached_brief_regenerates_disabled_hero(self, tmp_path, monkeypatch):
        settings = _main_settings(tmp_path, guardrail=False)
        _write_brief(settings, brief_source="cached")
        disabled_bytes = _png_bytes((11, 22, 33))
        _hero_path(settings).write_bytes(disabled_bytes)
        # A "disabled" verdict never passed validation, so it must not satisfy
        # the accepted-artwork reuse criterion even on a cached brief.
        _seed_style_state(settings, status="disabled", hero_bytes=disabled_bytes)

        new_bytes = _png_bytes((44, 55, 66))
        calls = {"n": 0}

        def fake_call(s, p, pr):
            calls["n"] += 1
            return new_bytes

        monkeypatch.setattr(gi, "_call_image_api", fake_call)
        logs = []
        _run_main(monkeypatch, settings, logs)

        assert calls["n"] == 1
        assert _hero_path(settings).read_bytes() == new_bytes
        assert any(entry["event_type"] == "hero_reuse_rejected" for entry in logs)
        assert not any(entry["event_type"] == "hero_reused" for entry in logs)
        state = json.loads(_state_path(settings).read_text(encoding="utf-8"))
        assert state["hero_validation"]["status"] == "disabled"


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
