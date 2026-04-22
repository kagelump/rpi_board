#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, absolute_path, get_openrouter_api_key, load_settings, read_json


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


def _download_image(image_url, timeout):
    with urllib.request.urlopen(image_url, timeout=timeout, context=_ssl_context()) as response:
        return response.read()


def _ssl_context():
    ctx = ssl.create_default_context()
    settings = load_settings()
    ca_bundle = settings.get("openrouter", {}).get("ca_bundle_file")
    if ca_bundle:
        ca_path = Path(ca_bundle).expanduser()
        if ca_path.exists():
            ctx.load_verify_locations(cafile=str(ca_path))
            return ctx

    # Python installs on macOS can miss system trust linkage; certifi is a solid default.
    try:
        import certifi  # type: ignore

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


def _proxy_hint():
    keys = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]
    present = [f"{k}={os.getenv(k)}" for k in keys if os.getenv(k)]
    if not present:
        return "none"
    return ", ".join(present)


def _describe_error(error):
    reason = getattr(error, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return (
            "TLS certificate verification failed. If you are behind a proxy with custom "
            "root certificates, configure openrouter.ca_bundle_file in config/settings.json "
            "to point to that CA bundle. Active proxy env: "
            + _proxy_hint()
        )
    message = str(error)
    if "Tunnel connection failed: 403" in message or "CONNECT tunnel failed" in message:
        return (
            "Proxy tunnel rejected OpenRouter (HTTP 403). Check proxy allowlist/policy "
            "for openrouter.ai. Active proxy env: "
            + _proxy_hint()
        )
    return message


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
    body = {
        "model": tool_model,
        "input": prompt,
        "tools": [
            {
                "type": "openrouter:image_generation",
                "parameters": {
                    "model": image_model,
                    "size": "1024x1024",
                    "output_format": "png",
                },
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
    with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if payload.get("error"):
        error = payload["error"]
        raise RuntimeError(
            f"OpenRouter server tool failed ({error.get('code', 'unknown')}): "
            f"{error.get('message', 'unknown error')}"
        )

    image_url = _extract_image_url(payload)
    if image_url:
        return _download_image(image_url, timeout=timeout)

    data_image = _extract_data_image(payload)
    if data_image:
        encoded = data_image.split(";base64,", 1)[1]
        return base64.b64decode(encoded)

    markdown_path = _extract_markdown_image_path(payload)
    if markdown_path:
        if markdown_path.startswith(("http://", "https://")):
            return _download_image(markdown_path, timeout=timeout)
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
        print(f"image-fallback-blank: {_describe_error(error)}")
    except (KeyError, json.JSONDecodeError, RuntimeError) as error:
        if output_abs.exists():
            output_abs.unlink()
        print(f"image-fallback-blank: {error}")


if __name__ == "__main__":
    main()
