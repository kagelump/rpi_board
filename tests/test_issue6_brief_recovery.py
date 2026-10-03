"""End-to-end regression for issue #6.

An OpenRouter brief that violates the advertised output contract must be
rejected before artwork generation and retried; exhaustion must fall back to a
deterministic brief that still reaches a complete render.

``tests/fixtures/issue6_brief_model_response.json`` stores, verbatim, the exact
payload of snapshot ``08edf1d91b5b4155ac2dae6186c8dea1`` from run
``aa5ea68fa13e43fb9b35a3bebebc4881``: a 141-character non-ASCII subtitle,
missing mood/accent/event_ref, and extra
date/part_of_day/time_frame/layout_emphasis keys. The recorded sha256 and
byte-size are asserted below so the fixture cannot silently drift from the
archive.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import scripts.openrouter.generate_brief as gb
from scripts.common import write_json
from scripts.openrouter.generate_brief import (
    _brief_violations,
    _renderable_deterministic_brief,
    _request_brief_with_fallback,
)
from scripts.render.compose_board import fit_panel_copy, render_board


FIXTURES = Path(__file__).parent / "fixtures"
ARCHIVED_RESPONSE = json.loads(
    (FIXTURES / "issue6_brief_model_response.json").read_text(encoding="utf-8")
)
DETERMINISTIC_FALLBACK = json.loads(
    (FIXTURES / "issue6_deterministic_fallback.json").read_text(encoding="utf-8")
)

# Digest/size recorded alongside snapshot 08edf1d9... in the Pi history API.
ARCHIVED_SHA256 = "f36f2744ed90f524e944cd45b36dcbaf4759a6705177ca9e7f0eb39043d7822e"
ARCHIVED_BYTE_SIZE = 678


def _settings(tmp_path):
    return {
        "display": {"width": 960, "height": 640, "panel_fraction": 0.25},
        "openrouter": {"text_model": "provider/model", "brief_temperature": 0.5},
        "context": {"events_mode": "online_model"},
        "pipeline": {"brief_timeout_seconds": 8, "brief_offline_retry_count": 2},
        "runtime": {
            "hero_file": str(tmp_path / "missing-hero.png"),
            "stale_file": str(tmp_path / "status.json"),
        },
    }


def _payload(brief, brief_source="deterministic_fallback_invalid_schema"):
    return {
        "brief": brief,
        "today": {
            "daily_summary": {
                "date": "2026-10-02",
                "condition": "Light drizzle",
                "weather_code": 51,
                "temp_min_c": 18,
                "temp_max_c": 24,
            }
        },
        "generated_at_local": "2026-10-02T08:00:00+09:00",
        "brief_source": brief_source,
    }


def test_fixture_is_the_exact_archived_payload():
    """The fixture must be the preserved snapshot payload, not a reconstruction."""
    canonical = json.dumps(ARCHIVED_RESPONSE, ensure_ascii=True, indent=2)
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == ARCHIVED_SHA256
    assert len(canonical.encode("utf-8")) == ARCHIVED_BYTE_SIZE
    assert ARCHIVED_RESPONSE == {
        "date": "2026-10-02",
        "headline": "The rain is a morning person",
        "illustration_prompt": (
            "A rain-speckled window looking onto a quiet Tokyo side street in "
            "early autumn; a single clear umbrella crosses wet asphalt; a gingko "
            "tree drops a few yellow leaves; soft grey overcast light; calm and "
            "still; no text."
        ),
        "layout_emphasis": {"rain": "high", "temperature": "medium"},
        "part_of_day": "morning",
        "subtitle": (
            "Damp 19\u00b0C start, 96% chance of drizzle, drying to a mild "
            "22\u00b0C afternoon. Tonight: 10% chance of a sprinkle. Tomorrow: "
            "partly cloudy, 17-24\u00b0C."
        ),
        "time_frame": "Morning briefing: umbrella for the morning, free hands by lunch.",
    }
    assert len(ARCHIVED_RESPONSE["subtitle"]) == 141
    assert not ARCHIVED_RESPONSE["subtitle"].isascii()


def test_archived_response_is_rejected_before_artwork(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    calls = []

    def fake_call(settings, prompt, model_override=None, **kwargs):
        calls.append(model_override)
        return dict(ARCHIVED_RESPONSE)

    snapshots = []
    events = []
    monkeypatch.setattr("scripts.openrouter.generate_brief._call_openrouter", fake_call)
    monkeypatch.setattr(
        "scripts.openrouter.generate_brief.record_current_snapshot",
        lambda kind, payload=None, **kwargs: snapshots.append(kind),
    )
    monkeypatch.setattr(
        "scripts.openrouter.generate_brief.record_current_log",
        lambda component, event_type, message, **kwargs: events.append(event_type),
    )

    candidate, metadata = _request_brief_with_fallback(settings, "prompt", "sig")

    # No candidate is returned: image generation never sees the invalid brief.
    assert candidate is None
    assert calls == ["provider/model:online", "provider/model", "provider/model"]
    assert metadata["kind"] == "invalid"
    assert metadata["candidate"] == ARCHIVED_RESPONSE
    violations = metadata["violations"]
    assert any("subtitle is 141 characters" in v for v in violations)
    assert any("non-ASCII" in v and "subtitle" in v for v in violations)
    assert any("missing required field 'mood'" in v for v in violations)
    assert any("unexpected field 'date'" in v for v in violations)
    # Each attempt is archived and the configured retry budget is exhausted.
    assert snapshots == ["brief_model_response"] * 3
    assert events.count("brief_attempt_rejected") == 2
    assert events.count("brief_rejected") == 1


def test_archived_copy_would_have_aborted_the_compositor(tmp_path):
    """Prove the exact archived copy reproduces the late fit failure."""
    assert ARCHIVED_RESPONSE["headline"] == "The rain is a morning person"
    assert len(ARCHIVED_RESPONSE["subtitle"]) == 141
    with pytest.raises(ValueError, match="do not fit the text panel"):
        fit_panel_copy(
            _settings(tmp_path),
            ARCHIVED_RESPONSE["headline"],
            ARCHIVED_RESPONSE["subtitle"],
        )


def test_archived_copy_raises_inside_render_board_from_artwork_to_compositor(tmp_path):
    """The compositor itself, not just the shared fit helper, rejects it."""
    settings = _settings(tmp_path)
    output = tmp_path / "board.png"
    preview = tmp_path / "preview.png"
    with pytest.raises(ValueError, match="do not fit the text panel"):
        render_board(
            settings,
            _payload(dict(ARCHIVED_RESPONSE), brief_source="openrouter"),
            str(output),
            str(preview),
        )
    # Composition failed before any artifact or status file was written.
    assert not output.exists()
    assert not preview.exists()


def test_exhaustion_falls_back_to_renderable_brief_and_completes(tmp_path, monkeypatch):
    settings = _settings(tmp_path)

    # Exhaust the model attempts with the archived response.
    monkeypatch.setattr(
        "scripts.openrouter.generate_brief._call_openrouter",
        lambda s, p, model_override=None, **kwargs: dict(ARCHIVED_RESPONSE),
    )
    monkeypatch.setattr(
        "scripts.openrouter.generate_brief.record_current_snapshot", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "scripts.openrouter.generate_brief.record_current_log", lambda *a, **k: None
    )
    candidate, metadata = _request_brief_with_fallback(settings, "prompt", "sig")
    assert candidate is None and metadata["kind"] == "invalid"
    assert _brief_violations(ARCHIVED_RESPONSE, settings)

    # Recovery step from generate_brief.main(): deterministic copy, validated
    # with compose_board's own fit logic, is handed on to image + composition.
    fallback = _renderable_deterministic_brief(settings, DETERMINISTIC_FALLBACK)
    assert fallback is DETERMINISTIC_FALLBACK

    drawn = []
    original_text = ImageDraw.ImageDraw.text

    def capture_text(draw, xy, text, *args, **kwargs):
        if xy[1] >= 480:
            drawn.append((text, kwargs["font"].size))
        return original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)

    output = tmp_path / "board.png"
    preview = tmp_path / "preview.png"
    render_board(settings, _payload(fallback), str(output), str(preview))

    panel_copy = " ".join(text for text, _ in drawn)
    assert panel_copy == DETERMINISTIC_FALLBACK["headline"] + " " + DETERMINISTIC_FALLBACK["subtitle"]
    assert "..." not in panel_copy and "\u2026" not in panel_copy
    assert len(drawn) <= 4
    assert all(size >= 26 for _, size in drawn)
    assert Image.open(output).size == (960, 640)
    assert Image.open(preview).size == (480, 320)
    assert json.loads((tmp_path / "status.json").read_text())["brief_source"] == (
        "deterministic_fallback_invalid_schema"
    )


def test_valid_concise_brief_still_renders(tmp_path):
    settings = _settings(tmp_path)
    brief = {
        "headline": "Rain by 3pm, 22C",
        "subtitle": "Bring a coat and an umbrella.",
        "illustration_prompt": "Rain over a city street.",
        "mood": "stormy",
        "accent": "yellow",
        "event_ref": "",
    }
    assert _brief_violations(brief, settings) == []

    output = tmp_path / "board.png"
    preview = tmp_path / "preview.png"
    render_board(settings, _payload(brief, brief_source="openrouter"), str(output), str(preview))
    assert Image.open(output).size == (960, 640)


def _main_settings(tmp_path, deterministic_brief):
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    input_path = runtime / "last_brief.json"
    write_json(input_path, {
        "generated_at_local": "2026-10-02T08:00:00+09:00",
        "day_context": {"date_iso": "2026-10-02", "part_of_day": "morning"},
        "today": {"daily_summary": {"date": "2026-10-02", "condition": "Light drizzle",
                                    "weather_code": 51, "temp_min_c": 18, "temp_max_c": 24}},
        "brief": deterministic_brief,
        "brief_context": {"ordered_facts": []},
    })
    return {
        "display": {"width": 960, "height": 640, "panel_fraction": 0.25},
        "runtime": {
            "brief_file": str(input_path),
            "history_file": str(runtime / "history.json"),
            "day_context_file": str(runtime / "day_context.json"),
            "last_good_brief_file": str(runtime / "last_good_brief.json"),
            "hero_file": str(runtime / "missing-hero.png"),
            "stale_file": str(runtime / "status.json"),
        },
        "openrouter": {"text_model": "provider/model", "brief_temperature": 0.5},
        "context": {"events_mode": "online_model"},
        "pipeline": {
            "brief_timeout_seconds": 8,
            "brief_offline_retry_count": 2,
            "enable_openrouter_brief": True,
            "regen_min_interval_seconds": 0,
            "skip_unchanged": False,
        },
        "voice": {"history_window": 6},
    }


def _run_main(tmp_path, monkeypatch, deterministic_brief):
    settings = _main_settings(tmp_path, deterministic_brief)
    monkeypatch.setattr(gb, "load_settings", lambda: settings)
    monkeypatch.setattr(gb, "_call_openrouter",
                        lambda s, p, model_override=None, **kwargs: dict(ARCHIVED_RESPONSE))
    monkeypatch.setattr(gb, "record_current_log", lambda *a, **k: None)
    monkeypatch.setattr(gb, "record_current_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["generate_brief.py"])
    gb.main()
    return json.loads(Path(settings["runtime"]["brief_file"]).read_text(encoding="utf-8")), settings


def test_main_writes_renderable_fallback_when_all_model_briefs_invalid(tmp_path, monkeypatch):
    """Replay 08edf1d9... end to end: rejection, retries, fallback, full render."""
    written, settings = _run_main(tmp_path, monkeypatch, dict(DETERMINISTIC_FALLBACK))

    assert written["brief_source"] == "deterministic_fallback_invalid_schema"
    assert written["brief"]["headline"] == DETERMINISTIC_FALLBACK["headline"]
    assert written["brief"]["subtitle"] == DETERMINISTIC_FALLBACK["subtitle"]
    # The fallback is proven renderable with the compositor's own fit logic.
    fit_panel_copy(settings, written["brief"]["headline"], written["brief"]["subtitle"])

    # The scheduled update reaches a complete render instead of aborting late:
    # every word of the fallback copy is drawn, with no truncation markers.
    drawn = []
    original_text = ImageDraw.ImageDraw.text

    def capture_text(draw, xy, text, *args, **kwargs):
        if xy[1] >= 480:
            drawn.append(text)
        return original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    output = tmp_path / "board.png"
    preview = tmp_path / "preview.png"
    render_board(settings, written, str(output), str(preview))

    panel_copy = " ".join(drawn)
    assert panel_copy == (
        DETERMINISTIC_FALLBACK["headline"] + " " + DETERMINISTIC_FALLBACK["subtitle"]
    )
    assert "..." not in panel_copy and "\u2026" not in panel_copy
    assert Image.open(output).size == (960, 640)
    assert Image.open(preview).size == (480, 320)


def test_main_downgrades_unrenderable_deterministic_fallback(tmp_path, monkeypatch):
    unrenderable = dict(DETERMINISTIC_FALLBACK, headline="W" * 60, subtitle="x" * 200)
    written, settings = _run_main(tmp_path, monkeypatch, unrenderable)

    assert written["brief_source"] == "deterministic_fallback_invalid_schema"
    assert written["brief"]["headline"] == "Weather update"
    assert written["brief"]["subtitle"] == "Check the forecast before heading out."
    fit_panel_copy(settings, written["brief"]["headline"], written["brief"]["subtitle"])


def test_main_publishes_deterministic_fallback_when_budget_exhausted(tmp_path, monkeypatch):
    """A zero/expired brief budget falls straight through to the fallback."""
    settings = _main_settings(tmp_path, dict(DETERMINISTIC_FALLBACK))
    settings["pipeline"]["brief_total_budget_seconds"] = 0
    calls = []

    monkeypatch.setattr(gb, "load_settings", lambda: settings)
    monkeypatch.setattr(
        gb, "_call_openrouter",
        lambda *a, **k: calls.append(1) or dict(ARCHIVED_RESPONSE),
    )
    monkeypatch.setattr(gb, "record_current_log", lambda *a, **k: None)
    monkeypatch.setattr(gb, "record_current_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["generate_brief.py"])

    gb.main()

    written = json.loads(Path(settings["runtime"]["brief_file"]).read_text(encoding="utf-8"))
    assert calls == []  # budget of zero means no model attempt is started
    assert written["brief_source"] == "deterministic_fallback_error"
    assert written["brief"]["headline"] == DETERMINISTIC_FALLBACK["headline"]
    assert written["brief"]["subtitle"] == DETERMINISTIC_FALLBACK["subtitle"]


def _run_deterministic_only_main(tmp_path, monkeypatch, deterministic_brief):
    """Run ``generate_brief.main()`` with ``enable_openrouter_brief=false``.

    ``_call_openrouter`` is replaced with a sentinel that records any provider
    invocation, so a test can prove the deterministic-only path never asks a
    model to rescue unrenderable copy.
    """
    settings = _main_settings(tmp_path, deterministic_brief)
    settings["pipeline"]["enable_openrouter_brief"] = False
    calls = []

    def fail_on_provider_call(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("model provider called in deterministic-only mode")

    monkeypatch.setattr(gb, "load_settings", lambda: settings)
    monkeypatch.setattr(gb, "_call_openrouter", fail_on_provider_call)
    monkeypatch.setattr(gb, "record_current_log", lambda *a, **k: None)
    monkeypatch.setattr(gb, "record_current_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["generate_brief.py"])

    gb.main()
    written = json.loads(Path(settings["runtime"]["brief_file"]).read_text(encoding="utf-8"))
    return written, settings, calls


def test_deterministic_only_mode_sanitizes_unrenderable_copy(tmp_path, monkeypatch):
    """Issue #8: disabling OpenRouter must not bypass the renderability check.

    The deterministic-only branch used to write the transform-derived brief
    straight to disk, so deliberately unrenderable copy reached ``compose_board``
    and raised the late text-panel ``ValueError``. Replay that input and confirm
    the sanitized brief is what the compositor actually draws.
    """
    unrenderable = dict(
        DETERMINISTIC_FALLBACK,
        headline="W" * 60,
        subtitle="x" * 200,
    )
    written, settings, calls = _run_deterministic_only_main(
        tmp_path, monkeypatch, unrenderable
    )

    # No model provider was consulted: the deterministic path is self-sufficient.
    assert calls == []
    # Provenance stays deterministic and the unrenderable copy is replaced.
    assert written["brief_source"] == "deterministic"
    assert written["brief"]["headline"] == "Weather update"
    assert written["brief"]["subtitle"] == "Check the forecast before heading out."
    # The exact copy that would have aborted the compositor is what was repaired.
    with pytest.raises(ValueError, match="do not fit the text panel"):
        fit_panel_copy(settings, unrenderable["headline"], unrenderable["subtitle"])
    # Non-copy deterministic fields survive the downgrade.
    assert written["brief"]["illustration_prompt"] == unrenderable["illustration_prompt"]
    fit_panel_copy(settings, written["brief"]["headline"], written["brief"]["subtitle"])

    # Prove the sanitized copy, not the original, is what reaches the panel.
    drawn = []
    original_text = ImageDraw.ImageDraw.text

    def capture_text(draw, xy, text, *args, **kwargs):
        if xy[1] >= 480:
            drawn.append(text)
        return original_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture_text)
    output = tmp_path / "board.png"
    preview = tmp_path / "preview.png"
    render_board(settings, written, str(output), str(preview))

    panel_copy = " ".join(drawn)
    assert panel_copy == (
        written["brief"]["headline"] + " " + written["brief"]["subtitle"]
    )
    assert unrenderable["headline"] not in panel_copy
    assert unrenderable["subtitle"] not in panel_copy
    assert "..." not in panel_copy and "\u2026" not in panel_copy
    assert Image.open(output).size == (960, 640)
    assert Image.open(preview).size == (480, 320)


def test_deterministic_only_mode_keeps_concise_copy_unchanged(tmp_path, monkeypatch):
    """A renderable deterministic brief still passes through byte-for-byte."""
    concise = dict(DETERMINISTIC_FALLBACK)
    written, settings, calls = _run_deterministic_only_main(
        tmp_path, monkeypatch, concise
    )

    assert calls == []
    assert written["brief_source"] == "deterministic"
    assert written["brief"] == concise
    fit_panel_copy(settings, written["brief"]["headline"], written["brief"]["subtitle"])
