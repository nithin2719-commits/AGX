#!/usr/bin/env bash
# Safely store a free-tier API key in config.env.
# The key is typed hidden, so it never lands in your shell history.
#   ./set-key.sh            -> asks which provider
#   ./set-key.sh nvidia     -> NVIDIA NIM
#   ./set-key.sh openrouter -> OpenRouter
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
CFG="$DIR/config.env"

WHICH="${1:-}"
if [ -z "$WHICH" ]; then
  echo "Which key are you adding?"
  echo "  1) NVIDIA NIM   (recommended - ~40 req/min, 100+ models)"
  echo "  2) OpenRouter   (20 req/min, 50/day under \$10 credits)"
  printf '> '
  read -r n
  case "$n" in 1) WHICH=nvidia ;; 2) WHICH=openrouter ;; *) echo "pick 1 or 2"; exit 1 ;; esac
fi

case "$WHICH" in
  nvidia)     VAR=NVIDIA_API_KEY;     PREFIX="nvapi-";  SITE="https://build.nvidia.com" ;;
  openrouter) VAR=OPENROUTER_API_KEY; PREFIX="sk-or-";  SITE="https://openrouter.ai/keys" ;;
  *) echo "usage: set-key.sh [nvidia|openrouter]"; exit 1 ;;
esac

echo "Get a free key at: $SITE"
printf 'Paste your %s key (input hidden), then press Enter:\n> ' "$WHICH"
IFS= read -rs KEY
echo

KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
[ -z "$KEY" ] && { echo "nothing entered - aborted."; exit 1; }
case "$KEY" in
  "$PREFIX"*) : ;;
  *) echo "that does not look like a $WHICH key (should start with $PREFIX)"; exit 1 ;;
esac

# Replace any previous line for this provider (commented or not).
grep -v "$VAR=" "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
printf 'export %s="%s"\n' "$VAR" "$KEY" >> "$CFG"
chmod 600 "$CFG"

echo "stored $VAR in $CFG (permissions 600)"
echo "checking it works..."
( set -a; . "$CFG"; set +a; "$DIR/.venv/bin/python" "$DIR/router.py" models )
