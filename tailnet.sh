#!/usr/bin/env bash
# Reach the dashboard from your other devices over Tailscale.
#
#   bash tailnet.sh on     serve https://<this-machine>.<tailnet>.ts.net
#   bash tailnet.sh off    stop serving it
#   bash tailnet.sh link   print the address and a one-tap sign-in link
#
# The dashboard itself stays bound to 127.0.0.1. `tailscale serve` proxies the
# tailnet address to it over HTTPS, so only devices on your tailnet can reach
# it, and each one signs in once with the token in state/token.
#
# Needs Tailscale connected (sudo tailscale up). If serve says access denied,
# let your user manage it once with:  sudo tailscale set --operator=$USER
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${AGX_PORT:-8765}"
HOSTFILE="$DIR/state/tailnet-host"
TOKEN="$DIR/state/token"

usage() { sed -n '4,6p' "$0" | sed 's/^# //'; exit 1; }

# This machine's tailnet name, or nothing when Tailscale is not connected.
tailnet_name() {
  tailscale status --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit()
if d.get("BackendState") == "Running":
    print(((d.get("Self") or {}).get("DNSName") or "").rstrip("."))'
}

case "${1:-}" in
  on)
    command -v tailscale >/dev/null || { echo "tailscale is not installed"; exit 1; }
    NAME="$(tailnet_name)"
    [ -n "$NAME" ] || { echo "Tailscale is not connected. Run:  sudo tailscale up"; exit 1; }
    if ! tailscale serve --bg --https=443 "http://127.0.0.1:$PORT"; then
      echo
      echo "tailscale serve failed. If it said access denied, run once:"
      echo "  sudo tailscale set --operator=$USER"
      echo "If it asked you to enable HTTPS or Serve, open the link it printed, then rerun this."
      exit 1
    fi
    mkdir -p "$DIR/state"
    printf '%s\n' "$NAME" > "$HOSTFILE"
    echo
    echo "Serving https://$NAME -> 127.0.0.1:$PORT (your tailnet only)."
    echo "Each device signs in once with the token:  bash $0 link"
    ;;
  off)
    tailscale serve --https=443 off
    rm -f "$HOSTFILE"
    echo "Stopped serving the dashboard on the tailnet. Remote requests are refused."
    ;;
  link)
    [ -s "$HOSTFILE" ] || { echo "Not serving. Run:  bash $0 on"; exit 1; }
    [ -s "$TOKEN" ] || { echo "No token yet: start the dashboard once."; exit 1; }
    NAME="$(head -1 "$HOSTFILE")"
    T="$(cat "$TOKEN")"
    echo "Address:       https://$NAME/"
    echo "Token:         $T"
    echo "Sign-in link:  https://$NAME/login#$T"
    echo
    echo "The sign-in link contains the token. Open it only on your own devices."
    if command -v qrencode >/dev/null; then
      echo
      qrencode -t ansiutf8 "https://$NAME/login#$T"
    fi
    ;;
  *) usage ;;
esac
