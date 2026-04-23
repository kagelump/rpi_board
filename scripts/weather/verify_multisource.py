#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import ROOT, load_settings, read_json
from scripts.weather.aggregate_weather_sources import build_aggregated_context
from scripts.weather.fetch_yahoo_weather import parse_yahoo_weather_html
from scripts.weather.transform_weather import build_payload


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def verify_yahoo_parser():
    fixture = ROOT / "config" / "fixtures" / "yahoo_tokyo_sample.html"
    html = fixture.read_text(encoding="utf-8")
    payload = parse_yahoo_weather_html(html, "https://weather.yahoo.co.jp/weather/jp/13/4410.html")
    _assert(len(payload["today_tomorrow"]) == 2, "expected two today/tomorrow entries")
    _assert(payload["today_tomorrow"][0]["condition"] == "雨", "expected rainy first-day condition")
    _assert(payload["alerts"], "expected at least one alert")
    _assert(payload["weekly"], "expected weekly forecast entries")
    _assert(payload["indices"]["days"], "expected daily index entries")
    return payload


def verify_aggregate_and_transform(yahoo_payload):
    settings = load_settings()
    open_meteo_payload = read_json(settings["runtime"]["payload_file"])
    context = build_aggregated_context(settings, open_meteo_payload, yahoo_payload, extras={})

    required_context_keys = {
        "schema_version",
        "generated_at_local",
        "timezone",
        "location",
        "source_priority",
        "sources",
        "ordered_facts",
        "conflicts",
        "missing_sections",
    }
    _assert(required_context_keys.issubset(set(context.keys())), "missing required context keys")
    _assert(context["ordered_facts"], "ordered_facts should not be empty")

    transformed = build_payload(context)
    for key in ("generated_at_local", "timezone", "location", "today", "tomorrow", "brief", "brief_context"):
        _assert(key in transformed, f"missing transformed key: {key}")
    for key in ("headline", "subtitle", "illustration_prompt"):
        _assert(key in transformed["brief"], f"missing brief key: {key}")
    _assert(
        transformed["brief_context"]["ordered_facts"],
        "transformed brief_context should include ordered_facts",
    )


def main():
    yahoo_payload = verify_yahoo_parser()
    verify_aggregate_and_transform(yahoo_payload)
    print("verify_multisource.py: all checks passed")


if __name__ == "__main__":
    main()
