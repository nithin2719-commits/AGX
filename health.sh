#!/usr/bin/env bash
# Check every moving part of the agent team. Exits non-zero if something is down.
DIR="$(cd "$(dirname "$0")" && pwd)"
BAD=0
ok(){ printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad(){ printf '  \033[31mDOWN\033[0m %s\n' "$1"; printf '       fix: %s\n' "$2"; BAD=1; }

echo "Agent Team health"

command -v claude >/dev/null && ok "claude CLI" || bad "claude CLI" "install Claude Code"
command -v agy >/dev/null && ok "agy CLI (Antigravity)" || bad "agy CLI" "install Antigravity CLI"


if docker ps --filter name=agentteam-neo4j --format '{{.Names}}' 2>/dev/null | grep -q .; then
  ok "graph memory (neo4j)"
else
  bad "graph memory (neo4j)" "cd $DIR/graph && docker compose up -d"
fi

if curl -s -m 5 -o /dev/null http://127.0.0.1:8765/; then
  ok "dashboard :8765"
else
  bad "dashboard :8765" "systemctl --user start agent-dashboard.service"
fi

if curl -s -m 5 -o /dev/null http://127.0.0.1:8790/; then
  ok "pixel office :8790"
else
  bad "pixel office :8790" "systemctl --user start agent-pixel.service"
fi

[ -x "$DIR/.venv/bin/python" ] && ok "python venv" || bad "python venv" "uv venv $DIR/.venv"

N=$(grep -cvE '^\s*(#|$)' "$DIR/projects.conf" 2>/dev/null || echo 0)
[ "$N" -gt 0 ] && ok "$N project(s) registered" \
  || bad "no projects" "bash $DIR/add-project.sh /path/to/project"

echo "  ---"
for t in agent-dashboard.service agent-pixel.service \
         agent-claude.timer agent-agy.timer; do
  s=$(systemctl --user is-active "$t" 2>/dev/null)
  case "$s" in
    active|activating) ok "$t ($s)" ;;
    *) bad "$t ($s)" "systemctl --user enable --now $t" ;;
  esac
done

[ "$BAD" -eq 0 ] && echo "Everything is up." || echo "Some parts are down (see fixes above)."
exit $BAD
