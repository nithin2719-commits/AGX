#!/usr/bin/env bash
# One agent does ONE task in ONE project, then commits.
# Usage: ./run.sh <claude|agy> <project-dir>
set -uo pipefail

AGENT="${1:?usage: run.sh <claude|agy> <project-dir>}"
PROJ="${2:?usage: run.sh <claude|agy> <project-dir>}"
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/config.env"

PROJ="$(cd "$PROJ" && pwd)" || { echo "no such project: $PROJ"; exit 1; }
MEM="$PROJ/.agent-team"            # per-project memory lives inside the project
BACKLOG="$MEM/backlog.md"
STATUS="$MEM/status-$AGENT.json"
SESSION_MARK="$MEM/.session-$AGENT"
NAME="$(basename "$PROJ")"

mkdir -p "$LOGDIR" "$MEM"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$LOGDIR/${NAME}-${AGENT}-${STAMP}.log"

# Write status for the dashboard GUI.
status() {
  printf '{"agent":"%s","project":"%s","state":"%s","task":"%s","updated":"%s","log":"%s","pid":%s}\n' \
    "$AGENT" "$NAME" "$1" "$(printf '%s' "${2:-}" | tr -d '"\\' | cut -c1-160)" \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG" "$$" > "$STATUS"
}

# If we are killed, leave an honest status behind instead of a stuck "working".
on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ] && grep -q '"state":"working"' "$STATUS" 2>/dev/null; then
    status stopped "killed or interrupted"
  fi
}
trap on_exit EXIT INT TERM

# One agent at a time per project.
# Never stack: at most ONE worker per agent per project may be queued. Without
# this, every timer tick adds another worker behind the same lock, and they all
# eventually run - burning quota on stale tasks.
exec 8>"$MEM/.queue-$AGENT"
if ! flock -n 8; then
  echo "[$STAMP] $NAME: a $AGENT worker is already queued, skipping this tick"
  exit 0
fi

exec 9>"$MEM/.lock"
if ! flock -w 900 9; then
  status waiting "another agent holds this project"
  echo "[$STAMP] $NAME: locked by another agent, skipping" | tee -a "$LOG"
  exit 0
fi

[ -f "$BACKLOG" ] || { status idle "no backlog file"; echo "no backlog: $BACKLOG"; exit 0; }

# Per-project agent selection. If .agent-team/agents contains a list, only those
# agents may work here; otherwise every agent is allowed (backwards compatible).
if [ -s "$MEM/agents" ] && ! grep -qxF "$AGENT" "$MEM/agents"; then
  echo "[$STAMP] $NAME: $AGENT is not enabled for this project, skipping"
  exit 0
fi

# Claude and agy are the expensive tier: they take (L) and untagged tasks and
# leave (S) small ones to the free OpenRouter models.
TASK="$(grep -m1 -E '^- \[ \] (\(L\) )?[^(]' "$BACKLOG" | sed -E 's/^- \[ \] (\(L\) )?//')"
if [ -z "$TASK" ]; then
  status idle "backlog empty"
  echo "[$STAMP] $NAME: backlog empty, nothing to do" | tee -a "$LOG"
  exit 0
fi

status working "$TASK"

# Show this agent in the Pixel Agents office. Claude appears on its own via its
# real transcript; agy needs the bridge. Never allowed to fail the run.
pixel() { [ "$AGENT" = "claude" ] && return 0
          bash "$DIR/pixel-bridge.sh" "$1" "$AGENT" "$PROJ" "${2:-}" >/dev/null 2>&1 || true; }
pixel start
pixel work "$TASK"

# Pull the shared graph memory (never fatal if Neo4j is down).
GRAPH="$("$DIR/.venv/bin/python" "$DIR/graph/memory.py" context "$PROJ" 2>/dev/null)"

# The owner's plan outranks everything else.
PLAN=""
if [ -s "$MEM/PLAN.md" ] && ! grep -q "(nothing yet" "$MEM/PLAN.md"; then
  PLAN="=== THE OWNER'S PLAN (obey this above all) ===
$(cat "$MEM/PLAN.md")
"
fi

# A project's own CLAUDE.md / AGENTS.md outranks this system's conventions.
HOUSE=""
for f in CLAUDE.md AGENTS.md GEMINI.md; do
  if [ -s "$PROJ/$f" ]; then
    HOUSE="=== $PROJ/$f - THIS PROJECT'S OWN RULES ===
These OVERRIDE every convention below, including commit message format and
author. If they forbid something this prompt asks for, obey the project.
$(head -c 6000 "$PROJ/$f")
"
    break
  fi
done

PROMPT="You are an autonomous worker on a shared AI team, working in the git repo $PROJ.

$HOUSE
$PLAN
$GRAPH

Your memory for this project is in $MEM/ :
  - PROJECT.md  = what this project is. Read it FIRST.
  - PROGRESS.md = everything the team already did. Read it so you NEVER restart from zero.
  - backlog.md  = the shared task list.
If $PROJ/graphify-out/graph.json exists there is a knowledge graph of this codebase:
use the graphify skill (graphify explain / graphify path, or GRAPH_REPORT.md) to
understand structure and relationships instead of grepping blindly.
Steps:
1. Read PROJECT.md and PROGRESS.md before anything else.
2. In backlog.md pick the FIRST task marked '- [ ]' and change it to '- [~]' now.
3. Do that one task fully. Edit ONLY files inside $PROJ.
   If the task naturally breaks into separate independent changes, make a
   SEPARATE commit for each one rather than a single combined commit. One
   logical change per commit, always.
4. Run the project's tests if it has any; do not leave things broken.
5. Mark the task '- [x]' in backlog.md.
6. Append one line to PROGRESS.md: what you did and anything the next agent should know.
7. git add -A and git commit.

COMMIT RULES - these apply to EVERY project, no exceptions:
- Write the message as the repository owner would: plain, factual, describing
  the change itself. e.g. 'Fix registrable-domain extraction in lookalike.py'
- NEVER write 'agent(...)', 'claude', 'agy', 'AI', 'assistant', 'generated by',
  or any Co-Authored-By trailer. No AI attribution of any kind, anywhere in the
  message or trailers.
- Never mention this agent-team system in a commit message.
- Do not add emoji or tool watermarks.
- If the project's own CLAUDE.md/AGENTS.md specifies a different format, follow
  that instead - it always wins.
Do ONE task, then stop.

HARD SAFETY RULES - breaking these is worse than failing the task:
- NEVER delete files or directories. No rm, no rm -rf, no rmdir, no shutil.rmtree.
- NEVER run git reset --hard, git clean, git checkout -- ., or force push.
- NEVER touch anything outside $PROJ.
- If a task seems to require deleting something, STOP and write what you would
  have deleted into PROGRESS.md instead, then finish without deleting.
- Renaming with 'git mv' is allowed. Editing files is allowed. Removing is not."

cd "$PROJ" || exit 1
BEFORE_SHA="$(git -C "$PROJ" rev-parse HEAD 2>/dev/null)"
echo "[$STAMP] $NAME / $AGENT / task: $TASK" | tee -a "$LOG"

# Resume the previous conversation for this project so context carries over.
CONT=""
[ -f "$SESSION_MARK" ] && CONT="--continue"

# A hung agent must never hold the project lock forever.
MAXRUN="${AGENT_TIMEOUT:-25m}"

# Model chosen in the dashboard (settings.json). Empty = each CLI's default.
MODEL=""
if [ -f "$DIR/settings.json" ]; then
  MODEL="$(sed -n "s/.*\"model_$AGENT\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" \
           "$DIR/settings.json" | head -1)"
fi
# Selection is "model" or "model|effort" (claude supports a separate effort).
MODEL_ARG=""
if [ -n "$MODEL" ]; then
  EFFORT="${MODEL#*|}"
  BASE_MODEL="${MODEL%%|*}"
  MODEL_ARG="--model $BASE_MODEL"
  if [ "$EFFORT" != "$MODEL" ] && [ -n "$EFFORT" ]; then
    MODEL_ARG="$MODEL_ARG --effort $EFFORT"
  fi
  echo "[$STAMP] using model: $BASE_MODEL${EFFORT:+ (effort $EFFORT)}" >>"$LOG"
fi

# CRITICAL: the prompt must come IMMEDIATELY after -p. Both CLIs treat -p as
# taking a value, so any flag placed between them is swallowed AS the prompt and
# the real task is silently dropped. This is what made agy fail ~20 runs in a
# row: it was literally being asked "--continue" and replied "fresh conversation,
# what would you like to work on?". Never reorder these.
case "$AGENT" in
  claude) timeout "$MAXRUN" claude -p "$PROMPT" $CONT $MODEL_ARG --dangerously-skip-permissions >>"$LOG" 2>&1 ;;
  agy)    timeout "$MAXRUN" agy -p "$PROMPT" $CONT $MODEL_ARG --dangerously-skip-permissions --print-timeout 15m >>"$LOG" 2>&1 ;;
  free)   # Free-tier API worker (OpenRouter / NVIDIA) via router.py. Handles
          # small, text-shaped tasks when no Claude/agy quota is available.
          timeout "$MAXRUN" "$DIR/.venv/bin/python" "$DIR/router.py" do "$PROJ" >>"$LOG" 2>&1 ;;
  *)      echo "unknown agent: $AGENT"; status error "unknown agent"; exit 1 ;;
esac
RC=$?
if [ "$RC" -eq 124 ]; then
  echo "[$(date +%H:%M:%S)] $NAME/$AGENT TIMED OUT after $MAXRUN" | tee -a "$LOG"
  status error "timed out after $MAXRUN"
fi
touch "$SESSION_MARK"   # a conversation now exists for this project

# Reliable memory: log only what this agent actually committed.
AFTER_SHA="$(git -C "$PROJ" rev-parse HEAD 2>/dev/null)"
if [ "$AFTER_SHA" != "$BEFORE_SHA" ]; then
  MSG="$(git -C "$PROJ" log -1 --pretty=%s)"
  printf -- '- %s | %s | %s\n' "$(date '+%Y-%m-%d %H:%M')" "$AGENT" "$MSG" >> "$MEM/PROGRESS.md"
  "$DIR/.venv/bin/python" "$DIR/graph/memory.py" commit "$PROJ" "$AGENT" >/dev/null 2>&1
  status done "$MSG"
else
  printf -- '- %s | %s | (no commit) task was: %s\n' "$(date '+%Y-%m-%d %H:%M')" "$AGENT" "$TASK" >> "$MEM/PROGRESS.md"
  status nocommit "$TASK"
fi

# The progress line is written after the agent's own commit, so commit it too -
# otherwise the worktree stays permanently dirty and blocks branch switches.
if [ -n "$(git -C "$PROJ" status --porcelain -- "$MEM/PROGRESS.md" 2>/dev/null)" ]; then
  git -C "$PROJ" add -- "$MEM/PROGRESS.md" 2>/dev/null
  git -C "$PROJ" commit -q -m "Update progress log" -- "$MEM/PROGRESS.md" 2>/dev/null
fi

pixel stop
echo "[$(date +%H:%M:%S)] $NAME / $AGENT finished (rc=$RC)"
