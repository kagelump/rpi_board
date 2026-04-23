#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../.. && pwd)"
cd "${ROOT_DIR}"

mkdir -p runtime runtime/logs
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

run_step "push to display or preview" "${PYTHON_BIN}" scripts/display/push_to_epd.py
echo "[weather-display] complete"
