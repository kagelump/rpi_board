"""Tests for issue #18: shared artwork recipe ledger, motif cooldowns, and the
two-dimension novelty rule."""
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PIL import Image

from scripts.history import record as record_mod
from scripts.openrouter import art_recipes as ar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path, **policy):
    settings = {"runtime": {"art_recipe_ledger_file": str(tmp_path / "ledger.jsonl")}}
    if policy:
        settings["art_variety"] = policy
    return settings


def _tags(**overrides):
    tags = {
        "subject": "lone_walker",
        "subject_text": "a lone pedestrian walking away from the viewer",
        "motif": "small red bag",
        "motifs": ["small red bag"],
        "setting": "residential_lane",
        "setting_text": "a narrow residential lane",
        "viewpoint": "eye_level",
        "viewpoint_text": "eye-level medium shot",
        "composition": "cropped_edge",
        "composition_text": "subject cropped by the frame edge",
    }
    tags.update(overrides)
    return tags


def _record(settings, recipe, *, sha, published_at=None, mode="pi_display",
            target_date="2026-10-01", source="scheduled"):
    return ar.record_publication(
        settings,
        recipe=recipe,
        artwork_sha256=sha,
        target_date=target_date,
        source=source,
        mode=mode,
        published_at=published_at,
    )


def _iso(value):
    return value.isoformat()


# ---------------------------------------------------------------------------
# Novelty / two-dimension rule
# ---------------------------------------------------------------------------


def test_identical_recipe_is_in_cooldown_and_not_novel(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    _record(settings, _tags(), sha="a" * 64, published_at=_iso(now - timedelta(days=2)))
    cooldown = ar.recent_published_recipes(settings, now=now, days=7)
    assert ar._in_cooldown({"tags": _tags()}, cooldown) is True
    assert ar.passes_two_dimension_rule(
        {"tags": _tags()}, cooldown[-1], ar.resolve_policy(settings)
    ) is False


def test_style_clothing_and_palette_changes_do_not_establish_novelty(tmp_path):
    """The motivation example: a new outfit and sky over the same walker."""
    settings = _settings(tmp_path)
    recent = {"tags": _tags()}
    candidate = {
        "tags": _tags(
            subject_text="a lone pedestrian walking away in a new coat",
            motif="small red bag",
            style="Bauhaus",
            dominant_ink="Red Signal",
            sky="clearing",
        )
    }
    assert ar.passes_two_dimension_rule(candidate, recent, ar.resolve_policy(settings)) is False
    # The subject/motif cooldown also blocks it outright.
    assert ar._in_cooldown(candidate, [recent]) is True


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"setting": "riverside_path"}, False),                      # setting only
        ({"subject": "cyclist", "motif": "bicycle", "motifs": ["bicycle"]}, False),  # subject only
        ({"viewpoint": "high_angle"}, False),                        # viewpoint only
        ({"subject": "cyclist", "motif": "bicycle", "motifs": ["bicycle"],
          "viewpoint": "high_angle"}, True),                         # subject + viewpoint
        ({"subject": "cyclist", "motif": "bicycle", "motifs": ["bicycle"],
          "composition": "layered_depth"}, True),                    # subject + composition
    ],
)
def test_two_dimension_rule_matches_issue_wording(changes, expected):
    candidate = {"tags": _tags(**changes)}
    assert ar.passes_two_dimension_rule(candidate, {"tags": _tags()}, {"min_dimension_differences": 2}) is expected


def test_min_dimension_differences_is_configurable():
    # Exactly two dimensions differ: subject/motif and viewpoint.
    candidate = {"tags": _tags(subject="cyclist", viewpoint="high_angle")}
    assert ar.passes_two_dimension_rule(
        candidate, {"tags": _tags()}, {"min_dimension_differences": 2}) is True
    assert ar.passes_two_dimension_rule(
        candidate, {"tags": _tags()}, {"min_dimension_differences": 3}) is False


# ---------------------------------------------------------------------------
# Cooldown expiry / configuration
# ---------------------------------------------------------------------------


def test_motif_cooldown_expires(tmp_path):
    settings = _settings(tmp_path, motif_cooldown_days=7)
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    _record(settings, _tags(), sha="a" * 64, published_at=_iso(now - timedelta(days=8)))
    assert ar.recent_published_recipes(settings, now=now, days=7) == []
    # Still inside the wider lookback, but the motif cooldown has lapsed.
    assert len(ar.recent_published_recipes(settings, now=now, days=14)) == 1
    assert ar._in_cooldown(
        {"tags": _tags()}, ar.recent_published_recipes(settings, now=now, days=7)
    ) is False


def test_motif_cooldown_is_configurable(tmp_path):
    settings = _settings(tmp_path, motif_cooldown_days=3)
    now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    _record(settings, _tags(), sha="a" * 64, published_at=_iso(now - timedelta(days=4)))
    assert ar.recent_published_recipes(settings, now=now, days=3) == []
    assert ar._in_cooldown(
        {"tags": _tags()}, ar.recent_published_recipes(settings, now=now, days=3)
    ) is False


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_choose_recipe_avoids_recent_walker_recipe(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    _record(settings, _tags(), sha="a" * 64,
            published_at=_iso(now - timedelta(hours=12)), target_date="2026-10-01")

    chosen = ar.choose_recipe(settings, target_date="2026-10-02",
                              daypart_role="morning", now=now)

    assert chosen is not None
    tags = chosen["tags"]
    assert not (tags["subject"] == "lone_walker" and "small red bag" in tags["motifs"])
    latest = {"tags": _tags()}
    assert chosen["status"] == "selected"
    assert ar.passes_two_dimension_rule({"tags": tags}, latest, ar.resolve_policy(settings))


def test_selection_is_deterministic_for_the_same_target_day(tmp_path):
    settings = _settings(tmp_path)
    first = ar.choose_recipe(settings, target_date="2026-10-02", daypart_role="morning")
    second = ar.choose_recipe(settings, target_date="2026-10-02", daypart_role="morning")
    assert first["tags"] == second["tags"]
    assert first["rationale"] == second["rationale"]


def test_selection_advances_the_recipe_on_a_new_target_day(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    first = ar.choose_recipe(settings, target_date="2026-10-01", daypart_role="morning", now=now)
    _record(settings, first["tags"], sha="a" * 64,
            published_at=_iso(now - timedelta(hours=20)), target_date="2026-10-01")
    second = ar.choose_recipe(settings, target_date="2026-10-02", daypart_role="morning", now=now)
    assert first["tags"] != second["tags"]


def test_variety_exhausted_falls_back_without_blocking(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    _record(settings, _tags(), sha="a" * 64,
            published_at=_iso(now - timedelta(hours=12)), target_date="2026-10-01")
    # Only one (recently used) candidate exists, so novelty is impossible.
    monkeypatch.setattr(ar, "build_candidates", lambda **kwargs: [{"tags": _tags()}])

    chosen = ar.choose_recipe(settings, target_date="2026-10-02",
                              daypart_role="morning", now=now)

    assert chosen is not None
    assert chosen["status"] == "variety_exhausted"
    assert "variety_exhausted" in chosen["rationale"]


def test_candidate_pools_filter_night_and_rain():
    day_subjects = {item["tags"]["subject"] for item in ar.build_candidates(daypart_role="morning")}
    night_subjects = {item["tags"]["subject"] for item in ar.build_candidates(daypart_role="night")}
    assert "lantern_street" not in day_subjects
    assert "lantern_street" in night_subjects
    assert "school_children" not in night_subjects
    wet_subjects = {item["tags"]["subject"] for item in ar.build_candidates(daypart_role="midday", wet=True)}
    assert "balcony_laundry" not in wet_subjects


# ---------------------------------------------------------------------------
# Issue #20: weather compatibility in both directions
# ---------------------------------------------------------------------------

ARCHIVED_DRY_FIXTURE = (
    Path(__file__).parent / "fixtures" / "issue20_archived_dry_oct6_brief.json"
)


def _archived_dry_payload():
    """The archived 2026-10-06 input that produced the buggy umbrella artwork.

    Snapshot ``2b43c2d198a04c05ae34d639038f16d1`` from live run
    ``976e7473dc1d4ed78e974eee1556d876``: Open-Meteo ``rain_sum_mm: 0.0`` and an
    8% maximum rain probability, with a brief that explicitly stays dry.
    """
    return json.loads(ARCHIVED_DRY_FIXTURE.read_text(encoding="utf-8"))


def _wet_payload():
    return {
        "today": {"daily_summary": {
            "condition": "Light rain", "weather_code": 61,
            "rain_prob_max_pct": 80, "rain_sum_mm": 4.2,
        }},
        "brief": {
            "headline": "Rain on and off: take an umbrella",
            "subtitle": "Showers through the afternoon with a light breeze.",
            "rain_level": "light",
            "illustration_prompt": "A commuter under an umbrella crossing a wet lane.",
        },
        "brief_context": {},
    }


def test_subject_weather_classification_is_deterministic():
    assert ar.recipe_weather_class(
        {"subject": "umbrella_crowd", "motifs": ["umbrellas"]}) == "wet"
    assert ar.recipe_weather_class(
        {"subject": "window_worker", "motifs": ["desk lamp"]}) == "wet"
    assert ar.recipe_weather_class(
        {"subject": "balcony_laundry", "motifs": ["bedsheets"]}) == "dry"
    assert ar.recipe_weather_class(
        {"subject": "lone_walker", "motifs": ["small red bag"]}) == "neutral"
    # Free-text external recipes are still caught by the keyword fallback.
    assert ar.recipe_weather_class(
        {"subject_text": "a crowd beneath open umbrellas"}) == "wet"
    # "no rain" must not be misread as rain by the wetness heuristic.
    no_rain = {"brief": {"subtitle": "clouds above, no rain."}}
    assert ar.brief_declares_dry(no_rain) is True
    assert ar._is_wet(no_rain) is False


def test_candidate_pools_filter_both_wet_and_dry_subjects():
    dry = {
        item["tags"]["subject"]
        for item in ar.build_candidates(daypart_role="midday", wet=False)
    }
    wet = {
        item["tags"]["subject"]
        for item in ar.build_candidates(daypart_role="midday", wet=True)
    }

    # Rain-specific subjects must never appear on a dry forecast.
    assert "umbrella_crowd" not in dry
    assert "window_worker" not in dry
    # Dry-only subjects must never appear on a wet forecast.
    assert "balcony_laundry" not in wet
    # Neutral subjects remain eligible for both classes.
    assert "lone_walker" in dry and "lone_walker" in wet
    # Compatible families remain available.
    assert "umbrella_crowd" in wet and "window_worker" in wet
    assert "balcony_laundry" in dry


def test_archived_dry_oct6_replay_never_selects_rain_staging(tmp_path):
    """Replay the archived dry run.

    With the same recent ledger history the pre-fix selector picked
    ``umbrella_crowd``; the weather filter must make that impossible and keep the
    selected staging dry/neutral.
    """
    payload = _archived_dry_payload()
    assert ar._is_wet(payload) is False
    assert ar.brief_declares_dry(payload) is True

    settings = _settings(tmp_path)
    now = datetime(2026, 10, 5, 12, 3, 20, tzinfo=timezone.utc)
    ar.record_publication(
        settings,
        recipe={
            "subject": "window_worker",
            "motifs": ["desk lamp"],
            "setting": "under_overpass",
            "viewpoint": "high_angle",
            "composition": "emblem",
        },
        artwork_sha256="a" * 64,
        target_date="2026-10-05",
        mode="pi_display",
        published_at="2026-10-05T04:03:03+00:00",
    )

    chosen = ar.choose_recipe(
        settings, target_date="2026-10-06", daypart_role="primary",
        payload=payload, now=now,
    )

    assert chosen is not None
    tags = chosen["tags"]
    assert tags["subject"] not in {"umbrella_crowd", "window_worker"}
    assert "umbrellas" not in (tags.get("motifs") or [])
    assert ar.recipe_weather_class(tags) in {"dry", "neutral"}
    assert chosen["forecast_weather"] == "dry"
    assert chosen["recipe_weather"] in {"dry", "neutral"}
    assert chosen["rationale"]


def test_recipe_conflicts_with_brief_both_directions():
    dry = _archived_dry_payload()
    wet = _wet_payload()

    assert ar.recipe_conflicts_with_brief(
        {"subject": "umbrella_crowd", "motifs": ["umbrellas"]}, dry) is True
    assert ar.recipe_conflicts_with_brief(
        {"subject": "window_worker", "motifs": ["desk lamp"]}, dry) is True
    assert ar.recipe_conflicts_with_brief(
        {"subject": "balcony_laundry", "motifs": ["bedsheets"]}, wet) is True
    # Neutral subjects are compatible with both classes.
    assert ar.recipe_conflicts_with_brief(
        {"subject": "lone_walker", "motifs": ["small red bag"]}, dry) is False
    assert ar.recipe_conflicts_with_brief(
        {"subject": "lone_walker", "motifs": ["small red bag"]}, wet) is False
    # Wet-specific subjects are valid once the brief supports rain.
    assert ar.recipe_conflicts_with_brief(
        {"subject": "umbrella_crowd", "motifs": ["umbrellas"]}, wet) is False


def test_no_weather_compatible_candidate_preserves_brief(monkeypatch, tmp_path):
    """No compatible candidate means ``None``: the caller keeps the accepted
    brief's original subject instead of forcing contradictory recipe staging."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(ar, "build_candidates", lambda **kwargs: [])
    chosen = ar.choose_recipe(
        settings, target_date="2026-10-06", daypart_role="primary",
        payload=_archived_dry_payload(),
    )
    assert chosen is None


# ---------------------------------------------------------------------------
# Issue #24: weather cues must preserve negation and the hero's scene window
# ---------------------------------------------------------------------------

ARCHIVED_ISSUE24_FIXTURE = (
    Path(__file__).parent / "fixtures" / "issue24_archived_oct22_brief.json"
)


def _archived_issue24_payload():
    """The archived 2026-10-21 run for target date 2026-10-22."""
    return json.loads(ARCHIVED_ISSUE24_FIXTURE.read_text(encoding="utf-8"))


def _issue24_delivery_rider_recipe():
    return {
        "status": "selected",
        "recipe_id": "issue24-delivery-rider",
        "forecast_weather": "dry",
        "recipe_weather": "neutral",
        "tags": {
            "subject": "delivery_rider",
            "subject_text": "a delivery rider pausing at a roadside bus stop",
            "motif": "insulated box",
            "motifs": ["insulated box"],
            "setting": "bus_stop",
            "setting_text": "a roadside bus stop",
            "viewpoint": "eye_level",
            "viewpoint_text": "eye-level medium shot",
            "composition": "emblem",
            "composition_text": (
                "near-symmetrical emblem with one deliberate disruption"
            ),
        },
    }


@pytest.mark.parametrize(
    "brief",
    [
        "A mostly dry day, no rain, with a breeze and daylight.",
        "Dry with a breeze; without showers.",
        "Mostly dry despite late drizzle, with bright daylight.",
    ],
)
def test_issue24_negated_precipitation_never_becomes_positive_cue(brief):
    cues = ar._brief_weather_cues(brief)
    assert not any(ar._PRECIPITATION_CUE_RE.search(cue) for cue in cues)
    # A negation must not erase an explicitly dry scene meaning.
    assert "dry conditions" in cues


def test_issue24_negation_scope_does_not_erase_independent_light_cues():
    cues = ar._brief_weather_cues("No rain and a light breeze in daylight.")
    lowered = [cue.lower() for cue in cues]
    assert "breeze" in lowered
    assert "daylight" in lowered
    assert "dry conditions" in lowered
    assert not any(ar._PRECIPITATION_CUE_RE.search(cue) for cue in cues)


def test_issue24_positive_precipitation_cue_is_still_retained():
    cues = ar._brief_weather_cues(
        "Fine drizzle in soft overcast daylight with a calm breeze."
    )
    lowered = [cue.lower() for cue in cues]
    assert "fine drizzle" in lowered
    assert "daylight" in lowered


def test_issue24_forecast_cues_respect_daylight_window_not_whole_day_trace():
    payload = _archived_issue24_payload()
    assert payload["today"]["daily_summary"]["rain_sum_mm"] == 0.2
    assert payload["today"]["hourly"][3]["rain_mm"] == 0.2

    cues = ar._forecast_weather_cues(payload)
    assert cues == ["dry conditions"]

    # A real daytime shower in the same payload still produces wet cues.
    rainy = json.loads(json.dumps(payload))
    rainy["today"]["hourly"][14]["rain_mm"] = 1.2
    rainy["today"]["hourly"][14]["rain_probability_pct"] = 80
    rainy["today"]["hourly"][14]["weather_code"] = 61
    rainy_cues = ar._forecast_weather_cues(rainy)
    assert "Light drizzle" in rainy_cues
    assert "wet conditions" in rainy_cues


def test_issue24_archived_replay_synthesizes_coherent_dry_recipe_scene():
    payload = _archived_issue24_payload()
    recipe = _issue24_delivery_rider_recipe()
    scene = ar.synthesize_recipe_scene(
        recipe,
        brief_scene=payload["brief"]["illustration_prompt"],
        forecast=payload,
    )
    assert scene is not None
    lowered = scene.lower()

    # Recipe realization from issue #23 remains intact.
    for expected in (
        "delivery rider", "insulated box", "roadside bus stop",
        "eye-level medium shot", "near-symmetrical emblem",
    ):
        assert expected in lowered
    assert scene.count("Subject and motif:") == 1

    # The 03:00 trace and negated brief wording stay out of the daytime hero.
    for contradictory in ("rain", "drizzle", "wet conditions"):
        assert contradictory not in lowered
    assert "dry conditions" in lowered
    assert "daylight" in lowered
    assert "breeze" in lowered


# ---------------------------------------------------------------------------
# Issue #26: clause-scoped negation and dry-forecast authority
# ---------------------------------------------------------------------------


ARCHIVED_ISSUE26_FIXTURE = (
    Path(__file__).parent / "fixtures" / "issue26_archived_oct22_brief.json"
)


def _archived_issue26_payload():
    """Representative replay of the archived 2026-10-22 afternoon run."""
    return json.loads(ARCHIVED_ISSUE26_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "brief",
    [
        "Keep the contrast clear without suggesting rain or strong sun.",
        "A calm street without showing rain in the daylight.",
        "Cloudy and dry without implying showers later.",
        "A gentle afternoon not expected to look wet.",
    ],
)
def test_issue26_negated_weather_variants_do_not_become_positive_cues(brief):
    cues = ar._brief_weather_cues(brief)
    assert not any(ar._PRECIPITATION_CUE_RE.search(cue) for cue in cues)
    assert "dry conditions" in cues


def test_issue26_positive_weather_control_survives():
    cues = ar._brief_weather_cues(
        "Clouds build through the afternoon, suggesting rain later."
    )
    assert any(ar._PRECIPITATION_CUE_RE.search(cue) for cue in cues)
    assert "rain" in [cue.lower() for cue in cues]


def test_issue26_archived_replay_synthesizes_dry_overcast_daylight():
    payload = _archived_issue26_payload()
    recipe = _issue24_delivery_rider_recipe()
    brief_scene = payload["brief"]["illustration_prompt"]

    brief_cues = ar._brief_weather_cues(brief_scene)
    forecast_cues = ar._forecast_weather_cues(payload)
    assert "dry conditions" in brief_cues
    assert not any(ar._PRECIPITATION_CUE_RE.search(cue) for cue in brief_cues)
    assert "Overcast" in forecast_cues
    assert "dry conditions" in forecast_cues

    scene = ar.synthesize_recipe_scene(
        recipe, brief_scene=brief_scene, forecast=payload
    )
    assert scene is not None
    lowered = scene.lower()
    for contradictory in ("rain", "drizzle", "wet conditions", "puddle"):
        assert contradictory not in lowered
    assert "dry conditions" in lowered
    assert "overcast" in lowered
    assert "daylight" in lowered
    assert "delivery rider" in lowered
    assert "roadside bus stop" in lowered


def test_issue26_dry_brief_plus_dry_forecast_beats_incidental_lexical_rain():
    payload = _archived_issue26_payload()
    # This scene contains a positive-looking "rain shower" phrase as well as
    # explicit dry wording. The positive token alone must not override the
    # accepted dry meaning and the structured dry scene window.
    brief_scene = (
        "Mostly dry through the afternoon, but a rain shower is possible later. "
        "Keep the contrast clear without suggesting rain."
    )
    assert ar._brief_weather_cues(brief_scene)
    scene = ar.synthesize_recipe_scene(
        _issue24_delivery_rider_recipe(),
        brief_scene=brief_scene,
        forecast=payload,
    )
    assert scene is not None
    lowered = scene.lower()
    assert "rain" not in lowered
    assert "dry conditions" in lowered
    assert "overcast" in lowered
    assert "daylight" in lowered


def test_issue26_positive_rain_is_kept_when_forecast_window_agrees():
    payload = _archived_issue26_payload()
    rainy = json.loads(json.dumps(payload))
    rainy["today"]["daily_summary"].update({
        "condition": "Light rain",
        "rain_prob_max_pct": 80,
        "rain_sum_mm": 1.2,
        "weather_code": 61,
    })
    rainy["today"]["hourly"][14].update({
        "rain_mm": 1.2,
        "rain_probability_pct": 80,
        "weather_code": 61,
    })
    brief_scene = (
        "Clouds build through the afternoon, suggesting rain later. "
        "Emotional beat: a quiet wait before the shower."
    )
    scene = ar.synthesize_recipe_scene(
        _issue24_delivery_rider_recipe(),
        brief_scene=brief_scene,
        forecast=rainy,
    )
    assert scene is not None
    lowered = scene.lower()
    assert "rain" in lowered
    assert "wet conditions" in lowered
    assert "dry conditions" not in lowered


# ---------------------------------------------------------------------------
# Publication / reuse bookkeeping
# ---------------------------------------------------------------------------


def test_preview_does_not_count_and_same_artwork_is_reuse(tmp_path):
    settings = _settings(tmp_path)

    first = _record(settings, _tags(), sha="a" * 64)
    assert first["status"] == "published"
    assert len(ar.published_recipes(settings)) == 1

    preview = _record(settings, _tags(subject="cyclist", motif="bicycle"),
                      sha="b" * 64, mode="local_preview")
    assert preview["status"] == "skipped_preview"
    assert len(ar.published_recipes(settings)) == 1

    reuse = _record(settings, _tags(), sha="a" * 64)
    assert reuse["status"] == "reuse"
    assert reuse["recipe_id"] == first["recipe_id"]
    assert len(ar.published_recipes(settings)) == 1

    events = ar.load_ledger(settings)
    assert sum(event["event"] == ar.RECIPE_REUSE_EVENT for event in events) == 1


def test_recorded_tags_include_style_and_dominant_ink(tmp_path):
    settings = _settings(tmp_path)
    result = ar.record_publication(
        settings, recipe=_tags(), artwork_sha256="a" * 64, target_date="2026-10-01",
        mode="pi_display", style="Linocut", dominant_ink="Red Signal",
        rationale="novel vs recent history",
    )
    assert result["status"] == "published"
    entry = ar.published_recipes(settings)[0]
    assert entry["tags"]["style"] == "linocut"
    assert entry["tags"]["dominant_ink"] == "red signal"
    assert entry["rationale"] == "novel vs recent history"


def test_empty_or_disabled_recipe_is_not_recorded(tmp_path):
    settings = _settings(tmp_path)
    assert ar.record_publication(
        settings, recipe={}, artwork_sha256="a" * 64, target_date="2026-10-01", mode="pi_display"
    )["status"] == "skipped_empty_recipe"
    disabled = _settings(tmp_path, enabled=False)
    assert ar.record_publication(
        disabled, recipe=_tags(), artwork_sha256="a" * 64,
        target_date="2026-10-01", mode="pi_display",
    )["status"] == "disabled"
    assert ar.published_recipes(disabled) == []


def test_history_summary_is_compact_and_secret_free(tmp_path):
    settings = _settings(tmp_path)
    _record(settings, _tags(), sha="a" * 64)
    summary = ar.history_summary(settings)
    assert summary["policy"]["motif_cooldown_days"] == 7
    assert summary["required_dimensions"] == ["subject/motif", "viewpoint/composition"]
    assert summary["recent_recipes"][0]["tags"]["subject"] == "lone walker"
    text = json.dumps(summary).lower()
    for secret in ("api_key", "token", "password", "secret", "authorization", "bearer"):
        assert secret not in text


# ---------------------------------------------------------------------------
# External authoring helpers
# ---------------------------------------------------------------------------


def test_derive_recipe_prefers_explicit_metadata():
    tags, source = ar.derive_recipe_from_update(
        {"brief": {"illustration_prompt": "a lone pedestrian"}},
        {"recipe": {
            "subject": "lone_walker",
            "motifs": ["red bag"],
            "setting": "residential lane",
            "viewpoint": "high angle",
            "composition": "cropped edge",
        }},
    )
    assert source == "metadata"
    assert tags["subject"] == "lone walker"
    assert tags["motifs"] == ["small red bag"]
    assert tags["setting"] == "residential lane"


def test_derive_recipe_infers_from_text_without_vision_calls():
    tags, source = ar.derive_recipe_from_update(
        {"brief": {"illustration_prompt": "a cyclist on a riverside path, high angle"}}, {}
    )
    assert source == "derived"
    assert tags["subject"] == "cyclist"
    assert tags["setting"] == "riverside path"
    assert tags["viewpoint"] == "high angle"


# ---------------------------------------------------------------------------
# Scheduled publication recorder (record.py bridge)
# ---------------------------------------------------------------------------


def _write_runtime(tmp_path, *, target_date="2026-10-01", mode="pi_display"):
    hero = tmp_path / "hero.png"
    Image.new("RGB", (12, 8), "red").save(hero)
    (tmp_path / "last_brief.json").write_text(
        json.dumps({"day_context": {"target_date_iso": target_date}}), encoding="utf-8"
    )
    state = {
        "target_date": target_date,
        "art_recipe_target_date": target_date,
        "last_selected": "Linocut",
        "last_palette": "Red Signal",
        "art_recipe": {
            "status": "selected",
            "rationale": "novel vs recent history",
            "tags": {
                "subject": "cyclist",
                "motifs": ["bicycle"],
                "setting": "riverside_path",
                "viewpoint": "high_angle",
                "composition": "layered_depth",
            },
        },
    }
    (tmp_path / "image_style_state.json").write_text(json.dumps(state), encoding="utf-8")
    return {
        "runtime": {
            "image_style_state_file": str(tmp_path / "image_style_state.json"),
            "brief_file": str(tmp_path / "last_brief.json"),
            "hero_file": str(hero),
            "final_file": str(tmp_path / "final.png"),
            "art_recipe_ledger_file": str(tmp_path / "ledger.jsonl"),
        },
        "display": {"mode": mode},
        "art_variety": {"lookback_days": 14, "motif_cooldown_days": 7, "min_dimension_differences": 2},
    }


class _LogStore:
    def __init__(self):
        self.entries = []

    def log(self, *args, **kwargs):
        self.entries.append((args, kwargs))


def test_record_cli_records_only_panel_delivery(tmp_path, monkeypatch):
    settings = _write_runtime(tmp_path)
    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    monkeypatch.setenv("GENERATION_RUN_ID", "run-test-1")
    store = _LogStore()

    result = record_mod._record_published_recipe(store, mode="pi_display")
    assert result["status"] == "published"
    entries = ar.published_recipes(settings)
    assert entries[0]["tags"]["subject"] == "cyclist"
    assert entries[0]["tags"]["style"] == "linocut"
    assert entries[0]["tags"]["dominant_ink"] == "red signal"
    assert store.entries  # logged into generation history

    assert record_mod._record_published_recipe(store, mode="local_preview")["status"] == "skipped_preview"
    assert len(ar.published_recipes(settings)) == 1

    assert record_mod._record_published_recipe(store, mode="pi_display")["status"] == "reuse"
    assert len(ar.published_recipes(settings)) == 1


def test_record_cli_skips_stale_target_date(tmp_path, monkeypatch):
    settings = _write_runtime(tmp_path)
    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    (tmp_path / "last_brief.json").write_text(
        json.dumps({"day_context": {"target_date_iso": "2026-11-01"}}), encoding="utf-8"
    )
    result = record_mod._record_published_recipe(_LogStore(), mode="pi_display")
    assert result["status"] == "skipped"
    assert ar.published_recipes(settings) == []


def test_record_cli_skips_without_selected_recipe(tmp_path, monkeypatch):
    settings = _write_runtime(tmp_path)
    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    (tmp_path / "image_style_state.json").write_text(json.dumps({"target_date": "2026-10-01"}), encoding="utf-8")
    result = record_mod._record_published_recipe(_LogStore(), mode="pi_display")
    assert result["status"] == "skipped"


def test_record_cli_skips_without_published_hero(tmp_path, monkeypatch):
    settings = _write_runtime(tmp_path)
    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    Path(settings["runtime"]["hero_file"]).unlink()
    result = record_mod._record_published_recipe(_LogStore(), mode="pi_display")
    assert result["status"] == "skipped"
    assert ar.published_recipes(settings) == []


def test_unset_mode_resolves_to_configured_display_mode(tmp_path):
    preview = _settings(tmp_path)
    preview["display"] = {"mode": "local_preview"}
    result = ar.record_publication(
        preview, recipe=_tags(), artwork_sha256="a" * 64,
        target_date="2026-10-01", mode=None,
    )
    assert result["status"] == "skipped_preview"
    assert ar.published_recipes(preview) == []

    panel = _settings(tmp_path)
    panel["display"] = {"mode": "pi_display"}
    assert ar.record_publication(
        panel, recipe=_tags(), artwork_sha256="a" * 64,
        target_date="2026-10-01", mode=None,
    )["status"] == "published"


def test_evaluate_recipe_reports_cooldown_not_novel_and_novel(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    _record(settings, _tags(), sha="a" * 64, published_at=_iso(now - timedelta(days=1)))

    assert ar.evaluate_recipe(settings, _tags(), now=now)["status"] == "cooldown"
    # New subject/motif but the same viewpoint + composition is not novel yet.
    subject_only = _tags(subject="cyclist", motif="bicycle", motifs=["bicycle"])
    assert ar.evaluate_recipe(settings, subject_only, now=now)["status"] == "not_novel"
    novel = _tags(subject="cyclist", motif="bicycle", motifs=["bicycle"], viewpoint="high_angle")
    advisory = ar.evaluate_recipe(settings, novel, now=now)
    assert advisory["status"] == "novel"
    assert advisory["differs_two_dimensions_from_latest"] is True


def test_external_publication_records_advisory_without_blocking(tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    _record(settings, _tags(), sha="a" * 64, published_at=_iso(now - timedelta(days=1)))

    result = ar.record_publication(
        settings,
        recipe=_tags(),
        artwork_sha256="b" * 64,
        target_date="2026-10-08",
        source="external",
        mode="pi_display",
        published_at=_iso(now),
    )
    assert result["status"] == "published"
    assert result["variety_status"] == "cooldown"
    assert result["advisory"]["in_motif_cooldown"] is True
    # It still published: an urgent external update is never blocked.
    assert len(ar.published_recipes(settings)) == 2


def test_record_cli_empty_override_resolves_configured_display_mode(tmp_path, monkeypatch):
    """update_display.sh passes ``--mode "${DISPLAY_MODE_OVERRIDE}"``, which is an
    empty string unless the operator overrides the configured mode. The scheduled
    recorder's effective mode must therefore come from ``settings.display.mode``:
    an empty override under ``local_preview`` consumes no motif cooldown, while
    ``pi_display`` keeps recording production publications.
    """
    preview_dir = tmp_path / "preview"
    preview_dir.mkdir()
    panel_dir = tmp_path / "panel"
    panel_dir.mkdir()

    preview = _write_runtime(preview_dir, mode="local_preview")
    monkeypatch.setattr(record_mod, "load_settings", lambda: preview)
    result = record_mod._record_published_recipe(_LogStore(), mode="")
    assert result["status"] == "skipped_preview"
    assert ar.published_recipes(preview) == []

    panel = _write_runtime(panel_dir, mode="pi_display")
    monkeypatch.setattr(record_mod, "load_settings", lambda: panel)
    assert record_mod._record_published_recipe(_LogStore(), mode="")["status"] == "published"
    assert len(ar.published_recipes(panel)) == 1


# ---------------------------------------------------------------------------
# Issue #21: the ledger books only the recipe that guided the published hero
# ---------------------------------------------------------------------------


def _archived_dry_brief():
    return json.loads(
        (Path(__file__).parent / "fixtures" / "issue20_archived_dry_oct6_brief.json")
        .read_text(encoding="utf-8")
    )


def _umbrella_recipe():
    return {
        "recipe_id": "4e9fd1194606ca7a580e",
        "status": "selected",
        "rationale": "selected before the dry filter existed",
        "tags": {
            "subject": "umbrella_crowd",
            "subject_text": "a cluster of commuters under umbrellas",
            "motif": "umbrellas",
            "motifs": ["umbrellas"],
            "setting": "rooftop_terrace",
            "setting_text": "a rooftop terrace",
            "viewpoint": "close_crop",
            "viewpoint_text": "tight crop on the main motif",
            "composition": "upward",
            "composition_text": "upward view toward the sky",
        },
    }


def _neutral_recipe():
    return {
        "recipe_id": "neutral-recipe-id-0001",
        "status": "selected",
        "rationale": "weather-compatible selection",
        "tags": _tags(),
    }


def _write_replay_runtime(tmp_path, state, *, mode="pi_display"):
    """Runtime shaped like the archived dry Oct 6 runs (matching target date)."""
    hero = tmp_path / "hero.png"
    Image.new("RGB", (12, 8), "red").save(hero)
    (tmp_path / "last_brief.json").write_text(
        json.dumps(_archived_dry_brief()), encoding="utf-8")
    (tmp_path / "image_style_state.json").write_text(json.dumps(state), encoding="utf-8")
    return {
        "runtime": {
            "image_style_state_file": str(tmp_path / "image_style_state.json"),
            "brief_file": str(tmp_path / "last_brief.json"),
            "hero_file": str(hero),
            "final_file": str(tmp_path / "final.png"),
            "art_recipe_ledger_file": str(tmp_path / "ledger.jsonl"),
        },
        "display": {"mode": mode},
        "art_variety": {
            "lookback_days": 14, "motif_cooldown_days": 7, "min_dimension_differences": 2,
        },
    }


def _replay_state(recipe, *, used="missing", suppressed=None):
    state = {
        "target_date": "2026-10-06",
        "art_recipe_target_date": "2026-10-06",
        "art_recipe": recipe,
        "last_selected": "Linocut",
        "last_palette": "Red Signal",
    }
    if used != "missing":
        state["art_recipe_used"] = used
    if suppressed is not None:
        state["art_recipe_suppressed"] = suppressed
    return state


def test_record_cli_skips_suppressed_recipe_and_keeps_audit_event(tmp_path, monkeypatch):
    state = _replay_state(_umbrella_recipe(), used=None, suppressed=True)
    settings = _write_replay_runtime(tmp_path, state)
    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    monkeypatch.setenv("GENERATION_RUN_ID", "run-replay-suppressed")
    store = _LogStore()

    result = record_mod._record_published_recipe(store, mode="pi_display")

    assert result["status"] == "skipped_suppressed"
    assert result["recipe_id"] == "4e9fd1194606ca7a580e"
    assert ar.published_recipes(settings) == []
    assert ar.recent_published_recipes(settings) == []
    assert ar.history_summary(settings)["count"] == 0
    # History still tells the two states apart: selected/suppressed vs published.
    event_types = [entry[1]["event_type"] for entry in store.entries]
    assert "recipe_skipped_suppressed" in event_types
    assert "recipe_published" not in event_types


def test_record_cli_books_used_recipe_not_the_suppressed_locked_recipe(tmp_path, monkeypatch):
    # The locked recipe is the contradictory wet one, but the hero was actually
    # guided by the compatible neutral recipe: only the latter is published.
    state = _replay_state(
        _umbrella_recipe(), used=_neutral_recipe(), suppressed=False)
    settings = _write_replay_runtime(tmp_path, state)
    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)
    monkeypatch.setenv("GENERATION_RUN_ID", "run-replay-used")

    result = record_mod._record_published_recipe(_LogStore(), mode="pi_display")

    assert result["status"] == "published"
    entries = ar.published_recipes(settings)
    assert len(entries) == 1
    assert entries[0]["tags"]["subject"] == "lone walker"
    assert entries[0]["recipe_id"] != "4e9fd1194606ca7a580e"


def test_record_cli_legacy_state_never_books_brief_contradicting_recipe(tmp_path, monkeypatch):
    # State carried across the upgrade has no usage marker. The accepted dry
    # brief is authoritative, so the locked umbrella recipe must still stay out.
    state = _replay_state(_umbrella_recipe())
    settings = _write_replay_runtime(tmp_path, state)
    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)

    result = record_mod._record_published_recipe(_LogStore(), mode="pi_display")

    assert result["status"] == "skipped_suppressed"
    assert ar.published_recipes(settings) == []


def test_record_cli_preview_skips_used_recipe(tmp_path, monkeypatch):
    state = _replay_state(_neutral_recipe(), used=_neutral_recipe(), suppressed=False)
    settings = _write_replay_runtime(tmp_path, state, mode="local_preview")
    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)

    result = record_mod._record_published_recipe(_LogStore(), mode="")

    assert result["status"] == "skipped_preview"
    assert ar.published_recipes(settings) == []


def test_failed_delivery_leaves_no_published_recipe(tmp_path, monkeypatch):
    """A delivery failure produces no hero, so nothing is booked even when the
    state carries a used recipe marker."""
    state = _replay_state(_neutral_recipe(), used=_neutral_recipe(), suppressed=False)
    settings = _write_replay_runtime(tmp_path, state)
    Path(settings["runtime"]["hero_file"]).unlink()
    monkeypatch.setattr(record_mod, "load_settings", lambda: settings)

    result = record_mod._record_published_recipe(_LogStore(), mode="pi_display")

    assert result["status"] == "skipped"
    assert ar.published_recipes(settings) == []


# ---------------------------------------------------------------------------
# Issue #21: append-only corrections for false published-recipe rows
# ---------------------------------------------------------------------------

LEGACY_LEDGER_FIXTURE = (
    Path(__file__).parent / "fixtures" / "issue21_archived_pi_ledger.jsonl"
)
GENUINE_UMBRELLA_SHA = (
    "2c965e74bbacd043e502c8bf8e32230f98a31bd885eea904c8e624f2f283c602"
)
FALSE_UMBRELLA_SHAS = (
    "f0fd7abfb63127778631da754b9d0beaeb6471dea765aed5d51475ef20229bbb",
    "4b5c5a585e69eb86b16609e372fb76ce120a5c597b551b6574beaeb837ac23de",
)
UMBRELLA_RECIPE_ID = "4e9fd1194606ca7a580e"
CORRECTED_AT = "2026-10-07T00:00:00+00:00"


def _legacy_ledger_settings(tmp_path):
    ledger = tmp_path / "art_recipe_ledger.jsonl"
    shutil.copy(LEGACY_LEDGER_FIXTURE, ledger)
    settings = {
        "runtime": {"art_recipe_ledger_file": str(ledger)},
        "art_variety": {
            "lookback_days": 14,
            "motif_cooldown_days": 7,
            "min_dimension_differences": 2,
        },
    }
    return settings, ledger


def _raw_events(ledger):
    return [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_issue21_fixture_corrections_retire_only_the_false_umbrella_rows(tmp_path):
    settings, ledger = _legacy_ledger_settings(tmp_path)
    assert len(ar.published_recipes(settings)) == 5

    results = ar.correct_legacy_suppressed_publications(
        settings, corrected_at=CORRECTED_AT
    )

    assert [result["status"] for result in results] == ["corrected", "corrected"]
    assert {result["artwork_sha256"] for result in results} == set(FALSE_UMBRELLA_SHAS)
    assert all(result["issue"] == "#21" for result in results)
    assert all(result["reason"] for result in results)

    # Append-only: the original publication rows and the correction rows all
    # remain in the raw ledger.
    raw = _raw_events(ledger)
    assert len(raw) == 7
    assert sum(event["event"] == ar.RECIPE_EVENT for event in raw) == 5
    correction_rows = [
        event for event in raw if event["event"] == ar.RECIPE_CORRECTION_EVENT
    ]
    assert len(correction_rows) == 2
    assert {event["artwork_sha256"] for event in correction_rows} == set(
        FALSE_UMBRELLA_SHAS
    )

    published = ar.published_recipes(settings)
    hashes = {event["artwork_sha256"] for event in published}
    assert len(published) == 3
    assert GENUINE_UMBRELLA_SHA in hashes
    assert set(FALSE_UMBRELLA_SHAS).isdisjoint(hashes)
    # The genuine Oct 5 umbrella publication keeps the shared recipe id visible;
    # the correction is keyed on artwork hash, not recipe id.
    genuine = [
        event for event in published if event["artwork_sha256"] == GENUINE_UMBRELLA_SHA
    ]
    assert genuine[0]["recipe_id"] == UMBRELLA_RECIPE_ID


def test_issue21_corrected_rows_do_not_feed_readers_or_selection(tmp_path):
    settings, _ = _legacy_ledger_settings(tmp_path)
    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    before_ids = [
        event["recipe_id"]
        for event in ar.recent_published_recipes(settings, now=now, days=14)
    ]
    assert before_ids.count(UMBRELLA_RECIPE_ID) == 3

    ar.correct_legacy_suppressed_publications(settings, corrected_at=CORRECTED_AT)

    recent = ar.recent_published_recipes(settings, now=now, days=14)
    assert [event["recipe_id"] for event in recent].count(UMBRELLA_RECIPE_ID) == 1
    assert {event["artwork_sha256"] for event in recent}.isdisjoint(FALSE_UMBRELLA_SHAS)

    summary = ar.history_summary(settings, now=now)
    assert summary["count"] == 3
    assert [entry["recipe_id"] for entry in summary["recent_recipes"]].count(
        UMBRELLA_RECIPE_ID
    ) == 1
    # The API/authoring summary ignores corrected rows entirely: the false
    # artwork hashes never appear in it.
    summary_text = json.dumps(summary)
    for false_sha in FALSE_UMBRELLA_SHAS:
        assert false_sha not in summary_text

    # Cooldown/recent scoring sees one real umbrella publication, not the
    # two phantoms that used to share its recipe id.
    advisory = ar.evaluate_recipe(
        settings,
        {"subject": "umbrella_crowd", "motifs": ["umbrellas"]},
        now=now,
    )
    assert advisory["in_motif_cooldown"] is True
    assert advisory["conflicting_recipe_ids"] == [UMBRELLA_RECIPE_ID]

    # The scheduled selector's recent ids reflect the same corrected view.
    chosen = ar.choose_recipe(
        settings,
        target_date="2026-10-07",
        daypart_role="morning",
        payload={"brief": {"headline": "Dry and clear", "illustration_prompt": "dry streets"}},
        now=now,
    )
    assert chosen is not None
    assert chosen["recent_recipe_ids"].count(UMBRELLA_RECIPE_ID) == 1


def test_issue21_corrections_are_idempotent_and_unambiguous(tmp_path):
    settings, ledger = _legacy_ledger_settings(tmp_path)
    first = ar.correct_legacy_suppressed_publications(settings, corrected_at=CORRECTED_AT)
    assert [result["status"] for result in first] == ["corrected", "corrected"]
    rows_after_first = len(_raw_events(ledger))

    second = ar.correct_legacy_suppressed_publications(settings, corrected_at=CORRECTED_AT)
    assert [result["status"] for result in second] == [
        "already_corrected",
        "already_corrected",
    ]
    assert len(_raw_events(ledger)) == rows_after_first

    correction_rows = ar.corrections(settings)
    assert len(correction_rows) == 2
    assert len({event["artwork_sha256"] for event in correction_rows}) == 2


def test_issue21_generic_correction_requires_a_real_publication_row(tmp_path):
    settings, ledger = _legacy_ledger_settings(tmp_path)

    unknown = ar.correct_publication(
        settings,
        artwork_sha256="a" * 64,
        reason="there is no such published row",
        corrected_at=CORRECTED_AT,
    )
    assert unknown["status"] == "skipped_unknown_artwork"
    assert len(_raw_events(ledger)) == 5

    corrected = ar.correct_publication(
        settings,
        artwork_sha256=FALSE_UMBRELLA_SHAS[0],
        reason="phantom row from a suppressed recipe",
        corrected_at=CORRECTED_AT,
    )
    assert corrected["status"] == "corrected"
    assert corrected["issue"] == "#21"
    assert len(_raw_events(ledger)) == 6
    assert len(ar.published_recipes(settings)) == 4

    repeat = ar.correct_publication(
        settings,
        artwork_sha256=FALSE_UMBRELLA_SHAS[0],
        reason="duplicate attempt must not append",
    )
    assert repeat["status"] == "already_corrected"
    assert len(_raw_events(ledger)) == 6


def test_issue21_cli_appends_known_corrections_idempotently(tmp_path, monkeypatch):
    from scripts.history import correct_recipe_publication as cli

    settings, ledger = _legacy_ledger_settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    argv = [
        "--known-suppressed-dry-oct6",
        "--corrected-at",
        CORRECTED_AT,
    ]
    assert cli.main(argv) == 0
    assert len(_raw_events(ledger)) == 7
    assert len(ar.published_recipes(settings)) == 3

    # A second rollout is a successful no-op and does not duplicate corrections.
    assert cli.main(argv) == 0
    assert len(_raw_events(ledger)) == 7
    assert len(ar.published_recipes(settings)) == 3
    assert len(ar.corrections(settings)) == 2


# ---------------------------------------------------------------------------
# Issue #27: subject/setting compatibility is enforced before novelty scoring
# ---------------------------------------------------------------------------


def _waterside_lane_recipe():
    return {
        "subject": "waterside_figure",
        "subject_text": "a lone figure at the water's edge",
        "motif": "fishing rod",
        "motifs": ["fishing rod"],
        "setting": "residential_lane",
        "setting_text": "a narrow residential lane",
        "viewpoint": "low_angle",
        "viewpoint_text": "low angle looking up",
        "composition": "repeating_rhythm",
        "composition_text": "repeating forms that create a bold visual rhythm",
    }


def test_waterside_figure_only_pairs_with_coherent_water_settings():
    candidates = ar.build_candidates(daypart_role="morning", wet=True)
    pairs = {
        (item["tags"]["subject"], item["tags"]["setting"])
        for item in candidates
    }
    assert ("waterside_figure", "residential_lane") not in pairs
    assert {
        setting for subject, setting in pairs if subject == "waterside_figure"
    } == {"riverside_path", "canal_side", "harbor_edge"}

    # The lane remains useful: exclude only the one incompatible subject while
    # retaining the rest of the pool for normal and fallback selection.
    lane_subjects = {
        subject for subject, setting in pairs if setting == "residential_lane"
    }
    assert "waterside_figure" not in lane_subjects
    assert len(lane_subjects) >= 10
    assert {"lone_walker", "cyclist", "dog_walker"}.issubset(lane_subjects)


def test_recipe_scene_compatibility_handles_pool_names_and_free_text():
    assert ar.subject_setting_compatible(
        "waterside_figure", "residential_lane") is False
    for setting in ("riverside_path", "canal_side", "harbor_edge"):
        assert ar.subject_setting_compatible("waterside_figure", setting) is True

    assert ar.recipe_scene_is_compatible(_waterside_lane_recipe()) is False
    assert ar.recipe_scene_is_compatible({
        **_waterside_lane_recipe(),
        "setting": "canal_side",
        "setting_text": "a canal lined with low buildings",
    }) is True

    # Free-text external recipes receive the same protection even without pool
    # names; generic non-waterside recipes are not narrowed.
    assert ar.subject_setting_compatible(
        {"subject_text": "a lone figure at the water's edge", "motifs": ["fishing rod"]},
        {"setting_text": "a narrow residential lane"},
    ) is False
    assert ar.subject_setting_compatible(
        {"subject_text": "a lone figure at the water's edge", "motifs": ["fishing rod"]},
        {"setting_text": "a harbor edge with stacked crates"},
    ) is True
    assert ar.recipe_scene_is_compatible(_tags()) is True


def test_wet_oct24_replay_never_builds_a_waterside_lane_scene(tmp_path):
    """Rain keeps the archived lane dry-ish, not an invented canal."""
    payload = _wet_payload()
    assert ar._forecast_is_wet(payload) is True
    chosen = ar.choose_recipe(
        _settings(tmp_path),
        target_date="2026-10-24",
        daypart_role="morning",
        payload=payload,
    )
    assert chosen is not None
    tags = chosen["tags"]
    assert not (
        tags["subject"].replace("_", " ") == "waterside figure"
        and tags["setting"].replace("_", " ") == "residential lane"
    )
    assert ar.recipe_scene_is_compatible(chosen) is True
    assert chosen["forecast_weather"] == "wet"

    # Every rain-compatible residential-lane candidate has a lane-compatible
    # subject and does not itself inject fishing or a waterway hazard.
    lane_candidates = [
        item for item in ar.build_candidates(daypart_role="morning", wet=True)
        if item["tags"]["setting"] == "residential_lane"
    ]
    assert lane_candidates
    for item in lane_candidates:
        scene = " ".join([
            item["tags"]["subject"],
            item["tags"]["subject_text"],
            item["tags"]["setting"],
            item["tags"]["setting_text"],
        ]).lower()
        assert "water's edge" not in scene
        assert "fishing rod" not in scene
        assert "canal" not in scene
        assert "flood" not in scene


def test_variety_exhausted_fallback_filters_incompatible_candidates(
        monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    _record(settings, _tags(), sha="a" * 64,
            published_at=_iso(now - timedelta(hours=12)),
            target_date="2026-10-23")

    incompatible = {"tags": _tags(
        subject="waterside_figure",
        subject_text="a lone figure at the water's edge",
        motif="fishing rod",
        motifs=["fishing rod"],
        setting="residential_lane",
        setting_text="a narrow residential lane",
    )}
    compatible = {"tags": _tags(
        subject="cyclist",
        subject_text="a cyclist leaning into the weather",
        motif="bicycle",
        motifs=["bicycle"],
        setting="train_platform",
        setting_text="an open train platform",
    )}
    monkeypatch.setattr(
        ar, "build_candidates", lambda **kwargs: [incompatible, compatible])

    chosen = ar.choose_recipe(
        settings, target_date="2026-10-24",
        daypart_role="morning", now=now,
    )

    assert chosen is not None
    assert chosen["status"] == "variety_exhausted"
    assert chosen["tags"]["subject"] == "cyclist"
    assert ar.recipe_scene_is_compatible(chosen) is True
    assert ar._fallback_candidate([incompatible], []) is None


# ---------------------------------------------------------------------------
# Issue #31: balcony laundry needs a private balcony/clothesline anchor
# ---------------------------------------------------------------------------


def _archived_balcony_riverside_recipe():
    """The exact selection dimensions recorded by recipe 79bc97914d302a625568."""
    return {
        "status": "selected",
        "recipe_id": "79bc97914d302a625568",
        "tags": {
            "subject": "balcony_laundry",
            "subject_text": "laundry lifting on a balcony line",
            "motif": "bedsheets",
            "motifs": ["bedsheets"],
            "setting": "riverside_path",
            "setting_text": "a riverside path",
            "viewpoint": "distant_wide",
            "viewpoint_text": "distant wide establishing shot",
            "composition": "layered_depth",
            "composition_text": (
                "layered foreground, middle distance, and far weather"
            ),
        },
    }


def test_balcony_laundry_candidates_require_private_clothesline_anchor():
    candidates = ar.build_candidates(daypart_role="morning", wet=False)
    pairs = {
        (item["tags"]["subject"], item["tags"]["setting"])
        for item in candidates
    }

    # The archived public-path combination is never constructed.
    assert ("balcony_laundry", "riverside_path") not in pairs
    balcony_candidates = [
        item for item in candidates
        if item["tags"]["subject"] == "balcony_laundry"
    ]
    assert balcony_candidates
    assert {
        item["tags"]["setting"] for item in balcony_candidates
    } == {"apartment_balcony", "balcony_over_riverside_path"}
    assert all(ar.recipe_scene_is_compatible(item) for item in balcony_candidates)

    # The cross-setting option is part of the ordinary pool, and its setting
    # keeps the private balcony as the laundry anchor while naming the view.
    overlook = next(
        item for item in balcony_candidates
        if item["tags"]["setting"] == "balcony_over_riverside_path"
    )
    setting_text = overlook["tags"]["setting_text"].lower()
    assert "private apartment balcony" in setting_text
    assert "overlooking a riverside path" in setting_text
    assert "laundry line anchored to the balcony railing" in setting_text
    assert "balcony kept clearly visible" in setting_text

    # Explicit free-text balcony scenes stay eligible; public paths do not.
    assert ar.subject_setting_compatible(
        {"subject": "balcony_laundry"},
        {"setting_text": "a private balcony above a market alley"},
    ) is True
    assert ar.subject_setting_compatible(
        {"subject": "balcony_laundry"},
        {"setting_text": "a public riverside path with a railing"},
    ) is False

    # Only balcony-dependent pairings are narrowed; the pool remains large and
    # every other setting continues to carry the rest of the subjects.
    assert len(candidates) >= 10_000
    assert len({item["tags"]["subject"] for item in candidates}) >= 12
    assert len({item["tags"]["setting"] for item in candidates}) == len(ar.SETTING_POOL)


def test_archived_balcony_riverside_recipe_is_rejected():
    recipe = _archived_balcony_riverside_recipe()
    assert ar._recipe_id(ar.extract_tags(recipe)) == "79bc97914d302a625568"
    assert ar.recipe_scene_is_compatible(recipe) is False


def test_normal_selection_can_choose_balcony_overlooking_riverside(tmp_path):
    chosen = ar.choose_recipe(
        _settings(tmp_path),
        target_date="2026-02-02",
        daypart_role="morning",
    )

    assert chosen is not None
    assert chosen["status"] == "selected"
    assert chosen["tags"]["subject"] == "balcony_laundry"
    assert chosen["tags"]["setting"] == "balcony_over_riverside_path"
    assert ar.recipe_scene_is_compatible(chosen) is True

    directive = ar.recipe_directive(chosen).lower()
    assert "private apartment balcony overlooking a riverside path" in directive
    assert "laundry line anchored to the balcony railing" in directive
    assert "balcony kept clearly visible" in directive


def test_variety_exhausted_fallback_applies_balcony_anchor_rule(
        monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    _record(settings, _tags(), sha="b" * 64,
            published_at=_iso(now - timedelta(hours=12)),
            target_date="2026-10-23")

    invalid = _archived_balcony_riverside_recipe()
    pool = ar.build_candidates(daypart_role="morning", wet=False)
    valid = next(
        item for item in pool
        if item["tags"]["subject"] == "balcony_laundry"
        and item["tags"]["setting"] == "balcony_over_riverside_path"
        and item["tags"]["viewpoint"] == "eye_level"
        and item["tags"]["composition"] == "cropped_edge"
    )

    # The valid candidate matches the recent recipe's viewpoint/composition,
    # so ordinary eligibility is exhausted and choose_recipe must fall back.
    monkeypatch.setattr(ar, "build_candidates", lambda **kwargs: [invalid, valid])
    chosen = ar.choose_recipe(
        settings, target_date="2026-10-24",
        daypart_role="morning", now=now,
    )

    assert chosen is not None
    assert chosen["status"] == "variety_exhausted"
    assert chosen["tags"]["subject"] == "balcony_laundry"
    assert chosen["tags"]["setting"] == "balcony_over_riverside_path"
    assert ar.recipe_scene_is_compatible(chosen) is True
    assert ar._fallback_candidate([invalid], []) is None
    assert ar._fallback_candidate([invalid, valid], []) is valid
