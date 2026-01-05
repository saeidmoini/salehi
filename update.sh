#!/usr/bin/env bash
set -euo pipefail

# Simple local deploy script for Salehi
# Runs a pull + venv deps + service restart from the repo root.

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="salehi.service"

echo "[salehi] Updating source in ${APP_DIR}"
cd "${APP_DIR}"

# Ensure asterisk user can write audio outputs and sounds dirs (optional if already set)
if id asterisk >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1; then
    sudo chown -R asterisk:asterisk "${APP_DIR}/assets/audio" || true
    sudo chown -R asterisk:asterisk /usr/share/asterisk/sounds/custom /usr/share/asterisk/sounds/en/custom || true
  else
    chown -R asterisk:asterisk "${APP_DIR}/assets/audio" || true
    chown -R asterisk:asterisk /usr/share/asterisk/sounds/custom /usr/share/asterisk/sounds/en/custom || true
  fi
fi

# Track current branch to pull the matching remote branch (per-env configs)
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
git fetch --all --prune
git reset --hard "origin/${BRANCH}"

python3 -m venv "${APP_DIR}/venv" || true
source "${APP_DIR}/venv/bin/activate"
pip install --upgrade pip
pip install --upgrade -r "${APP_DIR}/requirements.txt"

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
