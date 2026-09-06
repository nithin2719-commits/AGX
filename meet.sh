#!/usr/bin/env bash
# A real planning meeting between the TWO actual agents.
#   Round 1  Claude reads the repo + memory + your PLAN.md and proposes work
#   Round 2  agy (Antigravity) reads Claude's proposal, argues, and counter-proposes
#   Round 3  Claude reads agy's reply and writes the agreed task list into the backlog
# Both agents have real access to the repo, so they argue about real code.
# Usage: ./meet.sh <project-path>
set -uo pipefail

PROJ="${1:?usage: meet.sh <project-path>}"
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/config.env"

PROJ="$(cd "$PROJ" && pwd)" || { echo "no such project: $PROJ"; exit 1; }
MEM="$PROJ/.agent-team"
NAME="$(basename "$PROJ")"
BACKLOG="$MEM/backlog.md"
mkdir -p "$MEM/discussions" "$LOGDIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
TRANSCRIPT="$MEM/discussions/${STAMP}-meeting.md"
WORK="$(mktemp -d)"
# Clean up ONLY our own scratch dir, and only if it really is one under /tmp.
cleanup() {
  case "$WORK" in
    /tmp/tmp.*) [ -d "$WORK" ] && rm -rf -- "$WORK" ;;
    *) : ;;   # anything unexpected is left alone on purpose
  esac
}
trap cleanup EXIT

SAFETY="HARD RULES: never delete any file or directory, never run rm/rm -rf/git reset --hard/git clean. You are only THINKING here - do not modify the repository at all in this meeting."

PLAN=""
if [ -s "$MEM/PLAN.md" ] && ! grep -q "(nothing yet" "$MEM/PLAN.md"; then
  PLAN="=== THE OWNER'S PLAN (this outranks your own opinions) ===
$(cat "$MEM/PLAN.md")"
fi
GRAPH="$("$DIR/.venv/bin/python" "$DIR/graph/memory.py" context "$PROJ" 2>/dev/null)"

# Engineering roles, if this project has assigned any. Two specialists argue
# better than two generalists.
ROLE_CLAUDE="$("$DIR/.venv/bin/python" "$DIR/roles.py" prompt "$PROJ" claude 2>/dev/null)"
ROLE_AGY="$("$DIR/.venv/bin/python" "$DIR/roles.py" prompt "$PROJ" agy 2>/dev/null)"

BASECTX="You are in a planning meeting about the git repo $PROJ.
$PLAN

$GRAPH

Already done (do NOT propose these again):
$(tail -25 "$MEM/PROGRESS.md" 2>/dev/null)

Current backlog (do NOT duplicate):
$(cat "$BACKLOG" 2>/dev/null)

$SAFETY"

echo "=== Planning meeting: $NAME ==="

# Run one round in the background and show a live elapsed counter, because the
# agents print nothing at all until they are completely finished.
round() {
  local label="$1" out="$2"; shift 2
  "$@" > "$out" 2>>"$LOGDIR/${NAME}-meet.err" &
  local pid=$! s=0
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r    %s ... %ds' "$label" "$s"
    sleep 5; s=$((s+5))
  done
  wait "$pid"; local rc=$?
  printf '\r    %s ... done in %ds, %s chars\n' "$label" "$s" "$(wc -c <"$out" 2>/dev/null || echo 0)"
  return $rc
}

# ---------- Round 1: Claude proposes ----------
echo ">>> Round 1: Claude proposes"
cd "$PROJ" || exit 1
# NOTE: the prompt must come IMMEDIATELY after -p. Putting flags in between
# makes the CLI swallow the flag as the prompt text.
round "Claude thinking" "$WORK/r1.md" \
  timeout 15m claude -p "$BASECTX

$ROLE_CLAUDE

You are CLAUDE. Read the actual code in this repo, then propose the 4 most
valuable next tasks. For each: the task, and one line on why it matters.
Be concrete and reference real files. Do not write anything to disk." \
  --dangerously-skip-permissions
[ -s "$WORK/r1.md" ] || { echo "Claude produced nothing (see $LOGDIR/${NAME}-meet.err)"; exit 1; }

# ---------- Round 2: agy challenges ----------
echo ">>> Round 2: Antigravity responds"
round "agy reviewing" "$WORK/r2.md" \
  timeout 15m agy -p "$BASECTX

$ROLE_AGY

You are ANTIGRAVITY, the other engineer in this meeting. Claude proposed:

--- CLAUDE'S PROPOSAL ---
$(cat "$WORK/r1.md")
--- END ---

Read the real code yourself and respond honestly. Start each item with the word
AGREE, CHANGE or REJECT followed by your reason, and then add anything Claude
missed. Disagree where you genuinely disagree. Do not write anything to disk." \
  --dangerously-skip-permissions --print-timeout 14m

if ! grep -qiE '(AGREE|CHANGE|REJECT)' "$WORK/r2.md" 2>/dev/null; then
  echo "    !! agy did not review properly - its reply is in the transcript"
fi

# ---------- Round 3: Claude concludes ----------
echo ">>> Round 3: agreeing the final list"
round "Claude concluding" "$WORK/r3.md" \
  timeout 15m claude -p "$BASECTX

You are CLAUDE. This was your proposal:
--- YOURS ---
$(cat "$WORK/r1.md")
--- END ---

Antigravity replied:
--- THEIRS ---
$(cat "$WORK/r2.md" 2>/dev/null)
--- END ---

Take their objections seriously. Where they are right, change your mind.

Now write the final agreed task list. SPLIT THE WORK FINELY: every task must be
the smallest change that still stands on its own and can be committed by itself.
Prefer one task per file, per function, or per bug - never bundle several fixes
into one line. A task like 'fix the parser' should become several tasks, one per
concrete defect. Aim for 12-25 tasks.

Output ONLY the task lines, each starting with '- [ ] ', one concrete action per
line. No preamble, no numbering, no headings, nothing else." \
  --dangerously-skip-permissions

# ---------- save transcript ----------
{
  echo "# Planning meeting — $NAME"
  echo "_$(date '+%Y-%m-%d %H:%M')_"
  echo
  if [ -n "$PLAN" ]; then echo "## The plan they were given"; echo; cat "$MEM/PLAN.md"; echo; fi
  echo "## Round 1 — Claude proposes"; echo; cat "$WORK/r1.md"; echo
  echo "## Round 2 — Antigravity responds"; echo; cat "$WORK/r2.md" 2>/dev/null || echo "(no reply)"; echo
  echo "## Round 3 — agreed task list"; echo; cat "$WORK/r3.md" 2>/dev/null || echo "(none)"
} > "$TRANSCRIPT"

# ---------- append agreed tasks ----------
ADDED=0
while IFS= read -r line; do
  case "$line" in
    "- [ ] "*)
      body="${line#- [ ] }"
      [ -z "$body" ] && continue
      grep -qF -- "$body" "$BACKLOG" 2>/dev/null && continue
      printf '%s\n' "$line" >> "$BACKLOG"
      ADDED=$((ADDED+1))
      echo "  + $body"
      ;;
  esac
done < "$WORK/r3.md"

echo "=== $NAME: $ADDED task(s) agreed ==="
echo "transcript: $TRANSCRIPT"
