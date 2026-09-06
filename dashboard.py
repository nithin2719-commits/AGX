#!/usr/bin/env python3
"""Live GUI for the agent team. Run:  python3 dashboard.py  ->  http://localhost:8765

Shows every project: what each agent is doing, the plan you gave them, the tasks,
and the full text of their planning meetings and work logs.
"""
import http.server, socketserver, json, os, sys, subprocess, re, html
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(BASE, "logs")
PORT = 8765
AGENTS = ("claude", "agy")
# GitHub accounts that belong to the user, so the project scanner can tell
# their own repos apart from cloned third-party tools.
MINE = {"nithin2719-commits", "nithin2729-commits", "xcaptain09", "mano-dev-01"}
SETTINGS = os.path.join(BASE, "settings.json")

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
    """Only serve files that belong to the agent team."""
    path = os.path.abspath(path)
    if path.startswith(os.path.abspath(LOGDIR)):
        return True
    return any(path.startswith(os.path.join(os.path.abspath(p), ".agent-team"))
               for p, _ in all_projects())


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Press+Start+2P&family=JetBrains+Mono:wght@400;500;700&display=swap');
:root{
  /* AGX operations console. Grounded in observatory / telemetry, not arcade:
     a genuinely blue void, one periwinkle accent for anything interactive,
     gold reserved strictly for live telemetry, and two muted agent hues.
     Colour is scarce so a lit pixel always means something. */
  /* Arcade pixel: deep navy-black with bright 8-bit inks. Green is the primary
     signal, coral and cyan are the two agents. Kept sparse so a lit pixel
     always reads as a live element, not decoration. */
  --bg:#0b0e18; --surface:#12172480; --surface2:#161c2c; --raise:#1d2438;
  --edge:#1f2740; --edge2:#33406a;
  --ink:#eef2fb; --dim:#9aa6c6; --faint:#5f6b8c;
  --accent:#3df5a0;                         /* arcade green: interactive hue */
  --green:#3df5a0; --green-d:#0c3a28;       /* alias kept for downstream rules */
  --live:#3df5a0;                           /* green: live telemetry */
  --claude:#ff7a6b;                          /* coral */
  --agy:#4fd6e8;                             /* cyan */
  --run:#3df5a0; --wait:#4fd6e8; --stall:#ffd23f; --idle:#5f6b8c; --pause:#2a3350;
  --danger:#ff5f7e; --violet:#b58cff;
  --r:6px;                                  /* one radius, applied by hierarchy */
  --sh:0 1px 0 rgba(255,255,255,.03), 0 12px 30px -16px rgba(0,0,0,.8);
  --glow:0 0 0 1px var(--accent), 0 0 18px -5px var(--accent);
  --px:'Press Start 2P',ui-monospace,monospace;   /* pixel wordmark only */
  --mo:'JetBrains Mono',ui-monospace,monospace;    /* the whole system */
}
*{box-sizing:border-box}
body{margin:0;padding:22px 26px 60px;
  /* halftone Ben-Day dots + two coloured light sources bleeding in from the
     corners, the way a Spider-Verse frame is lit magenta on one side, cyan on
     the other. The dot grid is fixed and low-contrast so text stays readable. */
  background:
    radial-gradient(1000px 520px at 84% -12%,rgba(255,206,31,.08),transparent 60%),
    radial-gradient(820px 460px at 4% 2%,rgba(110,166,216,.06),transparent 58%),
    var(--bg);
  color:var(--ink);font:12.5px/1.45 var(--mo);font-variant-numeric:tabular-nums;
  letter-spacing:.1px;min-height:100vh;position:relative}
body::before{content:'';position:fixed;inset:0;z-index:-1;pointer-events:none;
  opacity:.22;
  background-image:radial-gradient(rgba(255,255,255,.035) 1px,transparent 1.5px);
  background-size:7px 7px}
.px{background:var(--surface);border:1px solid var(--edge);border-radius:var(--r);
  box-shadow:var(--sh)}
svg{width:14px;height:14px;flex:none;stroke-width:1.75;fill:none;stroke:currentColor;
  stroke-linecap:round;stroke-linejoin:round}
/* header */
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:14px;padding:4px 2px 12px;
  margin-bottom:14px;border-bottom:1px solid var(--edge)}
h1{font:14px/1 var(--px);margin:0;color:var(--accent);letter-spacing:2px}
h1 i{font-style:normal;color:var(--ink);animation:bl 1.15s steps(2) infinite}
@keyframes bl{50%{opacity:0}}
/* Glitch: two offset copies flicker on the red and blue channels, like
   chromatic aberration on a CRT. Long cycle so it reads as an artefact,
   not a distraction, and it is disabled under prefers-reduced-motion. */
.glitch{position:relative;display:inline-block}
.glitch::before,.glitch::after{content:attr(data-t);position:absolute;left:0;top:0;
  width:100%;pointer-events:none;text-shadow:none}
.glitch::before{color:var(--claude);clip-path:inset(0 0 58% 0);
  animation:gl1 5s steps(2) infinite}
.glitch::after{color:var(--agy);clip-path:inset(54% 0 0 0);
  animation:gl2 5s steps(2) infinite}
@keyframes gl1{0%,88%,100%{opacity:0;transform:none}
  89%{opacity:.95;transform:translate(-3px,-1px)}
  93%{opacity:.8;transform:translate(2px,1px)}
  96%{opacity:.6;transform:translate(-2px,0)}}
@keyframes gl2{0%,88%,100%{opacity:0;transform:none}
  90%{opacity:.95;transform:translate(3px,1px)}
  94%{opacity:.8;transform:translate(-2px,-1px)}
  97%{opacity:.6;transform:translate(1px,0)}}
#sub{color:var(--faint);font-size:17px;margin-left:auto}

/* buttons */
button,.btn{font:500 12.5px/1 var(--mo);min-height:32px;padding:0 12px;
  display:inline-flex;align-items:center;justify-content:center;gap:7px;
  background:var(--raise);color:var(--ink);border:1px solid var(--edge2);
  border-radius:var(--r);cursor:pointer;text-decoration:none;letter-spacing:.2px;
  transition:background .14s ease,border-color .14s ease,box-shadow .14s ease}
button:hover,.btn:hover{background:var(--surface2);border-color:var(--accent)}
button:active,.btn:active{transform:translateY(1px)}
button:focus-visible,.btn:focus-visible,input:focus-visible,textarea:focus-visible,
select:focus-visible,[tabindex]:focus-visible{outline:2px solid var(--accent);
  outline-offset:2px}
button[disabled]{opacity:.35;cursor:not-allowed}
.b-claude{background:#3a2113;border-color:var(--claude);color:#ffd6bb}
.b-agy{background:#0e2a3d;border-color:var(--agy);color:#cbeaff}
.b-go{background:var(--green-d);border-color:var(--green);color:var(--green)}
.b-danger{background:#3a1414;border-color:var(--danger);color:#ffcccc}
.b-alt{background:#2a163d;border-color:var(--violet);color:#e9d3ff}
/* top bar + hud */
.topbar{display:flex;flex-wrap:wrap;gap:9px;margin-bottom:16px}
.hud{display:grid;gap:9px;margin-bottom:22px;
  grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}
.hud>div{padding:12px 16px;border-left:3px solid var(--edge2)}
.hud>div.hot{border-left-color:var(--green)}
.hud b{display:block;font:700 18px/1.05 var(--mo)}
.hud span{font-size:15px;color:var(--faint);letter-spacing:.4px}
/* live activity feed */
.feedbox{margin-bottom:22px;overflow:hidden}
.feedhead{font:600 11.5px/1.4 var(--mo);letter-spacing:.4px;color:var(--faint);
  padding:11px 16px;border-bottom:1px solid var(--edge);background:var(--surface2)}

.fline{padding:7px 14px;
  border-bottom:1px solid var(--edge);font-size:16px;
  animation:slidein .25s ease-out}
.fline:last-child{border-bottom:0}
@keyframes slidein{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}
.fst{font:600 11px/1.3 var(--mo);padding:4px 8px;border:1px solid var(--edge2);
  color:var(--faint);min-width:74px;text-align:center}
.fline.k0{background:linear-gradient(90deg,rgba(255,179,71,.09),transparent 60%)}
.fline.k0 .fst{color:var(--run);border-color:var(--run)}
.fline.k1 .fst{color:var(--wait);border-color:var(--wait)}
.fline.k2 .fst{color:var(--faint)}
.fline.k2{opacity:.72}
.fproj{font:600 11.5px/1.3 var(--mo);color:var(--ink);min-width:88px}
.ftask{color:var(--dim);flex:1;min-width:180px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.fline.idle2{color:var(--faint);justify-content:center;padding:20px}
/* working pulse + typing dots */
.pulse{width:9px;height:9px;flex:none;background:var(--run);border-radius:50%;
  box-shadow:0 0 0 0 rgba(255,179,71,.6);animation:ping 1.6s ease-out infinite}
.pulse.off{background:var(--edge2);animation:none}
@keyframes ping{0%{box-shadow:0 0 0 0 rgba(255,179,71,.55)}
  70%{box-shadow:0 0 0 9px rgba(255,179,71,0)}100%{box-shadow:0 0 0 0 rgba(255,179,71,0)}}
.dots{display:flex;gap:3px;flex:none}
.dots i{width:4px;height:4px;background:var(--run);animation:bob 1.1s infinite}
.dots i:nth-child(2){animation-delay:.15s} .dots i:nth-child(3){animation-delay:.3s}
@keyframes bob{0%,60%,100%{opacity:.25;transform:translateY(0)}
  30%{opacity:1;transform:translateY(-3px)}}
/* group heading */
.group{display:flex;align-items:center;gap:12px;margin:22px 0 11px;
  font:600 12px/1.4 var(--mo);letter-spacing:.4px}

.group hr{flex:1;border:0;border-top:1px dashed var(--edge2)}
.g-running{color:var(--run)} .g-stalled{color:var(--stall)}
.g-waiting{color:var(--wait)} .g-idle{color:var(--idle)} .g-paused{color:var(--pause)}
/* THE FIX: auto-fit makes a single card stretch the full width instead of
   leaving dead space; wider min so cards use a 1440p screen properly. */
.grid{display:grid;gap:14px;grid-template-columns:minmax(0,1180px);justify-content:center}
.card{padding:0;overflow:hidden}
.card.running{border-color:var(--run);box-shadow:var(--sh),var(--glow)}
.card.stalled{border-color:var(--stall)}
.card.paused{opacity:.55}
/* card head bar */
.chead{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:9px 14px;
  background:var(--surface2);border-bottom:1px solid var(--edge2)}
.pname{font:700 15px/1.3 var(--mo);margin:0;color:var(--ink)}

.badge{display:inline-flex;align-items:center;gap:7px;padding:5px 10px;margin-left:auto;
  font:600 11px/1.3 var(--mo);border:1px solid}
.badge.running{color:var(--run);border-color:var(--run);background:var(--green-d)}
.badge.stalled{color:var(--stall);border-color:var(--stall);background:#332810}
.badge.waiting{color:var(--wait);border-color:var(--wait);background:#0c2433}
.badge.idle{color:var(--idle);border-color:var(--edge2);background:var(--surface2)}
.badge.paused{color:var(--pause);border-color:var(--edge2);background:var(--surface2)}
.badge .dot{width:8px;height:8px;background:currentColor}
.badge.running .dot{animation:bl .9s steps(2) infinite}
.ppath{padding:7px 16px;font-size:15px;color:var(--faint);border-bottom:1px dashed var(--edge);
  word-break:break-all}

/* github bar */
.gh{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:9px 16px;
  background:var(--bg);border-bottom:1px dashed var(--edge);font-size:16px}
.gh.off{color:var(--faint)}
.ghtag{font:600 11px/1.3 var(--mo);padding:4px 8px;border:1px solid var(--edge2);
  color:var(--faint)}
.ghtag.on{color:var(--green);border-color:var(--green);background:var(--green-d)}
.ghrepo{color:var(--agy);text-decoration:none;font-size:17px}
.ghrepo:hover{text-shadow:0 0 8px rgba(90,200,255,.6);text-decoration:underline}
.ghbranch{color:var(--dim)}
.ghbranch::before{content:'';}
.gh b{font-weight:400;font-size:16px}
.gh b.ahead{color:var(--stall)} .gh b.behind{color:var(--violet)}
.gh b.dirty{color:var(--danger)} .gh b.sync{color:var(--green)}
.ghspacer{flex:1}
.ghbtn{min-height:34px;padding:0 13px;font-size:8px}
/* settings bar: model pickers + what-am-I-doing note */
.setbar{display:grid;gap:14px;padding:14px 16px;margin-bottom:18px;
  grid-template-columns:minmax(240px,auto) 1fr}
@media(max-width:760px){.setbar{grid-template-columns:1fr}}
#models{display:flex;flex-direction:column;gap:8px}
.mrow{display:flex;align-items:center;gap:10px}
.mrow label{margin:0;width:56px;flex:none}
select{flex:1;background:var(--bg);color:var(--ink);border:1px solid var(--edge2);
  padding:9px 10px;font-family:var(--mo);font-size:17px;min-height:40px;cursor:pointer}
select:focus{border-color:var(--green);outline:none}
.notewrap{display:flex;flex-direction:column;gap:7px}
#note{min-height:52px;resize:vertical}
/* add-project scanner */
.found{display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:11px 12px;
  border:1px solid var(--edge);margin-bottom:7px;background:var(--bg)}
.fname{font:600 12px/1.3 var(--mo);color:var(--ink)}
.fmeta{font-size:16px;color:var(--dim)}
.fpath{font-size:15px;color:var(--faint);flex:1;min-width:160px;word-break:break-all}
.found button{min-height:36px;padding:0 14px}
.found.third{opacity:.5}
.scanclose{display:flex;justify-content:flex-end;margin-bottom:10px}
/* body: two columns on wide cards so the space is actually used */
.cbody{display:grid;gap:0;grid-template-columns:1fr}
@media(min-width:900px){.cbody{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
  .cbody>.colL{border-right:1px dashed var(--edge)}}
.colL,.colR{padding:12px 14px;min-width:0}
/* agents */
.agent{display:flex;align-items:center;gap:10px;padding:9px 11px;margin-bottom:7px;
  background:var(--bg);border:1px solid var(--edge)}
.agent.on{border-color:var(--run);box-shadow:inset 3px 0 0 var(--run)}
.who{font:600 11px/1.3 var(--mo);width:54px;flex:none}
.who.claude{color:var(--claude)} .who.agy{color:var(--agy)}
.what{font-size:13px;color:var(--dim);flex:1;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
/* progress */
.bar{display:flex;gap:2px;height:12px;margin:12px 0 6px}
.bar i{flex:1;background:var(--edge);box-shadow:inset 0 0 0 1px #000}
.bar i.d{background:var(--run)} .bar i.w{background:var(--stall)}
.counts{font-size:13px;color:var(--faint);margin-bottom:11px}
.counts b{color:var(--ink);font-weight:700}
.counts i{display:inline-block;width:16px}
.mini{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin:12px 0}
.mini div{background:var(--bg);border:1px solid var(--edge);padding:9px 2px;text-align:center}
.mini b{display:block;font:13px/1.3 var(--mo)}
.mini span{font-size:14px;color:var(--faint)}
.sec{font:600 11px/1.4 var(--mo);color:var(--faint);letter-spacing:.4px;margin:16px 0 8px;
  padding-top:12px;border-top:1px dashed var(--edge)}

.row{display:flex;flex-wrap:wrap;gap:7px;margin:11px 0}
code{display:block;font-family:var(--mo);font-size:16px;color:var(--dim);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:3px 0}
code::before{content:'$ ';color:var(--faint)}
a.ln{display:flex;align-items:center;gap:8px;color:var(--agy);font-size:16px;
  text-decoration:none;cursor:pointer;min-height:27px}
a.ln:hover{color:#a8dfff;text-shadow:0 0 8px rgba(90,200,255,.5)}
input[type=text],textarea{width:100%;background:var(--bg);color:var(--ink);
  border:1px solid var(--edge2);padding:11px;font-family:var(--mo);font-size:18px;
  min-height:44px}
input[type=text]:focus,textarea:focus{border-color:var(--green)}
textarea{min-height:150px;resize:vertical}
label{display:block;font:600 11px/1.4 var(--mo);color:var(--faint);margin-bottom:8px}
.plan{background:var(--bg);border-left:3px solid var(--agy);padding:10px 12px;
  font-size:13px;line-height:1.5;white-space:pre-wrap;max-height:220px;overflow:auto;
  color:var(--dim)}
.plan.noplan{border-left-color:var(--stall);color:var(--faint)}
/* chat */
.chatlog{max-height:300px;overflow:auto;background:var(--bg);border:1px solid var(--edge);
  padding:11px;margin-bottom:10px;min-height:54px}
.chatlog:empty::after{content:'no questions yet';color:var(--faint);font-size:16px}
.msg{margin-bottom:12px;font-size:17px;line-height:1.5}
.msg b{display:block;font:600 11px/1.4 var(--mo);margin-bottom:5px}
.msg.you b{color:var(--violet)} .msg.claude b{color:var(--claude)} .msg.agy b{color:var(--agy)}
.msg div{white-space:pre-wrap;color:var(--dim);border-left:2px solid var(--edge);padding-left:10px}
.msg.you div{border-left-color:var(--violet);color:var(--ink)}
.hint{font-size:15px;color:var(--faint);align-self:center}
#toast{position:fixed;left:50%;transform:translateX(-50%);bottom:22px;z-index:60;
  background:var(--green-d);border:1px solid var(--green);color:var(--green);
  padding:14px 20px;font-size:18px;display:none;max-width:90vw;box-shadow:var(--glow)}
#toast.err{background:#3a1414;border-color:var(--danger);color:#ffcccc;box-shadow:none}
#ov{position:fixed;inset:0;background:rgba(2,5,8,.93);display:none;z-index:80;padding:26px 14px}
#ov.on{display:block}
#ovb{max-width:1100px;margin:0 auto;max-height:100%;display:flex;flex-direction:column;
  background:var(--surface);border:1px solid var(--green);box-shadow:var(--glow)}
#ovh{display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:13px 16px;border-bottom:1px solid var(--edge2);font:600 11.5px/1.4 var(--mo);
  color:var(--green)}
#ovc{padding:18px;overflow:auto;white-space:pre-wrap;font-size:15px;color:var(--dim)}
/* project detail window - the big readable view */
#ovb{max-width:1180px;width:96vw}
.projview{white-space:normal!important;font-size:14px}
.projview .pv-path{color:var(--faint);font-size:13px;margin-bottom:10px;word-break:break-all}
.projview .pv-agents{margin:12px 0}
.projview .pv-actions{display:flex;flex-wrap:wrap;gap:7px;margin:14px 0 6px}
.projview .pv-cols{display:grid;gap:22px;grid-template-columns:1fr 1fr;margin-top:8px}
@media(max-width:820px){.projview .pv-cols{grid-template-columns:1fr}}
.projview h4{font:700 12px/1.3 var(--mo);letter-spacing:1px;text-transform:uppercase;
  color:var(--accent);margin:18px 0 9px;padding-bottom:6px;border-bottom:1px solid var(--edge)}
.projview .pv-plan{white-space:pre-wrap;background:var(--bg);border:1px solid var(--edge);
  border-radius:var(--r);padding:13px 15px;font-size:14px;line-height:1.55;color:var(--dim)}
.projview .pv-tasks{display:flex;flex-direction:column;gap:5px}
.projview .titem{display:flex;gap:9px;align-items:flex-start;padding:7px 10px;
  background:var(--bg);border:1px solid var(--edge);border-radius:var(--r);font-size:13.5px}
.projview .titem .tm{flex:none;width:14px;text-align:center}
.projview .t-done{color:var(--faint)} .projview .t-done .tm{color:var(--run)}
.projview .t-doing{border-color:var(--stall)} .projview .t-doing .tm{color:var(--stall)}
.projview .t-todo .tm{color:var(--faint)}
.projview .pv-commits code{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.projview .muted{color:var(--faint);font-size:13px;padding:4px 0}
.empty{padding:46px;text-align:center;color:var(--dim)}
@media (max-width:600px){#sub{margin-left:0;width:100%}.mini{grid-template-columns:repeat(2,1fr)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

JS = r"""
const I={
 play:'<svg viewBox="0 0 16 16"><path d="M4 2l9 6-9 6z"/></svg>',
 chat:'<svg viewBox="0 0 16 16"><path d="M2 2h12v9H8l-4 3v-3H2z"/></svg>',
 graph:'<svg viewBox="0 0 16 16"><path d="M2 14V2M2 14h12M5 11l3-4 3 2 3-5"/></svg>',
 plan:'<svg viewBox="0 0 16 16"><path d="M3 2h7l3 3v9H3zM10 2v3h3"/></svg>',
 stop:'<svg viewBox="0 0 16 16"><path d="M3 3h10v10H3z"/></svg>',
 doc:'<svg viewBox="0 0 16 16"><path d="M4 2h6l2 2v10H4zM6 7h4M6 10h4"/></svg>',
 plus:'<svg viewBox="0 0 16 16"><path d="M8 3v10M3 8h10"/></svg>',
};
const NAME={claude:'CLAUDE',agy:'AGY'};
const GROUP={running:'RUNNING NOW',stalled:'NEEDS ATTENTION',
 waiting:'READY - WAITING FOR AN AGENT',idle:'IDLE - NO WORK LEFT',paused:'PAUSED'};
let BUSY=false, OPEN={};
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML}
function toast(m,bad){const t=document.getElementById('toast');t.textContent=m;
 t.className=bad?'err':'';t.style.display='block';
 clearTimeout(window._t);window._t=setTimeout(()=>t.style.display='none',6500)}
async function act(a,p,x){
 if(BUSY){toast('one action at a time',true);return}
 BUSY=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{const r=await fetch('/action',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify(Object.assign({action:a,project:p},x||{}))});
  const j=await r.json();toast(j.msg||(j.ok?'done':'failed'),!j.ok);
 }catch(e){toast('request failed: '+e,true)}
 BUSY=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);tick();
}
const CHAT={};
function renderChat(n){
 const el=document.getElementById('log-'+n);if(!el)return;
 el.innerHTML=(CHAT[n]||[]).map(m=>`<div class="msg ${m.who}">
   <b>${m.who==='you'?'YOU':m.who.toUpperCase()}</b><div>${esc(m.text)}</div></div>`).join('');
 el.scrollTop=el.scrollHeight;
}
async function ask(n,agent){
 const t=document.getElementById('q-'+n);if(!t||!t.value.trim())return;
 const q=t.value.trim();t.value='';
 CHAT[n]=CHAT[n]||[];
 CHAT[n].push({who:'you',text:q});
 CHAT[n].push({who:agent,text:'thinking…'});
 renderChat(n);
 try{
  const r=await fetch('/action',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'chat',project:n,agent:agent,text:q})});
  const j=await r.json();
  CHAT[n].pop();
  CHAT[n].push({who:agent,text:j.reply||j.msg||'no reply'});
 }catch(e){CHAT[n].pop();CHAT[n].push({who:agent,text:'failed: '+e})}
 renderChat(n);
}
async function explain(n){
 CHAT[n]=CHAT[n]||[];
 CHAT[n].push({who:'you',text:'What happened in the logs?'});
 CHAT[n].push({who:'claude',text:'reading the logs…'});
 renderChat(n);
 try{
  const r=await fetch('/action',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'explain',project:n})});
  const j=await r.json();CHAT[n].pop();
  CHAT[n].push({who:'claude',text:j.reply||j.msg||'no reply'});
 }catch(e){CHAT[n].pop();CHAT[n].push({who:'claude',text:'failed: '+e})}
 renderChat(n);
}
function savePlan(n){const t=document.getElementById('plan-'+n);if(t)act('save_plan',n,{text:t.value})}
function addTask(n){const i=document.getElementById('task-'+n);
 if(i&&i.value.trim()){act('add_task',n,{text:i.value});i.value=''}}
function toggle(n,k){OPEN[n+k]=!OPEN[n+k];const el=document.getElementById(k+'-'+n);
 if(el)el.hidden=!OPEN[n+k]}
async function open_(f,t){const r=await fetch('/file?p='+encodeURIComponent(f));
 document.getElementById('ovt').textContent=t;
 document.getElementById('ovc').className='';
 document.getElementById('ovc').textContent=await r.text();
 document.getElementById('ov').classList.add('on');document.getElementById('ovx').focus()}
function close_(){document.getElementById('ov').classList.remove('on')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')close_()});

function agentRow(a,s){
 const on=s&&s.running&&s.state==='working';
 const st=s?s.state:'never run';
 return `<div class="agent${on?' on':''}">
  ${on?'<span class="pulse" aria-hidden="true"></span>':'<span class="pulse off" aria-hidden="true"></span>'}
  <span class="who ${a}">${NAME[a]}</span>
  <span class="what" title="${esc(s?(s.task||st):st)}">${esc(st.toUpperCase())}${
   s&&s.task?'  '+esc(s.task):''}</span>
  ${on?'<span class="dots"><i></i><i></i><i></i></span>':''}</div>`;
}
// A live strip of what the whole team is doing right now, so you never have to
// hunt through cards to answer "is anything happening?".
function feed(d){
 const rows=[];
 d.forEach(p=>['claude','agy'].forEach(a=>{
  const s=p.agents[a]; if(!s)return;
  if(s.state==='working'&&s.running)
   rows.push({k:0,a,p:p.name,t:s.task||'working',st:'WORKING'});
 }));
 d.forEach(p=>{ if(p.status==='waiting'&&p.next_task)
   rows.push({k:1,a:'',p:p.name,t:p.next_task,st:'NEXT UP'}); });
 d.forEach(p=>(p.progress||[]).slice(-1).forEach(l=>{
   const m=l.match(/\|\s*(claude|agy)\s*\|\s*(.*)$/);
   if(m)rows.push({k:2,a:m[1],p:p.name,t:m[2],st:'DONE'});
 }));
 rows.sort((x,y)=>x.k-y.k);
 const el=document.getElementById('feed');
 if(!rows.length){el.innerHTML='<div class="fline idle2">nothing running — press AUTO CYCLE to put the team to work</div>';return}
 el.innerHTML=rows.slice(0,8).map(r=>`<div class="fline k${r.k}">
   <span class="fst">${r.st}</span>
   ${r.a?`<span class="who ${r.a}">${NAME[r.a]}</span>`:'<span class="who"></span>'}
   <span class="fproj">${esc(r.p)}</span>
   <span class="ftask">${esc(r.t)}</span></div>`).join('');
}
function bar(t){
 const tot=Math.max(t.done+t.doing+t.todo,1),N=Math.min(tot,26);let o='';
 for(let i=0;i<N;i++){const p=(i+1)/N*tot;
  o+=`<i class="${p<=t.done?'d':p<=t.done+t.doing?'w':''}"></i>`}
 return `<div class="bar" role="img" aria-label="${t.done} done, ${t.doing} in progress, ${t.todo} left">${o}</div>`;
}
function toggleScan(){
 const box=document.getElementById('scanbox');
 if(!box.hidden){box.hidden=true;return}   // second press closes it
 scanRepos();
}
async function scanRepos(){
 const box=document.getElementById('scanbox');
 box.hidden=false;
 box.innerHTML='<div class="hint">scanning ~/Projects and ~ …</div>';
 try{
  const r=await fetch('/action',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action:'scan'})});
  const j=await r.json();
  const f=j.found||[];
  if(!f.length){box.innerHTML='<div class="hint">no new git repos found — everything is already added</div>';return}
  const mine=f.filter(x=>x.yours), other=f.filter(x=>!x.yours);
  const row=x=>`<div class="found${x.yours?'':' third'}">
     <span class="fname">${esc(x.name)}</span>
     <span class="fmeta">⑂ ${esc(x.branch)} · ${esc(x.commits)} commits${
       x.owner?' · '+esc(x.owner):' · local only'}</span>
     <span class="fpath">${esc(x.path)}</span>
     <button class="b-go" onclick="addProject('${esc(x.path)}')">ADD</button>
    </div>`;
  box.innerHTML=
   `<div class="scanclose"><button onclick="document.getElementById('scanbox').hidden=true"
      aria-label="Close the project scanner">CLOSE</button></div>`+
   (mine.length?`<div class="sec" style="margin-top:0">YOUR REPOS (${mine.length})</div>`+
     mine.map(row).join(''):'')+
   (other.length?`<div class="sec">CLONED FROM OTHERS (${other.length}) — probably not yours</div>`+
     other.map(row).join(''):'');
 }catch(e){box.innerHTML='<div class="hint">scan failed: '+esc(String(e))+'</div>'}
}
async function addProject(path){
 await act('add_project',null,{path:path});
 scanRepos();
}
async function addManual(){
 const i=document.getElementById('newpath');
 if(i&&i.value.trim())await addProject(i.value.trim());
}
function ghBar(p){
 const g=p.github||{};
 if(!g.connected){
  return `<div class="gh off"><span class="ghtag">NO REMOTE</span>
   <span>not connected to GitHub &mdash; work stays on this machine only</span></div>`;
 }
 const repo=g.web.replace(/^https?:\/\/(www\.)?github\.com\//,'');
 const bits=[];
 if(g.ahead) bits.push(`<b class="ahead">&uarr; ${g.ahead} to push</b>`);
 if(g.behind) bits.push(`<b class="behind">&darr; ${g.behind} behind</b>`);
 if(g.dirty) bits.push(`<b class="dirty">${g.dirty} uncommitted</b>`);
 if(!bits.length) bits.push('<b class="sync">in sync</b>');
 return `<div class="gh"><span class="ghtag on">GITHUB</span>
  <a href="${esc(g.web)}" target="_blank" rel="noopener" class="ghrepo">${esc(repo)}</a>
  <span class="ghbranch">${esc(g.branch)}</span>
  ${bits.join('')}
  <span class="ghspacer"></span>
  ${g.dirty?`<button class="ghbtn" onclick="commitNow('${esc(p.name)}')"
     aria-label="Commit ${g.dirty} changed files in ${esc(p.name)}">COMMIT</button>`:''}
  ${g.ahead?`<button class="b-go ghbtn" onclick="push('${esc(p.name)}')"
     aria-label="Push ${g.ahead} commits to GitHub">PUSH</button>`:''}
 </div>`;
}
async function push(n){
 if(!confirm('Push this branch to GitHub? This publishes the commits.'))return;
 act('push',n);
}
function card(p){
 const n=esc(p.name),t=p.tasks,w=p.work||{},st=p.status;
 const links=(arr,ic)=>arr.map(x=>`<a class="ln" role="button" tabindex="0"
   onclick="open_('${encodeURIComponent(x.file)}','${esc(x.label)}')"
   onkeydown="if(event.key==='Enter')open_('${encodeURIComponent(x.file)}','${esc(x.label)}')"
   >${ic}<span>${esc(x.label)}</span></a>`).join('');
 const detailOpen=OPEN[p.name+'det'];
 return `<section class="px card ${st}">
  <div class="chead">
   <h2 class="pname" role="button" tabindex="0" onclick="openProject('${n}')"
     onkeydown="if(event.key==='Enter')openProject('${n}')"
     title="Open ${n} in full">${n}</h2>
   <div class="badge ${st}"><span class="dot" aria-hidden="true"></span>${esc(p.status_text)}</div>
  </div>
  <div class="ppath">${esc(p.path)}</div>
  ${ghBar(p)}
  <div class="cbody"><div class="colL">
  ${agentRow('claude',p.agents.claude)}${agentRow('agy',p.agents.agy)}
  ${bar(t)}
  <div class="counts"><b>${t.done}</b> done<i></i><b>${t.doing}</b> doing<i></i><b>${t.todo}</b> left</div>
  <div class="mini">
   <div><b style="color:var(--claude)">${w.claude||0}</b><span>claude</span></div>
   <div><b style="color:var(--agy)">${w.agy||0}</b><span>agy</span></div>
   <div><b style="color:${w.failed?'var(--danger)':'var(--faint)'}">${w.failed||0}</b><span>no commit</span></div>
   <div><b style="color:${p.graph.up?'var(--run)':'var(--danger)'}">${p.graph.facts}</b><span>facts</span></div>
  </div>
  ${t.todo?`<div class="sec">NEXT TASK</div><code>${esc(p.next_task)}</code>`:''}
  </div><div class="colR">
  <div class="sec" style="margin-top:0;padding-top:0;border-top:0">THE PLAN THEY FOLLOW</div>
  ${p.plan?`<div class="plan">${esc(p.plan.slice(0,900))}${p.plan.length>900?'\n…':''}</div>`
    :`<div class="plan noplan">No plan written yet — the agents are deciding for
       themselves. Press PLAN and tell them what you want.</div>`}
  <div class="row">
   <button class="b-claude" onclick="act('run_claude','${n}')" aria-label="Run Claude on ${n}">${I.play}CLAUDE</button>
   <button class="b-agy" onclick="act('run_agy','${n}')" aria-label="Run agy on ${n}">${I.play}AGY</button>
   <button class="b-alt" onclick="act('meet','${n}')" aria-label="Planning meeting for ${n}">${I.chat}MEETING</button>
   <button class="b-danger" onclick="act('stop','${n}')" aria-label="Stop agents on ${n}">${I.stop}STOP</button>
  </div>
  <div class="row">
   <input type="text" id="task-${n}" placeholder="add a task…" aria-label="Add a task to ${n}"
     style="flex:1;min-width:150px" onkeydown="if(event.key==='Enter')addTask('${n}')">
   <button class="b-go" onclick="addTask('${n}')" aria-label="Add task">${I.plus}ADD</button>
  </div>
  <div class="row">
   <button class="b-go" onclick="act('run_both','${n}')" aria-label="Run both agents on ${n}">${I.play}BOTH</button>
   <button onclick="toggle('${n}','chat')" aria-label="Ask an agent about ${n}">${I.chat}ASK</button>
   <button onclick="toggle('${n}','det')" aria-expanded="${!!detailOpen}">
     ${detailOpen?'HIDE DETAILS':'DETAILS'}</button>
   <button onclick="toggle('${n}','pw')" aria-label="Edit plan for ${n}">${I.plan}PLAN</button>
   <button onclick="act('graphify','${n}')" aria-label="Rebuild code graph for ${n}">${I.graph}BUILD GRAPH</button>
   ${p.codegraph.built?`<a class="btn b-alt" href="/graph?p=${encodeURIComponent(p.name)}"
      target="_blank" rel="noopener">${I.graph}VIEW GRAPH (${p.codegraph.nodes})</a>`:''}
  </div>
  <div id="chat-${n}" ${OPEN[p.name+'chat']?'':'hidden'}>
   <label for="q-${n}">ASK THE AGENTS ABOUT THIS PROJECT</label>
   <div id="log-${n}" class="chatlog" aria-live="polite"></div>
   <textarea id="q-${n}" rows="2" style="min-height:60px"
     placeholder="what did you change? why is agy failing? what is left?"
     onkeydown="if(event.key==='Enter'&&(event.ctrlKey||event.metaKey))ask('${n}','claude')"></textarea>
   <div class="row">
    <button class="b-claude" onclick="ask('${n}','claude')">ASK CLAUDE</button>
    <button class="b-agy" onclick="ask('${n}','agy')">ASK AGY</button>
    <button class="b-alt" onclick="explain('${n}')">WHAT HAPPENED?</button>
    <span class="hint">ctrl+enter asks claude, replies take 10-60s</span>
   </div>
  </div>
  <div id="pw-${n}" ${OPEN[p.name+'pw']?'':'hidden'}>
   <label for="plan-${n}">PLAN.md &mdash; agents obey this</label>
   <textarea id="plan-${n}">${esc(p.plan_raw||'')}</textarea>
   <div class="row"><button class="b-go" onclick="savePlan('${n}')">SAVE PLAN</button></div>
  </div>
  <div id="det-${n}" ${detailOpen?'':'hidden'}>
   ${p.talks.length?`<div class="sec">PLANNING MEETINGS</div>${links(p.talks,I.chat)}`:''}
   ${p.logs.length?`<div class="sec">WORK LOGS</div>${links(p.logs,I.doc)}`:''}
   ${p.commits.length?`<div class="sec">RECENT COMMITS</div>${
     p.commits.slice(0,6).map(c=>`<code>${esc(c)}</code>`).join('')}`:''}
  </div>
  </div></div>
 </section>`;
}
let MODELS={},SET={},DATA=[];
// Full-screen detail for one project - the cards are a summary; this is the
// place to actually read the plan, tasks, meetings, logs and commits.
function openProject(name){
 const p=DATA.find(x=>x.name===name); if(!p)return;
 const t=p.tasks, g=p.github||{};
 const links=(arr,ic)=>arr.length?arr.map(x=>`<a class="ln" role="button" tabindex="0"
   onclick="open_('${encodeURIComponent(x.file)}','${esc(x.label)}')">${ic}<span>${esc(x.label)}</span></a>`).join(''):'<div class="muted">none yet</div>';
 const tasksBlock=(p.all_tasks||[]).map(x=>{
   const cls=x.state==='x'?'t-done':x.state==='~'?'t-doing':'t-todo';
   const mark=x.state==='x'?'✓':x.state==='~'?'▶':'○';
   return `<div class="titem ${cls}"><span class="tm">${mark}</span><span>${esc(x.text)}</span></div>`;
 }).join('')||'<div class="muted">no tasks — run a meeting</div>';
 document.getElementById('ovt').textContent=p.name;
 document.getElementById('ovc').className='projview';
 document.getElementById('ovc').innerHTML=`
  <div class="pv-path">${esc(p.path)}</div>
  ${ghBar(p)}
  <div class="pv-agents">${agentRow('claude',p.agents.claude)}${agentRow('agy',p.agents.agy)}</div>
  <div class="pv-actions">
   <button class="b-claude" onclick="act('run_claude','${esc(p.name)}')">${I.play}Claude</button>
   <button class="b-agy" onclick="act('run_agy','${esc(p.name)}')">${I.play}agy</button>
   <button class="b-go" onclick="act('run_both','${esc(p.name)}')">${I.play}Both</button>
   <button class="b-alt" onclick="act('meet','${esc(p.name)}')">${I.chat}Meeting</button>
   <button class="b-danger" onclick="act('stop','${esc(p.name)}')">${I.stop}Stop</button>
   ${g.dirty?`<button onclick="commitNow('${esc(p.name)}')">Commit ${g.dirty}</button>`:''}
   ${g.ahead?`<button class="b-go" onclick="push('${esc(p.name)}')">Push ${g.ahead}</button>`:''}
   ${p.codegraph&&p.codegraph.built?`<a class="btn" href="/graph?p=${encodeURIComponent(p.name)}" target="_blank" rel="noopener">${I.graph}View graph (${p.codegraph.nodes})</a>`:''}
  </div>
  <div class="pv-cols">
   <div>
    <h4>The plan</h4>
    <div class="pv-plan">${p.plan?esc(p.plan):'<span class="muted">No plan yet. Press Plan on the card to write one.</span>'}</div>
    <h4>Tasks (${t.done} done, ${t.doing} doing, ${t.todo} left)</h4>
    <div class="pv-tasks">${tasksBlock}</div>
   </div>
   <div>
    <h4>Recent commits</h4>
    <div class="pv-commits">${(p.commits||[]).slice(0,10).map(c=>`<code>${esc(c)}</code>`).join('')||'<div class="muted">none</div>'}</div>
    <h4>Planning meetings</h4>${links(p.talks,I.chat)}
    <h4>Work logs</h4>${links(p.logs,I.doc)}
   </div>
  </div>`;
 document.getElementById('ov').classList.add('on');
 document.getElementById('ovx').focus();
}
function modelBar(){
 const pick=(a)=>{
  const cur=SET['model_'+a]||'';
  const opts=(MODELS[a]||[]).map(m=>
    `<option value="${esc(m.id)}"${m.id===cur?' selected':''}>${esc(m.label)} — ${esc(m.note)}</option>`).join('');
  return `<div class="mrow"><label for="m-${a}" class="who ${a}">${NAME[a]}</label>
   <select id="m-${a}" onchange="setModel('${a}',this.value)"
     aria-label="Model for ${NAME[a]}">
    <option value=""${cur?'':' selected'}>default</option>${opts}</select></div>`;
 };
 document.getElementById('models').innerHTML=pick('claude')+pick('agy');
 const n=document.getElementById('note');
 if(n&&document.activeElement!==n)n.value=SET.session_note||'';
}
function setModel(a,v){act('set_model',null,{agent:a,model:v})}
let noteT;
function noteChanged(){clearTimeout(noteT);
 noteT=setTimeout(()=>{const n=document.getElementById('note');
  if(n)fetch('/action',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({action:'set_note',text:n.value})})},900)}
function commitNow(n){
 const m=prompt('Commit message (leave blank and one will be written from the diff):','');
 if(m===null)return;
 act('commit',n,{text:m});
}
async function tick(){
 let raw;try{raw=await (await fetch('/api')).json()}catch(e){return}
 const d=raw.projects||[];DATA=d;
 MODELS=raw.models||{};SET=raw.settings||{};
 modelBar();
 const el=document.getElementById('grid');
 if(!d.length){el.innerHTML=`<div class="px empty">No projects yet.<br><br>
   <code>bash ~/agent-team/add-project.sh /path/to/project</code></div>`;
  document.getElementById('sub').textContent='0 projects';return}
 let html='';
 ['running','stalled','waiting','idle','paused'].forEach(g=>{
  const items=d.filter(p=>p.status===g);
  if(!items.length)return;
  html+=`<div class="group g-${g}"><span>${GROUP[g]} (${items.length})</span><hr></div>
   <div class="grid">${items.map(card).join('')}</div>`;
 });
 el.innerHTML=html;
 feed(d);
 const run=d.filter(p=>p.status==='running').length;
 const done=d.reduce((a,p)=>a+p.tasks.done,0);
 const left=d.reduce((a,p)=>a+p.tasks.todo,0);
 const fail=d.reduce((a,p)=>a+((p.work||{}).failed||0),0);
 document.getElementById('hud').innerHTML=`
  <div class="px"><b style="color:${run?'var(--run)':'var(--faint)'}">${run}</b><span>RUNNING</span></div>
  <div class="px"><b>${d.length}</b><span>PROJECTS</span></div>
  <div class="px"><b style="color:var(--run)">${done}</b><span>DONE</span></div>
  <div class="px"><b>${left}</b><span>LEFT</span></div>
  <div class="px"><b style="color:${fail?'var(--danger)':'var(--faint)'}">${fail}</b><span>NO COMMIT</span></div>`;
 document.getElementById('sub').textContent='updated '+new Date().toLocaleTimeString();
}
tick();setInterval(tick,4000);
"""

PAGE = ("<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>AGX</title>"
        f"<style>{CSS}</style>"
        "<header class=px><h1><span class=glitch data-t=\"AGX\">AGX</span> <i>_</i></h1>"
        "<div id=sub>loading…</div></header>"
        "<div class=row style='margin-bottom:18px'>"
        "<button class='b-go' onclick=\"act('cycle')\">AUTO CYCLE &mdash; DECIDE + WORK</button>"
        "<button onclick=\"act('team')\">RUN TEAM</button>"
        "<a class='btn b-alt' href='/office' target=_blank rel=noopener>PIXEL OFFICE</a>"
        "<button class='b-alt' onclick=\"toggleScan()\">+ ADD PROJECT</button>"
        "<button class='b-danger' onclick=\"if(confirm('Stop every agent and disable "
        "the timers?'))act('stop_all')\">STOP EVERYTHING</button></div>"
        "<div class='px' id=scanbox hidden style='padding:14px 16px;margin-bottom:18px'></div>"
        "<div class=row style='margin-bottom:18px'>"
        "<input type=text id=newpath placeholder='or type a project path…' "
        "aria-label='Project path to add' style='flex:1;min-width:220px'>"
        "<button onclick='addManual()'>ADD PATH</button></div>"
        "<div class='px setbar'>"
        "<div><label>MODEL PER AGENT</label><div id=models></div></div>"
        "<div class=notewrap><label for=note>WHAT YOU ARE WORKING ON "
        "(kept between sessions)</label>"
        "<textarea id=note oninput='noteChanged()' "
        "placeholder='e.g. fixing the RGB fusion for SignFlow, then the Mac class_map bug'>"
        "</textarea></div></div>"
        "<div class=hud id=hud></div>"
        "<div class='px feedbox'><div class=feedhead>LIVE ACTIVITY</div>"
        "<div id=feed aria-live=polite></div></div>"
        "<main id=grid></main>"
        "<div id=toast role=status aria-live=polite></div>"
        "<div id=ov role=dialog aria-modal=true onclick=\"if(event.target.id=='ov')close_()\">"
        "<div id=ovb><div id=ovh><span id=ovt></span>"
        "<button id=ovx onclick=close_() aria-label=Close>CLOSE</button></div>"
        "<div id=ovc></div></div></div>"
        f"<script>{JS}</script></html>")
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


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype):
        self.send_response(200)
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
        u = urlparse(self.path)
        if u.path == "/api":
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
        elif u.path == "/file":
            p = (parse_qs(u.query).get("p") or [""])[0]
            txt = read(p) if allowed(p) else "not allowed"
            self._send(txt.encode(), "text/plain; charset=utf-8")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if urlparse(self.path).path != "/action":
            self._send(b"not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            res = do_action(body)
        except Exception as e:
            res = {"ok": False, "msg": f"error: {e}"}
        self._send(json.dumps(res).encode(), "application/json")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    try:
        srv = socketserver.TCPServer(("127.0.0.1", PORT), H)
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
