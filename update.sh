#!/usr/bin/env bash
set -euo pipefail

# Deployment script for Salehi CallCenter
# Supports both Salehi and Agrad scenarios via SCENARIO environment variable
# Usage: ./update.sh
#        SCENARIO=agrad ./update.sh

APP_DIR="$(cd "$(dirname "$0")" && pwd)"

# Detect scenario from environment or .env file
SCENARIO="${SCENARIO:-}"
if [ -z "$SCENARIO" ] && [ -f "${APP_DIR}/.env" ]; then
  SCENARIO="$(grep -E '^SCENARIO=' "${APP_DIR}/.env" | cut -d'=' -f2 | tr -d '"' | tr -d "'" || echo "")"
fi
SCENARIO="${SCENARIO:-salehi}"

# Service name matches the scenario
SERVICE_NAME="${SCENARIO}.service"

echo "[CallCenter] Updating ${SCENARIO} scenario in ${APP_DIR}"
cd "${APP_DIR}"

# Use main branch (single codebase now supports both scenarios)
BRANCH="main"
echo "[CallCenter] Pulling from branch: ${BRANCH}"
git fetch --all --prune
git reset --hard "origin/${BRANCH}"

# Ensure asterisk user can write audio outputs and sounds dirs (run after pull so new files are covered)
if id asterisk >/dev/null 2>&1; then
  CHOWN_BIN="chown"
  CHMOD_BIN="chmod"
  if command -v sudo >/dev/null 2>&1; then
    CHOWN_BIN="sudo chown"
    CHMOD_BIN="sudo chmod"
  fi

  ${CHOWN_BIN} -R asterisk:asterisk "${APP_DIR}/assets/audio" || true
  ${CHMOD_BIN} -R 775 "${APP_DIR}/assets/audio" || true

  for path in /usr/share/asterisk/sounds/custom /usr/share/asterisk/sounds/en/custom /var/lib/asterisk/sounds/custom /var/lib/asterisk/sounds/en/custom; do
    ${CHOWN_BIN} -R asterisk:asterisk "$path" || true
    ${CHMOD_BIN} -R 775 "$path" || true
  done
fi

python3 -m venv "${APP_DIR}/.venv" || true
source "${APP_DIR}/.venv/bin/activate"
pip install --upgrade pip
pip install --upgrade -r "${APP_DIR}/requirements.txt"

if command -v systemctl >/dev/null 2>&1; then
  echo "[CallCenter] Restarting ${SERVICE_NAME} (${SCENARIO} scenario)"
  if sudo -n true 2>/dev/null; then
    sudo systemctl restart "${SERVICE_NAME}"
  else
    systemctl restart "${SERVICE_NAME}"
  fi
else
  echo "[CallCenter] systemctl not found; skipping service restart"
fi

echo "[CallCenter] Update complete for ${SCENARIO} scenario"
echo "[CallCenter] Active scenario: ${SCENARIO}"
echo "[CallCenter] To change scenario, update SCENARIO= in .env file"
