#!/usr/bin/env python3
"""Per-project MCP servers for the agent team.

A project can enable MCP servers (e.g. HexStrike for cyber work). When enabled,
the server is written into the project's own .mcp.json, which Claude Code reads
automatically - so the agents working that project gain those tools, and no
other project is affected.

  mcp.py catalog                 list available MCP servers
  mcp.py get <project>           which servers this project has enabled
  mcp.py enable  <project> <id>  add a server to the project's .mcp.json
  mcp.py disable <project> <id>  remove it
"""
import os, sys, json

BASE = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(BASE, "mcp-catalog.json")


def catalog():
    try:
        return json.load(open(CATALOG))
    except Exception:
        return {}


def proj_mcp(proj):
    return os.path.join(proj, ".mcp.json")


def load(proj):
    try:
        return json.load(open(proj_mcp(proj)))
    except Exception:
        return {"mcpServers": {}}


def enabled(proj):
    return list(load(proj).get("mcpServers", {}).keys())


def enable(proj, sid):
    cat = catalog()
    if sid not in cat:
        return f"no such server: {sid}"
    cfg = load(proj)
    cfg.setdefault("mcpServers", {})[sid] = cat[sid]["server"]
    json.dump(cfg, open(proj_mcp(proj), "w"), indent=2)
    return f"enabled {sid} for {os.path.basename(proj)}"


def disable(proj, sid):
    cfg = load(proj)
    if sid in cfg.get("mcpServers", {}):
        del cfg["mcpServers"][sid]
        json.dump(cfg, open(proj_mcp(proj), "w"), indent=2)
        return f"disabled {sid}"
    return f"{sid} was not enabled"


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "catalog":
        for sid, c in catalog().items():
            print(f"{sid:<12} {c['label']:<34} ({c['for']})")
    elif a[0] == "get" and len(a) > 1:
        print(json.dumps(enabled(a[1])))
    elif a[0] == "enable" and len(a) > 2:
        print(enable(a[1], a[2]))
    elif a[0] == "disable" and len(a) > 2:
        print(disable(a[1], a[2]))
    else:
        sys.exit(__doc__)
