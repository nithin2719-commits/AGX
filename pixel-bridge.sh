#!/usr/bin/env bash
# Make non-Claude agents (agy) visible in the Pixel Agents office.
#
# Pixel Agents renders one character per Claude session transcript under
# ~/.claude/projects/. agy has no such transcript, so it never appears. This
# writes a small, well-formed transcript for agy and pings the hook endpoint,
# which makes it show up as its own character beside Claude.
#
# Usage: pixel-bridge.sh <event> <agent> <project-dir> [detail]
#   event: start | work | stop
set -uo pipefail

EVENT="${1:?event: start|work|stop}"
AGENT="${2:?agent}"
PROJ="${3:?project dir}"
DETAIL="${4:-Edit}"

command -v python3 >/dev/null || exit 0

python3 - "$EVENT" "$AGENT" "$PROJ" "$DETAIL" <<'PY' 2>/dev/null || true
import hashlib, json, os, sys, uuid, urllib.request
from datetime import datetime, timezone

event, agent, proj, detail = sys.argv[1:5]
proj = os.path.abspath(proj)
home = os.path.expanduser("~")

# Claude encodes the cwd into the project dir name: / and . become -
slug = proj.replace("/", "-").replace(".", "-")
pdir = os.path.join(home, ".claude", "projects", slug)
os.makedirs(pdir, exist_ok=True)

# Stable session id per agent+project, in real UUID shape.
h = hashlib.md5(f"{agent}:{proj}".encode()).hexdigest()
sid = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
path = os.path.join(pdir, f"{sid}.jsonl")
now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

text = {
    "start": f"[{agent}] starting work in {os.path.basename(proj)}",
    "work":  f"[{agent}] {detail}",
    "stop":  f"[{agent}] finished",
}.get(event, f"[{agent}] {event}")

role = "assistant" if event == "work" else "user"
line = {
    "parentUuid": None, "isSidechain": False, "type": role,
    "message": {"role": role, "content": [{"type": "text", "text": text}]},
    "uuid": str(uuid.uuid4()), "timestamp": now,
    "permissionMode": "bypassPermissions", "userType": "external",
    "cwd": proj, "sessionId": sid, "version": "2.1.251", "gitBranch": "agent-work",
}
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(line) + "\n")

# Nudge the office so it notices immediately instead of on its next poll.
reg = os.path.join(home, ".pixel-agents", "server.json")
try:
    cfg = json.load(open(reg))
    body = json.dumps({
        "hook_event_name": {"start": "SessionStart", "work": "PreToolUse",
                            "stop": "Stop"}.get(event, "PreToolUse"),
        "session_id": sid, "cwd": proj, "transcript_path": path,
        "tool_name": detail,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{cfg['port']}/api/hooks/claude", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['token']}"})
    urllib.request.urlopen(req, timeout=3).read()
except Exception:
    pass  # office not running - never block the worker
PY
exit 0
