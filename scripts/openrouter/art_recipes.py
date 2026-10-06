#!/usr/bin/env python3
"""Shared published-artwork recipe ledger, motif cooldowns, and local selection.

Issue #18: the board kept republishing the same visual *recipe* -- subject,
setting, viewpoint and composition -- even while the style and palette rotated.
This module adds a small, local, deterministic policy so new artwork differs in
at least two meaningful dimensions (subject/motif **and**
viewpoint/composition) before an image is generated. It deliberately makes no
extra image-generation, vision, or embedding calls.

The ledger records only recipes for artwork that was **actually delivered to the
panel**. Preview renders, failed deliveries, and repeated delivery of the same
artwork bytes never enter it as a new recipe: the same ``artwork_sha256`` is
recorded as a reuse event instead, so it cannot reset a motif cooldown.

Everything here is pure local bookkeeping. The only shared state is the
append-only JSON-lines ledger under the configured runtime directory.

Issue #21: a mistaken ``recipe_published`` row is never edited or deleted.
An appended ``recipe_publication_corrected`` row keyed by the exact
``artwork_sha256`` retires it from every reader while the original row stays
in the ledger for audit.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.common import absolute_path, load_settings, utc_now_iso

RECIPE_SCHEMA_VERSION = 1
RECIPE_EVENT = "recipe_published"
RECIPE_REUSE_EVENT = "recipe_reused"
# Issue #21: append-only correction row that retires a falsely recorded
# ``recipe_published`` entry without editing or deleting the original.
RECIPE_CORRECTION_EVENT = "recipe_publication_corrected"

DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_MOTIF_COOLDOWN_DAYS = 7
DEFAULT_MIN_DIMENSION_DIFFERENCES = 2
DEFAULT_LEDGER_FILE = "runtime/art_recipe_ledger.jsonl"

PUBLISHED_MODE = "pi_display"

# Dimensions the novelty rule compares. Style and dominant ink are recorded but
# deliberately excluded from the *gate*: rotating style or palette alone must not
# establish novelty (issue #18).
SUBJECT_DIMENSIONS = ("subject", "motif")
VIEWPOINT_DIMENSIONS = ("viewpoint", "composition")
SELECTION_DIMENSIONS = ("subject", "motif", "setting", "viewpoint", "composition")
RECORDED_DIMENSIONS = SELECTION_DIMENSIONS + ("style", "dominant_ink")

# Issue #21: two ``recipe_published`` rows were booked *after* successful
# dry 2026-10-06 deliveries even though the selected wet umbrella recipe had
# been suppressed before generation and none of its staging reached the
# hero. They stay in the raw ledger for audit but are corrected (append-only,
# keyed by exact artwork hash) so no reader counts them as visual history.
LEGACY_SUPPRESSED_PUBLICATION_CORRECTIONS = (
    {
        "artwork_sha256": (
            "f0fd7abfb63127778631da754b9d0beaeb6471dea765aed5d51475ef20229bbb"
        ),
        "reason": (
            "False publication from the dry 2026-10-06 08:01 JST delivery "
            "(run 7dacc9c3bf8042068068afd10d91d8f9): the wet umbrella recipe "
            "was suppressed before generation and absent from the hero."
        ),
    },
    {
        "artwork_sha256": (
            "4b5c5a585e69eb86b16609e372fb76ce120a5c597b551b6574beaeb837ac23de"
        ),
        "reason": (
            "False publication from the dry 2026-10-06 13:01 JST delivery "
            "(run c092b9fd53bd4586a941187259a9d7c6): the wet umbrella recipe "
            "was suppressed before generation and absent from the hero."
        ),
    },
)

# Local vocabularies. They are weather-agnostic and colour-free; the image
# template still enforces the four-ink palette. ``time`` keeps night subjects out
# of daytime runs (and vice versa) and ``weather`` classifies each subject's
# rain compatibility so the selector never stages rain on a dry day (or dry-only
# scenes on a wet one): ``wet`` subjects are eligible only for wet forecasts,
# ``dry`` subjects only for dry forecasts, and ``neutral`` subjects for both.
SUBJECT_POOL = [
    {"name": "lone_walker", "subject": "a lone pedestrian walking away from the viewer",
     "motifs": ["small red bag"], "time": "any", "weather": "neutral"},
    {"name": "umbrella_crowd", "subject": "a small cluster of commuters under umbrellas",
     "motifs": ["umbrellas"], "time": "any", "weather": "wet"},
    {"name": "cyclist", "subject": "a cyclist leaning into the weather",
     "motifs": ["bicycle"], "time": "any", "weather": "neutral"},
    {"name": "street_vendor", "subject": "a street vendor beside a small cart",
     "motifs": ["steam"], "time": "day", "weather": "neutral"},
    {"name": "delivery_rider", "subject": "a delivery rider pausing at a corner",
     "motifs": ["insulated box"], "time": "any", "weather": "neutral"},
    {"name": "bird_flock", "subject": "a flock of birds turning against the sky",
     "motifs": ["birds"], "time": "any", "weather": "neutral"},
    {"name": "window_cat", "subject": "a cat watching from a window ledge",
     "motifs": ["potted plant"], "time": "any", "weather": "neutral"},
    {"name": "balcony_laundry", "subject": "laundry lifting on a balcony line",
     "motifs": ["bedsheets"], "time": "any", "weather": "dry"},
    {"name": "train_bridge", "subject": "a train crossing a bridge",
     "motifs": ["railing"], "time": "any", "weather": "neutral"},
    {"name": "waterside_figure", "subject": "a lone figure at the water's edge",
     "motifs": ["fishing rod"], "time": "any", "weather": "neutral"},
    {"name": "dog_walker", "subject": "a person walking a small dog",
     "motifs": ["leash"], "time": "any", "weather": "neutral"},
    {"name": "lantern_street", "subject": "paper lanterns strung over a street",
     "motifs": ["lanterns"], "time": "night", "weather": "neutral"},
    {"name": "gardener", "subject": "a gardener tending a few plants",
     "motifs": ["watering can"], "time": "day", "weather": "neutral"},
    {"name": "sleeping_cat", "subject": "a cat asleep on a warm ledge",
     "motifs": ["tail"], "time": "any", "weather": "neutral"},
    {"name": "school_children", "subject": "children hurrying through a school gate",
     "motifs": ["backpacks"], "time": "day", "weather": "neutral"},
    {"name": "window_worker", "subject": "a worker seen through a rain-streaked window",
     "motifs": ["desk lamp"], "time": "any", "weather": "wet"},
]

SETTING_POOL = [
    {"name": "residential_lane", "text": "a narrow residential lane", "time": "any"},
    {"name": "riverside_path", "text": "a riverside path", "time": "any"},
    {"name": "train_platform", "text": "an open train platform", "time": "any"},
    {"name": "rooftop_terrace", "text": "a rooftop terrace", "time": "any"},
    {"name": "corner_store", "text": "a corner convenience store", "time": "any"},
    {"name": "neighborhood_park", "text": "a small neighborhood park", "time": "day"},
    {"name": "under_overpass", "text": "under a concrete overpass", "time": "any"},
    {"name": "subway_entrance", "text": "a subway entrance", "time": "any"},
    {"name": "apartment_balcony", "text": "an apartment balcony", "time": "any"},
    {"name": "market_alley", "text": "a covered market alley", "time": "any"},
    {"name": "canal_side", "text": "a canal lined with low buildings", "time": "any"},
    {"name": "shrine_steps", "text": "the steps of a small shrine", "time": "any"},
    {"name": "empty_parking_lot", "text": "a nearly empty parking lot", "time": "any"},
    {"name": "bus_stop", "text": "a roadside bus stop", "time": "any"},
    {"name": "school_gate", "text": "a school gate", "time": "day"},
    {"name": "harbor_edge", "text": "a harbor edge with stacked crates", "time": "any"},
]

VIEWPOINT_POOL = [
    {"name": "eye_level", "text": "eye-level medium shot"},
    {"name": "high_angle", "text": "high angle looking down"},
    {"name": "low_angle", "text": "low angle looking up"},
    {"name": "distant_wide", "text": "distant wide establishing shot"},
    {"name": "over_shoulder", "text": "over-the-shoulder view"},
    {"name": "close_crop", "text": "tight crop on the main motif"},
    {"name": "flat_frontal", "text": "flat frontal view"},
]

COMPOSITION_POOL = [
    {"name": "cropped_edge", "text": "subject cropped by the frame edge"},
    {"name": "low_horizon_pano", "text": "wide panorama with a very low horizon"},
    {"name": "corner_diagonal", "text": "strong diagonal movement across the frame"},
    {"name": "layered_depth", "text": "layered foreground, middle distance, and far weather"},
    {"name": "tiny_focal", "text": "tiny focal subject surrounded by purposeful negative space"},
    {"name": "edge_weighted", "text": "asymmetrical composition weighted to one edge"},
    {"name": "overhead", "text": "overhead view looking down across the scene"},
    {"name": "upward", "text": "upward view through the scene toward the sky"},
    {"name": "repeating_rhythm", "text": "repeating forms that create a bold visual rhythm"},
    {"name": "emblem", "text": "near-symmetrical emblem with one deliberate disruption"},
]

# Small alias table so free-text motifs from an external author/Codex update map
# onto the local vocabulary used by scheduled cooldown checks.
_MOTIF_ALIASES = (
    ("red bag", "small red bag"),
    ("red handbag", "small red bag"),
    ("handbag", "small red bag"),
    ("umbrella", "umbrellas"),
    ("bicycle", "bicycle"),
    ("bike", "bicycle"),
    ("bird", "birds"),
    ("lantern", "lanterns"),
    ("watering can", "watering can"),
    ("backpack", "backpacks"),
    ("bedsheet", "bedsheets"),
    ("laundry", "bedsheets"),
)

_SUBJECT_KEYWORDS = {
    "lone_walker": ("walker", "pedestrian", "walking away", "lone figure"),
    "umbrella_crowd": ("crowd", "commuter", "umbrella"),
    "cyclist": ("cyclist", "bicycl", "bike"),
    "street_vendor": ("vendor", "cart", "stall"),
    "delivery_rider": ("delivery rider", "courier", "scooter"),
    "bird_flock": ("birds", "flock"),
    "window_cat": ("cat", "window ledge"),
    "balcony_laundry": ("laundry", "bedsheet"),
    "train_bridge": ("train", "bridge"),
    "waterside_figure": ("water's edge", "fisher", "fishing"),
    "dog_walker": ("dog", "leash"),
    "lantern_street": ("lantern",),
    "gardener": ("garden", "watering"),
    "sleeping_cat": ("cat asleep", "sleeping cat"),
    "school_children": ("school", "children"),
    "window_worker": ("worker", "desk"),
}
_SETTING_KEYWORDS = {
    "residential_lane": ("residential lane", "narrow lane", "alley"),
    "riverside_path": ("riverside", "river"),
    "train_platform": ("platform", "station"),
    "rooftop_terrace": ("rooftop", "roof"),
    "corner_store": ("convenience store", "corner store"),
    "neighborhood_park": ("park",),
    "under_overpass": ("overpass", "underpass"),
    "subway_entrance": ("subway", "metro entrance"),
    "apartment_balcony": ("balcony",),
    "market_alley": ("market",),
    "canal_side": ("canal",),
    "shrine_steps": ("shrine", "temple"),
    "empty_parking_lot": ("parking lot", "car park"),
    "bus_stop": ("bus stop",),
    "school_gate": ("school gate",),
    "harbor_edge": ("harbor", "harbour", "dock"),
}
_VIEWPOINT_KEYWORDS = {
    "eye_level": ("eye level", "eye-level"),
    "high_angle": ("high angle", "looking down"),
    "low_angle": ("low angle", "looking up"),
    "distant_wide": ("wide shot", "wide establishing", "distant"),
    "over_shoulder": ("over-the-shoulder", "over the shoulder"),
    "close_crop": ("close-up", "close up", "tight crop"),
    "flat_frontal": ("frontal", "straight on"),
}


# ---------------------------------------------------------------------------
# Configuration / ledger location
# ---------------------------------------------------------------------------


def resolve_policy(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the resolved art-variety policy with defaults applied."""
    settings = settings if settings is not None else load_settings()
    raw = settings.get("art_variety") or {}
    if not isinstance(raw, dict):
        raw = {}

    def _int(key: str, default: int) -> int:
        try:
            value = int(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    return {
        "enabled": bool(raw.get("enabled", True)),
        "lookback_days": _int("lookback_days", DEFAULT_LOOKBACK_DAYS),
        "motif_cooldown_days": _int("motif_cooldown_days", DEFAULT_MOTIF_COOLDOWN_DAYS),
        "min_dimension_differences": _int(
            "min_dimension_differences", DEFAULT_MIN_DIMENSION_DIFFERENCES
        ),
    }


def ledger_path(settings: dict[str, Any] | None = None) -> Path:
    """Resolve the configured append-only recipe ledger path."""
    settings = settings if settings is not None else load_settings()
    runtime = settings.get("runtime") or {}
    raw = runtime.get("art_recipe_ledger_file")
    if not raw:
        raw = (settings.get("art_variety") or {}).get("ledger_file")
    if not raw:
        raw = DEFAULT_LEDGER_FILE
    return absolute_path(str(raw))


# ---------------------------------------------------------------------------
# Canonicalisation helpers
# ---------------------------------------------------------------------------


def _norm_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def canonical_motif(value: Any) -> str:
    text = _norm_text(value)
    for alias, canonical in _MOTIF_ALIASES:
        if alias in text:
            return canonical
    return text


def _canonical_motifs(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    result: list[str] = []
    for value in values:
        canonical = canonical_motif(value)
        if canonical and canonical not in result:
            result.append(canonical)
    return result


def _canonical_subject(value: Any) -> str:
    return _norm_text(value)


def _dimension_value(item: dict[str, Any], dimension: str) -> Any:
    tags = item.get("tags") if isinstance(item.get("tags"), dict) else item
    if dimension == "subject":
        return _canonical_subject(tags.get("subject"))
    if dimension == "motif":
        return tuple(sorted(_canonical_motifs(tags.get("motifs") or tags.get("motif"))))
    return _norm_text(tags.get(dimension))


def _subject_motif_key(item: dict[str, Any]) -> tuple[Any, Any]:
    return (_dimension_value(item, "subject"), _dimension_value(item, "motif"))


def _difference_dimensions(candidate: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    return [
        dimension
        for dimension in SELECTION_DIMENSIONS
        if _dimension_value(candidate, dimension) != _dimension_value(entry, dimension)
    ]


def passes_two_dimension_rule(
    candidate: dict[str, Any], entry: dict[str, Any], policy: dict[str, Any]
) -> bool:
    """True when ``candidate`` is meaningfully different from a published recipe.

    It must differ in at least ``min_dimension_differences`` dimensions and that
    set must include both a subject/motif difference and a viewpoint/composition
    difference. Style and palette changes alone never satisfy this rule.
    """
    differences = _difference_dimensions(candidate, entry)
    if len(differences) < int(policy.get("min_dimension_differences", 2)):
        return False
    has_subject = any(dim in SUBJECT_DIMENSIONS for dim in differences)
    has_viewpoint = any(dim in VIEWPOINT_DIMENSIONS for dim in differences)
    return has_subject and has_viewpoint


def _in_cooldown(candidate: dict[str, Any], entries: list[dict[str, Any]]) -> bool:
    key = _subject_motif_key(candidate)
    return any(_subject_motif_key(entry) == key for entry in entries)


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------


def _append_event(settings: dict[str, Any], event: dict[str, Any]) -> None:
    path = ledger_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with path.open("a+b", buffering=0) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() > 0:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
            handle.write(line)
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_ledger(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read valid ledger events, ignoring a partial trailing line."""
    settings = settings if settings is not None else load_settings()
    path = ledger_path(settings)
    try:
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                for raw_line in handle:
                    try:
                        event = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("schema_version") != RECIPE_SCHEMA_VERSION:
                        continue
                    if event.get("event") not in {
                        RECIPE_EVENT, RECIPE_REUSE_EVENT, RECIPE_CORRECTION_EVENT
                    }:
                        continue
                    events.append(event)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return events
    except OSError:
        # A broken/unreadable ledger must never break generation; the policy
        # simply behaves as if there were no history yet.
        return []


def _corrected_artwork_hashes(events: list[dict[str, Any]]) -> set[str]:
    """Artwork hashes whose published-recipe row has an appended correction."""
    return {
        event["artwork_sha256"]
        for event in events
        if event.get("event") == RECIPE_CORRECTION_EVENT
        and isinstance(event.get("artwork_sha256"), str)
    }


def corrections(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return the append-only publication-correction events (for audit)."""
    return [
        event
        for event in load_ledger(settings)
        if event.get("event") == RECIPE_CORRECTION_EVENT
    ]


def published_recipes(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Published recipes excluding rows retired by an appended correction.

    Corrections are append-only: the mistaken ``recipe_published`` row stays
    in the raw ledger for audit, but it is not artwork that reached the panel,
    so it must not feed cooldowns, novelty scoring, or the published summary.
    """
    events = load_ledger(settings)
    corrected = _corrected_artwork_hashes(events)
    return [
        event
        for event in events
        if event.get("event") == RECIPE_EVENT
        and event.get("artwork_sha256") not in corrected
    ]


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def recent_published_recipes(
    settings: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Published recipes within ``days``, oldest first (deduplicated by recipe)."""
    if days is not None and days <= 0:
        return []
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(days=days) if days is not None else None
    result: list[dict[str, Any]] = []
    for event in published_recipes(settings):
        timestamp = _parse_timestamp(event.get("published_at"))
        if timestamp is None:
            continue
        if cutoff is not None and timestamp < cutoff:
            continue
        result.append(event)
    result.sort(key=lambda item: (str(item.get("published_at") or ""), str(item.get("recipe_id") or "")))
    return result


def evaluate_recipe(
    settings: dict[str, Any] | None,
    recipe: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Score a recipe against the ledger without changing it (local only).

    Used to advise external/Codex authoring: a supplied recipe is reported as
    ``novel``, ``not_novel`` (fails the two-dimension rule vs the latest
    publication) or ``cooldown`` (repeats a recent subject+motif). Recording an
    external recipe never blocks on this -- an urgent update must still publish
    -- but the advisory is stored with the ledger entry.
    """
    settings = settings if settings is not None else load_settings()
    policy = resolve_policy(settings)
    tags = extract_tags(recipe)
    recent = recent_published_recipes(settings, now=now, days=policy["lookback_days"])
    cooldown = recent_published_recipes(settings, now=now, days=policy["motif_cooldown_days"])
    key = _subject_motif_key(tags)
    in_cooldown = any(_subject_motif_key(entry) == key for entry in cooldown)
    conflicts = [entry.get("recipe_id") for entry in cooldown if _subject_motif_key(entry) == key]
    latest = recent[-1] if recent else None
    novel = (
        passes_two_dimension_rule(tags, latest, policy) if latest is not None else True
    )
    if in_cooldown:
        status = "cooldown"
    elif not novel:
        status = "not_novel"
    else:
        status = "novel"
    return {
        "status": status,
        "in_motif_cooldown": in_cooldown,
        "differs_two_dimensions_from_latest": bool(novel),
        "conflicting_recipe_ids": conflicts,
        "latest_recipe_id": latest.get("recipe_id") if latest else None,
        "policy": {
            "lookback_days": policy["lookback_days"],
            "motif_cooldown_days": policy["motif_cooldown_days"],
            "min_dimension_differences": policy["min_dimension_differences"],
        },
        "evaluated_at": utc_now_iso(),
    }


def history_summary(
    settings: dict[str, Any] | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Compact, read-only contract for external/Codex authoring.

    Contains only the local policy and recent structured recipe tags. It never
    includes provider credentials or request bodies.
    """
    settings = settings if settings is not None else load_settings()
    policy = resolve_policy(settings)
    recent = recent_published_recipes(
        settings, now=now, days=policy["lookback_days"]
    )
    return {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "enabled": policy["enabled"],
        "policy": dict(policy),
        "required_dimensions": ["subject/motif", "viewpoint/composition"],
        "recorded_dimensions": list(RECORDED_DIMENSIONS),
        "recent_recipes": [
            {
                "recipe_id": event.get("recipe_id"),
                "published_at": event.get("published_at"),
                "target_date": event.get("target_date"),
                "source": event.get("source"),
                "tags": dict(event.get("tags") or {}),
                "rationale": event.get("rationale"),
                "variety_status": event.get("variety_status"),
            }
            for event in recent
        ],
        "count": len(recent),
        "generated_at": utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# Recipe normalisation / external authoring helpers
# ---------------------------------------------------------------------------


def normalize_recipe(raw: Any) -> dict[str, Any]:
    """Coerce an arbitrary recipe mapping into the recorded tag shape."""
    if not isinstance(raw, dict):
        raw = {}
    subject = raw.get("subject")
    if isinstance(subject, dict):
        subject = subject.get("name")
    motifs = _canonical_motifs(
        raw.get("motifs") if raw.get("motifs") is not None else raw.get("motif")
    )
    settings = raw.get("setting")
    if isinstance(settings, dict):
        settings = settings.get("name")
    viewpoint = raw.get("viewpoint")
    if isinstance(viewpoint, dict):
        viewpoint = viewpoint.get("name")
    composition = raw.get("composition")
    if isinstance(composition, dict):
        composition = composition.get("name")
    return {
        "subject": _canonical_subject(subject) or None,
        "subject_text": _norm_text(raw.get("subject_text")) or None,
        "motif": motifs[0] if motifs else None,
        "motifs": motifs,
        "setting": _norm_text(settings) or None,
        "setting_text": _norm_text(raw.get("setting_text")) or None,
        "viewpoint": _norm_text(viewpoint) or None,
        "viewpoint_text": _norm_text(raw.get("viewpoint_text")) or None,
        "composition": _norm_text(composition) or None,
        "composition_text": _norm_text(raw.get("composition_text")) or None,
        "style": _norm_text(raw.get("style")) or None,
        "dominant_ink": _norm_text(raw.get("dominant_ink") or raw.get("palette")) or None,
    }


def extract_tags(recipe: Any) -> dict[str, Any]:
    """Return the recorded tag mapping from a selector result or plain mapping."""
    if isinstance(recipe, dict) and isinstance(recipe.get("tags"), dict):
        return normalize_recipe(recipe["tags"])
    return normalize_recipe(recipe)


def _keyword_lookup(text: str, table: dict[str, tuple[str, ...]]) -> str | None:
    lowered = (text or "").lower()
    best: tuple[int, str] | None = None
    for name, keywords in table.items():
        for keyword in keywords:
            if keyword in lowered:
                match = (len(keyword), name)
                if best is None or match[0] > best[0]:
                    best = match
    return best[1] if best else None


def derive_recipe_from_update(
    payload: dict[str, Any] | None, metadata: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    """Best-effort local recipe tags for an external publication (no vision calls).

    ``metadata["recipe"]`` (or flat metadata tag fields) wins. Otherwise tags are
    inferred only from the supplied text fields with a keyword lookup, so an
    external publication can still influence scheduled direction.
    """
    payload = payload or {}
    metadata = metadata or {}
    supplied = metadata.get("recipe") if isinstance(metadata.get("recipe"), dict) else None
    source = "metadata"
    if supplied is None:
        flat = {
            key: metadata.get(key)
            for key in (
                "subject", "subject_text", "motif", "motifs", "setting", "setting_text",
                "viewpoint", "viewpoint_text", "composition", "composition_text",
                "style", "dominant_ink", "palette",
            )
            if metadata.get(key) is not None
        }
        supplied = flat if flat else None

    if supplied is not None:
        tags = normalize_recipe(supplied)
        return tags, source

    prompt = " ".join(
        str(value)
        for value in (
            (payload.get("brief") or {}).get("illustration_prompt"),
            (payload.get("brief") or {}).get("subtitle"),
            metadata.get("notes"),
            metadata.get("description"),
        )
        if value
    )
    subject = _keyword_lookup(prompt, _SUBJECT_KEYWORDS)
    setting = _keyword_lookup(prompt, _SETTING_KEYWORDS)
    viewpoint = _keyword_lookup(prompt, _VIEWPOINT_KEYWORDS)
    tags = normalize_recipe({
        "subject": subject,
        "setting": setting,
        "viewpoint": viewpoint,
        "motif": metadata.get("motif"),
        "style": metadata.get("style") or metadata.get("art_style"),
        "dominant_ink": metadata.get("dominant_ink") or metadata.get("palette"),
    })
    return tags, "derived"


# ---------------------------------------------------------------------------
# Publication bookkeeping
# ---------------------------------------------------------------------------


def _recipe_id(tags: dict[str, Any]) -> str:
    # Identity covers the five selection dimensions only; style and dominant ink
    # are recorded alongside the event but do not make it a *different recipe*.
    identity = {key: tags.get(key) for key in SELECTION_DIMENSIONS}
    identity["motifs"] = list(tags.get("motifs") or [])
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest[:20]


def _is_preview_mode(mode: Any) -> bool:
    return mode is not None and str(mode) != PUBLISHED_MODE


def record_publication(
    settings: dict[str, Any] | None,
    *,
    recipe: Any,
    artwork_sha256: str | None,
    target_date: str | None,
    source: str = "scheduled",
    rationale: str | None = None,
    mode: str | None = None,
    style: str | None = None,
    dominant_ink: str | None = None,
    published_at: str | None = None,
) -> dict[str, Any]:
    """Record a *successfully published* recipe against the shared ledger.

    * A preview/off-panel mode is skipped (no cooldown consumed).
    * A repeated ``artwork_sha256`` is booked as reuse, never a new recipe.
    """
    settings = settings if settings is not None else load_settings()
    policy = resolve_policy(settings)
    if not policy["enabled"]:
        return {"status": "disabled", "reason": "art variety policy disabled"}
    resolved_mode = mode or (settings.get("display") or {}).get("mode")
    if _is_preview_mode(resolved_mode):
        return {
            "status": "skipped_preview",
            "reason": f"display mode {resolved_mode!r} is not a panel publication",
        }
    if not artwork_sha256:
        return {"status": "skipped_no_artwork", "reason": "no artwork hash available"}

    tags = extract_tags(recipe)
    if style:
        tags["style"] = _norm_text(style) or tags.get("style")
    if dominant_ink:
        tags["dominant_ink"] = _norm_text(dominant_ink) or tags.get("dominant_ink")
    if not any(tags.get(key) for key in ("subject", "motif", "setting", "viewpoint", "composition")):
        return {"status": "skipped_empty_recipe", "reason": "recipe had no meaningful tags"}

    existing = published_recipes(settings)
    previous = next(
        (event for event in existing if event.get("artwork_sha256") == artwork_sha256), None
    )
    if previous is not None:
        event = {
            "schema_version": RECIPE_SCHEMA_VERSION,
            "event": RECIPE_REUSE_EVENT,
            "recipe_id": previous.get("recipe_id"),
            "published_at": published_at or utc_now_iso(),
            "target_date": target_date,
            "source": source,
            "artwork_sha256": artwork_sha256,
            "reason": "duplicate artwork bytes; delivered as reuse",
        }
        _append_event(settings, event)
        return {
            "status": "reuse",
            "recipe_id": previous.get("recipe_id"),
            "artwork_sha256": artwork_sha256,
            "tags": dict(previous.get("tags") or {}),
        }

    recipe_id = _recipe_id(tags)
    published_timestamp = published_at or utc_now_iso()
    advisory = evaluate_recipe(settings, tags, now=_parse_timestamp(published_timestamp))
    event = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "event": RECIPE_EVENT,
        "recipe_id": recipe_id,
        "published_at": published_timestamp,
        "target_date": target_date,
        "source": source,
        "artwork_sha256": artwork_sha256,
        "tags": dict(tags),
        "rationale": rationale or "",
        "variety_status": advisory["status"],
        "policy": dict(policy),
    }
    _append_event(settings, event)
    return {
        "status": "published",
        "recipe_id": recipe_id,
        "artwork_sha256": artwork_sha256,
        "tags": dict(tags),
        "source": source,
        "variety_status": advisory["status"],
        "advisory": advisory,
    }


def _append_correction_if_absent(settings: dict[str, Any], event: dict[str, Any]) -> bool:
    """Append a correction unless an equal one already exists.

    The existence check and append share one exclusive lock, so a repeated or
    concurrent rollout cannot insert duplicate correction rows.
    """
    path = ledger_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (
        json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    with path.open("a+b", buffering=0) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            for raw_line in handle.read().splitlines():
                try:
                    existing = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if (
                    isinstance(existing, dict)
                    and existing.get("event") == RECIPE_CORRECTION_EVENT
                    and existing.get("artwork_sha256") == event.get("artwork_sha256")
                ):
                    return False
            handle.seek(0, os.SEEK_END)
            if handle.tell() > 0:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
            handle.write(line)
            os.fsync(handle.fileno())
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _correction_for(
    events: list[dict[str, Any]], artwork_sha256: str
) -> dict[str, Any] | None:
    return next(
        (
            event
            for event in events
            if event.get("event") == RECIPE_CORRECTION_EVENT
            and event.get("artwork_sha256") == artwork_sha256
        ),
        None,
    )


def correct_publication(
    settings: dict[str, Any] | None,
    *,
    artwork_sha256: str,
    reason: str,
    issue: str = "#21",
    corrected_at: str | None = None,
) -> dict[str, Any]:
    """Append an audit correction for a falsely recorded publication.

    The ledger is append-only: the original ``recipe_published`` row is kept and
    a ``recipe_publication_corrected`` row keyed by the exact ``artwork_sha256``
    is appended. Readers then ignore the corrected publication.

    Idempotent: an equivalent correction is never appended twice. A hash with no
    matching ``recipe_published`` row is reported and left untouched.
    """
    settings = settings if settings is not None else load_settings()
    if not artwork_sha256:
        return {"status": "skipped_no_artwork", "reason": "no artwork hash available"}
    events = load_ledger(settings)
    existing = _correction_for(events, artwork_sha256)
    if existing is not None:
        return {
            "status": "already_corrected",
            "artwork_sha256": artwork_sha256,
            "recipe_id": existing.get("recipe_id"),
            "reason": existing.get("reason"),
            "issue": existing.get("issue"),
        }
    publication = next(
        (
            event
            for event in events
            if event.get("event") == RECIPE_EVENT
            and event.get("artwork_sha256") == artwork_sha256
        ),
        None,
    )
    if publication is None:
        return {
            "status": "skipped_unknown_artwork",
            "reason": f"no recipe_published row for artwork {artwork_sha256}",
            "artwork_sha256": artwork_sha256,
        }
    event = {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "event": RECIPE_CORRECTION_EVENT,
        "recipe_id": publication.get("recipe_id"),
        "artwork_sha256": artwork_sha256,
        "corrected_at": corrected_at or utc_now_iso(),
        "issue": issue,
        "reason": reason,
    }
    if not _append_correction_if_absent(settings, event):
        return {
            "status": "already_corrected",
            "artwork_sha256": artwork_sha256,
            "recipe_id": event.get("recipe_id"),
            "reason": reason,
            "issue": issue,
        }
    return {
        "status": "corrected",
        "artwork_sha256": artwork_sha256,
        "recipe_id": event.get("recipe_id"),
        "reason": reason,
        "issue": issue,
    }


def correct_legacy_suppressed_publications(
    settings: dict[str, Any] | None = None,
    *,
    corrected_at: str | None = None,
) -> list[dict[str, Any]]:
    """Apply the two known issue #21 corrections (idempotent)."""
    settings = settings if settings is not None else load_settings()
    return [
        correct_publication(
            settings,
            artwork_sha256=entry["artwork_sha256"],
            reason=entry["reason"],
            issue="#21",
            corrected_at=corrected_at,
        )
        for entry in LEGACY_SUPPRESSED_PUBLICATION_CORRECTIONS
    ]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _is_night(daypart_role: Any) -> bool:
    return str(daypart_role or "").strip().lower() in {"evening", "night"}


def _allows_time(item: dict[str, Any], is_night: bool) -> bool:
    window = item.get("time", "any")
    if window == "any":
        return True
    return window == ("night" if is_night else "day")


def _subject_weather_class(item: dict[str, Any]) -> str:
    """Return ``wet``, ``dry`` or ``neutral`` for a subject-pool entry.

    The explicit ``weather`` field is authoritative; ``dry_only`` is still
    honoured so any externally-authored pool entry with the old shape keeps
    working.
    """
    weather = str(item.get("weather") or "").strip().lower()
    if weather in {"wet", "dry", "neutral"}:
        return weather
    return "dry" if item.get("dry_only") else "neutral"


def _allows_weather(item: dict[str, Any], wet: bool) -> bool:
    """True when a subject is compatible with the forecast's wetness.

    Wet-only subjects are eligible only for wet forecasts, dry-only subjects
    only for dry forecasts, and neutral subjects for both. Filtering runs before
    novelty scoring so the selector can never stage rain on a dry day.
    """
    weather = _subject_weather_class(item)
    if weather == "wet":
        return wet
    if weather == "dry":
        return not wet
    return True


_WET_CONDITION_WORDS = ("rain", "shower", "drizzle", "storm", "snow", "sleet")
_WET_WEATHER_CODES = frozenset({
    51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 71, 73, 75, 77,
    80, 81, 82, 85, 86, 95, 96, 99,
})

# Wording that makes a brief explicitly dry. "no rain" must win over the
# ``rain`` substring when classifying the accepted brief.
_EXPLICIT_DRY_PHRASES = (
    "no rain",
    "without rain",
    "rain-free",
    "rain free",
    "rainless",
    "stays dry",
    "stay dry",
    "staying dry",
    "remains dry",
    "remain dry",
    "and dry",
    "dry day",
    "dry days",
    "dry weather",
    "dry forecast",
    "dry conditions",
    "dry spell",
    "dry spells",
    "dry stretch",
    "dry period",
    "dry morning",
    "dry afternoon",
    "dry evening",
    "dry night",
    "dry today",
    "dry tomorrow",
    "dry throughout",
    "dry all day",
    "mostly dry",
    "largely dry",
    "no showers",
    "no drizzle",
    "won't rain",
    "will not rain",
    "umbrella not needed",
    "umbrellas not needed",
    "no umbrella needed",
    "no need for an umbrella",
    "no need for umbrella",
)


def _is_wet(payload: dict[str, Any] | None) -> bool:
    payload = payload or {}
    daily = (payload.get("today") or {}).get("daily_summary") or {}
    condition = str(daily.get("condition") or "").lower()
    if any(word in condition for word in _WET_CONDITION_WORDS):
        return True
    code = daily.get("weather_code")
    if isinstance(code, int) and code in _WET_WEATHER_CODES:
        return True
    # An explicit dry claim in the brief ("no rain", "stays dry") must not be
    # read as wet just because of the ``rain`` substring.
    if brief_declares_dry(payload):
        return False
    # Fall back to the transformed brief context when available.
    text = " ".join(
        str(value)
        for value in (
            (payload.get("brief") or {}).get("rain_level"),
            (payload.get("brief_context") or {}).get("summary"),
        )
        if value
    ).lower()
    return any(word in text for word in _WET_CONDITION_WORDS)


def brief_weather_text(payload: dict[str, Any] | None) -> str:
    """Flatten the accepted brief's user-visible weather wording."""
    payload = payload or {}
    brief = payload.get("brief") if isinstance(payload.get("brief"), dict) else {}
    context = payload.get("brief_context") if isinstance(payload.get("brief_context"), dict) else {}
    parts: list[Any] = [
        brief.get("headline"),
        brief.get("subtitle"),
        brief.get("illustration_prompt"),
        brief.get("event_ref"),
        context.get("summary"),
    ]
    bullets = brief.get("bullets")
    if isinstance(bullets, list):
        parts.extend(bullets)
    return _norm_text(" ".join(str(part) for part in parts if part))


def brief_declares_dry(payload: dict[str, Any] | None) -> bool:
    """True when the accepted brief explicitly tells the user it stays dry."""
    text = brief_weather_text(payload)
    if not text:
        return False
    return any(phrase in text for phrase in _EXPLICIT_DRY_PHRASES)


def _forecast_is_wet(payload: dict[str, Any] | None) -> bool:
    """Whether the accepted forecast/brief supports rain staging.

    The brief's explicit dry meaning is authoritative: a "stays dry" / "no rain"
    brief is never treated as wet, so rain-specific recipes stay ineligible.
    """
    if brief_declares_dry(payload):
        return False
    return _is_wet(payload)


_RECIPE_WET_KEYWORDS = (
    "umbrella", "rain", "drizzle", "shower", "storm", "sleet", "snow", "wet",
    "puddle", "downpour", "raincoat", "gumboot", "waterproof",
)
_RECIPE_DRY_KEYWORDS = ("laundry", "bedsheet", "line-dried", "sun-dried")


def _subject_pool_weather(subject: Any) -> str | None:
    target = _norm_text(subject)
    if not target:
        return None
    for item in SUBJECT_POOL:
        if _norm_text(item["name"]) == target:
            return _subject_weather_class(item)
    return None


def recipe_weather_class(recipe: Any) -> str:
    """Classify a recipe's staging weather as ``wet``, ``dry`` or ``neutral``.

    The known subject pool is authoritative; for external/free-text recipes the
    recorded subject/motif wording is inspected with a small, deterministic
    keyword table so umbrella/rain staging can never be treated as neutral.
    """
    tags = extract_tags(recipe)
    pool_class = _subject_pool_weather(tags.get("subject"))
    if pool_class in {"wet", "dry"}:
        return pool_class
    parts: list[Any] = [
        tags.get("subject"),
        tags.get("subject_text"),
        tags.get("setting"),
        tags.get("setting_text"),
    ]
    motifs = tags.get("motifs")
    if isinstance(motifs, list):
        parts.extend(motifs)
    text = _norm_text(" ".join(str(part) for part in parts if part))
    if any(keyword in text for keyword in _RECIPE_WET_KEYWORDS):
        return "wet"
    if any(keyword in text for keyword in _RECIPE_DRY_KEYWORDS):
        return "dry"
    return "neutral"


def recipe_conflicts_with_brief(
    recipe: Any, payload: dict[str, Any] | None
) -> bool:
    """True when injecting ``recipe`` staging would contradict the forecast.

    Rain-specific staging is invalid on a dry forecast and dry-only staging is
    invalid on a wet one. Neutral recipes never conflict, and the accepted
    brief's explicit dry claim always wins over a loose wet heuristic.
    """
    weather = recipe_weather_class(recipe)
    if weather == "neutral":
        return False
    forecast_wet = _forecast_is_wet(payload)
    if weather == "wet":
        return not forecast_wet
    return forecast_wet


def build_candidates(
    *, daypart_role: Any = None, wet: bool = False
) -> list[dict[str, Any]]:
    """Product of weather-compatible subject/setting/viewpoint/composition pools."""
    is_night = _is_night(daypart_role)
    subjects = [
        item
        for item in SUBJECT_POOL
        if _allows_time(item, is_night) and _allows_weather(item, wet)
    ]
    settings = [item for item in SETTING_POOL if _allows_time(item, is_night)]
    viewpoints = [item for item in VIEWPOINT_POOL]
    compositions = [item for item in COMPOSITION_POOL]
    candidates: list[dict[str, Any]] = []
    for subject in subjects:
        for setting in settings:
            for viewpoint in viewpoints:
                for composition in compositions:
                    tags = {
                        "subject": subject["name"],
                        "subject_text": subject["subject"],
                        "motif": subject["motifs"][0] if subject["motifs"] else None,
                        "motifs": list(subject["motifs"]),
                        "setting": setting["name"],
                        "setting_text": setting["text"],
                        "viewpoint": viewpoint["name"],
                        "viewpoint_text": viewpoint["text"],
                        "composition": composition["name"],
                        "composition_text": composition["text"],
                    }
                    candidates.append({"tags": tags})
    return candidates


def _selection_seed(target_date: str | None) -> int:
    basis = str(target_date or "")
    return int(hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12], 16)


def recipe_directive(recipe: dict[str, Any]) -> str:
    """Human-readable, colour-free image-prompt addendum for a chosen recipe."""
    tags = extract_tags(recipe)
    motif = ", ".join(tags.get("motifs") or []) or "no distinct motif"
    return (
        "Artwork recipe for this forecast day. The weather, light and emotional "
        "beat described in SUBJECT remain authoritative and must not be "
        "contradicted. Within that weather, stage the scene with this "
        "subject/motif, setting, viewpoint and composition (these control staging "
        "and framing only; keep the four-ink e-ink constraints below):\n"
        f"- Subject and motif: {tags.get('subject_text') or tags.get('subject') or 'unspecified'} "
        f"(motif: {motif})\n"
        f"- Setting: {tags.get('setting_text') or tags.get('setting') or 'unspecified'}\n"
        f"- Viewpoint / camera distance: {tags.get('viewpoint_text') or tags.get('viewpoint') or 'unspecified'}\n"
        f"- Composition: {tags.get('composition_text') or tags.get('composition') or 'unspecified'}"
    )


def _score_candidate(
    candidate: dict[str, Any], recent: list[dict[str, Any]], policy: dict[str, Any]
) -> float:
    score = 0.0
    candidate_subject = _dimension_value(candidate, "subject")
    candidate_setting = _dimension_value(candidate, "setting")
    candidate_key = _subject_motif_key(candidate)
    for entry in recent:
        differences = _difference_dimensions(candidate, entry)
        score += len(differences)
        if candidate_subject and candidate_subject == _dimension_value(entry, "subject"):
            score -= 3.0
        if candidate_setting and candidate_setting == _dimension_value(entry, "setting"):
            score -= 2.0
        if _subject_motif_key(entry) == candidate_key:
            score -= 6.0
        if not passes_two_dimension_rule(candidate, entry, policy):
            score -= 2.0
    if recent and passes_two_dimension_rule(candidate, recent[-1], policy):
        score += 3.0
    return score


def _fallback_candidate(
    candidates: list[dict[str, Any]], recent: list[dict[str, Any]]
) -> dict[str, Any]:
    """Pick the most-different available recipe rather than blocking an update."""
    def key(candidate: dict[str, Any]) -> int:
        if not recent:
            return 0
        return min(len(_difference_dimensions(candidate, entry)) for entry in recent)

    return max(candidates, key=key)


def choose_recipe(
    settings: dict[str, Any] | None = None,
    *,
    target_date: str | None,
    daypart_role: Any = None,
    payload: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Choose a weather-compatible recipe that is novel versus recent publishing.

    Returns ``None`` when the policy is disabled or no candidate exists. When
    every candidate is blocked by the motif cooldown and/or the two-dimension
    rule, it returns the most-different recipe with ``status="variety_exhausted"``
    and a documented rationale instead of raising, so an urgent update is never
    blocked.
    """
    settings = settings if settings is not None else load_settings()
    policy = resolve_policy(settings)
    if not policy["enabled"]:
        return None

    recent = recent_published_recipes(
        settings, now=now, days=policy["lookback_days"]
    )
    cooldown = recent_published_recipes(
        settings, now=now, days=policy["motif_cooldown_days"]
    )
    forecast_wet = _forecast_is_wet(payload)
    candidates = build_candidates(daypart_role=daypart_role, wet=forecast_wet)
    if not candidates:
        # No weather-compatible subject exists: return ``None`` so the caller
        # keeps the accepted brief's original SUBJECT instead of forcing a
        # contradictory recipe.
        return None

    rng = random.Random(_selection_seed(target_date))
    rng.shuffle(candidates)

    eligible: list[dict[str, Any]] = []
    for candidate in candidates:
        if _in_cooldown(candidate, cooldown):
            continue
        if recent and not passes_two_dimension_rule(candidate, recent[-1], policy):
            continue
        eligible.append(candidate)

    if eligible:
        chosen = max(eligible, key=lambda candidate: _score_candidate(candidate, recent, policy))
        status = "selected"
        score = _score_candidate(chosen, recent, policy)
        forecast_name = "wet" if forecast_wet else "dry"
        chosen_weather = recipe_weather_class(chosen["tags"])
        rationale = (
            f"novelty score {score:.1f} vs {len(recent)} recent recipe(s); "
            f"weather-compatible for a {forecast_name} forecast ({chosen_weather} "
            f"staging); subject/motif and viewpoint/composition both differ from "
            f"the latest publication"
        )
    else:
        chosen = _fallback_candidate(candidates, recent)
        status = "variety_exhausted"
        rationale = (
            "variety_exhausted: every weather-compatible local candidate was "
            "blocked by the motif cooldown or the two-dimension rule; chose the "
            "most-different available subject/setting/viewpoint so an urgent "
            "forecast update is not blocked"
        )

    return {
        "schema_version": RECIPE_SCHEMA_VERSION,
        "recipe_id": _recipe_id(extract_tags(chosen["tags"])),
        "status": status,
        "rationale": rationale,
        "target_date": target_date,
        "selected_at": utc_now_iso(),
        "tags": dict(chosen["tags"]),
        "forecast_weather": "wet" if forecast_wet else "dry",
        "recipe_weather": recipe_weather_class(chosen["tags"]),
        "policy": {
            "lookback_days": policy["lookback_days"],
            "motif_cooldown_days": policy["motif_cooldown_days"],
            "min_dimension_differences": policy["min_dimension_differences"],
        },
        "recent_recipe_ids": [event.get("recipe_id") for event in recent],
    }
