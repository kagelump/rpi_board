#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, get_openrouter_api_key, load_settings, read_json, write_json
from scripts.openrouter.network import describe_network_error, urlopen_with_context


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
    ordered_facts = payload.get("brief_context", {}).get("ordered_facts", [])
    return (
        template
        + "\n\nORDERED_FACTS:\n"
        + json.dumps(ordered_facts, ensure_ascii=True)
        + "\n\nINPUT_JSON:\n"
        + json.dumps(payload, ensure_ascii=True)
    )


def _call_openrouter(settings, prompt, model_override=None):
    api_key = get_openrouter_api_key(settings)
    if not api_key:
        raise RuntimeError(
            "OpenRouter key not found. Set OPENROUTER_API_KEY or place a key in "
            "~/.openrouter.key or ~/.config/openrouter/api_key"
        )
    timeout = settings["pipeline"]["brief_timeout_seconds"]
    url = settings["openrouter"]["base_url"].rstrip("/") + "/chat/completions"
    model = model_override or settings["openrouter"]["text_model"]
    body = {
        "model": model,
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
        with urlopen_with_context(request, timeout=timeout, settings=settings) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as error:
        raise RuntimeError(f"openrouter brief request failed: {describe_network_error(error)}") from error

    content = payload["choices"][0]["message"]["content"]
    return json.loads(content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--force-openrouter", action="store_true")
    parser.add_argument("--model", default=None, help="Override OpenRouter text model for this run")
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

    print(f"[brief] use_openrouter={use_openrouter}")
    try:
        model_name = args.model or settings["openrouter"]["text_model"]
        print(f"[brief] requesting OpenRouter model={model_name}")
        candidate = _call_openrouter(settings, prompt, model_override=args.model)
        if _is_valid_brief(candidate):
            transformed["brief"] = candidate
            transformed["brief_source"] = "openrouter"
            print("[brief] OpenRouter response accepted: ")
            print(json.dumps(candidate, indent=2, ensure_ascii=True))
        else:
            transformed["brief"] = deterministic
            transformed["brief_source"] = "deterministic_fallback_invalid_schema"
            print("[brief] OpenRouter response rejected (invalid schema); using deterministic fallback.")
    except Exception as error:
        transformed["brief"] = deterministic
        transformed["brief_source"] = "deterministic_fallback_error"
        print(f"[brief] OpenRouter request failed ({type(error).__name__}: {error}); using deterministic fallback.")

    write_json(output_path, transformed)
    print(output_path)


if __name__ == "__main__":
    main()
