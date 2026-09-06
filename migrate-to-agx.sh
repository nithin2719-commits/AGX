#!/usr/bin/env bash
# Move ~/agent-team to ~/AGX and repair every absolute path that points at the
# old location: scripts, systemd units, the launcher, and the venv (a Python
# venv stores its own path, so it has to be rebuilt, not copied).
set -uo pipefail
OLD="$HOME/agent-team"
NEW="$HOME/AGX"

[ -d "$OLD" ] || { echo "$OLD does not exist - already migrated?"; exit 1; }
[ -e "$NEW" ] && { echo "$NEW already exists - refusing to overwrite"; exit 1; }

echo ">>> stopping services and agents"
systemctl --user stop agent-dashboard.service agent-pixel.service 2>/dev/null
bash "$OLD/stop.sh" >/dev/null 2>&1

echo ">>> moving $OLD -> $NEW"
mv "$OLD" "$NEW" || { echo "move failed"; exit 1; }

echo ">>> rewriting absolute paths"
# Scripts, units and configs. Skip the venv and logs: the venv is rebuilt below
# and logs are historical records that should not be rewritten.
while IFS= read -r f; do
  sed -i "s|$OLD|$NEW|g" "$f"
done < <(find "$NEW" -maxdepth 2 -type f \
           \( -name '*.sh' -o -name '*.py' -o -name '*.service' -o -name '*.timer' \
              -o -name '*.env' -o -name '*.md' -o -name '*.conf' -o -name '*.yml' \) \
           -not -path "*/.venv/*" -not -path "*/logs/*")

# The Hyprland launcher lives outside the project.
[ -f "$HOME/.local/bin/agent-team-launch.sh" ] && \
  sed -i "s|$OLD|$NEW|g" "$HOME/.local/bin/agent-team-launch.sh"

echo ">>> rebuilding the virtualenv (venvs cannot be moved)"
rm -rf "$NEW/.venv"
uv venv --python 3.12 "$NEW/.venv" >/dev/null 2>&1
uv pip install --python "$NEW/.venv/bin/python" -q crewai graphiti-core neo4j openai \
  >/dev/null 2>&1 || uv pip install --python "$NEW/.venv/bin/python" -q neo4j

echo ">>> reinstalling systemd units"
cp "$NEW"/systemd/*.service "$NEW"/systemd/*.timer "$HOME/.config/systemd/user/" 2>/dev/null
systemctl --user daemon-reload
systemctl --user restart agent-dashboard.service agent-pixel.service 2>/dev/null

echo ">>> leftover references to the old path:"
grep -rIl "$OLD" "$NEW" --exclude-dir=.venv --exclude-dir=logs --exclude-dir=.git 2>/dev/null \
  | head -10 || echo "    none"

echo
echo "Done. AGX now lives at $NEW"
echo "Check it with:  bash $NEW/health.sh"
