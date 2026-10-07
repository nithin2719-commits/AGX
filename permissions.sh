# Scoped tool permissions for headless Claude runs. Sourced by run.sh and
# meet.sh; replaces --dangerously-skip-permissions for claude.
#
# In print mode (-p) nobody can answer a permission prompt, so any tool call
# not allowed here is refused and the agent carries on without it.
#
# Paths are scoped to the project. A bare "Read" or "Edit" would also allow
# files anywhere else on the machine. "//" makes a rule path absolute (a single
# "/" is relative to the settings file). Edit rules cover every file-writing
# tool (Edit and Write); Read rules cover Read, Glob and Grep.
#
# agy is NOT covered: it has no allowlist flag, so run.sh and meet.sh still pass
# it --dangerously-skip-permissions.

# The project's test command: the first line of .agent-team/test-command if it
# exists, otherwise a guess from the files in the repo. Prints nothing when the
# project has no recognisable tests.
project_test_cmd() {
  local proj="$1" f="$1/.agent-team/test-command"
  if [ -s "$f" ]; then head -1 "$f"; return; fi
  if [ -f "$proj/package.json" ] && grep -q '"test"[[:space:]]*:' "$proj/package.json"; then
    echo "npm test"
  elif [ -f "$proj/Cargo.toml" ]; then
    echo "cargo test"
  elif [ -f "$proj/go.mod" ]; then
    echo "go test"
  elif [ -f "$proj/pytest.ini" ] || [ -f "$proj/conftest.py" ] || [ -d "$proj/tests" ] \
       || grep -qs '\[tool.pytest' "$proj/pyproject.toml"; then
    echo "python -m pytest"
  elif [ -f "$proj/Makefile" ] && grep -q '^test:' "$proj/Makefile"; then
    echo "make test"
  fi
}

# A worker run: read and edit inside the project, commit, run its tests.
# No rm, no git push/reset/clean/checkout, no network tools.
claude_worker_tools() {
  local proj="$1" test
  CLAUDE_TOOLS=(
    "Read(/$proj/**)" "Edit(/$proj/**)"
    "Bash(git status:*)" "Bash(git diff:*)" "Bash(git add:*)"
    "Bash(git commit:*)" "Bash(git log:*)"
  )
  test="$(project_test_cmd "$proj")"
  [ -n "$test" ] && CLAUDE_TOOLS+=("Bash($test:*)")
}

# A planning meeting: read-only.
claude_meet_tools() {
  local proj="$1"
  CLAUDE_TOOLS=(
    "Read(/$proj/**)"
    "Bash(git log:*)" "Bash(git diff:*)" "Bash(git show:*)"
  )
}
