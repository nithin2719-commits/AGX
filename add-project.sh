#!/usr/bin/env bash
# Register a project with the agent team and give it its own memory.
# Usage: ./add-project.sh /path/to/project
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/config.env"

RAW="${1:?usage: add-project.sh /path/to/project}"
PROJ="$(cd "$RAW" 2>/dev/null && pwd)" || { echo "no such folder: $RAW"; exit 1; }
NAME="$(basename "$PROJ")"
MEM="$PROJ/.agent-team"

# The agents commit their work, so the project must be a git repo.
if ! git -C "$PROJ" rev-parse --git-dir >/dev/null 2>&1; then
  echo "!! $PROJ is not a git repo."
  echo "   Run:  git -C '$PROJ' init && git -C '$PROJ' add -A && git -C '$PROJ' commit -m init"
  exit 1
fi

mkdir -p "$MEM"

[ -f "$MEM/PROJECT.md" ] || cat > "$MEM/PROJECT.md" <<EOF
# $NAME — project memory

Agents read this FIRST every run so they know the goal and never start from zero.

## What this project is
<!-- Describe it in a few lines: what it does, language/stack, the goal. -->
(fill this in)

## Key facts agents must know
- (conventions, decisions, things not to break)

## Definition of done
- Tests pass (if any), work committed to git.
EOF

[ -f "$MEM/PLAN.md" ] || cat > "$MEM/PLAN.md" <<EOF
# $NAME — your plan

Write what YOU want done here, in plain words. The planning meeting treats this
as the boss: every task it creates must serve this plan. Leave it empty and the
agents decide for themselves.

Example:
  Goal: make the theme work on Qt5 as well as Qt6.
  Rules: never change colors. Do not touch install.sh.
  Priority: fix bugs before adding anything new.

(nothing yet — write your plan here)
EOF

[ -f "$MEM/PROGRESS.md" ] || cat > "$MEM/PROGRESS.md" <<EOF
# $NAME — progress log

Newest last. Each agent appends a line after finishing, so the next agent
continues instead of restarting.

EOF

[ -f "$MEM/backlog.md" ] || cat > "$MEM/backlog.md" <<EOF
# $NAME — backlog

Workers take the first '- [ ]' task and mark it '- [~]' while working, '- [x]' when done.
Keep tasks small and verifiable.

- [ ] Read the codebase and fill in .agent-team/PROJECT.md with an accurate description
EOF

cat > "$MEM/.gitignore" <<'EOF'
# transient runtime files - memory files themselves stay tracked
status-*.json
.session-*
.lock
EOF

touch "$DIR/projects.conf"
if grep -qxF "$PROJ" "$DIR/projects.conf" 2>/dev/null; then
  echo "already registered: $PROJ"
else
  echo "$PROJ" >> "$DIR/projects.conf"
  echo "registered: $PROJ"
fi

echo
echo "Memory created at: $MEM"
echo "  PROJECT.md  <- describe the project here (or let the first task do it)"
echo "  backlog.md  <- add your tasks here"
