#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")"/../.. && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${VENV_DIR}/bin/python3"

echo "[setup_pi] Project root: ${ROOT_DIR}"
echo "[setup_pi] Installing OS packages"
sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-pip \
  python3-venv \
  python3-dev \
  build-essential \
  swig \
  liblgpio-dev \
  python3-rpi-lgpio \
  git \
  curl \
  rsync \
  libjpeg-dev \
  zlib1g-dev \
  libfreetype6-dev

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[setup_pi] Creating virtualenv at ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
fi

echo "[setup_pi] Installing Python dependencies"
"${PYTHON_BIN}" -m pip install --upgrade pip
"${PYTHON_BIN}" -m pip install -r "${ROOT_DIR}/requirements.txt"
"${PYTHON_BIN}" -m pip install spidev RPi.GPIO gpiozero lgpio

echo "[setup_pi] Installing Waveshare driver bundle"
"${ROOT_DIR}/scripts/ops/install_waveshare_driver.sh"

echo "[setup_pi] Verifying SPI is enabled"
if [[ ! -e /dev/spidev0.0 ]]; then
  echo "[setup_pi] SPI device /dev/spidev0.0 missing."
  echo "[setup_pi] Enable SPI with: sudo raspi-config -> Interface Options -> SPI, then reboot."
else
  echo "[setup_pi] SPI device present."
fi

echo "[setup_pi] Next steps:"
echo "  1) Set OpenRouter key: printf '%s\\n' 'sk-or-v1-...' > ~/.openrouter.key && chmod 600 ~/.openrouter.key"
echo "  2) Run preflight: ${PYTHON_BIN} ${ROOT_DIR}/scripts/ops/preflight.py"
echo "  3) Run pipeline: ${ROOT_DIR}/scripts/display/update_display.sh"
