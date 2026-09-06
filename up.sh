#!/usr/bin/env bash
# Bring the whole agent team online. Safe to run any time - it only starts
# what is not already running, and prints the links you need.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DIR/logs"

up() { curl -s -m 3 -o /dev/null "$1"; }

# 1. graph memory
if ! docker ps --filter name=agentteam-neo4j --format '{{.Names}}' 2>/dev/null | grep -q .; then
  ( cd "$DIR/graph" && docker compose up -d >/dev/null 2>&1 ) && echo "started graph memory"
fi

# 2. dashboard
if ! up http://127.0.0.1:8765/; then
  setsid nohup "$DIR/.venv/bin/python" "$DIR/dashboard.py" \
    > "$DIR/logs/dashboard.log" 2>&1 < /dev/null &
  echo "started dashboard"
fi

# 3. pixel office
if ! up http://127.0.0.1:8790/; then
  setsid nohup pixel-agents --port 8790 \
    > "$DIR/logs/pixel-agents.log" 2>&1 < /dev/null &
  echo "started pixel office"
fi

# give them a moment to bind
for _ in 1 2 3 4 5 6 7 8; do
  up http://127.0.0.1:8765/ && up http://127.0.0.1:8790/ && break
  sleep 1
done

TOKEN=$(sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        "$HOME/.pixel-agents/server.json" 2>/dev/null | head -1)

echo
echo "  Control panel : http://localhost:8765"
if [ -n "$TOKEN" ]; then
  echo "  Pixel office  : http://127.0.0.1:8790/?token=$TOKEN"
else
  echo "  Pixel office  : not running"
fi
echo "  Graph memory  : http://localhost:7474   (neo4j / agentteam123)"
echo
bash "$DIR/focus.sh"
