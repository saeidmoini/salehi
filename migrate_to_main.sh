#!/usr/bin/env bash
set -euo pipefail

# Migration script for switching from agrad/salehi branches to main branch
# This handles the audio file conflicts and directory restructuring

echo "=========================================="
echo "Migration Script: Branch to Main"
echo "=========================================="
echo ""

# Detect current directory
CURRENT_DIR="$(pwd)"
echo "Current directory: ${CURRENT_DIR}"

# Detect current branch
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
echo "Current branch: ${CURRENT_BRANCH}"
echo ""

# Backup scenario detection
if [ "${CURRENT_BRANCH}" = "agrad" ]; then
    SCENARIO="agrad"
elif [ "${CURRENT_BRANCH}" = "salehi" ]; then
    SCENARIO="salehi"
else
    # Try to detect from .env
    if [ -f ".env" ]; then
        SCENARIO="$(grep -E '^SCENARIO=' .env | cut -d'=' -f2 | tr -d '"' | tr -d "'" || echo 'salehi')"
    else
        SCENARIO="salehi"
    fi
fi

echo "Detected scenario: ${SCENARIO}"
echo ""

# Step 1: Backup current audio files if they exist
echo "Step 1: Backing up current audio files..."
if [ -d "assets/audio/src" ]; then
    BACKUP_DIR="assets/audio/backup_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "${BACKUP_DIR}"
    cp -r assets/audio/src/* "${BACKUP_DIR}/" 2>/dev/null || true
    echo "  ✓ Backed up to ${BACKUP_DIR}"
else
    echo "  ℹ No src directory to backup"
fi

if [ -d "assets/audio/wav" ]; then
    BACKUP_WAV_DIR="assets/audio/backup_wav_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "${BACKUP_WAV_DIR}"
    cp -r assets/audio/wav/* "${BACKUP_WAV_DIR}/" 2>/dev/null || true
    echo "  ✓ Backed up wav files to ${BACKUP_WAV_DIR}"
else
    echo "  ℹ No wav directory to backup"
fi
echo ""

# Step 2: Remove conflicting files
echo "Step 2: Removing conflicting files..."
rm -rf assets/audio/src 2>/dev/null || true
rm -rf assets/audio/wav 2>/dev/null || true
echo "  ✓ Removed old audio directories"
echo ""

# Step 3: Discard local changes to git-tracked files
echo "Step 3: Discarding local changes..."
git checkout -- . 2>/dev/null || true
echo "  ✓ Discarded local changes"
echo ""

# Step 4: Fetch latest from remote
echo "Step 4: Fetching latest from remote..."
git fetch --all --prune
echo "  ✓ Fetched updates"
echo ""

# Step 5: Switch to main branch
echo "Step 5: Switching to main branch..."
git checkout main
echo "  ✓ Switched to main"
echo ""

# Step 6: Pull latest main
echo "Step 6: Pulling latest main..."
git pull origin main
echo "  ✓ Pulled latest main"
echo ""

# Step 7: Update .env with scenario
echo "Step 7: Updating .env file..."
if [ ! -f ".env" ]; then
    echo "  Creating .env from .env.example..."
    cp .env.example .env
fi

# Update or add SCENARIO line
if grep -q "^SCENARIO=" .env; then
    sed -i "s/^SCENARIO=.*/SCENARIO=${SCENARIO}/" .env
    echo "  ✓ Updated SCENARIO=${SCENARIO} in .env"
else
    echo "SCENARIO=${SCENARIO}" >> .env
    echo "  ✓ Added SCENARIO=${SCENARIO} to .env"
fi
echo ""

# Step 8: Verify new structure
echo "Step 8: Verifying new structure..."
if [ -d "assets/audio/${SCENARIO}/src" ]; then
    echo "  ✓ Scenario audio directory exists: assets/audio/${SCENARIO}/src"
    echo "  Files:"
    ls -lh "assets/audio/${SCENARIO}/src" | tail -n +2 | awk '{print "    - " $9 " (" $5 ")"}'
else
    echo "  ⚠ Warning: assets/audio/${SCENARIO}/src not found"
fi
echo ""

# Step 9: Show git status
echo "Step 9: Current git status..."
git status
echo ""

echo "=========================================="
echo "Migration Complete!"
echo "=========================================="
echo ""
echo "Current branch: $(git rev-parse --abbrev-ref HEAD)"
echo "Active scenario: ${SCENARIO}"
echo ""
echo "Next steps:"
echo "  1. Review the git status above"
echo "  2. Verify .env has correct settings"
echo "  3. Run: ./update.sh"
echo "  4. Restart service: sudo systemctl restart salehi.service"
echo ""
echo "Backups created (if applicable):"
ls -d assets/audio/backup* 2>/dev/null || echo "  (none)"
echo ""
