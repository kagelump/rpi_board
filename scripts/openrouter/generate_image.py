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
from scripts.openrouter import art_recipes
from scripts.openrouter.art_direction import artwork_context
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
        "prompt": "De Stijl-inspired illustration: orthogonal rhythms, black dividing lines and red, yellow and white color blocks integrated across the scene, with drawn material detail and depth; keep the whole scene illustrated rather than placing a photo inside a grid",
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
        "prompt": "hand-cut paper illustration: irregular scissor edges, overlapping ink silhouettes with each ink kept separate, playful depth from shape boundaries across one continuous scene",
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
        "prompt": "noir-inspired ink illustration: oblique viewpoints, strong daylight shadows, cropped silhouettes, tense empty space and expressive directional marks; dark ink does not imply nighttime",
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
        "prompt": "Make black the dominant printed field with white carved openings and one concentrated red or yellow accent. This is an ink balance, not a nighttime setting; preserve the scene's daylight.",
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
    "Use drawn detail and mark spacing for depth instead of photographic blur, "
    "depth of field, smooth gradients or 3D rendering. Texture and tonal variation "
    "must come from deliberate marks in the selected style using the device inks. "
    "Keep a continuous illustrated scene edge to edge, including detailed "
    "backgrounds and unified paper-cut forms where appropriate; no inserted "
    "photographs or framed or inset pictures."
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


def _inject_style_prompt(template, illustration_prompt, style, palette, recipe=None,
                         *, forecast=None):
    style_block = (
        f"Style: {style['name']}. {style['prompt']}. "
        "Let this style control the drawing and texture, using the four device inks."
    )
    # Issue #20: the accepted brief/forecast is authoritative. When the selected
    # recipe's staging would contradict it (for example an umbrella crowd on a
    # forecast that explicitly stays dry), inject no recipe guidance so the
    # brief's original SUBJECT wins instead of forcing contradictory rain art.
    #
    # Issue #23: when the recipe is compatible, do not put the accepted brief's
    # concrete scene and a second recipe staging block in the same prompt. The
    # provider followed the first scene and the ledger still claimed the appended
    # recipe had guided the hero. Synthesize ONE scene instead: the recipe owns
    # subject/motif, setting, viewpoint and composition, while only bounded
    # weather/light cues and the emotional beat carry over from the accepted
    # brief.
    scene_text = illustration_prompt.strip()
    recipe_is_compatible = (
        recipe is not None and art_recipes.recipe_scene_is_compatible(recipe)
    )
    if recipe and recipe_is_compatible and not (
        forecast is not None and art_recipes.recipe_conflicts_with_brief(recipe, forecast)
    ):
        synthesized_scene = art_recipes.synthesize_recipe_scene(
            recipe, brief_scene=illustration_prompt, forecast=forecast
        )
        if synthesized_scene is not None:
            scene_text = synthesized_scene
    prompt = template.replace(
        "{{IMAGE_PROMPT}}", _rewrite_scene_direction(scene_text)
    )
    lighting = (artwork_context(forecast)["guidance"] if forecast is not None else
                "Keep the time of day explicitly described in the scene. Do not "
                "add celestial objects or change the scene's lighting to match "
                "a palette or style example.")
    if "{{LIGHTING_GUIDANCE}}" in prompt:
        prompt = prompt.replace("{{LIGHTING_GUIDANCE}}", lighting)
    elif forecast is not None:
        prompt += "\n\nScene lighting:\n" + lighting
    if "{{PALETTE_GUIDANCE}}" in prompt:
        palette_block = palette['prompt']
        prompt = prompt.replace("{{PALETTE_GUIDANCE}}", palette_block)
    # Recipe content is already integrated into the single Scene block above.
    # Keeping this slot empty prevents a second, potentially conflicting scene
    # description from being appended to the provider request.
    if "{{RECIPE_GUIDANCE}}" in prompt:
        prompt = prompt.replace("{{RECIPE_GUIDANCE}}", "")
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
    "has_watermark_or_signature": (
        "The previous attempt was rejected for a creator signature, watermark, "
        "or prominent logo/brand mark. Draw NO signature, handwritten mark, "
        "watermark, logo, brand mark, or other creator mark anywhere in the "
        "image. Ordinary environmental signage and natural scene lettering are "
        "still allowed."
    ),
    "is_collage": (
        "The previous attempt was rejected as a collage or framed image. Produce "
        "ONE unified illustration edge to edge. Draw the foreground and background "
        "as parts of one scene; retain illustrated detail, depth or unified "
        "paper-cut forms without inserting photographs or framed pictures."
    ),
}
_OFF_PALETTE_RETRY_CLAUSE = (
    "Use only white, black, red and yellow ink marks with paper between them. "
    "Replace unsupported hues and blended shading with hatching, stippling, "
    "carved marks or separated ink shapes in the selected style. Preserve the "
    "scene's lighting, weather, composition and material detail; a palette "
    "correction must not turn daylight into night or introduce new objects."
)

_SCENE_HEADING = "Scene:\n"
_LEGACY_SCENE_HEADING = (
    "Scene (translate all materials, light and atmosphere into the specified ink shapes):\n"
)


def _rewrite_palette_colors(scene):
    """Map incompatible surface hues without flattening light or materials."""
    replacements = [
        (r"\b(?:grey|gray)\s+sky\b", "overcast sky"),
        (r"\b(?:blue|cyan|teal|azure)\b(?=\s+(?:sky|water|river|sea|clouds?|panes?|windows?|light|hue))", "white"),
        (r"\b(?:green|brown|grey|gray|purple|violet|indigo)\b(?=\s+(?:trees?|leaves|foliage|sky|water|river|clouds?|panes?|windows?|light|hue))", "black"),
        (r"\b(?:pink|magenta)\b(?=\s+(?:sky|clouds?|blossoms?|flowers?|light|hue))", "red"),
        (r"\borange\b(?=\s+(?:sky|light|glow|clouds?|leaves|hue))", "yellow"),
    ]
    for pattern, replacement in replacements:
        scene = re.sub(pattern, replacement, scene, flags=re.IGNORECASE)
    return scene


def _rewrite_scene_direction(scene):
    """Keep cached scene depth without asking the image model for camera blur."""
    for pattern, replacement in (
        (r"\b(?:softly\s+)?(?:blurred|blurry)\b", "less detailed"),
        (r"\bout[- ]of[- ]focus\b", "drawn with fewer distant marks"),
        (r"\bin\s+(?:sharp|soft)\s+focus\b", "with deliberate drawn detail"),
        (r"\b(?:shallow\s+)?depth of field\b", "foreground and background separation through drawn detail"),
        (r"\bbokeh\b", "sparse background ink marks"),
    ):
        def replace(match):
            prefix = scene[max(0, match.start() - 24):match.start()]
            if re.search(r"\b(?:no|not|without|avoid)\s+(?:any\s+)?$", prefix, re.I):
                return match.group(0)
            return replacement
        scene = re.sub(pattern, replace, scene, flags=re.I)
    return _rewrite_palette_colors(scene)


def _rewrite_palette_scene(scene):
    """Stricter material translation used only after a palette rejection.

    Only the scene is edited; exclusions and style directions are not subject
    to colour replacement. This bounded vocabulary complements the rendering
    contract; it is not a general semantic rewriter.
    """
    replacements = [
        (r"\bglass office building\b", "office building with black window dividers and opaque white or yellow panes"),
        (r"\breflection on the glass\b", "pattern of repeated white or yellow shapes on the window panes"),
        (r"\b(?:crisp )?reflections\b", "repeated white or yellow ink shapes"),
        (r"\breflected night sky\b", "solid black night sky with white marks"),
        (r"\bsoft cool night air\b", "still night air"),
        (r"\bbarely visible\b", "solid white"),
        (r"\bfaint ghost\b", "white silhouette"),
        (r"\b(?:soft(?:\s+(?:cool|warm|golden))?|cool|warm|golden)\s+(?:sunlight|streetlight|light|glow)\b(?!\s+(?:rain|drizzle|snow|breeze|wind)\b)", "solid yellow shapes with hard edges"),
        (r"\btranslucent leaves\b", "opaque black or yellow leaves"),
    ]
    for pattern, replacement in replacements:
        scene = re.sub(pattern, replacement, scene, flags=re.IGNORECASE)
    return _rewrite_scene_direction(scene)


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
    """Rewrite conflicting scene cues after a palette rejection.

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
        for heading in (_SCENE_HEADING, _LEGACY_SCENE_HEADING):
            before, separator, after = prompt.partition(heading)
            if separator:
                scene, ending, tail = after.partition("\n\n")
                prompt = before + separator + _rewrite_palette_scene(scene) + ending + tail
                break
        prompt = prompt.replace(
            "Solid ink shapes following the palette emphasis; large readable silhouettes.",
            "Separate opaque ink shapes, crisp edges, a readable silhouette.")
    if not clauses:
        return prompt
    return "Retry correction:\n- " + "\n- ".join(clauses) + "\n\n" + prompt


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
    max_off_palette = float(pipeline.get("image_guardrail_max_off_palette_pct", 0.40))
    attempts = []
    rejected_reasons = []
    for attempt in range(max_retries + 1):
        current_prompt = prompt if not attempts else _retry_prompt(prompt, rejected_reasons)
        image_bytes = _call_image_api(settings, current_prompt, provider)
        verdict = inspect_art(image_bytes, settings)  # vision: text, creator marks, collage
        inspection = verdict.get("status")
        if inspection is None:
            # Verdict dicts from older callers omit ``status``; fall back to the
            # ``ok``/flag shape so the guardrail still behaves sensibly.
            vision_reasons = [
                k for k in ("has_text", "has_watermark_or_signature", "is_collage")
                if verdict.get(k)
            ]
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
                [k for k in ("has_text", "has_watermark_or_signature", "is_collage")
                 if verdict.get(k)]
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

    # Issue #18: lock a structured artwork recipe to the forecast day and make
    # sure it is novel versus recently *published* art. The recipe is chosen
    # locally, before any image API call; changing only style or palette cannot
    # establish novelty. A new target date rolls a new recipe, while same-day
    # refreshes keep the day's existing one.
    if new_target_day:
        style_state.pop("art_recipe", None)
        # Issue #21: the usage marker describes the hero currently on disk. A new
        # target day generates a new hero, so drop the prior day's marker rather
        # than letting it vouch for unrelated artwork.
        style_state.pop("art_recipe_used", None)
        style_state.pop("art_recipe_suppressed", None)
    if new_target_day or not isinstance(style_state.get("art_recipe"), dict):
        try:
            art_recipe = art_recipes.choose_recipe(
                settings, target_date=target_date, daypart_role=daypart_role, payload=payload)
        except Exception as error:  # noqa: BLE001 - variety must never break art
            art_recipe = None
            record_current_log(
                "generate_image", "art_recipe_selection_failed", str(error),
                level="warning",
                data={"target_date": target_date, "daypart_role": daypart_role},
            )
        if art_recipe is not None:
            style_state["art_recipe"] = art_recipe
            style_state["art_recipe_target_date"] = target_date
            record_current_log(
                "generate_image", "art_recipe_selected",
                f"Selected artwork recipe ({art_recipe.get('status')}): "
                f"{art_recipe['tags'].get('subject')} / {art_recipe['tags'].get('setting')}",
                data={
                    "target_date": target_date,
                    "daypart_role": daypart_role,
                    "status": art_recipe.get("status"),
                    "tags": art_recipe.get("tags"),
                    "rationale": art_recipe.get("rationale"),
                    "policy": art_recipe.get("policy"),
                    "recent_recipe_ids": art_recipe.get("recent_recipe_ids"),
                    "forecast_weather": art_recipe.get("forecast_weather"),
                    "recipe_weather": art_recipe.get("recipe_weather"),
                },
            )
    else:
        art_recipe = style_state.get("art_recipe")
        record_current_log(
            "generate_image", "art_recipe_locked",
            "Kept the forecast day's artwork recipe",
            data={"target_date": target_date, "tags": art_recipes.extract_tags(art_recipe)},
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

    # Issue #20/#21: record when an audited recipe is suppressed because its
    # staging contradicts the accepted brief/forecast. The brief's SUBJECT still
    # drives the image, so forecast accuracy is preserved. A suppressed recipe is
    # audit-only: it must never be booked as the recipe that guided the published
    # hero (the ledger reads ``art_recipe_used``, not ``art_recipe``).
    recipe_weather_conflict = bool(
        art_recipe is not None
        and art_recipes.recipe_conflicts_with_brief(art_recipe, payload)
    )
    # Issue #27: a locked recipe can predate the deterministic compatibility
    # gate.  It remains audit-only if its subject and setting cannot form one
    # coherent place; the accepted brief's original scene then drives the hero.
    recipe_scene_incompatible = bool(
        art_recipe is not None
        and not art_recipes.recipe_scene_is_compatible(art_recipe)
    )
    recipe_suppressed = bool(
        art_recipe is not None
        and (recipe_weather_conflict or recipe_scene_incompatible)
    )
    recipe_suppression_reason = (
        "incompatible_subject_setting" if recipe_scene_incompatible else
        "weather_conflict" if recipe_weather_conflict else
        None
    )
    recipe_dimensions = (
        art_recipes.recipe_scene_dimensions(art_recipe)
        if art_recipe is not None else
        {"complete": False, "present": [], "missing": [], "tags": {}}
    )
    recipe_not_realized = bool(
        art_recipe is not None
        and not recipe_suppressed
        and not recipe_dimensions["complete"]
    )
    used_recipe = (
        None if recipe_suppressed or recipe_not_realized else art_recipe
    )
    if recipe_suppressed:
        record_current_log(
            "generate_image", "art_recipe_suppressed",
            "Suppressed artwork recipe staging that contradicts the accepted forecast/brief",
            level="warning",
            data={
                "target_date": target_date,
                "daypart_role": daypart_role,
                "recipe_id": art_recipe.get("recipe_id"),
                "tags": art_recipes.extract_tags(art_recipe),
                "recipe_weather": art_recipes.recipe_weather_class(art_recipe),
                "forecast_weather": (
                    "wet" if art_recipes._forecast_is_wet(payload) else "dry"
                ),
                "suppression_reason": recipe_suppression_reason,
                "used_for_hero": False,
                "rationale": (
                    "The accepted brief/forecast is authoritative; the brief's "
                    "original SUBJECT is used instead of contradictory or "
                    "physically incompatible recipe staging."
                ),
            },
        )
    elif recipe_not_realized:
        # Issue #23: a selected recipe is audit-only unless all of its staging
        # dimensions can be integrated into one coherent prompt scene. Merely
        # appending a few tags to the accepted brief is not proof of realization.
        record_current_log(
            "generate_image", "art_recipe_not_realized",
            "Selected artwork recipe lacks dimensions needed for a coherent scene",
            level="warning",
            data={
                "target_date": target_date,
                "daypart_role": daypart_role,
                "recipe_id": art_recipe.get("recipe_id"),
                "tags": art_recipes.extract_tags(art_recipe),
                "dimensions_present": recipe_dimensions["present"],
                "dimensions_missing": recipe_dimensions["missing"],
                "used_for_hero": False,
                "rationale": (
                    "The accepted brief's scene is used unchanged; the selected "
                    "recipe was not realized and must not enter publication history."
                ),
            },
        )

    realization_status = "not_selected"
    if art_recipe is not None:
        if recipe_scene_incompatible:
            realization_status = "suppressed_incompatible_scene"
        elif recipe_suppressed:
            realization_status = "suppressed_forecast_conflict"
        elif recipe_not_realized:
            realization_status = "not_realized"
        else:
            realization_status = "prompt_synthesized"
    recipe_realization = {
        "status": realization_status,
        "used_for_hero": used_recipe is not None,
        "recipe_id": art_recipe.get("recipe_id") if art_recipe is not None else None,
        "dimensions_present": list(recipe_dimensions["present"]),
        "dimensions_missing": list(recipe_dimensions["missing"]),
        "brief_scene_replaced": used_recipe is not None,
        "prompt_sha256": None,
    }

    template_path = ROOT / "config" / "prompt_templates" / "weather_image.txt"
    template = template_path.read_text(encoding="utf-8")
    prompt = _inject_style_prompt(
        template, illustration_prompt, style, palette, recipe=used_recipe, forecast=payload
    )
    recipe_realization["prompt_sha256"] = _sha256_bytes(prompt.encode("utf-8"))
    provider = _resolve_image_provider(settings, args.force_openrouter)
    record_current_snapshot("image_generation_input", {
        "target_date": target_date,
        "daypart_role": daypart_role,
        "artwork_context": artwork_context(payload),
        "illustration_prompt": illustration_prompt,
        "selected_style": style,
        "selected_palette": palette,
        "art_recipe": art_recipe,
        "art_recipe_suppressed": recipe_suppressed,
        "art_recipe_used": used_recipe,
        "art_recipe_realization": recipe_realization,
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
        # Issue #21/#23: persist which recipe actually guided this hero and the
        # evidence that its dimensions were integrated into a single coherent
        # prompt scene. A selected-but-suppressed or dimensionally incomplete
        # recipe remains audit-only and is never booked as published art.
        style_state["art_recipe_used"] = used_recipe
        style_state["art_recipe_suppressed"] = recipe_suppressed
        style_state["art_recipe_realization"] = recipe_realization
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
