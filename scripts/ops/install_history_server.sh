#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../.. && pwd)"
TEMPLATE="${ROOT_DIR}/scripts/ops/systemd/weather-eink-history.service"
DESTINATION="/etc/systemd/system/weather-eink-history.service"
RUN_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python3"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

tmp_service="$(mktemp)"
trap 'rm -f "${tmp_service}"' EXIT
sed \
  -e "s|__ROOT_DIR__|${ROOT_DIR}|g" \
  -e "s|__RUN_USER__|${RUN_USER}|g" \
  -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
  "${TEMPLATE}" > "${tmp_service}"

sudo cp "${tmp_service}" "${DESTINATION}"
sudo chmod 644 "${DESTINATION}"
sudo systemctl daemon-reload
sudo systemctl enable --now weather-eink-history.service

echo "[history-server] Installed at http://127.0.0.1:8787"
echo "[history-server] From another machine, tunnel with:"
echo "  ssh -L 8787:127.0.0.1:8787 ${RUN_USER}@<raspberry-pi>"
