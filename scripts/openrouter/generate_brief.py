#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, get_openrouter_api_key, load_settings, read_json, write_json


def _is_valid_brief(brief):
    if not isinstance(brief, dict):
        return False
    required_str = ["headline", "subtitle", "illustration_prompt"]
    for key in required_str:
        if key not in brief or not isinstance(brief[key], str):
            return False
    if len(brief["headline"].strip()) == 0 or len(brief["subtitle"].strip()) == 0:
        return False
    return True


def _render_prompt(template, payload):
    return template + "\n\nINPUT_JSON:\n" + json.dumps(payload, ensure_ascii=True)


def _call_openrouter(settings, prompt):
    api_key = get_openrouter_api_key(settings)
    if not api_key:
        raise RuntimeError(
            "OpenRouter key not found. Set OPENROUTER_API_KEY or place a key in "
            "~/.openrouter.key or ~/.config/openrouter/api_key"
        )
    timeout = settings["pipeline"]["brief_timeout_seconds"]
    url = settings["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
    body = {
        "model": settings["openrouter"]["text_model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise RuntimeError(f"openrouter brief request failed: {error}") from error

    content = payload["choices"][0]["message"]["content"]
    return json.loads(content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--force-openrouter", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    input_path = args.input or settings["runtime"]["brief_file"]
    output_path = args.output or settings["runtime"]["brief_file"]
    transformed = read_json(input_path)
    deterministic = transformed["brief"]

    use_openrouter = settings["pipeline"]["enable_openrouter_brief"] or args.force_openrouter
    if not use_openrouter:
        transformed["brief_source"] = "deterministic"
        write_json(output_path, transformed)
        print(output_path)
        return

    template_path = ROOT / "config" / "prompt_templates" / "weather_brief.txt"
    template = template_path.read_text(encoding="utf-8")
    prompt = _render_prompt(template, transformed)

    try:
        candidate = _call_openrouter(settings, prompt)
        if _is_valid_brief(candidate):
            transformed["brief"] = candidate
            transformed["brief_source"] = "openrouter"
        else:
            transformed["brief"] = deterministic
            transformed["brief_source"] = "deterministic_fallback_invalid_schema"
    except Exception:
        transformed["brief"] = deterministic
        transformed["brief_source"] = "deterministic_fallback_error"

    write_json(output_path, transformed)
    print(output_path)


if __name__ == "__main__":
    main()
