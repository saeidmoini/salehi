#!/usr/bin/env bash
set -euo pipefail

# Simple local deploy script for Salehi
# Runs a pull + venv deps + service restart from the repo root.

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="salehi.service"

echo "[salehi] Updating source in ${APP_DIR}"
cd "${APP_DIR}"

git fetch --all --prune
git reset --hard origin/main

python3 -m venv "${APP_DIR}/venv" || true
source "${APP_DIR}/venv/bin/activate"
pip install --upgrade pip
pip install --upgrade -r "${APP_DIR}/requirements.txt"

# Sync custom audio prompts to Asterisk sounds/custom if present
CUSTOM_SRC="${APP_DIR}/assets/audio"
CUSTOM_DEST="/var/lib/asterisk/sounds/custom"
if [ -d "${CUSTOM_SRC}" ] && [ -n "$(ls -A "${CUSTOM_SRC}")" ]; then
  echo "[salehi] Syncing custom audio prompts to ${CUSTOM_DEST}"
  if sudo -n true 2>/dev/null; then
    sudo mkdir -p "${CUSTOM_DEST}"
    sudo cp -f "${CUSTOM_SRC}/"* "${CUSTOM_DEST}/"
    sudo chown asterisk:asterisk "${CUSTOM_DEST}/"*
  else
    mkdir -p "${CUSTOM_DEST}"
    cp -f "${CUSTOM_SRC}/"* "${CUSTOM_DEST}/"
    chown asterisk:asterisk "${CUSTOM_DEST}/"*
  fi
fi

if command -v systemctl >/dev/null 2>&1; then
  echo "[salehi] Restarting ${SERVICE_NAME}"
  if sudo -n true 2>/dev/null; then
    sudo systemctl restart "${SERVICE_NAME}"
  else
    systemctl restart "${SERVICE_NAME}"
  fi
else
  echo "[salehi] systemctl not found; skipping service restart"
fi

echo "[salehi] Update complete"
