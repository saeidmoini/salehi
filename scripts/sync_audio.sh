#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)/assets/audio/wav"
TARGET_DIR="${AST_SOUND_DIR:-/var/lib/asterisk/sounds/custom}"
PROMPTS=("hello" "goodby" "yes" "number")

if [ ! -d "$SOURCE_DIR" ]; then
  echo "Source dir not found: $SOURCE_DIR" >&2
  exit 1
fi

echo "Copying prompts to $TARGET_DIR"
mkdir -p "$TARGET_DIR"
missing=0
for prompt in "${PROMPTS[@]}"; do
  if [ -f "$SOURCE_DIR/${prompt}.wav" ]; then
    cp -f "$SOURCE_DIR/${prompt}.wav" "$TARGET_DIR/${prompt}.wav"
    chmod 644 "$TARGET_DIR/${prompt}.wav"
  else
    echo "WARN: $SOURCE_DIR/${prompt}.wav not found" >&2
    missing=1
  fi
done

if [ "$missing" -eq 1 ]; then
  echo "Some prompts were missing; ensure assets/audio/wav is up to date (run app startup conversion)." >&2
fi

echo "Done. Reload Asterisk sounds if needed."
