#!/usr/bin/env bash
# One agent does one task in EVERY registered project.
# Usage: ./round.sh <claude|agy>
set -uo pipefail
AGENT="${1:?usage: round.sh <claude|agy>}"
DIR="$(cd "$(dirname "$0")" && pwd)"

[ -s "$DIR/projects.conf" ] || { echo "no projects registered. use: ./add-project.sh /path"; exit 0; }

while IFS= read -r p; do
  [ -z "$p" ] && continue
  case "$p" in \#*) continue ;; esac
  [ -d "$p" ] || { echo "skip (missing): $p"; continue; }
  bash "$DIR/run.sh" "$AGENT" "$p"
done < "$DIR/projects.conf"
