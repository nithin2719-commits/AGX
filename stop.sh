#!/usr/bin/env bash
# Emergency stop. Kills agent work without touching your own claude/agy sessions.
#   ./stop.sh            stop every running agent-team worker
#   ./stop.sh <project>  stop only workers on that project
#   ./stop.sh --timers   also disable the systemd timers so nothing restarts
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

TIMERS=0
TARGET=""
for a in "$@"; do
  case "$a" in
    --timers) TIMERS=1 ;;
    *) TARGET="$a" ;;
  esac
done

killed=0
# Workers record their PID in each project's status file.
while IFS= read -r p; do
  [ -z "$p" ] && continue
  case "$p" in \#*) continue ;; esac
  [ -n "$TARGET" ] && [ "$(basename "$p")" != "$(basename "$TARGET")" ] && continue
  for f in "$p"/.agent-team/status-*.json; do
    [ -f "$f" ] || continue
    grep -q '"state":"working"' "$f" 2>/dev/null || continue
    pid=$(sed -n 's/.*"pid":\([0-9]*\).*/\1/p' "$f")
    [ -z "$pid" ] && continue
    kill -0 "$pid" 2>/dev/null || continue
    # Kill the whole process group so claude/agy children die with the worker.
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [ -n "$pgid" ]; then kill -TERM -"$pgid" 2>/dev/null; else kill -TERM "$pid" 2>/dev/null; fi
    echo "stopped $(basename "$p") / $(basename "$f" .json | sed 's/^status-//')  (pid $pid)"
    killed=$((killed+1))
  done
done < "$DIR/projects.conf"

# Anything launched by this system but not recorded (e.g. a round in progress).
for pat in "$DIR/run.sh" "$DIR/round.sh" "$DIR/team.sh" "$DIR/meet.sh"; do
  for pid in $(pgrep -f "$pat" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    kill -TERM "$pid" 2>/dev/null && { echo "stopped $(basename "$pat") (pid $pid)"; killed=$((killed+1)); }
  done
done

# A worker blocked in flock ignores SIGTERM, so escalate to SIGKILL on anything
# still alive after a grace period.
sleep 3
for pat in "$DIR/run.sh" "$DIR/round.sh" "$DIR/team.sh" "$DIR/meet.sh"; do
  for pid in $(pgrep -f "$pat" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    kill -9 "$pid" 2>/dev/null && echo "force-killed $(basename "$pat") (pid $pid)"
  done
done

[ "$killed" -eq 0 ] && echo "nothing was running."

if [ "$TIMERS" -eq 1 ]; then
  systemctl --user disable --now agent-claude.timer agent-agy.timer agent-planner.timer 2>&1 | tail -3
  echo "timers disabled - re-enable with: systemctl --user enable --now agent-claude.timer agent-agy.timer agent-planner.timer"
fi
