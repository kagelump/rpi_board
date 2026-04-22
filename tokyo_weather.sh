#!/usr/bin/env bash
set -euo pipefail

# Default coordinates near Tokyo 153-0051 (Komaba, Meguro City).
LATITUDE="${LATITUDE:-35.6580}"
LONGITUDE="${LONGITUDE:-139.6835}"
TIMEZONE="${TIMEZONE:-Asia/Tokyo}"
LOCATION_LABEL="${LOCATION_LABEL:-Tokyo 153-0051}"
OUTPUT_MODE="both"

usage() {
  cat <<'EOF'
Usage: ./tokyo_weather.sh [flags]

Flags:
  --human-only               Print only the human-readable section
  --json-only                Print only the machine-readable JSON section
  --both                     Print both sections (default)
  --lat <value>              Override latitude (default: 35.6580)
  --lon <value>              Override longitude (default: 139.6835)
  --timezone <value>         Override timezone (default: Asia/Tokyo)
  --location-label <value>   Override display label (default: Tokyo 153-0051)
  --help                     Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --human-only)
      OUTPUT_MODE="human"
      shift
      ;;
    --json-only)
      OUTPUT_MODE="json"
      shift
      ;;
    --both)
      OUTPUT_MODE="both"
      shift
      ;;
    --lat)
      [[ $# -ge 2 ]] || { echo "Missing value for --lat" >&2; exit 1; }
      LATITUDE="$2"
      shift 2
      ;;
    --lon)
      [[ $# -ge 2 ]] || { echo "Missing value for --lon" >&2; exit 1; }
      LONGITUDE="$2"
      shift 2
      ;;
    --timezone)
      [[ $# -ge 2 ]] || { echo "Missing value for --timezone" >&2; exit 1; }
      TIMEZONE="$2"
      shift 2
      ;;
    --location-label)
      [[ $# -ge 2 ]] || { echo "Missing value for --location-label" >&2; exit 1; }
      LOCATION_LABEL="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown flag: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

API_URL="https://api.open-meteo.com/v1/forecast"
TIMEZONE_ESCAPED="${TIMEZONE//\//%2F}"
QUERY="latitude=${LATITUDE}&longitude=${LONGITUDE}&timezone=${TIMEZONE_ESCAPED}&forecast_days=3&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum&hourly=temperature_2m,precipitation_probability,precipitation,weather_code"
json_response="$(curl -fsSL "${API_URL}?${QUERY}")"

python3 - "$json_response" "$OUTPUT_MODE" "$LOCATION_LABEL" <<'PY'
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

payload = json.loads(sys.argv[1])
output_mode = sys.argv[2]
location_label = sys.argv[3]
tz = payload.get("timezone", "Asia/Tokyo")

daily = payload["daily"]
hourly = payload["hourly"]
times_daily = daily["time"]
times_hourly = hourly["time"]

today = datetime.strptime(times_daily[0], "%Y-%m-%d").date()
tomorrow = today + timedelta(days=1)
today_str = today.isoformat()
tomorrow_str = tomorrow.isoformat()

if tomorrow_str not in times_daily:
    raise SystemExit(f"Could not find tomorrow ({tomorrow_str}) in daily forecast.")

daily_idx = times_daily.index(tomorrow_str)

weather_labels = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Heavy thunderstorm with hail",
}

try:
    now_local = datetime.now(ZoneInfo(tz))
except Exception:
    now_local = datetime.utcnow() + timedelta(hours=9)
current_hour_floor = now_local.replace(minute=0, second=0, microsecond=0, tzinfo=None)

def daily_summary(day_idx):
    code = daily["weather_code"][day_idx]
    return {
        "date": daily["time"][day_idx],
        "condition": weather_labels.get(code, f"Unknown ({code})"),
        "weather_code": code,
        "temp_max_c": round(daily["temperature_2m_max"][day_idx], 1),
        "temp_min_c": round(daily["temperature_2m_min"][day_idx], 1),
        "precipitation_probability_max_pct": daily["precipitation_probability_max"][day_idx],
        "precipitation_sum_mm": round(daily["precipitation_sum"][day_idx], 1),
    }

def hourly_rows_for_date(date_str, remaining_today=False):
    rows = []
    for i, stamp in enumerate(times_hourly):
        if not stamp.startswith(date_str):
            continue
        dt = datetime.fromisoformat(stamp)
        if remaining_today and dt < current_hour_floor:
            continue
        code = hourly["weather_code"][i]
        rows.append(
            {
                "time": stamp,
                "temp_c": round(hourly["temperature_2m"][i], 1),
                "precipitation_probability_pct": hourly["precipitation_probability"][i],
                "precipitation_mm": round(hourly["precipitation"][i], 1),
                "weather_code": code,
                "condition": weather_labels.get(code, f"Unknown ({code})"),
            }
        )
    return rows

today_idx = times_daily.index(today_str)
today_summary = daily_summary(today_idx)
tomorrow_summary = daily_summary(daily_idx)
today_remaining_hourly = hourly_rows_for_date(today_str, remaining_today=True)
tomorrow_hourly = hourly_rows_for_date(tomorrow_str, remaining_today=False)

if output_mode in ("both", "human"):
    print("=== HUMAN_READABLE ===")
    print(f"Location: {location_label}")
    print(f"Timezone: {tz}")
    print(f"Generated_at_local: {now_local.strftime('%Y-%m-%dT%H:%M:%S')}")
    print("")
    print(f"[TODAY_REMAINING] {today_summary['date']}")
    print(
        "Daily summary: "
        f"{today_summary['condition']}, "
        f"high {today_summary['temp_max_c']:.1f}C, "
        f"low {today_summary['temp_min_c']:.1f}C, "
        f"max rain chance {today_summary['precipitation_probability_max_pct']}%, "
        f"rain total {today_summary['precipitation_sum_mm']:.1f} mm"
    )
    print("time,temp_c,rain_pct,rain_mm,condition")
    for row in today_remaining_hourly:
        hhmm = row["time"].split("T", 1)[1]
        print(
            f"{hhmm},{row['temp_c']:.1f},{row['precipitation_probability_pct']},"
            f"{row['precipitation_mm']:.1f},{row['condition']}"
        )

    print("")
    print(f"[TOMORROW] {tomorrow_summary['date']}")
    print(
        "Daily summary: "
        f"{tomorrow_summary['condition']}, "
        f"high {tomorrow_summary['temp_max_c']:.1f}C, "
        f"low {tomorrow_summary['temp_min_c']:.1f}C, "
        f"max rain chance {tomorrow_summary['precipitation_probability_max_pct']}%, "
        f"rain total {tomorrow_summary['precipitation_sum_mm']:.1f} mm"
    )
    print("time,temp_c,rain_pct,rain_mm,condition")
    for row in tomorrow_hourly:
        hhmm = row["time"].split("T", 1)[1]
        print(
            f"{hhmm},{row['temp_c']:.1f},{row['precipitation_probability_pct']},"
            f"{row['precipitation_mm']:.1f},{row['condition']}"
        )

llm_payload = {
    "location": {
        "name": location_label,
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
    },
    "timezone": tz,
    "generated_at_local": now_local.strftime("%Y-%m-%dT%H:%M:%S"),
    "today_remaining": {
        "date": today_summary["date"],
        "daily_summary": today_summary,
        "hourly": today_remaining_hourly,
    },
    "tomorrow": {
        "date": tomorrow_summary["date"],
        "daily_summary": tomorrow_summary,
        "hourly": tomorrow_hourly,
    },
}

if output_mode in ("both", "json"):
    if output_mode == "both":
        print("")
    print("=== LLM_JSON ===")
    print(json.dumps(llm_payload, ensure_ascii=True))
PY
