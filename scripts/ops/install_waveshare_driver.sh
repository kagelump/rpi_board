#!/usr/bin/env bash
set -euo pipefail

TARGET_DIR="${TARGET_DIR:-$HOME/e-Paper}"
WAVESHARE_REPO="${WAVESHARE_REPO:-https://github.com/waveshare/e-Paper.git}"
BCM_TARBALL_URL="${BCM_TARBALL_URL:-http://www.airspayce.com/mikem/bcm2835/bcm2835-1.71.tar.gz}"
BCM_LIB="/usr/local/lib/libbcm2835.a"

echo "[waveshare] target dir: ${TARGET_DIR}"

if [[ ! -d "${TARGET_DIR}" ]]; then
  if sudo test -d "/home/trainboard/e-Paper"; then
    echo "[waveshare] copying existing driver tree from /home/trainboard/e-Paper"
    sudo rsync -a "/home/trainboard/e-Paper/" "${TARGET_DIR}/"
    sudo chown -R "${USER}:${USER}" "${TARGET_DIR}"
  else
    echo "[waveshare] cloning ${WAVESHARE_REPO}"
    git clone --depth 1 "${WAVESHARE_REPO}" "${TARGET_DIR}"
  fi
else
  if [[ -d "${TARGET_DIR}/.git" ]]; then
    echo "[waveshare] driver tree already present"
  else
    echo "[waveshare] removing incomplete driver tree"
    rm -rf "${TARGET_DIR}"
    git clone --depth 1 "${WAVESHARE_REPO}" "${TARGET_DIR}"
  fi
fi

if [[ ! -f "${BCM_LIB}" ]]; then
  echo "[waveshare] installing BCM2835 library"
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "${tmp_dir}"' EXIT
  curl -fsSL "${BCM_TARBALL_URL}" -o "${tmp_dir}/bcm2835.tar.gz"
  tar -xzf "${tmp_dir}/bcm2835.tar.gz" -C "${tmp_dir}"
  bcm_dir="$(echo "${tmp_dir}"/bcm2835-*)"
  (
    cd "${bcm_dir}"
    ./configure
    make
    make check
    sudo make install
  )
else
  echo "[waveshare] BCM2835 library already installed"
fi

echo "[waveshare] install complete"
