#!/usr/bin/env bash
# Run the whole team once across all registered projects.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

echo ">>> Claude's round"
bash "$DIR/round.sh" claude

echo ">>> Antigravity's round"
bash "$DIR/round.sh" agy

echo ">>> Team round complete."
