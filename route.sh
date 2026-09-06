#!/usr/bin/env bash
# Weight-based routing round across every registered project:
#   1. classify any untagged backlog tasks as (S) small or (L) large
#   2. free OpenRouter models clear the (S) tasks
# The heavy tier (claude / agy) is left to run.sh and only takes (L) work.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/config.env"

if [ -z "${NVIDIA_API_KEY:-}" ] && [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "No free-tier key set - skipping the cheap tier."
  echo "Add one with:  bash $DIR/set-key.sh"
  exit 0
fi

PY="$DIR/.venv/bin/python"
MAX="${1:-3}"          # how many small tasks per project per round

[ -s "$DIR/projects.conf" ] || { echo "no projects registered"; exit 0; }

while IFS= read -r p; do
  [ -z "$p" ] && continue
  case "$p" in \#*) continue ;; esac
  [ -d "$p" ] || continue
  echo ">>> $(basename "$p")"
  "$PY" "$DIR/router.py" classify "$p"
  for _ in $(seq 1 "$MAX"); do
    out="$("$PY" "$DIR/router.py" do "$p" 2>&1)"
    echo "$out" | sed 's/^/    /'
    case "$out" in *"no small tasks"*) break ;; esac
  done
done < "$DIR/projects.conf"
