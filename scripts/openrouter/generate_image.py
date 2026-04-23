#!/usr/bin/env python3
import argparse
import base64
import json
import random
import re
import sys
import urllib.error
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, absolute_path, get_openrouter_api_key, load_settings, read_json, write_json
from scripts.openrouter.network import describe_network_error, urlopen_with_context


ART_STYLE_POOL = [
    {
        "name": "Bauhaus",
        "prompt": "Bauhaus poster, Swiss grid discipline, geometric abstraction, bold black structure with red/yellow accents",
    },
    {
        "name": "Constructivism",
        "prompt": "Soviet constructivist poster language, stark geometry, dynamic diagonals, red-black-white impact",
    },
    {
        "name": "Pop Art Comic",
        "prompt": "Lichtenstein-inspired pop art, thick black outlines, comic-panel clarity, Ben-Day-dot-like flat patterning",
    },
    {
        "name": "Minimal Ukiyo-e",
        "prompt": "minimal ukiyo-e inspired woodblock print, strong contour lines, flat color fields, elegant negative space",
    },
    {
        "name": "De Stijl",
        "prompt": "De Stijl / Mondrian composition, orthogonal black lines, primary color blocks, severe geometric balance",
    },
    {
        "name": "WPA Travel Poster",
        "prompt": "1930s WPA travel poster, flat vector screen-print look, strong silhouettes, clear atmospheric storytelling",
    },
    {
        "name": "Linocut",
        "prompt": "linocut print aesthetic, carved high-contrast shapes, simplified forms, bold inked contours",
    },
    {
        "name": "Stencil Graphic",
        "prompt": "stencil graphic poster style, cutout shapes, hard edges, minimal details, strong visual hierarchy",
    },
    {
        "name": "Pictogram Minimalism",
        "prompt": "modern pictogram minimalism, icon-like weather motifs, clean vectors, immediate readability",
    },
]

NEGATIVE_STYLE_CONSTRAINTS = (
    "Avoid gradients, soft shading, photorealism, 3D rendering, blur, glow, "
    "depth of field, painterly texture, and dense micro-details."
)


def _style_state_path(settings):
    state_path = settings["runtime"].get("image_style_state_file", "runtime/image_style_state.json")
    return absolute_path(state_path)


def _pick_art_style(settings):
    styles = list(ART_STYLE_POOL)
    names = [item["name"] for item in styles]
    by_name = {item["name"]: item for item in styles}
    state_file = _style_state_path(settings)
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    remaining = [name for name in state.get("remaining", []) if name in by_name]
    if not remaining:
        remaining = names[:]
        random.SystemRandom().shuffle(remaining)

    chosen_name = remaining.pop(0)
    write_json(str(state_file), {"remaining": remaining, "last_selected": chosen_name})
    return by_name[chosen_name]


def _inject_style_prompt(template, brief_payload, style):
    style_block = (
        f"Selected art style: {style['name']}\n"
        f"Style direction: {style['prompt']}\n"
        f"{NEGATIVE_STYLE_CONSTRAINTS}"
    )
    prompt = template.replace("{{BRIEF_JSON}}", json.dumps(brief_payload, ensure_ascii=True))
    if "{{STYLE_GUIDANCE}}" in prompt:
        return prompt.replace("{{STYLE_GUIDANCE}}", style_block)
    return prompt + "\n\nStyle guidance:\n" + style_block


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


def _call_image_api(settings, prompt):
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

    raise RuntimeError(f"No image URL found in response: {payload}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--force-openrouter", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    input_path = args.input or settings["runtime"]["brief_file"]
    output_path = args.output or settings["runtime"]["hero_file"]
    output_abs = absolute_path(output_path)
    output_abs.parent.mkdir(parents=True, exist_ok=True)

    use_openrouter = settings["pipeline"]["enable_openrouter_image"] or args.force_openrouter
    if not use_openrouter:
        if output_abs.exists():
            output_abs.unlink()
        print("image-disabled")
        return

    payload = read_json(input_path)
    template_path = ROOT / "config" / "prompt_templates" / "weather_image.txt"
    template = template_path.read_text(encoding="utf-8")
    style = _pick_art_style(settings)
    prompt = _inject_style_prompt(template, payload["brief"], style)
    print(f"[image] selected_style={style['name']}")

    try:
        image_bytes = _call_image_api(settings, prompt)
        output_abs.write_bytes(image_bytes)
        print(output_path)
    except urllib.error.URLError as error:
        if output_abs.exists():
            output_abs.unlink()
        print(f"image-fallback-blank: {describe_network_error(error)}")
    except (KeyError, json.JSONDecodeError, RuntimeError) as error:
        if output_abs.exists():
            output_abs.unlink()
        print(f"image-fallback-blank: {error}")


if __name__ == "__main__":
    main()
