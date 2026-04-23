#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../.. && pwd)"
cd "${ROOT_DIR}"

mkdir -p runtime runtime/logs
DISPLAY_MODE_OVERRIDE="${DISPLAY_MODE_OVERRIDE:-}"
PYTHON_BIN="python3"
if [[ -x "${ROOT_DIR}/.venv/bin/python3" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"
fi

run_step() {
  local label="$1"
  shift
  echo "[weather-display] ${label}"
  "$@"
}

if run_step "fetch weather" "${PYTHON_BIN}" scripts/weather/fetch_weather.py; then
  run_step "fetch yahoo weather" "${PYTHON_BIN}" scripts/weather/fetch_yahoo_weather.py || true
  run_step "aggregate weather sources" "${PYTHON_BIN}" scripts/weather/aggregate_weather_sources.py
  run_step "transform weather" "${PYTHON_BIN}" scripts/weather/transform_weather.py
  run_step "generate brief" "${PYTHON_BIN}" scripts/openrouter/generate_brief.py
  run_step "generate image (optional)" "${PYTHON_BIN}" scripts/openrouter/generate_image.py || true
  run_step "compose board" "${PYTHON_BIN}" scripts/render/compose_board.py
  run_step "quantize palette" "${PYTHON_BIN}" scripts/render/palette_quantize.py
else
  echo "[weather-display] fetch failed, using last successful render if present"
  if [[ ! -f runtime/final_display.png ]]; then
    echo "[weather-display] no fallback render available" >&2
    exit 1
  fi
fi

if [[ -n "${DISPLAY_MODE_OVERRIDE}" ]]; then
  run_step "push to display or preview" "${PYTHON_BIN}" scripts/display/push_to_epd.py --mode "${DISPLAY_MODE_OVERRIDE}"
else
  run_step "push to display or preview" "${PYTHON_BIN}" scripts/display/push_to_epd.py
fi
echo "[weather-display] complete"
