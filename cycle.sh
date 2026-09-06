#!/usr/bin/env bash
# One full self-running cycle for every active project:
#
#   backlog nearly empty?  -> Claude and agy hold a meeting and agree new tasks
#   tasks waiting?         -> Claude works one, then agy works one
#
# This is what makes the team run without you pressing anything. The systemd
# timer calls it; you can also press AUTO CYCLE in the dashboard.
#
# Usage: ./cycle.sh [min-tasks-before-meeting]   (default 2)
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
MIN="${1:-2}"

[ -s "$DIR/projects.conf" ] || { echo "no projects registered"; exit 0; }

while IFS= read -r p; do
  [ -z "$p" ] && continue
  case "$p" in \#*) continue ;; esac      # paused projects are skipped
  [ -d "$p" ] || continue
  NAME="$(basename "$p")"
  BL="$p/.agent-team/backlog.md"
  [ -f "$BL" ] || continue

  LEFT="$(grep -cE '^- \[ \]' "$BL" 2>/dev/null || echo 0)"
  echo "=== $NAME: $LEFT task(s) waiting ==="

  # 1. Out of work -> the two agents decide what to do next, together.
  if [ "$LEFT" -lt "$MIN" ]; then
    echo "  backlog low - holding a planning meeting"
    bash "$DIR/meet.sh" "$p" 2>&1 | sed 's/^/    /'
    LEFT="$(grep -cE '^- \[ \]' "$BL" 2>/dev/null || echo 0)"
    echo "  meeting produced $LEFT task(s)"
  fi

  # 2. Work whatever is there. run.sh's queue lock stops these stacking up.
  if [ "$LEFT" -gt 0 ]; then
    bash "$DIR/run.sh" claude "$p" 2>&1 | sed 's/^/    /'
    bash "$DIR/run.sh" agy    "$p" 2>&1 | sed 's/^/    /'
  else
    echo "  nothing to do - the meeting agreed no new work"
  fi
done < "$DIR/projects.conf"

echo "=== cycle complete ==="
