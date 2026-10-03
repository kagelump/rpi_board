#!/usr/bin/env python3
import argparse
import base64
import hashlib
import json
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, absolute_path, get_fal_api_key, get_openrouter_api_key, load_settings, read_json, utc_now_iso, write_json
from scripts.history.store import record_current_log, record_current_snapshot
from scripts.openrouter.network import describe_network_error, urlopen_with_context
from scripts.openrouter.art_guardrail import inspect_art
from scripts.render.palette_metrics import analyze as analyze_palette


ART_STYLE_POOL = [
    {
        "name": "Bauhaus",
        "prompt": "Bauhaus poster language: strict geometric construction, circles and bars, asymmetric balance, decisive blocks of empty space",
    },
    {
        "name": "Constructivism",
        "prompt": "constructivist poster language: forceful diagonals, cropped monumental forms, radiating geometry, urgent visual motion",
    },
    {
        "name": "Pop Art Comic",
        "prompt": "vintage pop-comic treatment: thick contour lines, bold spot shapes, coarse halftone fields, energetic panel-like staging without borders",
    },
    {
        "name": "Minimal Ukiyo-e",
        "prompt": "minimal ukiyo-e woodblock language: flowing contour lines, cropped foreground forms, layered distance, elegant negative space and carved texture",
    },
    {
        "name": "De Stijl",
        "prompt": "De Stijl treatment applied to the WHOLE scene: the entire subject simplified into orthogonal black dividing lines and flat primary color blocks, the forms themselves abstracted into rectilinear shapes -- NOT a realistic image placed inside a Mondrian grid or border",
    },
    {
        "name": "WPA Travel Poster",
        "prompt": "1930s travel-poster treatment: simplified scenic depth, monumental perspective, broad screen-printed masses, atmospheric storytelling",
    },
    {
        "name": "Linocut",
        "prompt": "hand-cut linocut aesthetic: visibly gouged edges, carved white marks inside dark masses, rough ink texture, muscular contours",
    },
    {
        "name": "Stencil Graphic",
        "prompt": "layered stencil graphic: interrupted bridges in shapes, overspray-like dot texture made only from device inks, hard cut edges, bold overlap",
    },
    {
        "name": "Pictogram Minimalism",
        "prompt": "modern pictogram minimalism, icon-like weather motifs, clean vectors, immediate readability",
    },
    {
        "name": "Paper Cutout",
        "prompt": "hand-cut paper collage rendered as one unified illustration: irregular scissor edges, overlapping color silhouettes, playful depth from shape boundaries",
    },
    {
        "name": "Scratchboard Engraving",
        "prompt": "scratchboard engraving: black-dominant field, bright carved strokes, dense directional hatching, dramatic illumination and tactile incised marks",
    },
    {
        "name": "Sumi-e Brush",
        "prompt": "expressive sumi-e brush language adapted to solid inks: sweeping brush silhouettes, dry-brush gaps, calligraphic motion, sparse forceful accents",
    },
    {
        "name": "Risograph",
        "prompt": "risograph print aesthetic: chunky offset ink layers, coarse dot screens, imperfect registration, playful overlaps using only device inks",
    },
    {
        "name": "Silkscreen",
        "prompt": "hand-pulled silkscreen poster: broad ink fields, visible screen texture, slightly imperfect edges, layered shapes with assertive negative space",
    },
    {
        "name": "Retro Pixel Art",
        "prompt": "low-resolution retro pixel art: block clusters, stepped diagonals, crisp pixel silhouettes, limited-scale texture and readable game-scene staging",
    },
    {
        "name": "Stained Glass",
        "prompt": "stained-glass interpretation: thick black leading divides irregular luminous cells, sweeping connected shapes, bold colored sections filling the frame",
    },
    {
        "name": "Editorial Ink",
        "prompt": "editorial ink illustration: loose expressive contour, brushy spot shapes, witty asymmetric visual metaphor, deliberate unfinished gaps",
    },
    {
        "name": "Art Nouveau",
        "prompt": "Art Nouveau graphic language: flowing whiplash curves, organic silhouettes, rhythmic botanical or cloud forms integrated into the entire scene",
    },
    {
        "name": "Pulp Sci-Fi",
        "prompt": "retro pulp science-fiction poster: exaggerated perspective, radial energy, strange monumental weather, dramatic silhouettes and theatrical scale",
    },
    {
        "name": "Folk Print",
        "prompt": "naive folk-print treatment: hand-carved uneven shapes, repeated decorative motifs, flattened perspective, warm narrative energy and visible craft",
    },
    {
        "name": "Op Art",
        "prompt": "op-art weather abstraction: vibrating stripes, repeated high-contrast geometry, optical rhythm bending around recognizable weather and landscape forms",
    },
    {
        "name": "Mosaic",
        "prompt": "graphic mosaic: scene assembled from irregular tile-like pieces separated by strong black joins, clustered color rhythm, no smooth vector surfaces",
    },
    {
        "name": "Noir Shadow",
        "prompt": "noir shadow play: black-dominant composition, oblique viewpoints, sharp beams of light, cropped silhouettes, tense empty space and rain-sliced geometry",
    },
    {
        "name": "Retro Railway Poster",
        "prompt": "mid-century railway-poster language: bold receding perspective, cropped architecture or landscape, simplified speed lines, confident graphic masses",
    },
]


PALETTE_STRATEGY_POOL = [
    {
        "name": "Red Signal",
        "prompt": "Black and white dominate. Use red as the decisive focal signal; yellow may be absent or extremely sparse.",
    },
    {
        "name": "Golden Light",
        "prompt": "Black and white provide structure. Let yellow carry the light and atmosphere; use red only as a tiny counterpoint if useful.",
    },
    {
        "name": "Night Signal",
        "prompt": "Make black the dominant field with white carved openings and one concentrated red or yellow light source.",
    },
    {
        "name": "Yellow Field",
        "prompt": "Use a substantial yellow field as sky, light, or ground, with black structure and restrained white/red interruptions.",
    },
    {
        "name": "Red Field",
        "prompt": "Use a substantial red field as weather, atmosphere, or motion, organized by black forms and white negative space; yellow is optional.",
    },
    {
        "name": "Balanced Four-Ink",
        "prompt": "Use all four device inks in clearly separated, meaningful masses without making every color equally dominant.",
    },
    {
        "name": "Ink Drawing",
        "prompt": "Black and white dominate like an ink drawing. Choose either red or yellow for one sparse emotional accent; do not force both.",
    },
    {
        "name": "Dueling Accents",
        "prompt": "Keep black and white structural, then use red and yellow as two distinct opposing forces in separate parts of the composition.",
    },
]

NEGATIVE_STYLE_CONSTRAINTS = (
    "Avoid photorealism, 3D rendering, blur, depth of field, smooth gradients, "
    "photographic lighting, and tiny illegible detail. Texture and tonal variation "
    "must come from deliberate marks in the selected style using the device inks. "
    "The result must be one unified illustration edge to edge -- not a collage, "
    "not a framed or inset image, and not a realistic photo with a graphic border."
)


def _style_state_path(settings):
    state_path = settings["runtime"].get("image_style_state_file", "runtime/image_style_state.json")
    return absolute_path(state_path)


def _load_style_state(settings):
    state_file = _style_state_path(settings)
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_style_state(settings, state):
    write_json(str(_style_state_path(settings)), state)


def _pick_art_style(settings, state, target_date):
    """Choose the art style for this forecast day, locking it across the day.

    While ``target_date`` is unchanged (the 8am / 1pm refreshes of the same
    forecast day) the previously selected style is reused so the daily theme
    stays fixed. A new target date (the 9pm run) advances the no-repeat rotation
    and picks the next style. Mutates ``state`` in place; the caller persists it.
    """
    styles = list(ART_STYLE_POOL)
    names = [item["name"] for item in styles]
    by_name = {item["name"]: item for item in styles}

    locked = state.get("last_selected")
    if state.get("target_date") == target_date and locked in by_name:
        return by_name[locked]

    remaining = [name for name in state.get("remaining", []) if name in by_name]
    if not remaining:
        remaining = names[:]
        random.SystemRandom().shuffle(remaining)
    chosen_name = remaining.pop(0)
    state["remaining"] = remaining
    state["last_selected"] = chosen_name
    state["target_date"] = target_date
    # A new forecast day: any existing hero belongs to the previous day's prompt.
    state["hero_prompt"] = None
    return by_name[chosen_name]


def _pick_palette_strategy(state, new_target_day):
    """Choose and lock a palette emphasis independently from the art style."""
    by_name = {item["name"]: item for item in PALETTE_STRATEGY_POOL}
    locked = state.get("last_palette")
    if not new_target_day and locked in by_name:
        return by_name[locked]

    remaining = [
        name for name in state.get("palette_remaining", []) if name in by_name
    ]
    if not remaining:
        remaining = list(by_name)
        random.SystemRandom().shuffle(remaining)
    chosen_name = remaining.pop(0)
    state["palette_remaining"] = remaining
    state["last_palette"] = chosen_name
    return by_name[chosen_name]


def _prompt_similar(prompt_a, prompt_b, threshold):
    """True when two illustration prompts share >= ``threshold`` of their tokens.

    The afternoon (1pm) refresh re-renders the hero in the locked style by
    default, but if the new prompt barely differs from the one behind the
    current art, that art still fits the evening and we reuse it rather than pay
    to regenerate something that would look the same.
    """
    tokens_a = set(re.findall(r"[a-z0-9]+", (prompt_a or "").lower()))
    tokens_b = set(re.findall(r"[a-z0-9]+", (prompt_b or "").lower()))
    if not tokens_a and not tokens_b:
        return True
    if not tokens_a or not tokens_b:
        return False
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b) >= threshold


def _inject_style_prompt(template, illustration_prompt, style, palette):
    style_block = (
        f"Selected art style: {style['name']}\n"
        f"Style direction: {style['prompt']}\n"
        f"{NEGATIVE_STYLE_CONSTRAINTS}"
    )
    prompt = template.replace("{{IMAGE_PROMPT}}", illustration_prompt.strip())
    if "{{PALETTE_GUIDANCE}}" in prompt:
        palette_block = f"{palette['name']}: {palette['prompt']}"
        prompt = prompt.replace("{{PALETTE_GUIDANCE}}", palette_block)
    if "{{STYLE_GUIDANCE}}" in prompt:
        prompt = prompt.replace("{{STYLE_GUIDANCE}}", style_block)
    else:
        prompt = prompt + "\n\nStyle guidance:\n" + style_block
    return prompt


def _extract_image_url(payload):
    # OpenRouter tool responses can nest fields; walk JSON to find a usable image URL.
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in ("imageUrl", "image_url", "url"):
                value = node.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    return value
            for value in node.values():
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _extract_data_image(payload):
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for value in node.values():
                if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value:
                    return value
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _extract_markdown_image_path(payload):
    text = json.dumps(payload, ensure_ascii=False)
    matches = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    if matches:
        return matches[0]
    return None


def _download_image(image_url, timeout, settings):
    with urlopen_with_context(image_url, timeout=timeout, settings=settings) as response:
        return response.read()


def _call_openrouter_image_api(settings, prompt):
    api_key = get_openrouter_api_key(settings)
    if not api_key:
        raise RuntimeError(
            "OpenRouter key not found. Set OPENROUTER_API_KEY or place a key in "
            "~/.openrouter.key or ~/.config/openrouter/api_key"
        )

    url = settings["openrouter"]["base_url"].rstrip("/") + "/responses"
    image_model = settings["openrouter"]["image_model"]
    tool_model = settings["openrouter"].get("image_tool_model", settings["openrouter"]["text_model"])
    configured_params = settings["openrouter"].get("image_generation_parameters", {})
    tool_parameters = {"model": image_model, **configured_params}
    tool_parameters.setdefault("output_format", "png")
    body = {
        "model": tool_model,
        "input": prompt,
        "tools": [
            {
                "type": "openrouter:image_generation",
                "parameters": tool_parameters,
            }
        ],
    }
    record_current_snapshot("image_api_request", {
        "provider": "openrouter",
        "tool_model": tool_model,
        "image_model": image_model,
        "request": body,
    })
    request = urllib.request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    timeout = settings["pipeline"]["image_timeout_seconds"]
    with urlopen_with_context(request, timeout=timeout, settings=settings) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if payload.get("error"):
        error = payload["error"]
        raise RuntimeError(
            f"OpenRouter server tool failed ({error.get('code', 'unknown')}): "
            f"{error.get('message', 'unknown error')}"
        )

    image_url = _extract_image_url(payload)
    if image_url:
        return _download_image(image_url, timeout=timeout, settings=settings)

    data_image = _extract_data_image(payload)
    if data_image:
        encoded = data_image.split(";base64,", 1)[1]
        return base64.b64decode(encoded)

    markdown_path = _extract_markdown_image_path(payload)
    if markdown_path:
        if markdown_path.startswith(("http://", "https://")):
            return _download_image(markdown_path, timeout=timeout, settings=settings)
        local = Path(markdown_path)
        if local.exists():
            return local.read_bytes()

    raise RuntimeError(f"No image URL found in OpenRouter response: {payload}")


def _call_fal_image_api(settings, prompt):
    api_key = get_fal_api_key(settings)
    if not api_key:
        raise RuntimeError(
            "FAL key not found. Set FAL_KEY or place a key in "
            "~/.fai.key, ~/.fal.key, or ~/.config/fal/api_key"
        )

    fal_settings = settings.get("fal", {})
    base_url = fal_settings.get("base_url", "https://fal.run").rstrip("/")
    image_model = fal_settings.get("image_model", "fal-ai/flux/schnell")
    configured_params = fal_settings.get("image_generation_parameters", {})
    body = {"prompt": prompt, **configured_params}
    # Fresh seed every run so an identical prompt still produces a new image.
    body.setdefault("seed", random.SystemRandom().randint(1, 2_000_000_000))
    record_current_snapshot("image_api_request", {
        "provider": "fal",
        "image_model": image_model,
        "request": body,
    })
    url = f"{base_url}/{image_model.lstrip('/')}"
    request = urllib.request.Request(
        url=url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        },
    )
    timeout = settings["pipeline"]["image_timeout_seconds"]
    with urlopen_with_context(request, timeout=timeout, settings=settings) as response:
        payload = json.loads(response.read().decode("utf-8"))

    image_url = _extract_image_url(payload)
    if image_url:
        return _download_image(image_url, timeout=timeout, settings=settings)

    data_image = _extract_data_image(payload)
    if data_image:
        encoded = data_image.split(";base64,", 1)[1]
        return base64.b64decode(encoded)

    raise RuntimeError(f"No image URL found in FAL response: {payload}")


def _resolve_image_provider(settings, force_openrouter):
    if force_openrouter:
        return "openrouter"
    return settings.get("pipeline", {}).get("image_provider", "openrouter").strip().lower()


def _call_image_api(settings, prompt, provider):
    if provider == "openrouter":
        return _call_openrouter_image_api(settings, prompt)
    if provider in ("fal", "fai"):
        return _call_fal_image_api(settings, prompt)
    raise RuntimeError(f"Unsupported image provider: {provider}")


def _off_palette_pct(image_bytes):
    """Fraction of art in hues the 4-ink panel cannot show (blue/green/etc).

    Deterministic and cheap (no API). The return value is tri-state for the
    guardrail: a float is a completed measurement, while ``None`` means the
    analyzer raised and the palette could not be verified. ``None`` must be
    treated as unavailable validation (not as a within-budget measurement)."""
    try:
        return analyze_palette(image_bytes)["off_palette_pct"]
    except Exception:  # noqa: BLE001 - never let the gate break generation
        return None


# Hero validation states that may become a reuse candidate.
#
# * "accepted" -- the image passed every validation check. Always reusable.
# * "disabled" -- the guardrail was off, so the image was intentionally never
#   validated. It is reusable only while the guardrail is *still* off: turning
#   validation on must invalidate it, forcing a fresh (validated) generation
#   instead of silently promoting unvalidated art to accepted.
#
# "unverified" (a check could not run) and explicit "rejected" candidates never
# become reusable, and neither does legacy state without a provenance record.
# Disabled generation stays distinguishable from those in the recorded
# validation provenance and logs.
_REUSABLE_HERO_STATUSES = {"accepted"}
_DISABLED_HERO_STATUS = "disabled"

_RETRY_PROMPT_CLAUSES = {
    "has_text": (
        "The previous attempt was rejected for baked-in lettering. Draw NO text "
        "of any kind anywhere in the image: no letters, words, numbers, "
        "percentages, signs, labels, logos, or watermarks."
    ),
    "is_collage": (
        "The previous attempt was rejected as a collage or framed image. Produce "
        "ONE unified illustration edge to edge with a single aesthetic; never "
        "frame, inset, or nest one image inside a border, grid, or block."
    ),
}
_OFF_PALETTE_RETRY_CLAUSE = (
    "The previous attempt used colours the panel cannot show. Use only white, "
    "black, red, and yellow, with no grey, blue, green, or other hues."
)


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path):
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        return None


def _hero_reuse_eligible(style_state, target_date, hero_path,
                         guardrail_enabled=True):
    """True only when the on-disk hero is safe to reuse under current policy.

    ``image_style_state.json`` records the validation outcome for the hero it
    describes in ``hero_validation``. An ``accepted`` hero is always reusable. A
    ``disabled`` hero (guardrail off, never validated) is reusable only while
    the guardrail is still disabled, so enabling validation forces a fresh,
    validated generation. Legacy state written before that record existed, an
    ``unverified`` candidate (a check was unavailable), and an explicitly
    rejected candidate are all ineligible -- unlike disabled art they cannot be
    trusted even in opt-out mode. The recorded target date and image hash must
    also match so a stale or unrelated record cannot vouch for the file on disk.
    """
    record = style_state.get("hero_validation")
    if not isinstance(record, dict):
        return False
    status = record.get("status")
    if status == _DISABLED_HERO_STATUS:
        # Unvalidated art may only be reused while validation stays off.
        if guardrail_enabled:
            return False
    elif status not in _REUSABLE_HERO_STATUSES:
        return False
    if record.get("target_date") != target_date:
        return False
    expected_sha = record.get("image_sha256")
    if not expected_sha or not hero_path.exists():
        return False
    return _file_sha256(hero_path) == expected_sha


def _hero_validation_record(status, *, target_date, style, palette, provider,
                            image_bytes, attempts, reasons):
    """Provenance for the hero currently on disk, written into style state."""
    return {
        "status": status,
        "target_date": target_date,
        "style": style,
        "palette": palette,
        "provider": provider,
        "image_byte_size": len(image_bytes) if image_bytes else None,
        "image_sha256": _sha256_bytes(image_bytes) if image_bytes else None,
        "attempts": [dict(item) for item in (attempts or [])],
        "reasons": list(reasons or []),
        "recorded_at": utc_now_iso(),
    }


def _retry_prompt(prompt, reasons):
    """Append targeted corrections for the previous explicit rejection.

    This addresses the actual rejection instead of adding weather wording or
    percentages, which can themselves encourage baked-in captions.
    """
    clauses = []
    for reason in reasons or []:
        clause = _RETRY_PROMPT_CLAUSES.get(reason)
        if clause:
            clauses.append(clause)
    if any(str(reason).startswith("off_palette=") for reason in reasons or []):
        clauses.append(_OFF_PALETTE_RETRY_CLAUSE)
    if not clauses:
        return prompt
    return prompt + "\n\nRetry correction:\n- " + "\n- ".join(clauses)


def _generate_with_guardrail(settings, prompt, provider):
    """Generate art and return a structured guardrail outcome.

    ``status`` is one of:

    * ``accepted``   -- every validation check ran and passed;
    * ``unverified`` -- a check was unavailable (vision no key/network error, or
      the palette analyzer raised), so the fail-open policy keeps the image but
      records it as unverified and therefore non-reusable;
    * ``rejected``   -- every attempt was explicitly rejected; ``image_bytes`` is
      ``None`` so the flagged candidate is never published;
    * ``disabled``   -- the guardrail is off and the image is passed through.

    Unavailable validation is deliberately distinct from an explicit rejection:
    it never blocks rendering, while a rejection returns no bytes so the caller
    can fall back to previously accepted art or the deterministic pictogram.
    """
    pipeline = settings.get("pipeline", {})
    enabled = pipeline.get("enable_image_guardrail", False)
    if not enabled:
        image_bytes = _call_image_api(settings, prompt, provider)
        return {
            "status": "disabled",
            "image_bytes": image_bytes,
            "reasons": [],
            "attempts": [{"attempt": 1, "verdict": {"status": "disabled"},
                          "reasons": [], "image_byte_size": len(image_bytes)}],
        }

    max_retries = int(pipeline.get("image_guardrail_max_retries", 1))
    max_off_palette = float(pipeline.get("image_guardrail_max_off_palette_pct", 0.15))
    attempts = []
    rejected_reasons = []
    for attempt in range(max_retries + 1):
        current_prompt = prompt if not attempts else _retry_prompt(prompt, attempts[-1]["reasons"])
        image_bytes = _call_image_api(settings, current_prompt, provider)
        verdict = inspect_art(image_bytes, settings)  # vision: text / collage
        inspection = verdict.get("status")
        if inspection is None:
            # Verdict dicts from older callers omit ``status``; fall back to the
            # ``ok``/flag shape so the guardrail still behaves sensibly.
            vision_reasons = [k for k in ("has_text", "is_collage") if verdict.get(k)]
            if not vision_reasons and not verdict.get("ok", True):
                vision_reasons = ["rejected"]
            if vision_reasons:
                inspection = "rejected"
            elif verdict.get("skipped") or verdict.get("error"):
                inspection = "unverified"
            else:
                inspection = "accepted"
        else:
            vision_reasons = (
                [k for k in ("has_text", "is_collage") if verdict.get(k)]
                if inspection == "rejected" else []
            )
        reasons = list(vision_reasons)
        off = _off_palette_pct(image_bytes)  # deterministic: off-palette colour
        # Palette analysis is tri-state: a measured fraction means the check ran,
        # while ``None`` means the analyzer raised and the colour budget could not
        # be verified. An unverifiable palette may still fail open so the board
        # renders, but it must never become a reusable ``accepted`` hero.
        palette_analyzed = off is not None
        if palette_analyzed and off > max_off_palette:
            reasons.append(f"off_palette={off * 100:.0f}%")
        record = {
            "attempt": attempt + 1,
            "status": inspection,
            "verdict": verdict,
            "off_palette_pct": off,
            "palette_status": "analyzed" if palette_analyzed else "unavailable",
            "reasons": reasons,
            "image_byte_size": len(image_bytes),
        }
        attempts.append(record)
        record_current_log(
            "generate_image", "guardrail_attempt",
            "Guardrail passed" if not reasons else "Guardrail rejected image",
            level="info" if not reasons else "warning",
            data=record,
        )
        if not reasons:
            if attempt:
                print(f"[image] guardrail passed on attempt {attempt + 1}")
            # Vision passed. Only a measured, within-budget palette lets the
            # candidate become reusable; an unavailable vision check *or* an
            # unavailable palette analyzer leaves it explicitly unverified.
            status = (
                "accepted"
                if inspection == "accepted" and palette_analyzed
                else "unverified"
            )
            if status == "unverified":
                if not palette_analyzed:
                    print("[image] palette analysis unavailable; keeping image as unverified")
                else:
                    print("[image] guardrail unavailable; keeping image as unverified")
            return {"status": status, "image_bytes": image_bytes,
                    "reasons": [], "attempts": attempts}
        for reason in reasons:
            if reason not in rejected_reasons:
                rejected_reasons.append(reason)
        print(f"[image] guardrail rejected attempt {attempt + 1} ({', '.join(reasons)})")
    print("[image] guardrail retries exhausted; refusing to publish the rejected image")
    return {"status": "rejected", "image_bytes": None,
            "reasons": rejected_reasons, "attempts": attempts}


def _handle_guardrail_exhaustion(settings, style_state, output_abs, *,
                                 target_date, daypart_role, style, palette,
                                 provider, outcome, prior_hero_eligible):
    """Record explicit-rejection exhaustion and choose the fallback hero.

    A previously accepted hero is kept only when its recorded validation still
    matches; otherwise the hero is removed so ``compose_board`` draws its
    deterministic pictogram. The error-level log carries the explicit rejection
    reasons so an otherwise successful update is visibly degraded.
    """
    reasons = list(outcome.get("reasons") or [])
    detail = ", ".join(reasons) if reasons else "guardrail rejected every attempt"
    fallback = "previous_accepted_hero" if prior_hero_eligible else "deterministic_pictogram"
    record_current_log(
        "generate_image", "image_guardrail_exhausted",
        f"Image guardrail rejected every attempt ({detail}); using fallback art",
        level="error",
        data={
            "target_date": target_date,
            "daypart_role": daypart_role,
            "style": style["name"],
            "palette": palette["name"],
            "provider": provider,
            "reasons": reasons,
            "attempts": outcome.get("attempts", []),
            "fallback": fallback,
        },
    )
    if prior_hero_eligible:
        # The rejected candidate was never written, so the accepted prior hero
        # and its validation record are still on disk and can be reused.
        record_current_log(
            "generate_image", "hero_fallback_reuse",
            "Kept the prior accepted hero after guardrail exhaustion",
            level="warning",
            data={"reasons": reasons, "validation": style_state.get("hero_validation")},
        )
        print(f"image-fallback-reuse (kept validated hero): {detail}")
    else:
        if output_abs.exists():
            output_abs.unlink()
        style_state["hero_prompt"] = None
        style_state["hero_validation"] = _hero_validation_record(
            "rejected", target_date=target_date, style=style["name"],
            palette=palette["name"], provider=provider, image_bytes=None,
            attempts=outcome.get("attempts", []), reasons=reasons,
        )
        record_current_log(
            "generate_image", "hero_fallback_pictogram",
            "No validated hero available; using the deterministic pictogram",
            level="warning",
            data={"reasons": reasons},
        )
        print(f"image-fallback-blank: {detail}")
    _save_style_state(settings, style_state)


def _handle_generation_failure(settings, style_state, output_abs, detail, *,
                               target_date, style, palette, provider, eligible):
    """Apply the fallback after an image API failure.

    The failed candidate was never written. The previous hero is kept only when
    its recorded validation accepts it (and it belongs to this target day);
    otherwise it is removed so ``compose_board`` uses the deterministic
    pictogram instead of republishing unvalidated or stale art.
    """
    if output_abs.exists() and eligible:
        print(f"image-fallback-reuse (kept validated previous hero): {detail}")
        _save_style_state(settings, style_state)
        return
    if output_abs.exists():
        output_abs.unlink()
    style_state["hero_prompt"] = None
    style_state["hero_validation"] = _hero_validation_record(
        "unverified", target_date=target_date, style=style["name"],
        palette=palette["name"], provider=provider, image_bytes=None,
        attempts=[], reasons=[],
    )
    _save_style_state(settings, style_state)
    print(f"image-fallback-blank: {detail}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--force-openrouter", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate even if the brief was cached (keeps the configured provider)")
    args = parser.parse_args()

    settings = load_settings()
    input_path = args.input or settings["runtime"]["brief_file"]
    output_path = args.output or settings["runtime"]["hero_file"]
    output_abs = absolute_path(output_path)
    output_abs.parent.mkdir(parents=True, exist_ok=True)

    image_generation_enabled = settings["pipeline"]["enable_openrouter_image"] or args.force_openrouter
    if not image_generation_enabled:
        if output_abs.exists():
            output_abs.unlink()
        print("image-disabled")
        record_current_log(
            "generate_image", "image_disabled", "Image generation is disabled"
        )
        return

    payload = read_json(input_path)
    day_context = payload.get("day_context", {})
    target_date = day_context.get("target_date_iso") or day_context.get("date_iso", "")
    daypart_role = day_context.get("daypart_role", "")

    illustration_prompt = payload.get("brief", {}).get("illustration_prompt", "").strip()
    if not illustration_prompt:
        raise RuntimeError("Missing brief.illustration_prompt for image generation")

    # Lock the art style to the forecast day so all three daily refreshes share
    # one theme; only a new target date (the 9pm run) rolls a new style.
    style_state = _load_style_state(settings)
    prior_style_state = dict(style_state)
    new_target_day = style_state.get("target_date") != target_date
    style = _pick_art_style(settings, style_state, target_date)
    palette = _pick_palette_strategy(style_state, new_target_day)
    record_current_log(
        "generate_image", "style_selected",
        f"Selected {style['name']} with {palette['name']} palette",
        data={
            "target_date": target_date,
            "daypart_role": daypart_role,
            "new_target_day": new_target_day,
            "selected_palette": palette,
            "previous_state": prior_style_state,
            "selected_state": style_state,
        },
    )

    # Only reuse a cached hero whose recorded validation is safe under the
    # current policy: accepted art is always reusable, while a disabled
    # (never-validated) hero is reusable only while the guardrail stays disabled.
    # Legacy state with no ``hero_validation`` record, an unverified candidate,
    # and a rejected candidate are always ineligible so flagged art cannot become
    # the accepted reuse candidate.
    guardrail_enabled = bool(
        settings.get("pipeline", {}).get("enable_image_guardrail", False))
    prior_hero_eligible = _hero_reuse_eligible(
        style_state, target_date, output_abs,
        guardrail_enabled=guardrail_enabled)

    # Decide whether to reuse the existing hero or regenerate it.
    reuse_reason = None
    if output_abs.exists() and not args.force and not new_target_day:
        if payload.get("brief_source") == "cached":
            # Unchanged forecast (e.g. 8am with no major update): keep the art.
            reuse_reason = "brief cached"
        elif daypart_role == "afternoon":
            # Afternoon re-frame: re-render in the locked style unless the new
            # prompt barely differs from the morning's, so the art still fits.
            threshold = float(settings.get("pipeline", {}).get(
                "afternoon_art_prompt_similarity_threshold", 0.8))
            prev_prompt = style_state.get("hero_prompt")
            if prev_prompt and _prompt_similar(prev_prompt, illustration_prompt, threshold):
                reuse_reason = "afternoon prompt ~ unchanged"
    if reuse_reason is not None and prior_hero_eligible:
        _save_style_state(settings, style_state)
        record_current_log(
            "generate_image", "hero_reused", reuse_reason,
            data={
                "target_date": target_date,
                "style": style["name"],
                "palette": palette["name"],
                "validation": style_state.get("hero_validation"),
            },
        )
        print(f"image-skip-reuse: keeping existing hero ({reuse_reason})")
        return
    if reuse_reason is not None:
        record_current_log(
            "generate_image", "hero_reuse_rejected",
            f"Cached hero is not eligible for reuse ({reuse_reason}); regenerating",
            level="warning",
            data={
                "target_date": target_date,
                "reuse_reason": reuse_reason,
                "validation": style_state.get("hero_validation"),
            },
        )
        print(f"image-regenerate: cached hero has no recorded acceptance ({reuse_reason})")

    template_path = ROOT / "config" / "prompt_templates" / "weather_image.txt"
    template = template_path.read_text(encoding="utf-8")
    prompt = _inject_style_prompt(template, illustration_prompt, style, palette)
    provider = _resolve_image_provider(settings, args.force_openrouter)
    record_current_snapshot("image_generation_input", {
        "target_date": target_date,
        "daypart_role": daypart_role,
        "illustration_prompt": illustration_prompt,
        "selected_style": style,
        "selected_palette": palette,
        "provider": provider,
    })
    record_current_snapshot("image_prompt", prompt, content_type="text/plain; charset=utf-8")
    print(f"[image] target_date={target_date or 'n/a'} role={daypart_role or 'n/a'}")
    print(f"[image] selected_style={style['name']}")
    print(f"[image] selected_palette={palette['name']}")
    print(f"[image] provider={provider}")

    try:
        outcome = _generate_with_guardrail(settings, prompt, provider)
        status = outcome.get("status")
        if status == "rejected":
            _handle_guardrail_exhaustion(
                settings, style_state, output_abs,
                target_date=target_date, daypart_role=daypart_role,
                style=style, palette=palette, provider=provider,
                outcome=outcome, prior_hero_eligible=prior_hero_eligible,
            )
            return
        image_bytes = outcome["image_bytes"]
        output_abs.write_bytes(image_bytes)
        # Record the prompt and validation verdict behind the current art so the
        # afternoon refresh and the cache-reuse paths can trust it.
        style_state["hero_prompt"] = illustration_prompt
        style_state["hero_validation"] = _hero_validation_record(
            status, target_date=target_date, style=style["name"],
            palette=palette["name"], provider=provider, image_bytes=image_bytes,
            attempts=outcome.get("attempts", []), reasons=outcome.get("reasons", []),
        )
        _save_style_state(settings, style_state)
        if status == "unverified":
            generated_message = (
                "Wrote a new hero image without complete validation "
                "(guardrail or palette analysis unavailable)"
            )
            generated_level = "warning"
        elif status == "disabled":
            generated_message = (
                "Wrote a new hero image without validation (guardrail disabled)"
            )
            generated_level = "info"
        else:
            generated_message = "Wrote a new hero image"
            generated_level = "info"
        record_current_log(
            "generate_image", "image_generated", generated_message,
            level=generated_level,
            data={
                "target_date": target_date,
                "style": style["name"],
                "palette": palette["name"],
                "provider": provider,
                "byte_size": len(image_bytes),
                "output_path": str(output_abs),
                "validation_status": status,
                "guardrail_attempts": outcome.get("attempts", []),
            },
        )
        print(output_path)
    except urllib.error.URLError as error:
        detail = describe_network_error(error)
        record_current_log(
            "generate_image", "image_generation_failed", detail, level="error",
            data={
                "error_type": type(error).__name__,
                "style": style["name"],
                "palette": palette["name"],
                "provider": provider,
            },
        )
        _handle_generation_failure(
            settings, style_state, output_abs, detail,
            target_date=target_date, style=style, palette=palette,
            provider=provider, eligible=prior_hero_eligible,
        )
    except (KeyError, json.JSONDecodeError, RuntimeError) as error:
        record_current_log(
            "generate_image", "image_generation_failed", str(error), level="error",
            data={
                "error_type": type(error).__name__,
                "style": style["name"],
                "palette": palette["name"],
                "provider": provider,
            },
        )
        _handle_generation_failure(
            settings, style_state, output_abs, str(error),
            target_date=target_date, style=style, palette=palette,
            provider=provider, eligible=prior_hero_eligible,
        )


if __name__ == "__main__":
    main()
