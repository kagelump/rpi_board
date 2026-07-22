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

# --force re-fetches/regenerates everything, bypassing the brief regen cache,
# the cached-hero skip, and the on-disk holiday cache. (Weather is always
# fetched fresh regardless.)
# --tomorrow forces the "9pm primary" role regardless of the current hour:
# targets tomorrow's forecast, rolls a new art style, and regenerates fully.
FORCE_FLAG=""
for arg in "$@"; do
  case "${arg}" in
    --force|-f) FORCE_FLAG="--force" ;;
    --tomorrow) export FORECAST_TARGET=tomorrow; FORCE_FLAG="--force" ;;
    -h|--help) echo "usage: update_display.sh [--force] [--tomorrow]"; exit 0 ;;
    *) echo "[weather-display] unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done
if [[ -n "${FORCE_FLAG}" ]]; then
  echo "[weather-display] --force: bypassing brief/hero/holiday caches"
fi
if [[ "${FORECAST_TARGET:-}" == "tomorrow" ]]; then
  echo "[weather-display] --tomorrow: targeting tomorrow's forecast (primary role)"
fi

GENERATION_RUN_ID=""
GENERATION_FINISHED=0
if GENERATION_RUN_ID="$("${PYTHON_BIN}" scripts/history/record.py start \
  ${FORCE_FLAG:+--forced} \
  ${FORECAST_TARGET:+--forecast-target "${FORECAST_TARGET}"})"; then
  export GENERATION_RUN_ID
  echo "[weather-display] history run=${GENERATION_RUN_ID}"
else
  GENERATION_RUN_ID=""
  echo "[weather-display] warning: history recording unavailable" >&2
fi

finish_generation_on_exit() {
  local exit_code=$?
  if [[ -n "${GENERATION_RUN_ID}" && "${GENERATION_FINISHED}" -eq 0 ]]; then
    GENERATION_FINISHED=1
    "${PYTHON_BIN}" scripts/history/record.py finish "${GENERATION_RUN_ID}" \
      --status failed --error "pipeline exited with code ${exit_code}" >/dev/null 2>&1 || true
  fi
}
trap finish_generation_on_exit EXIT

run_step() {
  local stage="$1"
  local label="$2"
  shift 2
  local stage_id=""
  local stage_log=""
  local exit_code=0
  if [[ -n "${GENERATION_RUN_ID}" ]]; then
    stage_id="$("${PYTHON_BIN}" scripts/history/record.py stage-start "${GENERATION_RUN_ID}" "${stage}" 2>/dev/null || true)"
  fi
  echo "[weather-display] ${label}"
  if [[ -n "${GENERATION_RUN_ID}" ]]; then
    stage_log="$(mktemp)"
    set +e
    "$@" 2>&1 | tee "${stage_log}"
    exit_code=${PIPESTATUS[0]}
    set -e
    "${PYTHON_BIN}" scripts/history/record.py stage-log \
      "${GENERATION_RUN_ID}" "${stage}" "${stage_log}" >/dev/null 2>&1 || true
    rm -f "${stage_log}"
  else
    if "$@"; then
      exit_code=0
    else
      exit_code=$?
    fi
  fi
  if [[ -n "${stage_id}" ]]; then
    if [[ "${exit_code}" -eq 0 ]]; then
      "${PYTHON_BIN}" scripts/history/record.py stage-end "${stage_id}" \
        --status succeeded --exit-code 0 >/dev/null 2>&1 || true
      "${PYTHON_BIN}" scripts/history/record.py capture "${GENERATION_RUN_ID}" "${stage}" \
        >/dev/null 2>&1 || true
    else
      "${PYTHON_BIN}" scripts/history/record.py stage-end "${stage_id}" \
        --status failed --exit-code "${exit_code}" >/dev/null 2>&1 || true
    fi
  fi
  return "${exit_code}"
}

if run_step "fetch_weather" "fetch weather" "${PYTHON_BIN}" scripts/weather/fetch_weather.py; then
  run_step "fetch_yahoo_weather" "fetch yahoo weather" "${PYTHON_BIN}" scripts/weather/fetch_yahoo_weather.py || true
  run_step "aggregate_weather_sources" "aggregate weather sources" "${PYTHON_BIN}" scripts/weather/aggregate_weather_sources.py
  run_step "transform_weather" "transform weather" "${PYTHON_BIN}" scripts/weather/transform_weather.py
  run_step "fetch_day_context" "fetch day context" "${PYTHON_BIN}" scripts/weather/fetch_context.py ${FORCE_FLAG} || true
  run_step "generate_brief" "generate brief" "${PYTHON_BIN}" scripts/openrouter/generate_brief.py ${FORCE_FLAG}
  run_step "generate_image" "generate image (optional)" "${PYTHON_BIN}" scripts/openrouter/generate_image.py ${FORCE_FLAG} || true
  run_step "compose_board" "compose board" "${PYTHON_BIN}" scripts/render/compose_board.py
  run_step "quantize_palette" "quantize palette" "${PYTHON_BIN}" scripts/render/palette_quantize.py
else
  echo "[weather-display] fetch failed, using last successful render if present"
  if [[ ! -f runtime/final_display.png ]]; then
    echo "[weather-display] no fallback render available" >&2
    exit 1
  fi
fi

if [[ -n "${DISPLAY_MODE_OVERRIDE}" ]]; then
  run_step "push_to_display" "push to display or preview" "${PYTHON_BIN}" scripts/display/push_to_epd.py --mode "${DISPLAY_MODE_OVERRIDE}"
else
  run_step "push_to_display" "push to display or preview" "${PYTHON_BIN}" scripts/display/push_to_epd.py
fi
if [[ -n "${GENERATION_RUN_ID}" ]]; then
  "${PYTHON_BIN}" scripts/history/record.py finish "${GENERATION_RUN_ID}" --status succeeded >/dev/null 2>&1 || true
  GENERATION_FINISHED=1
fi
echo "[weather-display] complete"
