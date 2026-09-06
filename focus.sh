#!/usr/bin/env bash
# Point the whole team at ONE project (or back at all of them).
# Disabled projects stay registered - they are just commented out, so nothing
# is lost and the timers skip them.
#
#   ./focus.sh SignFlow     work only on SignFlow
#   ./focus.sh all          work on every project again
#   ./focus.sh              show what is active right now
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$DIR/projects.conf"
[ -f "$CONF" ] || { echo "no projects registered"; exit 1; }

show() {
  echo "Active projects:"
  local any=0
  while IFS= read -r l; do
    case "$l" in
      ""|\#*) [ -n "${l#\#}" ] && echo "  (paused)  $(basename "${l#\#}")" ;;
      *) echo "  ACTIVE    $(basename "$l")"; any=1 ;;
    esac
  done < "$CONF"
  [ "$any" -eq 0 ] && echo "  (none - everything is paused)"
}

TARGET="${1:-}"
[ -z "$TARGET" ] && { show; exit 0; }

TMP="$CONF.tmp"; : > "$TMP"
found=0
while IFS= read -r l; do
  raw="${l#\#}"                       # line without any leading #
  [ -z "$raw" ] && continue
  name="$(basename "$raw")"
  if [ "$TARGET" = "all" ] || [ "$name" = "$TARGET" ]; then
    printf '%s\n' "$raw" >> "$TMP"; found=1
  else
    printf '#%s\n' "$raw" >> "$TMP"
  fi
done < "$CONF"

if [ "$found" -eq 0 ]; then
  rm -f "$TMP"
  echo "no registered project called '$TARGET'"; echo; show; exit 1
fi
mv "$TMP" "$CONF"
[ "$TARGET" = "all" ] && echo "the team will work on every project again" \
                      || echo "the team is now focused on: $TARGET"
echo
show
