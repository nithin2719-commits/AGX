#!/usr/bin/env python3
"""Free-model tier for AGX: light work that does not need Claude or agy.

Claude and agy are expensive and limited, so they spend their quota on real
code, decisions and git. Small text-shaped work (docs, comments, README tables,
summaries), questions about a project and anything involving images go to free
models instead: cloud free tiers when a key is set, Ollama on this machine
otherwise. providers.py decides which model serves each tier.

Commands
  router.py models                       providers, keys and the model per tier
  router.py classify <project>           tag untagged backlog tasks (S) or (L)
  router.py do <project>                 do the next (S) task and commit it
  router.py ask <project> "<question>"   one-off question about the project
  router.py see <image>... ["question"]  describe or answer a question about images

Keys go in ~/agent-team/config.env (easiest: bash set-key.sh). With no key at
all, every command still works through Ollama if it is running.
"""
import base64, mimetypes, os, re, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import providers  # noqa: E402

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def chat(prompt, tier="small", system="You are a precise, concise software assistant."):
    try:
        return providers.chat([{"role": "system", "content": system},
                               {"role": "user", "content": prompt}], tier=tier)
    except providers.ProviderError as e:
        sys.exit(f"{e}\nAdd a free key with: bash {providers.BASE}/set-key.sh "
                 "(or start Ollama: systemctl start ollama)")


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
    providers.print_status()


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


def looks_truncated(old, new):
    """A model that runs out of output tokens returns half a file. Writing that
    back would silently delete the rest, so refuse big shrinks of big files."""
    a, b = old.count("\n"), new.count("\n")
    return a >= 30 and b < a * 0.5


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
        "Never delete files. Never touch anything outside the repo. Never add AI "
        "attribution, 'generated by' notes or tool names to the files. "
        "If the task cannot be done this way, reply exactly: CANNOT")
    out, model = chat(prompt)
    if out.strip().startswith("CANNOT"):
        print(f"[{model}] declined - leaving the task for a big agent")
        return

    written = []
    for mth in re.finditer(r"<<<FILE\s+(.+?)\n(.*?)\n>>>", out, re.S):
        rel, body = mth.group(1).strip(), mth.group(2)
        dest = os.path.normpath(os.path.join(proj, rel))
        if not dest.startswith(proj + os.sep) or f"{os.sep}.git{os.sep}" in dest + os.sep:
            print(f"  refused (outside the work tree): {rel}")
            continue
        old = read(dest, 10_000_000)
        if old and looks_truncated(old, body):
            print(f"  refused (reply looks truncated, {old.count(chr(10))} -> "
                  f"{body.count(chr(10))} lines): {rel}")
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
    out, model = chat(f"{project_context(proj)}\n\nQuestion: {q}", tier="big")
    print(f"[{model}]\n{out}")


def see(args):
    """Answer a question about one or more images with a vision model."""
    images = [a for a in args if a.lower().endswith(IMAGE_EXT) and os.path.isfile(a)]
    words = [a for a in args if a not in images]
    if not images:
        sys.exit("usage: router.py see <image>... [\"question\"]  (png/jpg/webp/gif)")
    question = " ".join(words) or (
        "Describe what this image shows. If it is a user interface, list concrete "
        "problems with layout, spacing, contrast, alignment and readability.")
    parts = [{"type": "text", "text": question}]
    for path in images:
        mime = mimetypes.guess_type(path)[0] or "image/png"
        data = base64.b64encode(open(path, "rb").read()).decode()
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    try:
        out, model = providers.chat([{"role": "user", "content": parts}], tier="vision")
    except providers.ProviderError as e:
        sys.exit(f"{e}\nVision needs Ollama with a vision model "
                 "(ollama pull qwen3-vl:8b) or a key for NVIDIA, Gemini or Z.ai.")
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
    elif c == "see" and len(sys.argv) > 2:
        see(sys.argv[2:])
    else:
        sys.exit(__doc__)
