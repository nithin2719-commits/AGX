#!/usr/bin/env bash
# Create the AGX git history by committing the system in the order it was
# actually built: one commit per real piece of work. No padding, no empty
# commits, no fabricated dates - every commit contains a real change.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

git rev-parse --git-dir >/dev/null 2>&1 || { git init -q; echo "initialised repo"; }
git config user.name  >/dev/null 2>&1 || git config user.name "nithin2719-commits"
git config user.email >/dev/null 2>&1 || git config user.email "thangamnithin1980@gmail.com"

n=0
# add <message> <file...>  - commit only if those paths actually changed
add() {
  local msg="$1"; shift
  local any=0
  for f in "$@"; do [ -e "$f" ] && { git add -- "$f" 2>/dev/null && any=1; }; done
  [ "$any" -eq 1 ] || return 0
  git diff --cached --quiet && return 0
  git commit -q -m "$msg" && { n=$((n+1)); printf '%3d  %s\n' "$n" "$msg"; }
}

add "Add gitignore for runtime state and vendored clones"        .gitignore
add "Add worker configuration"                                    config.env
add "Add the autonomous worker that runs one backlog task"        run.sh
add "Add project registration with per-project memory"            add-project.sh
add "Add a round runner so one agent sweeps every project"        round.sh
add "Add the team runner for a full pass by both agents"          team.sh
add "Add the two-agent planning meeting"                          meet.sh
add "Add the self-running cycle: meet when out of work, then work" cycle.sh
add "Add an emergency stop that escalates to SIGKILL"             stop.sh
add "Add a health check covering every moving part"               health.sh
add "Add one-command startup for the whole system"                up.sh
add "Add focus control to point the team at a single project"     focus.sh
add "Add the graph memory schema and query helpers"               graph/memory.py
add "Add Neo4j service definition for the graph memory"           graph/docker-compose.yml
add "Add the code knowledge graph builder"                        build-graph.sh
add "Add engineering role definitions for the agents"             roles.py
add "Add the pixel office bridge so both agents are visible"      pixel-bridge.sh
add "Add the free-tier model router"                              router.py
add "Add weight-based routing across projects"                    route.sh
add "Add the CrewAI planner"                                      planner.py
add "Add the planner sweep for projects running low on work"      plan.sh
add "Add the key setup helper"                                    set-key.sh
add "Add the dashboard"                                           dashboard.py
add "Add the migration script to the AGX layout"                  migrate-to-agx.sh
add "Add the history builder"                                     build-history.sh

for u in systemd/*.service systemd/*.timer; do
  [ -e "$u" ] || continue
  add "Add ${u##*/} unit" "$u"
done

add "Document the system"                                         README.md
add "Add the quick reference"                                     HOWTO.md

# Anything not covered above, one commit per file so nothing is lumped together.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  add "Add ${f#./}" "$f"
done < <(git ls-files --others --exclude-standard)

echo
echo "$n commit(s) created."
git log --oneline | head -5
