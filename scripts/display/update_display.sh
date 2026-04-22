#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../.. && pwd)"
cd "${ROOT_DIR}"

mkdir -p runtime runtime/logs

run_step() {
  local label="$1"
  shift
  echo "[weather-display] ${label}"
  "$@"
}

if run_step "fetch weather" python3 scripts/weather/fetch_weather.py; then
  run_step "transform weather" python3 scripts/weather/transform_weather.py
  run_step "generate brief" python3 scripts/openrouter/generate_brief.py
  run_step "generate image (optional)" python3 scripts/openrouter/generate_image.py || true
  run_step "compose board" python3 scripts/render/compose_board.py
  run_step "quantize palette" python3 scripts/render/palette_quantize.py
else
  echo "[weather-display] fetch failed, using last successful render if present"
  if [[ ! -f runtime/final_display.png ]]; then
    echo "[weather-display] no fallback render available" >&2
    exit 1
  fi
fi

run_step "push to display or preview" python3 scripts/display/push_to_epd.py
echo "[weather-display] complete"
