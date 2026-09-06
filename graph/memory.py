#!/usr/bin/env python3
"""Graph memory for the agent team, stored in Neo4j.

Every project becomes a node; facts, tasks and commits hang off it, so the
agents can recall what a project is and what has already been done — across
sessions and across agents.

Usage:
  memory.py context  <project-path>              # text block to feed an agent
  memory.py fact     <project-path> "<text>"     # remember a fact
  memory.py commit   <project-path> <agent>      # record the newest git commit
  memory.py facts    <project-path>              # list stored facts
  memory.py wipe     <project-path>              # forget this project
"""
import os, sys, subprocess
from datetime import datetime, timezone

try:
    from neo4j import GraphDatabase
except ImportError:
    sys.exit("neo4j driver missing: use /home/nithin/agent-team/.venv/bin/python")

URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
USER = os.environ.get("NEO4J_USER", "neo4j")
PASS = os.environ.get("NEO4J_PASS", "agentteam123")


def driver():
    return GraphDatabase.driver(URI, auth=(USER, PASS))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pname(path):
    return os.path.basename(os.path.abspath(path.rstrip("/")))


def ensure(tx, name, path):
    tx.run("MERGE (p:Project {name:$n}) SET p.path=$p", n=name, p=path)


def add_fact(path, text, source="agent"):
    n = pname(path)
    with driver() as d, d.session() as s:
        s.execute_write(ensure, n, os.path.abspath(path))
        s.run("""MATCH (p:Project {name:$n})
                 MERGE (f:Fact {text:$t})-[:ABOUT]->(p)
                 ON CREATE SET f.source=$s, f.created=$c""",
              n=n, t=text.strip(), s=source, c=now())
    print(f"remembered about {n}: {text[:70]}")


def add_commit(path, agent):
    n = pname(path)
    sha = subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    msg = subprocess.run(["git", "-C", path, "log", "-1", "--pretty=%s"],
                         capture_output=True, text=True).stdout.strip()
    if not sha:
        return
    with driver() as d, d.session() as s:
        s.execute_write(ensure, n, os.path.abspath(path))
        s.run("""MATCH (p:Project {name:$n})
                 MERGE (c:Commit {sha:$sha})-[:IN]->(p)
                 ON CREATE SET c.message=$m, c.agent=$a, c.created=$t""",
              n=n, sha=sha, m=msg, a=agent, t=now())
    print(f"recorded commit {sha} by {agent} on {n}")


def get_context(path, limit=25):
    """A compact memory block to paste into an agent prompt."""
    n = pname(path)
    with driver() as d, d.session() as s:
        facts = [r["t"] for r in s.run(
            "MATCH (f:Fact)-[:ABOUT]->(:Project {name:$n}) "
            "RETURN f.text AS t ORDER BY f.created DESC LIMIT $l", n=n, l=limit)]
        commits = [(r["s"], r["m"], r["a"]) for r in s.run(
            "MATCH (c:Commit)-[:IN]->(:Project {name:$n}) "
            "RETURN c.sha AS s, c.message AS m, c.agent AS a "
            "ORDER BY c.created DESC LIMIT 10", n=n)]
    out = [f"### Graph memory for {n}"]
    if facts:
        out.append("Known facts:")
        out += [f"- {f}" for f in facts]
    if commits:
        out.append("Recent work by the team:")
        out += [f"- {s} ({a}): {m}" for s, m, a in commits]
    if not facts and not commits:
        out.append("(nothing remembered yet — you are the first agent here)")
    return "\n".join(out)


def list_facts(path):
    n = pname(path)
    with driver() as d, d.session() as s:
        for r in s.run("MATCH (f:Fact)-[:ABOUT]->(:Project {name:$n}) "
                       "RETURN f.text AS t, f.source AS s, f.created AS c "
                       "ORDER BY f.created", n=n):
            print(f"[{r['c']}] ({r['s']}) {r['t']}")


def wipe(path):
    n = pname(path)
    with driver() as d, d.session() as s:
        s.run("MATCH (p:Project {name:$n}) "
              "OPTIONAL MATCH (x)-[:ABOUT|IN]->(p) DETACH DELETE x, p", n=n)
    print(f"forgot {n}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, path = sys.argv[1], sys.argv[2]
    try:
        if cmd == "context":
            print(get_context(path))
        elif cmd == "fact":
            add_fact(path, " ".join(sys.argv[3:]) or sys.exit("need text"))
        elif cmd == "commit":
            add_commit(path, sys.argv[3] if len(sys.argv) > 3 else "unknown")
        elif cmd == "facts":
            list_facts(path)
        elif cmd == "wipe":
            wipe(path)
        else:
            sys.exit(__doc__)
    except Exception as e:
        # Never let memory problems block the agents.
        print(f"[graph memory unavailable: {e}]", file=sys.stderr)
        if cmd == "context":
            print("### Graph memory unavailable this run")
        sys.exit(0)
