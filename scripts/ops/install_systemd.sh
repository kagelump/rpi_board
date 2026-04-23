#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../.. && pwd)"
SYSTEMD_SRC_DIR="${ROOT_DIR}/scripts/ops/systemd"
SERVICE_TEMPLATE="${SYSTEMD_SRC_DIR}/weather-eink-board.service"
TIMER_TEMPLATE="${SYSTEMD_SRC_DIR}/weather-eink-board.timer"
SERVICE_NAME="weather-eink-board.service"
TIMER_NAME="weather-eink-board.timer"
SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}"
TIMER_DEST="/etc/systemd/system/${TIMER_NAME}"
RUN_USER="${SUDO_USER:-$USER}"
PYTHON_PATH="${ROOT_DIR}/.venv/bin/python3"

if [[ ! -f "${SERVICE_TEMPLATE}" || ! -f "${TIMER_TEMPLATE}" ]]; then
  echo "[install_systemd] Missing systemd templates in ${SYSTEMD_SRC_DIR}" >&2
  exit 1
fi

if [[ ! -x "${PYTHON_PATH}" ]]; then
  echo "[install_systemd] Missing ${PYTHON_PATH}. Run scripts/ops/setup_pi.sh first." >&2
  exit 1
fi

tmp_service="$(mktemp)"
trap 'rm -f "${tmp_service}"' EXIT

sed \
  -e "s|__ROOT_DIR__|${ROOT_DIR}|g" \
  -e "s|__RUN_USER__|${RUN_USER}|g" \
  -e "s|__PYTHON_BIN__|${PYTHON_PATH}|g" \
  "${SERVICE_TEMPLATE}" > "${tmp_service}"

echo "[install_systemd] Installing ${SERVICE_NAME} and ${TIMER_NAME}"
sudo cp "${tmp_service}" "${SERVICE_DEST}"
sudo cp "${TIMER_TEMPLATE}" "${TIMER_DEST}"
sudo chmod 644 "${SERVICE_DEST}" "${TIMER_DEST}"

sudo systemctl daemon-reload
sudo systemctl enable --now "${TIMER_NAME}"

echo "[install_systemd] Installed and enabled timer."
echo "[install_systemd] Check status with:"
echo "  systemctl status ${SERVICE_NAME}"
echo "  systemctl status ${TIMER_NAME}"
echo "  systemctl list-timers ${TIMER_NAME}"
