#!/usr/bin/env python3
import argparse
import base64
import json
import re
import sys
import urllib.error
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, absolute_path, get_openrouter_api_key, load_settings, read_json
from scripts.openrouter.network import describe_network_error, urlopen_with_context


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
    prompt = template.replace("{{BRIEF_JSON}}", json.dumps(payload["brief"], ensure_ascii=True))

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
