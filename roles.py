#!/usr/bin/env python3
"""Engineering roles for the agent team.

Instead of two identical workers, each agent can take on a real engineering
role (AI engineer, backend architect, DevOps automator, code reviewer...).
The role definitions come from the agency-agents collection in ~/multi-agent.

  roles.py list                  show the roles that fit software work
  roles.py show <role>           print one role definition
  roles.py get <project>         which roles this project uses
  roles.py set <project> <claude-role> <agy-role>
"""
import os, sys, json, re

BASE = os.path.dirname(os.path.abspath(__file__))
ROLE_DIRS = [
    os.path.expanduser("~/multi-agent/repos/agency-agents/engineering"),
    os.path.expanduser("~/multi-agent/repos/agency-agents/design"),
    os.path.expanduser("~/multi-agent/repos/agency-agents/product"),
    os.path.expanduser("~/multi-agent/repos/agency-agents/testing"),
]
# Roles that actually make sense for a coding team, kept short on purpose:
# a picker with 318 entries is not a picker.
FEATURED = [
    "engineering-ai-engineer",
    "engineering-backend-architect",
    "engineering-devops-automator",
    "engineering-code-reviewer",
    "engineering-data-engineer",
    "engineering-api-platform-engineer",
    "engineering-codebase-onboarding-engineer",
    "engineering-database-optimizer",
    "engineering-developer-tooling-engineer",
    "engineering-frontend-developer",
    "engineering-performance-benchmarker",
    "engineering-security-engineer",
]


def find(role):
    for d in ROLE_DIRS:
        p = os.path.join(d, role + ".md")
        if os.path.exists(p):
            return p
    return ""


def available():
    out = []
    for d in ROLE_DIRS:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".md"):
                out.append(f[:-3])
    return out


def catalogue():
    """Featured roles first, each with a one-line description."""
    have = set(available())
    items = []
    for r in FEATURED:
        if r not in have:
            continue
        items.append({"id": r, "label": pretty(r), "desc": one_liner(r)})
    return items


def pretty(role):
    return re.sub(r"^(engineering|design|product|testing)-", "", role).replace("-", " ").title()


def one_liner(role):
    p = find(role)
    if not p:
        return ""
    try:
        txt = open(p, encoding="utf-8", errors="replace").read(4000)
    except OSError:
        return ""
    m = re.search(r"^description:\s*(.+)$", txt, re.M)
    if m:
        return m.group(1).strip().strip('"')[:150]
    for line in txt.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-", "`", "---", "name:")):
            return line[:150]
    return ""


def definition(role, limit=4000):
    p = find(role)
    if not p:
        return ""
    try:
        return open(p, encoding="utf-8", errors="replace").read()[:limit]
    except OSError:
        return ""


def roles_file(proj):
    return os.path.join(proj, ".agent-team", "roles.json")


def get(proj):
    try:
        return json.load(open(roles_file(proj)))
    except Exception:
        return {"claude": "", "agy": ""}


def set_roles(proj, claude_role, agy_role):
    d = {"claude": claude_role or "", "agy": agy_role or ""}
    os.makedirs(os.path.dirname(roles_file(proj)), exist_ok=True)
    json.dump(d, open(roles_file(proj), "w"), indent=2)
    return d


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "list":
        for r in catalogue():
            print(f"{r['id']:<45} {r['desc'][:70]}")
    elif a[0] == "show" and len(a) > 1:
        print(definition(a[1]) or "role not found")
    elif a[0] == "get" and len(a) > 1:
        print(json.dumps(get(a[1])))
    elif a[0] == "set" and len(a) > 3:
        print(json.dumps(set_roles(a[1], a[2], a[3])))
    elif a[0] == "prompt" and len(a) > 2:
        # Used by meet.sh: emit the role block for one agent.
        r = get(a[1]).get(a[2], "")
        if r:
            print(f"=== YOUR ROLE IN THIS MEETING: {pretty(r)} ===\n"
                  f"{definition(r, 2500)}\n"
                  f"Argue from this role's priorities. Do not be a generalist.")
    else:
        sys.exit(__doc__)
