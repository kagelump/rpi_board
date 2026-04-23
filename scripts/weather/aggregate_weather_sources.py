#!/usr/bin/env python3
import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import load_settings, read_json, write_json


MULTISOURCE_SCHEMA_VERSION = "1.0"


def _source_entry(name, available, payload, missing_sections):
    return {
        "source_name": name,
        "available": available,
        "missing_sections": sorted(set(missing_sections)),
        "payload": payload,
    }


def _yahoo_facts(yahoo_payload):
    facts = []
    for day_idx, day in enumerate(yahoo_payload.get("today_tomorrow", [])):
        base = f"yahoo.today_tomorrow.{day_idx}"
        facts.append(
            {
                "id": f"{base}.condition",
                "source": "yahoo",
                "text": f"{day.get('date_label')}({day.get('weekday')}): {day.get('condition')}",
                "value": day.get("condition"),
            }
        )
        facts.append(
            {
                "id": f"{base}.temperature",
                "source": "yahoo",
                "text": f"Temp {day.get('temp_min_c')}C to {day.get('temp_max_c')}C",
                "value": {"min_c": day.get("temp_min_c"), "max_c": day.get("temp_max_c")},
            }
        )
        for window in day.get("precipitation_windows", []):
            facts.append(
                {
                    "id": f"{base}.precip.{window.get('period')}",
                    "source": "yahoo",
                    "text": f"Rain {window.get('period')}={window.get('raw')}",
                    "value": window.get("precipitation_probability_pct"),
                }
            )

    for alert_idx, alert in enumerate(yahoo_payload.get("alerts", [])):
        facts.append(
            {
                "id": f"yahoo.alerts.{alert_idx}",
                "source": "yahoo",
                "text": f"{alert.get('level')}: {alert.get('text')}",
                "value": alert,
            }
        )

    for day_idx, day in enumerate(yahoo_payload.get("indices", {}).get("days", [])):
        for item_name, item in day.get("items", {}).items():
            facts.append(
                {
                    "id": f"yahoo.indices.{day_idx}.{item_name}",
                    "source": "yahoo",
                    "text": f"{day.get('date_label')} {item_name}={item.get('score_text')} ({item.get('note')})",
                    "value": item,
                }
            )
    return facts


def _open_meteo_facts(payload):
    raw = payload.get("raw", {})
    facts = []
    daily = raw.get("daily", {})
    for idx, date in enumerate(daily.get("time", [])):
        max_list = daily.get("temperature_2m_max", [])
        min_list = daily.get("temperature_2m_min", [])
        rain_list = daily.get("precipitation_probability_max", [])
        facts.append(
            {
                "id": f"open_meteo.daily.{idx}",
                "source": "open_meteo",
                "text": (
                    f"{date} max={max_list[idx] if idx < len(max_list) else None}C "
                    f"min={min_list[idx] if idx < len(min_list) else None}C "
                    f"rain_prob_max={rain_list[idx] if idx < len(rain_list) else None}%"
                ),
                "value": {
                    "date": date,
                    "temp_max_c": max_list[idx] if idx < len(max_list) else None,
                    "temp_min_c": min_list[idx] if idx < len(min_list) else None,
                    "rain_prob_max_pct": rain_list[idx] if idx < len(rain_list) else None,
                },
            }
        )
    return facts


def _detect_conflicts(yahoo_payload, open_meteo_payload):
    conflicts = []
    yahoo_today = yahoo_payload.get("today_tomorrow", [{}])[0]
    daily = open_meteo_payload.get("raw", {}).get("daily", {})
    if daily.get("temperature_2m_max"):
        open_meteo_max = daily["temperature_2m_max"][0]
        yahoo_max = yahoo_today.get("temp_max_c")
        if yahoo_max is not None and open_meteo_max is not None:
            delta = abs(float(yahoo_max) - float(open_meteo_max))
            if delta >= 3:
                conflicts.append(
                    {
                        "metric": "today.temp_max_c",
                        "yahoo": yahoo_max,
                        "open_meteo": open_meteo_max,
                        "delta": round(delta, 1),
                    }
                )
    return conflicts


def build_aggregated_context(settings, open_meteo_payload, yahoo_payload, extras):
    tz_name = settings["location"]["timezone"]
    now_local = datetime.now(ZoneInfo(tz_name)).replace(microsecond=0).isoformat()
    source_priority = settings["pipeline"].get("source_order", ["yahoo", "open_meteo"])
    normalized_sources = {
        "yahoo": _source_entry(
            "yahoo",
            available=bool(yahoo_payload),
            payload=yahoo_payload,
            missing_sections=yahoo_payload.get("missing_sections", []) if yahoo_payload else ["all"],
        ),
        "open_meteo": _source_entry(
            "open_meteo",
            available=bool(open_meteo_payload),
            payload=open_meteo_payload,
            missing_sections=[] if open_meteo_payload else ["all"],
        ),
    }
    for name, payload in extras.items():
        normalized_sources[name] = _source_entry(
            name,
            available=bool(payload),
            payload=payload,
            missing_sections=[] if payload else ["all"],
        )

    ordered_facts = []
    for source in source_priority:
        if source == "yahoo" and yahoo_payload:
            ordered_facts.extend(_yahoo_facts(yahoo_payload))
        elif source == "open_meteo" and open_meteo_payload:
            ordered_facts.extend(_open_meteo_facts(open_meteo_payload))
        elif source in extras and extras[source]:
            ordered_facts.append(
                {
                    "id": f"{source}.raw",
                    "source": source,
                    "text": f"{source} source included for prompt context",
                    "value": extras[source],
                }
            )

    missing_sections = []
    for source_data in normalized_sources.values():
        for section in source_data["missing_sections"]:
            missing_sections.append(f"{source_data['source_name']}:{section}")

    return {
        "schema_version": MULTISOURCE_SCHEMA_VERSION,
        "generated_at_local": now_local,
        "timezone": tz_name,
        "location": settings["location"],
        "source_priority": source_priority,
        "sources": normalized_sources,
        "ordered_facts": ordered_facts,
        "conflicts": _detect_conflicts(yahoo_payload or {}, open_meteo_payload or {}),
        "missing_sections": sorted(set(missing_sections)),
    }


def _load_optional_source(path):
    target = Path(path)
    if not target.exists():
        return None
    return read_json(str(target))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--open-meteo-input", default=None)
    parser.add_argument("--yahoo-input", default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    settings = load_settings()
    open_meteo_input = args.open_meteo_input or settings["runtime"]["payload_file"]
    yahoo_input = args.yahoo_input or settings["runtime"]["yahoo_weather_file"]
    output_path = args.output or settings["runtime"]["brief_context_file"]

    try:
        open_meteo_payload = read_json(open_meteo_input)
    except FileNotFoundError:
        open_meteo_payload = {}

    try:
        yahoo_payload = read_json(yahoo_input)
    except FileNotFoundError:
        yahoo_payload = {}

    extras = {}
    for source in settings["pipeline"].get("additional_sources", []):
        name = source.get("name")
        path = source.get("file")
        if not name or not path:
            continue
        extras[name] = _load_optional_source(path)

    payload = build_aggregated_context(settings, open_meteo_payload, yahoo_payload, extras)
    write_json(output_path, payload)
    print(output_path)


if __name__ == "__main__":
    main()
