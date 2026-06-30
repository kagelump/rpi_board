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
from scripts.ops.render_gate import compute_signature, should_regenerate


_PUNCT_MAP = {
    "–": "-", "—": "-", "‒": "-", "−": "-",  # dashes
    "‘": "'", "’": "'", "“": '"', "”": '"',  # smart quotes
    "…": "...",  # ellipsis
    " ": " ",  # nbsp
}


def _normalize_brief_punct(brief):
    """Fold common non-ASCII punctuation to ASCII so the board never renders a
    dropped glyph. The model is told ASCII-only, but it slips occasionally."""
    if not isinstance(brief, dict):
        return brief
    for key in ("headline", "subtitle", "illustration_prompt"):
        value = brief.get(key)
        if isinstance(value, str):
            for bad, good in _PUNCT_MAP.items():
                value = value.replace(bad, good)
            brief[key] = value
    return brief


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


def _load_recent_history(settings):
    """Return the last N briefs so the model can actively avoid repeating itself."""
    history_path = settings["runtime"].get("history_file")
    if not history_path:
        return []
    window = settings.get("voice", {}).get("history_window", 6)
    try:
        entries = read_json(history_path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(entries, list):
        return []
    return entries[-window:]


def _append_history(settings, day_context, brief):
    history_path = settings["runtime"].get("history_file")
    if not history_path:
        return
    try:
        entries = read_json(history_path)
        if not isinstance(entries, list):
            entries = []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        entries = []
    entries.append(
        {
            "date": day_context.get("date_iso", ""),
            "part_of_day": day_context.get("part_of_day", ""),
            "headline": brief.get("headline", ""),
            "subtitle": brief.get("subtitle", ""),
            "illustration_prompt": brief.get("illustration_prompt", ""),
        }
    )
    # Keep the file bounded; 60 entries is plenty for anti-repetition + debugging.
    write_json(history_path, entries[-60:])


def _load_last_good(settings):
    path = settings["runtime"].get("last_good_brief_file")
    if not path:
        return {}
    try:
        data = read_json(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_last_good(settings, signature, generated_at, brief):
    path = settings["runtime"].get("last_good_brief_file")
    if not path:
        return
    write_json(path, {"signature": signature, "generated_at": generated_at, "brief": brief})


def _load_day_context_extra(settings):
    """Load holidays/moon/calendar produced by fetch_context.py (optional)."""
    path = settings["runtime"].get("day_context_file")
    if not path:
        return {}
    try:
        extra = read_json(path)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return extra if isinstance(extra, dict) else {}


def _select_text_model(settings, override=None):
    """Append :online for live web grounding when events_mode requests it."""
    base = override or settings["openrouter"]["text_model"]
    events_mode = settings.get("context", {}).get("events_mode", "off")
    if events_mode == "online_model" and not base.endswith(":online"):
        return base + ":online"
    return base


_TIME_FRAMES = {
    "morning": "Morning briefing: set up the day ahead and what to wear heading out.",
    "midday": "Midday check-in: how the rest of today actually unfolds from here.",
    "evening": "Evening wind-down: tonight, plus a short look at tomorrow.",
    "night": "Late update: overnight conditions and the shape of tomorrow.",
}


def _time_frame(part_of_day):
    """A framing directive so the same weather reads differently across the day."""
    return _TIME_FRAMES.get(part_of_day, _TIME_FRAMES["midday"])


def _enrich_payload(payload, settings):
    """Attach voice, recent history, day context, and a creative angle.

    These ride inside INPUT_JSON (serialised by _render_prompt), so the prompt
    structure and its tests are untouched while the model gets much richer input.
    """
    enriched = dict(payload)
    enriched["voice"] = settings.get("voice", {})
    enriched["board_context"] = settings.get("context", {})
    enriched["recent_history"] = _load_recent_history(settings)
    # Merge the holidays/moon/calendar extras onto the weather-derived day_context.
    extra = _load_day_context_extra(settings)
    if extra:
        merged = dict(payload.get("day_context", {}))
        merged.update({k: v for k, v in extra.items() if k != "fetched_at"})
        enriched["day_context"] = merged
    # A rotating lens keeps consecutive days distinct even when weather repeats.
    angles = [
        "what to wear walking out the door",
        "the commute and getting around",
        "time outdoors: a walk, the park, the riverside",
        "an evening or after-dark beat",
        "a small seasonal observation in the sky or streets",
        "plans with other people",
        "food or a warm/cold drink that fits",
    ]
    # Seed on the forecast (target) day only -- never part_of_day -- so the same
    # creative angle holds across the evening / morning / afternoon refreshes of
    # one forecast day, keeping the daily theme fixed across all three boards.
    day_context = payload.get("day_context", {})
    seed_basis = day_context.get("target_date_iso") or day_context.get("date_iso", "")
    enriched["creative_angle"] = angles[sum(ord(c) for c in seed_basis) % len(angles)]
    enriched["time_frame"] = _time_frame(day_context.get("part_of_day"))
    return enriched


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
    temperature = settings["openrouter"].get("brief_temperature", 0.85)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
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
    parser.add_argument("--force", action="store_true",
                        help="Bypass the regen cache (interval/skip-unchanged) and regenerate now")
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

    # Cost guardrail: reuse the last good brief when the material inputs are
    # unchanged and we generated recently, so a frequent timer doesn't pay for
    # near-identical output. A new daypart or forecast change forces a refresh.
    signature = compute_signature(transformed)
    last_good = _load_last_good(settings)
    now_iso = transformed.get("generated_at_local")
    regenerate = should_regenerate(
        last_good,
        signature,
        now_iso,
        settings["pipeline"].get("regen_min_interval_seconds", 0),
        force=args.force_openrouter or args.force,
        skip_enabled=settings["pipeline"].get("skip_unchanged", False),
    )
    if not regenerate and isinstance(last_good.get("brief"), dict):
        transformed["brief"] = last_good["brief"]
        transformed["brief_source"] = "cached"
        write_json(output_path, transformed)
        print(f"[brief] reusing cached brief (signature {signature} unchanged within interval)")
        print(output_path)
        return

    template_path = ROOT / "config" / "prompt_templates" / "weather_brief.txt"
    template = template_path.read_text(encoding="utf-8")
    prompt = _render_prompt(template, _enrich_payload(transformed, settings))

    print(f"[brief] use_openrouter={use_openrouter}")
    try:
        model_name = _select_text_model(settings, override=args.model)
        print(f"[brief] requesting OpenRouter model={model_name}")
        candidate = _normalize_brief_punct(_call_openrouter(settings, prompt, model_override=model_name))
        if _is_valid_brief(candidate):
            transformed["brief"] = candidate
            transformed["brief_source"] = "openrouter"
            _save_last_good(settings, signature, now_iso, candidate)
            _append_history(settings, transformed.get("day_context", {}), candidate)
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
