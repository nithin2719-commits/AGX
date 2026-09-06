#!/usr/bin/env bash
# Architect round: for every registered project whose backlog is running low,
# ask the CrewAI planner for new tasks. Keeps the workers from ever idling.
# Usage: ./plan.sh [min-remaining-tasks]
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
MIN="${1:-2}"

[ -s "$DIR/projects.conf" ] || { echo "no projects registered"; exit 0; }

# The planner needs the proxy; skip quietly if it is not up.
if ! curl -s -m 5 -o /dev/null "http://127.0.0.1:8317/v1/models" \
      -H "Authorization: Bearer local-agent-team"; then
  echo "proxy not running on :8317 - skipping planning round"
  exit 0
fi

while IFS= read -r p; do
  [ -z "$p" ] && continue
  case "$p" in \#*) continue ;; esac
  [ -d "$p" ] || continue
  BL="$p/.agent-team/backlog.md"
  [ -f "$BL" ] || continue
  LEFT="$(grep -cE '^- \[ \]' "$BL" 2>/dev/null || echo 0)"
  if [ "$LEFT" -lt "$MIN" ]; then
    echo ">>> planning for $(basename "$p") (only $LEFT task(s) left)"
    "$DIR/.venv/bin/python" "$DIR/planner.py" "$p" 4
  else
    echo "--- $(basename "$p") has $LEFT task(s), no planning needed"
  fi
done < "$DIR/projects.conf"
