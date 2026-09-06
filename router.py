#!/usr/bin/env python3
"""Cheap tier for the agent team: free OpenRouter models do the light work.

Idea: Claude and agy are expensive and limited, so they should spend their quota
on real code, decisions and git. Small text-shaped work (docs, comments, README
tables, summaries) goes to a free OpenRouter model instead.

Commands
  router.py models                       list the free models OpenRouter offers now
  router.py classify <project>           tag untagged backlog tasks (S) or (L)
  router.py do <project>                 do the next (S) task and commit it
  router.py ask <project> "<question>"   one-off question about the project

Needs OPENROUTER_API_KEY (free key from https://openrouter.ai/keys).
Put it in ~/agent-team/config.env as:  export OPENROUTER_API_KEY="sk-or-..."
"""
import os, sys, json, re, subprocess, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))

# Two free providers. NVIDIA is preferred: ~40 requests/min versus OpenRouter's
# 50/day cap on accounts with under $10 of lifetime credits.
PROVIDERS = {
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1",
        "env": "NVIDIA_API_KEY",
        "prefix": "nvapi-",
        "signup": "https://build.nvidia.com (free, 1000 credits, no card)",
        # best first; anything the account cannot serve is skipped
        "models": [
            "qwen/qwen3-coder-480b-a35b-instruct",
            "zai/glm-5.2",
            "nvidia/llama-3.3-nemotron-super-49b-v1",
            "deepseek-ai/deepseek-v3.2",
            "moonshotai/kimi-k2.5-instruct",
            "meta/llama-4-scout-17b-16e-instruct",
            "qwen/qwen2.5-coder-32b-instruct",
        ],
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY",
        "prefix": "sk-or-",
        "signup": "https://openrouter.ai/keys",
        "models": [
            "openai/gpt-oss-20b:free",
            "deepseek/deepseek-r1-distill-llama-70b:free",
            "meta-llama/llama-4-scout:free",
            "qwen/qwen3-8b:free",
            "qwen/qwen-2.5-72b-instruct:free",
        ],
    },
}


def provider():
    """Pick a provider from whichever key is present. NVIDIA wins if both are."""
    forced = os.environ.get("ROUTER_PROVIDER", "").strip().lower()
    order = [forced] if forced in PROVIDERS else ["nvidia", "openrouter"]
    for name in order:
        cfg = PROVIDERS[name]
        k = os.environ.get(cfg["env"], "").strip()
        if k:
            return name, cfg, k
    lines = ["No free-tier API key set. Add ONE of these to "
             f"{BASE}/config.env:"]
    for n, c in PROVIDERS.items():
        lines.append(f'  export {c["env"]}="{c["prefix"]}..."   # {c["signup"]}')
    sys.exit("\n".join(lines))
# Preferred free models, best first. The free lineup on OpenRouter rotates
# constantly (DeepSeek and Mistral free variants have come and gone), so this is
# only a preference order - pick_model() falls back to whatever is actually free.
PREFERRED = [
    "openai/gpt-oss-20b:free",                      # strongest free coder
    "deepseek/deepseek-r1-distill-llama-70b:free",  # strong reasoning
    "meta-llama/llama-4-scout:free",
    "qwen/qwen3-8b:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "google/gemma-2-9b-it:free",
    "mistralai/mistral-7b-instruct:free",
]


def http(path, payload=None, timeout=180):
    name, cfg, k = provider()
    url = f"{cfg['url']}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {k}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost/agent-team",
        "X-Title": "agent-team",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"OpenRouter error {e.code}: {e.read().decode()[:300]}")
    except Exception as e:
        sys.exit(f"OpenRouter unreachable: {e}")


def free_models():
    """Models the current provider serves for free."""
    name, _, _ = provider()
    data = http("/models").get("data", [])
    if name == "nvidia":
        # Everything on build.nvidia.com runs off the same free credit pool.
        return [m["id"] for m in data if m.get("id")]
    out = []
    for m in data:
        p = m.get("pricing") or {}
        try:
            if float(p.get("prompt", 1)) == 0 and float(p.get("completion", 1)) == 0:
                out.append(m["id"])
        except (TypeError, ValueError):
            continue
    return out


def pick_model():
    forced = os.environ.get("ROUTER_MODEL", "").strip()
    if forced:
        return forced
    name, cfg, _ = provider()
    avail = set(free_models())
    for m in cfg["models"]:
        if m in avail:
            return m
    if avail:
        return sorted(avail)[0]
    sys.exit(f"no free models available on {name} right now")


def chat(prompt, system="You are a precise, concise software assistant.", model=None):
    model = model or pick_model()
    r = http("/chat/completions", {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": 0.2,
    })
    try:
        return r["choices"][0]["message"]["content"], model
    except (KeyError, IndexError):
        sys.exit(f"unexpected reply: {json.dumps(r)[:300]}")


# ---------------------------------------------------------------- helpers
def mem(proj):
    return os.path.join(os.path.abspath(proj), ".agent-team")


def read(p, limit=8000):
    try:
        return open(p, encoding="utf-8", errors="replace").read()[:limit]
    except OSError:
        return ""


def git(proj, *a):
    return subprocess.run(["git", "-C", proj, *a], capture_output=True,
                          text=True).stdout.strip()


def project_context(proj):
    m = mem(proj)
    files = git(proj, "ls-files")
    return (f"PROJECT.md:\n{read(os.path.join(m, 'PROJECT.md'), 3000)}\n\n"
            f"Files:\n" + "\n".join(files.splitlines()[:80]))


# ---------------------------------------------------------------- commands
def cmd_models():
    name, cfg, _ = provider()
    fm = set(free_models())
    print(f"provider: {name}  ({len(fm)} models available)")
    for m in cfg["models"]:
        print(("  * " if m in fm else "  - ") + m +
              ("" if m in fm else "   (not available on your account)"))
    print(f"\nrouter would use: {pick_model()}")
    other = [n for n in PROVIDERS if n != name]
    if other:
        print(f"(set ROUTER_PROVIDER={other[0]} to switch)")


def classify(proj):
    """Tag each untagged backlog task (S) small or (L) large."""
    bl = os.path.join(mem(proj), "backlog.md")
    if not os.path.exists(bl):
        sys.exit(f"no backlog at {bl}")
    lines = open(bl, encoding="utf-8").read().splitlines()
    todo = [(i, l) for i, l in enumerate(lines)
            if l.startswith("- [ ] ") and not re.match(r"- \[ \] \((S|L)\) ", l)]
    if not todo:
        print("nothing new to classify")
        return
    listing = "\n".join(f"{n+1}. {l[6:]}" for n, (_, l) in enumerate(todo))
    prompt = (
        "Classify each task as S or L.\n"
        "S = small and text-shaped: docs, README, comments, renaming, formatting, "
        "writing a summary. No architectural judgement needed.\n"
        "L = large: changing program logic, fixing bugs, refactoring, anything "
        "needing design decisions, tests, or git branching judgement.\n"
        "When unsure choose L.\n\n"
        f"Project context:\n{project_context(proj)}\n\n"
        f"Tasks:\n{listing}\n\n"
        "Reply with one line per task, exactly: <number>:<S or L>. Nothing else.")
    out, model = chat(prompt)
    verdict = {}
    for mth in re.finditer(r"(\d+)\s*[:.\-]\s*([SL])", out.upper()):
        verdict[int(mth.group(1))] = mth.group(2)
    n = 0
    for idx, (line_no, line) in enumerate(todo, start=1):
        tag = verdict.get(idx, "L")
        lines[line_no] = f"- [ ] ({tag}) " + line[6:]
        n += 1
    open(bl, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    s = sum(1 for v in verdict.values() if v == "S")
    print(f"[{model}] tagged {n} task(s): {s} small, {n-s} large")


def do_small(proj):
    """Do the next (S) task with a free model and commit it."""
    proj = os.path.abspath(proj)
    bl = os.path.join(mem(proj), "backlog.md")
    lines = open(bl, encoding="utf-8").read().splitlines()
    target = next((i for i, l in enumerate(lines) if l.startswith("- [ ] (S) ")), None)
    if target is None:
        print("no small tasks waiting (run: router.py classify <project>)")
        return
    task = lines[target][10:].strip()
    print(f"small task: {task}")

    prompt = (
        f"You are doing one small task in the repo {proj}.\n\nTASK: {task}\n\n"
        f"{project_context(proj)}\n\n"
        "Reply with ONLY the files to write, in this exact format and nothing else:\n"
        "<<<FILE path/relative/to/repo\n<full new content of that file>\n>>>\n"
        "Repeat the block for each file. Give the COMPLETE file content, not a diff.\n"
        "Never delete files. Never touch anything outside the repo. "
        "If the task cannot be done this way, reply exactly: CANNOT")
    out, model = chat(prompt)
    if out.strip().startswith("CANNOT"):
        print(f"[{model}] declined - leaving the task for a big agent")
        return

    written = []
    for mth in re.finditer(r"<<<FILE\s+(.+?)\n(.*?)\n>>>", out, re.S):
        rel, body = mth.group(1).strip(), mth.group(2)
        dest = os.path.normpath(os.path.join(proj, rel))
        if not dest.startswith(proj + os.sep):
            print(f"  refused (outside repo): {rel}")
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "w", encoding="utf-8").write(body.rstrip() + "\n")
        written.append(rel)
        print(f"  wrote {rel}")
    if not written:
        print(f"[{model}] produced no usable files - leaving the task")
        return

    lines[target] = "- [x] (S) " + task
    open(bl, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    with open(os.path.join(mem(proj), "PROGRESS.md"), "a", encoding="utf-8") as f:
        f.write(f"- router({model.split('/')[-1]}) | {task} | files: {', '.join(written)}\n")
    subprocess.run(["git", "-C", proj, "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", proj, "commit", "-m", task[:70]],
                   capture_output=True)
    print(f"[{model}] done and committed")


def ask(proj, q):
    out, model = chat(f"{project_context(proj)}\n\nQuestion: {q}")
    print(f"[{model}]\n{out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    c = sys.argv[1]
    if c == "models":
        cmd_models()
    elif c == "classify" and len(sys.argv) > 2:
        classify(sys.argv[2])
    elif c == "do" and len(sys.argv) > 2:
        do_small(sys.argv[2])
    elif c == "ask" and len(sys.argv) > 3:
        ask(sys.argv[2], " ".join(sys.argv[3:]))
    else:
        sys.exit(__doc__)
