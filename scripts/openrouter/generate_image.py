#!/usr/bin/env python3
import argparse
import base64
import json
import random
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, absolute_path, get_fal_api_key, get_openrouter_api_key, load_settings, read_json, write_json
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
    Deterministic and cheap (no API). Returns None if analysis fails."""
    try:
        return analyze_palette(image_bytes)["off_palette_pct"]
    except Exception:  # noqa: BLE001 - never let the gate break generation
        return None


def _generate_with_guardrail(settings, prompt, provider):
    """Generate art, then (if enabled) reject baked-in text, collage frames, or art
    that leans on colours the e-ink panel cannot show, and regenerate up to a
    bounded number of retries. Keeps the last attempt if every try is flagged -- a
    flawed image still beats a blank board. Each check fails open, so the guardrail
    never prevents a board from rendering."""
    pipeline = settings.get("pipeline", {})
    enabled = pipeline.get("enable_image_guardrail", False)
    if not enabled:
        return _call_image_api(settings, prompt, provider)

    max_retries = int(pipeline.get("image_guardrail_max_retries", 1))
    max_off_palette = float(pipeline.get("image_guardrail_max_off_palette_pct", 0.15))
    last_bytes = None
    for attempt in range(max_retries + 1):
        image_bytes = _call_image_api(settings, prompt, provider)
        last_bytes = image_bytes
        reasons = []
        verdict = inspect_art(image_bytes, settings)  # vision: text / collage
        if not verdict.get("ok", True):
            reasons += [k for k in ("has_text", "is_collage") if verdict.get(k)]
        off = _off_palette_pct(image_bytes)  # deterministic: off-palette colour
        if off is not None and off > max_off_palette:
            reasons.append(f"off_palette={off * 100:.0f}%")
        record_current_log(
            "generate_image", "guardrail_attempt",
            "Guardrail passed" if not reasons else "Guardrail rejected image",
            level="info" if not reasons else "warning",
            data={
                "attempt": attempt + 1,
                "verdict": verdict,
                "off_palette_pct": off,
                "reasons": reasons,
                "image_byte_size": len(image_bytes),
            },
        )
        if not reasons:
            if attempt:
                print(f"[image] guardrail passed on attempt {attempt + 1}")
            return image_bytes
        print(f"[image] guardrail rejected attempt {attempt + 1} ({', '.join(reasons)})")
    print("[image] guardrail retries exhausted; keeping last image")
    return last_bytes


def _report_image_failure(output_abs, detail):
    """On generation failure keep the last good hero (reuse) rather than blanking.

    Only when no prior hero exists do we fall through to a blank, which
    compose_board then replaces with a code-drawn pictogram.
    """
    if output_abs.exists():
        print(f"image-fallback-reuse (kept previous hero): {detail}")
    else:
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
    if reuse_reason is not None:
        _save_style_state(settings, style_state)
        record_current_log(
            "generate_image", "hero_reused", reuse_reason,
            data={
                "target_date": target_date,
                "style": style["name"],
                "palette": palette["name"],
            },
        )
        print(f"image-skip-reuse: keeping existing hero ({reuse_reason})")
        return

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
        image_bytes = _generate_with_guardrail(settings, prompt, provider)
        output_abs.write_bytes(image_bytes)
        # Record the prompt behind the current art so the afternoon refresh can
        # tell whether re-rendering would actually look different.
        style_state["hero_prompt"] = illustration_prompt
        _save_style_state(settings, style_state)
        record_current_log(
            "generate_image", "image_generated", "Wrote a new hero image",
            data={
                "target_date": target_date,
                "style": style["name"],
                "palette": palette["name"],
                "provider": provider,
                "byte_size": len(image_bytes),
                "output_path": str(output_abs),
            },
        )
        print(output_path)
    except urllib.error.URLError as error:
        _save_style_state(settings, style_state)
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
        _report_image_failure(output_abs, detail)
    except (KeyError, json.JSONDecodeError, RuntimeError) as error:
        _save_style_state(settings, style_state)
        record_current_log(
            "generate_image", "image_generation_failed", str(error), level="error",
            data={
                "error_type": type(error).__name__,
                "style": style["name"],
                "palette": palette["name"],
                "provider": provider,
            },
        )
        _report_image_failure(output_abs, str(error))


if __name__ == "__main__":
    main()
