#!/usr/bin/env python3
"""Live GUI for the agent team. Run:  python3 dashboard.py  ->  http://localhost:8765

Shows every project: what each agent is doing, the plan you gave them, the tasks,
and the full text of their planning meetings and work logs.
"""
import http.server, socketserver, json, os, sys, subprocess, re, html, hmac, secrets, hashlib, time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(BASE, "logs")
PORT = int(os.environ.get("AGX_PORT", "8765"))
AGENTS = ("claude", "agy")
# GitHub accounts that belong to the user, so the project scanner can tell
# their own repos apart from cloned third-party tools.
MINE = {"nithin2719-commits", "nithin2729-commits", "xcaptain09", "mano-dev-01"}
SETTINGS = os.path.join(BASE, "settings.json")
TOKEN_FILE = os.path.join(BASE, "state", "token")
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
MAX_BODY = 12_000_000          # an attached image for the Workspace tab


def load_token():
    """The secret every POST must carry in X-AGX-Token. Created once, mode 600."""
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    try:
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    except FileExistsError:
        pass
    os.chmod(TOKEN_FILE, 0o600)
    tok = open(TOKEN_FILE).read().strip()
    if len(tok) < 32:
        raise SystemExit(f"{TOKEN_FILE} is too short; delete it to make a new one")
    return tok


TOKEN = load_token()

# Remote access over Tailscale. tailnet.sh runs `tailscale serve`, which
# proxies https://<machine>.<tailnet>.ts.net to this loopback port and adds
# forwarding headers, and writes that name to state/tailnet-host. A request
# that carries forwarding headers is remote: it must name the tailnet host and
# carry the session cookie from /login. Plain localhost needs no sign-in.
TAILNET_FILE = os.path.join(BASE, "state", "tailnet-host")
FORWARDED = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded",
             "Tailscale-User-Login")
# The cookie is derived from the token, so a new token signs everyone out.
SESSION = hmac.new(TOKEN.encode(), b"agx-session-v1", hashlib.sha256).hexdigest()
SESSION_AGE = 30 * 24 * 3600
PUBLIC = {"/login", "/app.css"}           # what a signed-out remote browser may load
_TAILNET = {"mtime": None, "hosts": frozenset()}


def tailnet_hosts():
    """Names the tailnet may use. Empty when remote access is off."""
    try:
        m = os.path.getmtime(TAILNET_FILE)
    except OSError:
        return frozenset()
    if m != _TAILNET["mtime"]:
        names = {l.strip().lower().rstrip(".") for l in read(TAILNET_FILE, 2000).splitlines()}
        _TAILNET.update(mtime=m, hosts=frozenset(n for n in names if n))
    return _TAILNET["hosts"]

_MODEL_CACHE = {"t": 0, "v": None}


def discover_models():
    """Ask each CLI what it can actually run, instead of hardcoding a guess.

    agy publishes a real list via `agy models` (effort is baked into the model
    id, e.g. -low/-medium/-high). Claude Code takes aliases plus a separate
    reasoning-effort setting, so we build the combinations it accepts.
    """
    import time
    if _MODEL_CACHE["v"] and time.time() - _MODEL_CACHE["t"] < 600:
        return _MODEL_CACHE["v"]

    claude = []
    for mid, note in (("haiku", "fastest, cheapest"),
                      ("sonnet", "balanced"),
                      ("opus", "strongest")):
        for eff, elabel in (("", "default"), ("low", "low effort"),
                            ("medium", "medium effort"), ("high", "high effort")):
            claude.append({
                "id": f"{mid}|{eff}" if eff else mid,
                "label": f"{mid.title()}" + (f" · {eff}" if eff else ""),
                "note": note if not eff else elabel,
            })

    agy = []
    try:
        out = subprocess.run(["agy", "models"], capture_output=True, text=True,
                             timeout=30).stdout
        for line in out.splitlines():
            if "\t" in line:
                mid, label = line.split("\t", 1)
                mid, label = mid.strip(), label.strip()
                if mid and not mid.lower().startswith("fetching"):
                    agy.append({"id": mid, "label": label, "note": ""})
    except Exception:
        pass
    if not agy:
        agy = [{"id": "gemini-3.1-pro-high", "label": "Gemini 3.1 Pro (High)",
                "note": "fallback - `agy models` unavailable"}]

    v = {"claude": claude, "agy": agy}
    _MODEL_CACHE.update({"t": time.time(), "v": v})
    return v


def settings():
    try:
        return json.load(open(SETTINGS))
    except Exception:
        return {"model_claude": "", "model_agy": "", "session_note": ""}


def save_settings(d):
    cur = settings()
    cur.update(d)
    with open(SETTINGS, "w") as f:
        json.dump(cur, f, indent=2)
    return cur


def all_projects():
    """Every registered project as (path, active). Paused ones are '#'-prefixed
    in projects.conf - they must still be visible, just clearly marked."""
    f = os.path.join(BASE, "projects.conf")
    if not os.path.exists(f):
        return []
    out = []
    for l in open(f):
        l = l.strip()
        if not l:
            continue
        active = not l.startswith("#")
        path = l.lstrip("#").strip()
        if path and os.path.isdir(path):
            out.append((path, active))
    return out


def projects():
    return [p for p, active in all_projects() if active]


def git(proj, *a):
    try:
        return subprocess.run(["git", "-C", proj, *a], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def read(p, limit=200000):
    try:
        return open(p, encoding="utf-8", errors="replace").read()[:limit]
    except OSError:
        return ""


def pick_remote(proj):
    """Choose the remote that actually belongs to the user.

    A repo can have several remotes (a fork, a collaborator's, an old one).
    Blindly using 'origin' showed someone else's fork as if it were theirs, so
    prefer a remote whose GitHub owner is one of the user's accounts.
    """
    out = git(proj, "remote", "-v")
    remotes = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = parts[1]
    if not remotes:
        return "", ""
    # An explicit choice always wins over any heuristic: write the remote name
    # into .agent-team/remote when a repo has several and only one is correct.
    pin = read(os.path.join(proj, ".agent-team", "remote"), 200).strip()
    if pin and pin in remotes:
        return pin, remotes[pin]
    owned = []
    for nm, url in remotes.items():
        m = re.search(r"github\.com[:/]([^/]+)/", url)
        if m and m.group(1).lower() in MINE:
            owned.append((nm, url))
    if owned:
        # Prefer a remote the current branch actually tracks, else the first.
        up = git(proj, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        tracked = up.split("/")[0] if up and "/" in up else ""
        for nm, url in owned:
            if nm == tracked:
                return nm, url
        return owned[0]
    if "origin" in remotes:
        return "origin", remotes["origin"]
    return next(iter(remotes.items()))


def graph_stats(name):
    """Facts and commits the graph memory holds. Never fatal if Neo4j is down."""
    try:
        sys.path.insert(0, os.path.join(BASE, "graph"))
        from neo4j import GraphDatabase
        uri = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
        with GraphDatabase.driver(uri, auth=("neo4j", "agentteam123")) as d, d.session() as s:
            f = s.run("MATCH (f:Fact)-[:ABOUT]->(:Project {name:$n}) RETURN count(f) AS c",
                      n=name).single()["c"]
            c = s.run("MATCH (x:Commit)-[:IN]->(:Project {name:$n}) RETURN count(x) AS c",
                      n=name).single()["c"]
        return {"facts": f, "commits": c, "up": True}
    except Exception:
        return {"facts": 0, "commits": 0, "up": False}


def collect():
    data = []
    for p, active in all_projects():
        mem = os.path.join(p, ".agent-team")
        name = os.path.basename(p)
        e = {"name": name, "path": p, "active": active, "agents": {},
             "commits": [], "progress": [],
             "tasks": {"done": 0, "todo": 0, "doing": 0}, "next_task": "",
             "plan": "", "talks": [], "logs": []}

        for a in AGENTS:
            sf = os.path.join(mem, f"status-{a}.json")
            if os.path.exists(sf):
                try:
                    st = json.load(open(sf))
                    # A "working" status is only real if the process still exists.
                    st["running"] = bool(st.get("pid")) and alive(st["pid"])
                    if st.get("state") == "working" and not st["running"]:
                        st["state"] = "stopped"
                        st["task"] = "(process gone)"
                    e["agents"][a] = st
                except Exception:
                    pass

        log = git(p, "log", "--oneline", "-8")
        e["commits"] = log.splitlines() if log else []

        # Who has actually done how much work. Commit prefixes are unreliable now
        # that agents follow each project's own convention, so count the runs the
        # workers themselves recorded in PROGRESS.md.
        prog = read(os.path.join(mem, "PROGRESS.md"), 200000)
        done = re.findall(r"^- .*?\|\s*(claude|agy)\s*\|\s*(.*)$", prog, re.M)
        e["work"] = {
            "claude": sum(1 for a, t in done if a == "claude" and "(no commit)" not in t),
            "agy": sum(1 for a, t in done if a == "agy" and "(no commit)" not in t),
            "failed": sum(1 for _, t in done if "(no commit)" in t),
        }
        allmsg = git(p, "log", "--pretty=%s")
        e["work"]["total"] = len(allmsg.splitlines()) if allmsg else 0

        # --- GitHub / remote status -------------------------------------
        rname, remote = pick_remote(p)
        branch = git(p, "rev-parse", "--abbrev-ref", "HEAD") or "?"
        gh = {"connected": bool(remote), "url": remote, "branch": branch,
              "remote_name": rname, "web": "", "ahead": 0, "behind": 0,
              "upstream": "", "dirty": 0}
        if remote:
            web = remote
            if web.startswith("git@"):                 # git@github.com:u/r.git
                web = "https://" + web[4:].replace(":", "/", 1)
            gh["web"] = re.sub(r"\.git$", "", web)
            up = git(p, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            if up and "fatal" not in up:
                gh["upstream"] = up
                counts = git(p, "rev-list", "--left-right", "--count", f"{up}...HEAD")
                parts = counts.split()
                if len(parts) == 2:
                    gh["behind"], gh["ahead"] = int(parts[0]), int(parts[1])
        gh["dirty"] = len([l for l in git(p, "status", "--porcelain").splitlines() if l])
        e["github"] = gh

        # what the shared graph memory holds for this project
        e["graph"] = graph_stats(name)

        # does a graphify code graph exist, and how big is it?
        gj = os.path.join(p, "graphify-out", "graph.json")
        e["codegraph"] = {"built": os.path.exists(gj), "nodes": 0}
        if e["codegraph"]["built"]:
            try:
                with open(gj, encoding="utf-8") as fh:
                    e["codegraph"]["nodes"] = len(json.load(fh).get("nodes", []))
            except Exception:
                pass

        pl = read(os.path.join(mem, "PLAN.md"), 20000)
        e["plan_raw"] = pl
        if pl and "(nothing yet" not in pl:
            e["plan"] = pl[:4000]

        bl = os.path.join(mem, "backlog.md")
        if os.path.exists(bl):
            t = read(bl)
            e["tasks"]["done"] = len(re.findall(r"^- \[x\]", t, re.M))
            e["tasks"]["doing"] = len(re.findall(r"^- \[~\]", t, re.M))
            todo = re.findall(r"^- \[ \] *(.*)$", t, re.M)
            e["tasks"]["todo"] = len(todo)
            e["next_task"] = todo[0] if todo else ""
            # Full task list for the project detail window (state + text).
            e["all_tasks"] = [
                {"state": st.strip() or " ", "text": txt}
                for st, txt in re.findall(r"^- \[([ x~])\] *(.*)$", t, re.M)
            ][:60]

        pf = os.path.join(mem, "PROGRESS.md")
        if os.path.exists(pf):
            e["progress"] = [l.strip() for l in open(pf, encoding="utf-8",
                             errors="replace") if l.strip().startswith("- ")][-6:]

        # planning meetings
        dd = os.path.join(mem, "discussions")
        if os.path.isdir(dd):
            for f in sorted(os.listdir(dd), reverse=True)[:8]:
                e["talks"].append({"label": f.replace(".md", ""),
                                   "file": os.path.join(dd, f)})

        # worker logs for this project
        if os.path.isdir(LOGDIR):
            ls = [f for f in os.listdir(LOGDIR) if f.startswith(name + "-")]
            ls.sort(reverse=True)
            for f in ls[:8]:
                e["logs"].append({"label": f.replace(".log", ""),
                                  "file": os.path.join(LOGDIR, f)})

        # One honest status for the whole project, so it can be sorted and read
        # at a glance instead of inferred from two agent rows.
        working = [a for a in AGENTS
                   if e["agents"].get(a, {}).get("state") == "working"
                   and e["agents"].get(a, {}).get("running")]
        if working:
            e["status"] = "running"
            e["status_text"] = ("BOTH AGENTS WORKING" if len(working) > 1
                                else f"{working[0].upper()} WORKING")
        elif not active:
            e["status"] = "paused"
            e["status_text"] = "PAUSED - timers skip this project"
        elif e["tasks"]["doing"]:
            e["status"] = "stalled"
            e["status_text"] = "TASK CLAIMED BUT NOBODY IS WORKING"
        elif e["tasks"]["todo"]:
            e["status"] = "waiting"
            e["status_text"] = f"{e['tasks']['todo']} TASKS WAITING"
        else:
            e["status"] = "idle"
            e["status_text"] = "NO WORK LEFT - run a meeting"
        e["busy_agents"] = working
        data.append(e)

    order = {"running": 0, "stalled": 1, "waiting": 2, "idle": 3, "paused": 4}
    data.sort(key=lambda x: (order.get(x["status"], 9), x["name"].lower()))
    return data


def allowed(path):
    """Only serve files that belong to the agent team: the logs folder and each
    project's .agent-team folder. Symlinks and ".." are resolved first, so a
    link inside an allowed folder cannot point anywhere else."""
    if not path:
        return False
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        return False
    roots = [os.path.realpath(LOGDIR)] + [
        os.path.realpath(os.path.join(p, ".agent-team")) for p, _ in all_projects()]
    return any(os.path.commonpath([real, r]) == r for r in roots)


WEB = os.path.join(BASE, "web")
STATIC = {"/app.css": "text/css; charset=utf-8",
          "/app.js": "application/javascript; charset=utf-8"}


def index_page():
    """web/index.html with the token filled in for the page's own requests.
    Read on every request so a UI edit shows up on reload."""
    return read(os.path.join(WEB, "index.html")).replace("__AGX_TOKEN__", TOKEN)


def proj_by_name(name):
    # Search ALL registered projects, paused included. Pausing only tells the
    # timers to skip a project - it must never break a button the user pressed.
    for p, _active in all_projects():
        if os.path.basename(p) == name:
            return p
    return None


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def spawn(cmd):
    """Fire and forget, in its own process group so it can be killed cleanly."""
    subprocess.Popen(cmd, cwd=BASE, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def do_action(body):
    act = body.get("action", "")
    name = body.get("project", "")
    proj = proj_by_name(name) if name else None

    if act.startswith("ws_"):
        # The Workspace tab: free-model chat, vision, keys, image generation.
        import workspace
        fn = workspace.ACTIONS.get(act)
        return fn(body) if fn else {"ok": False, "msg": f"unknown action: {act}"}

    if act in ("run_claude", "run_agy"):
        if not proj:
            return {"ok": False, "msg": "unknown project"}
        agent = "claude" if act == "run_claude" else "agy"
        spawn(["bash", os.path.join(BASE, "run.sh"), agent, proj])
        return {"ok": True, "msg": f"{agent} started on {name}"}

    if act == "meet":
        if not proj:
            return {"ok": False, "msg": "unknown project"}
        spawn(["bash", os.path.join(BASE, "meet.sh"), proj])
        return {"ok": True, "msg": f"planning meeting started on {name} (~10-20 min)"}

    if act == "team":
        spawn(["bash", os.path.join(BASE, "team.sh")])
        return {"ok": True, "msg": "team round started on every project"}

    if act == "cycle":
        spawn(["bash", os.path.join(BASE, "cycle.sh")])
        return {"ok": True, "msg": "full cycle started: agents will hold a meeting "
                                   "where work has run out, then both get to work"}

    if act == "graphify":
        if not proj:
            return {"ok": False, "msg": "unknown project"}
        spawn(["bash", os.path.join(BASE, "build-graph.sh"), proj])
        return {"ok": True, "msg": f"rebuilding knowledge graph for {name}"}

    if act == "stop":
        cmd = ["bash", os.path.join(BASE, "stop.sh")]
        if proj:
            cmd.append(proj)
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
        return {"ok": True, "msg": out.strip() or "nothing was running"}

    if act == "stop_all":
        out = subprocess.run(["bash", os.path.join(BASE, "stop.sh"), "--timers"],
                             capture_output=True, text=True, timeout=60).stdout
        return {"ok": True, "msg": out.strip() or "stopped"}

    if act == "save_plan":
        if not proj:
            return {"ok": False, "msg": "unknown project"}
        text = body.get("text", "")
        if len(text) > 100000:
            return {"ok": False, "msg": "plan too long"}
        with open(os.path.join(proj, ".agent-team", "PLAN.md"), "w",
                  encoding="utf-8") as f:
            f.write(text)
        return {"ok": True, "msg": "plan saved - agents use it on their next run"}

    if act == "chat":
        # Ask an agent a question about a project and wait for the answer.
        # Read-only by design: no permission bypass, so it cannot change the repo.
        agent = body.get("agent", "claude")
        msg = (body.get("text") or "").strip()
        if not msg:
            return {"ok": False, "msg": "empty question"}
        if agent not in ("claude", "agy"):
            return {"ok": False, "msg": "unknown agent"}
        cwd = proj or BASE
        mem = os.path.join(cwd, ".agent-team")
        ctx = (f"You are answering a question about the project at {cwd}.\n"
               f"Its plan: {read(os.path.join(mem, 'PLAN.md'), 2500)}\n"
               f"Recent work: {read(os.path.join(mem, 'PROGRESS.md'), 2500)[-1800:]}\n"
               f"Backlog: {read(os.path.join(mem, 'backlog.md'), 2500)}\n\n"
               f"QUESTION: {msg}\n\n"
               "Answer briefly and concretely. Do NOT modify any files.")
        cmd = ([ "claude", "-p", ctx] if agent == "claude"
               else ["agy", "-p", ctx, "--print-timeout", "4m"])
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                               timeout=300)
            out = (r.stdout or r.stderr or "").strip()
            return {"ok": True, "msg": f"{agent} replied",
                    "reply": out[:12000] or "(empty reply)", "agent": agent}
        except subprocess.TimeoutExpired:
            return {"ok": False, "msg": f"{agent} timed out after 5 minutes"}
        except Exception as e:
            return {"ok": False, "msg": f"{agent} failed: {e}"}

    if act == "set_model":
        agent = body.get("agent", "")
        model = body.get("model", "")
        if agent not in ("claude", "agy"):
            return {"ok": False, "msg": "unknown agent"}
        valid = {m["id"] for m in discover_models()[agent]}
        if model and model not in valid:
            return {"ok": False, "msg": "unknown model"}
        save_settings({f"model_{agent}": model})
        return {"ok": True,
                "msg": f"{agent} will use {model or 'its default model'} from the next run"}

    if act == "set_note":
        save_settings({"session_note": (body.get("text") or "")[:4000]})
        return {"ok": True, "msg": "saved - this is here next time you open the dashboard"}

    if act == "commit":
        # Commit whatever the agents left uncommitted, with a message written
        # from the actual diff. No AI attribution anywhere.
        if not proj:
            return {"ok": False, "msg": "unknown project"}
        dirty = git(proj, "status", "--porcelain")
        if not dirty.strip():
            return {"ok": False, "msg": "nothing to commit - working tree is clean"}
        msg = (body.get("text") or "").strip()
        if not msg:
            stat = git(proj, "diff", "--stat", "HEAD")
            files = [l.split()[0] for l in dirty.splitlines() if len(l.split()) > 1][:12]
            q = ("Write ONE git commit subject line for these changes.\n"
                 "Rules: imperative mood, under 72 characters, describe the change "
                 "itself. Do NOT mention AI, Claude, agents, or tools. Do NOT add "
                 "a body, quotes, or trailers. Output ONLY the subject line.\n\n"
                 f"Files:\n{chr(10).join(files)}\n\nDiffstat:\n{stat[:3000]}")
            try:
                r = subprocess.run(["claude", "-p", q], cwd=proj,
                                   capture_output=True, text=True, timeout=180)
                msg = (r.stdout or "").strip().splitlines()[0].strip().strip('"')
            except Exception:
                msg = ""
        if not msg:
            msg = "Update project files"
        # Strip any attribution that slipped through.
        msg = re.sub(r"(?i)\b(co-authored-by|claude|anthropic|agy|antigravity|"
                     r"generated (with|by)|ai[- ]?assisted)\b.*", "", msg).strip()
        msg = msg.strip(" -:") or "Update project files"
        try:
            subprocess.run(["git", "-C", proj, "add", "-A"],
                           capture_output=True, timeout=60)
            r = subprocess.run(["git", "-C", proj, "commit", "-m", msg],
                               capture_output=True, text=True, timeout=90)
            if r.returncode == 0:
                return {"ok": True, "msg": f'committed: "{msg}"'}
            return {"ok": False,
                    "msg": (r.stderr or r.stdout or "commit failed").strip()[:200]}
        except Exception as e:
            return {"ok": False, "msg": f"commit failed: {e}"}

    if act == "scan":
        # Find git repos worth adding, under the usual places.
        roots = [os.path.expanduser("~/Projects"), os.path.expanduser("~")]
        known = {os.path.abspath(p) for p, _ in all_projects()}
        found = []
        seen = set()
        for root in roots:
            if not os.path.isdir(root):
                continue
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                continue
            for d in entries:
                # Skip dotfolders and vendored tool clones - they are not the
                # user's projects and would flood the list.
                if d.startswith("."):
                    continue
                path = os.path.abspath(os.path.join(root, d))
                if path in seen or path in known or not os.path.isdir(path):
                    continue
                if not os.path.isdir(os.path.join(path, ".git")):
                    continue
                # A repo with no commits is usually a stray clone or a giant
                # data folder someone ran 'git init' in by accident.
                if (git(path, "rev-list", "--count", "HEAD") or "0") == "0":
                    continue
                seen.add(path)
                br = git(path, "rev-parse", "--abbrev-ref", "HEAD") or "?"
                n = git(path, "rev-list", "--count", "HEAD") or "0"
                rem = git(path, "remote", "get-url", "origin") or ""
                owner = ""
                m = re.search(r"github\.com[:/]([^/]+)/", rem)
                if m:
                    owner = m.group(1)
                # "Yours" = you own the remote, or it has no remote at all
                # (local-only work). Everything else is a clone of someone
                # else's repo and almost certainly not what you want the
                # agents touching.
                yours = (not rem) or (owner.lower() in MINE)
                found.append({"path": path, "name": os.path.basename(path),
                              "branch": br, "commits": n, "remote": rem,
                              "owner": owner, "yours": yours})
        found.sort(key=lambda x: (not x["yours"], x["name"].lower()))
        mine = sum(1 for f in found if f["yours"])
        return {"ok": True,
                "msg": f"{mine} of yours, {len(found) - mine} third-party clones",
                "found": found}

    if act == "add_project":
        path = (body.get("path") or "").strip()
        if not path:
            return {"ok": False, "msg": "no path given"}
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(path):
            return {"ok": False, "msg": f"not a folder: {path}"}
        if not os.path.isdir(os.path.join(path, ".git")):
            return {"ok": False, "msg": "not a git repo - run 'git init' there first"}
        try:
            r = subprocess.run(["bash", os.path.join(BASE, "add-project.sh"), path],
                               capture_output=True, text=True, timeout=90)
            ok = r.returncode == 0
            first = (r.stdout or r.stderr or "").strip().splitlines()
            msg = first[0] if first else ("added " + os.path.basename(path))
            if ok:
                # Set everything up straight away so the project is usable the
                # moment it appears: build its code graph in the background.
                spawn(["bash", os.path.join(BASE, "build-graph.sh"), path])
                msg += " — building its code graph now"
            return {"ok": ok, "msg": msg}
        except Exception as e:
            return {"ok": False, "msg": f"failed: {e}"}

    if act == "push":
        if not proj:
            return {"ok": False, "msg": "unknown project"}
        br = git(proj, "rev-parse", "--abbrev-ref", "HEAD")
        if not br or br == "HEAD":
            return {"ok": False, "msg": "detached HEAD - nothing to push"}
        rname, _ = pick_remote(proj)
        if not rname:
            return {"ok": False, "msg": "no remote configured"}
        try:
            r = subprocess.run(["git", "-C", proj, "push", "-u", rname, br],
                               capture_output=True, text=True, timeout=180)
            out = (r.stderr or r.stdout or "").strip().splitlines()
            tail = out[-1] if out else ""
            if r.returncode == 0:
                return {"ok": True, "msg": f"pushed {br} to origin. {tail}"}
            return {"ok": False, "msg": f"push failed: {tail}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "msg": "push timed out (auth prompt?)"}
        except Exception as e:
            return {"ok": False, "msg": f"push failed: {e}"}

    if act == "explain":
        # Read the recent logs for this project and have an agent explain what
        # actually happened. Nothing is written to disk - analysis only.
        if not proj:
            return {"ok": False, "msg": "unknown project"}
        pname = os.path.basename(proj)
        logs = sorted((f for f in os.listdir(LOGDIR) if f.startswith(pname + "-")),
                      reverse=True)[:6] if os.path.isdir(LOGDIR) else []
        blob = ""
        for f in logs:
            body = read(os.path.join(LOGDIR, f), 6000)
            blob += f"\n===== {f} =====\n{body[-4000:]}\n"
        mem = os.path.join(proj, ".agent-team")
        blob += "\n===== PROGRESS.md (tail) =====\n" + read(
            os.path.join(mem, "PROGRESS.md"), 60000)[-3000:]
        blob += "\n===== recent commits =====\n" + git(proj, "log", "--oneline", "-12")
        if not blob.strip():
            return {"ok": False, "msg": "no logs yet for this project"}
        q = ("You are reading the raw work logs of an autonomous agent team.\n"
             "Explain to the project owner, in plain language:\n"
             "1. What actually got DONE (real changes, with file names).\n"
             "2. What FAILED or was abandoned, and the likely reason.\n"
             "3. What the team should do next.\n"
             "Be specific and short. Use bullet points. Do not invent anything "
             "that is not in the logs. Do not write any files.\n\n" + blob[:60000])
        try:
            r = subprocess.run(["claude", "-p", q], cwd=proj, capture_output=True,
                               text=True, timeout=420)
            out = (r.stdout or r.stderr or "").strip()
            return {"ok": True, "msg": "log analysis ready",
                    "reply": out[:14000] or "(empty)", "agent": "claude"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "msg": "analysis timed out"}
        except Exception as e:
            return {"ok": False, "msg": f"analysis failed: {e}"}

    if act == "run_both":
        if not proj:
            return {"ok": False, "msg": "unknown project"}
        for a in ("claude", "agy"):
            spawn(["bash", os.path.join(BASE, "run.sh"), a, proj])
        return {"ok": True, "msg": f"Claude and agy both started on {name} "
                                   "(they take turns on the project lock)"}

    if act == "add_task":
        if not proj:
            return {"ok": False, "msg": "unknown project"}
        t = body.get("text", "").strip().replace("\n", " ")
        if not t:
            return {"ok": False, "msg": "empty task"}
        with open(os.path.join(proj, ".agent-team", "backlog.md"), "a",
                  encoding="utf-8") as f:
            f.write(f"- [ ] {t}\n")
        return {"ok": True, "msg": "task added"}

    return {"ok": False, "msg": f"unknown action: {act}"}


def host_name(value):
    """'localhost:8765' -> 'localhost'; '[::1]:8765' -> '::1'."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value[1:value.find("]")] if "]" in value else ""
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


class H(http.server.BaseHTTPRequestHandler):
    def _deny(self, code, msg):
        body = json.dumps({"ok": False, "msg": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where, cookie=None):
        self.send_response(303)
        self.send_header("Location", where)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _signed_in(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
        except Exception:
            return False
        v = c["agx_session"].value if "agx_session" in c else ""
        return hmac.compare_digest(v.encode(), SESSION.encode())

    def _gate(self, method):
        """Decide whether this request may go on; sets self.local.

        Local: no forwarding headers and Host is localhost or 127.0.0.1. Any
        other Host is refused, which stops DNS rebinding: a page on another
        site that points its name at 127.0.0.1 still sends its own name.

        Remote (forwarded by tailscale serve): remote access must be on, the
        name the browser used must be the tailnet host, and the browser must be
        signed in. Signed-out browsers are sent to /login."""
        host = host_name(self.headers.get("Host"))
        forwarded = any(self.headers.get(h) for h in FORWARDED)
        self.local = not forwarded and host in LOCAL_HOSTS
        if self.local:
            return True
        hosts = tailnet_hosts()
        xfh = self.headers.get("X-Forwarded-Host")
        name = host_name(xfh.split(",")[0]) if xfh else host
        # If the proxy rewrote Host to the loopback address without saying
        # where the request was addressed, the request is still forwarded.
        if not hosts or not (name in hosts or (forwarded and not xfh and name in LOCAL_HOSTS)):
            self._deny(403, "host not allowed")
            return False
        path = urlparse(self.path).path
        if self._signed_in() or path in PUBLIC:
            return True
        if method == "GET":
            self._redirect("/login")
        else:
            self._deny(401, "sign in first")
        return False

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        allowed = LOCAL_HOSTS if self.local else tailnet_hosts()
        return not origin or host_name(urlparse(origin).netloc) in allowed

    def _login(self):
        """POST /login: compare the token, then set the session cookie."""
        if not self._origin_ok():
            self._deny(403, "origin not allowed")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = -1
        if not 0 <= n <= 4096:
            self._deny(413, "body too large")
            return
        form = parse_qs(self.rfile.read(n).decode(errors="replace"))
        given = (form.get("token") or [""])[0].strip()
        if not hmac.compare_digest(given.encode(), TOKEN.encode()):
            time.sleep(1)                        # slow down guessing
            self._redirect("/login?e=1")
            return
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "") == "https" else ""
        self._redirect("/", f"agx_session={SESSION}; Path=/; Max-Age={SESSION_AGE}; "
                            f"HttpOnly; SameSite=Strict{secure}")

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Never cache: the UI changes often and a stale page looks like
        # "nothing changed" even after a redeploy.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._gate("GET"):
            return
        u = urlparse(self.path)
        if u.path == "/login":
            if self.local or self._signed_in():
                self._redirect("/")
            else:
                self._send(read(os.path.join(WEB, "login.html")).encode(),
                           "text/html; charset=utf-8")
        elif u.path == "/api":
            self._send(json.dumps({"projects": collect(), "models": discover_models(),
                                   "settings": settings()}).encode(),
                       "application/json")
        elif u.path == "/graph":
            # Serve a project's graphify visualisation so the GRAPH button has
            # somewhere to lead. Without this the graph is built and never seen.
            name = (parse_qs(u.query).get("p") or [""])[0]
            proj = proj_by_name(name)
            f = os.path.join(proj, "graphify-out", "graph.html") if proj else ""
            if f and os.path.exists(f):
                page = read(f, 40_000_000)
                # graphify pulls vis-network from unpkg, which VS Code's built-in
                # browser blocks via CSP - the canvas then renders blank. Point it
                # at our local copy so the graph works offline and in any viewer.
                page = re.sub(r'https://unpkg\.com/vis-network[^"\']*',
                              '/vendor/vis-network.min.js', page)
                self._send(page.encode(), "text/html; charset=utf-8")
            else:
                self._send(
                    b"<body style='background:#0b0e14;color:#e8ecf5;font-family:monospace;"
                    b"padding:40px'><h2>No graph yet for this project</h2>"
                    b"<p>Press GRAPH on the project card to build one, then reopen this.</p>"
                    b"<p>Projects with no parseable code (QML, config-only repos) "
                    b"cannot produce a useful graph.</p></body>",
                    "text/html; charset=utf-8")
        elif u.path.startswith("/vendor/"):
            f = os.path.join(BASE, "vendor", os.path.basename(u.path))
            if os.path.exists(f):
                self._send(read(f, 20_000_000).encode(),
                           "application/javascript; charset=utf-8")
            else:
                self._send(b"// not found", "application/javascript")
        elif u.path == "/report":
            name = (parse_qs(u.query).get("p") or [""])[0]
            proj = proj_by_name(name)
            f = os.path.join(proj, "graphify-out", "GRAPH_REPORT.md") if proj else ""
            self._send((read(f) if f and os.path.exists(f)
                        else "No graph report yet - press GRAPH first.").encode(),
                       "text/plain; charset=utf-8")
        elif u.path == "/office":
            # pixel-agents mints a new token each start; pull it from its log.
            url = "http://127.0.0.1:8790/"
            m = re.findall(r"http://127\.0\.0\.1:\d+/\?token=[0-9a-f-]+",
                           read(os.path.join(LOGDIR, "pixel-agents.log"), 60000))
            if m:
                url = m[-1]
            self.send_response(302)
            self.send_header("Location", url)
            self.end_headers()
        elif u.path in STATIC:
            self._send(read(os.path.join(WEB, u.path[1:])).encode(), STATIC[u.path])
        elif u.path == "/file":
            p = (parse_qs(u.query).get("p") or [""])[0]
            if allowed(p):
                self._send(read(os.path.realpath(p)).encode(), "text/plain; charset=utf-8")
            else:
                self._send(b"not allowed", "text/plain; charset=utf-8", 403)
        else:
            self._send(index_page().encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if not self._gate("POST"):
            return
        if urlparse(self.path).path == "/login":
            self._login()
            return
        if urlparse(self.path).path != "/action":
            self._send(b"not found", "text/plain", 404)
            return
        # A custom header cannot be sent cross-site without a CORS preflight,
        # which this server never answers, so a page on another site cannot
        # forge this request even before the token is compared.
        if not hmac.compare_digest(self.headers.get("X-AGX-Token", "").encode(),
                                   TOKEN.encode()):
            self._deny(403, "missing or wrong X-AGX-Token")
            return
        if not self._origin_ok():
            self._deny(403, "origin not allowed")
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._deny(415, "send application/json")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            self._deny(413, "body too large")
            return
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            res = do_action(body)
        except Exception as e:
            res = {"ok": False, "msg": f"error: {e}"}
        self._send(json.dumps(res).encode(), "application/json")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    # Threaded: an ASK or a commit message can take minutes, and the page's
    # refresh must keep working meanwhile.
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    try:
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), H)
    except OSError as e:
        if getattr(e, "errno", None) == 98:
            print(f"Dashboard already running -> http://localhost:{PORT}")
            print(f"Restart with:  pkill -f dashboard.py && python3 {__file__}")
            raise SystemExit(0)
        raise
    with srv as s:
        print(f"Agent Team dashboard -> http://localhost:{PORT}  (Ctrl+C to stop)")
        try:
            s.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
