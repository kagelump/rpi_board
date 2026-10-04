"""Tests for issue #18: shared artwork recipe ledger, motif cooldowns, and the
two-dimension novelty rule."""
import json
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
