#!/usr/bin/env python3
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from scripts.common import load_settings, read_json, write_json


def build_query(settings):
    location = settings["location"]
    params = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "timezone": location["timezone"],
        "forecast_days": 3,
        "daily": (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,precipitation_sum"
        ),
        "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
    }
    return urllib.parse.urlencode(params)


def fetch_forecast(settings):
    timeout = settings["pipeline"]["open_meteo_timeout_seconds"]
    retries = settings["pipeline"]["open_meteo_retry_count"]
    base_url = "https://api.open-meteo.com/v1/forecast"
    url = f"{base_url}?{build_query(settings)}"
    last_error = None

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"failed to fetch weather from Open-Meteo: {last_error}")


def main():
    settings = load_settings()
    try:
        raw = fetch_forecast(settings)
        source = "open-meteo"
    except RuntimeError:
        if not settings["pipeline"].get("allow_sample_weather_on_failure", False):
            raise
        sample_file = settings["pipeline"]["sample_weather_file"]
        raw = read_json(sample_file)
        source = "sample-fallback"
    payload = {
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "location": settings["location"],
        "source": source,
        "raw": raw,
    }
    write_json(settings["runtime"]["payload_file"], payload)
    print(settings["runtime"]["payload_file"])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"fetch_weather.py error: {exc}", file=sys.stderr)
        raise
