#!/usr/bin/env python3
"""CrewAI planning meeting — the 'thinking' half of the agent team.

Three roles discuss the project and agree on what to do next:
  Architect  proposes work
  Engineer   pushes back on what is actually doable in this repo
  Reviewer   keeps only small, safe, verifiable tasks and writes the final list

If the project has a PLAN.md, everything must serve that plan. The whole
discussion is saved so you can read exactly what they said and why.

Usage:  planner.py <project-path> [how-many-tasks]
  ~/agent-team/.venv/bin/python ~/agent-team/planner.py ~/pixie-sddm
"""
import os, sys, subprocess
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "graph"))

PROXY = os.environ.get("PROXY_URL", "http://127.0.0.1:8317/v1")
KEY = os.environ.get("PROXY_KEY", "local-agent-team")
MODEL = os.environ.get("PLANNER_MODEL", "gemini-3.1-pro-low")

os.environ.setdefault("OPENAI_API_KEY", KEY)
os.environ.setdefault("OPENAI_API_BASE", PROXY)

# CrewAI phones home for tracing; offline/slow networks make it hang and spew
# errors at exit. We only ever run locally, so switch it all off.
os.environ["CREWAI_TRACING_ENABLED"] = "false"
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "true"

from crewai import Agent, Task, Crew, LLM  # noqa: E402


def read(path, limit=6000):
    try:
        return open(path, encoding="utf-8", errors="replace").read()[:limit]
    except OSError:
        return ""


def repo_overview(proj):
    files = subprocess.run(["git", "-C", proj, "ls-files"],
                           capture_output=True, text=True).stdout.splitlines()
    head = subprocess.run(["git", "-C", proj, "log", "--oneline", "-10"],
                          capture_output=True, text=True).stdout
    return (f"{len(files)} tracked files. Sample:\n" + "\n".join(files[:60])
            + "\n\nRecent commits:\n" + head)


def graph_context(proj):
    try:
        import memory
        return memory.get_context(proj)
    except Exception as e:
        return f"(graph memory unavailable: {e})"


def plan(proj, n=5):
    proj = os.path.abspath(proj)
    mem = os.path.join(proj, ".agent-team")
    backlog = os.path.join(mem, "backlog.md")
    name = os.path.basename(proj)

    user_plan = read(os.path.join(mem, "PLAN.md")).strip()
    plan_block = (
        f"=== THE OWNER'S PLAN (this is the boss - every task MUST serve it) ===\n{user_plan}"
        if user_plan and "(nothing yet" not in user_plan
        else "=== THE OWNER'S PLAN ===\n(none written - use your judgement to improve the project)")

    context = f"""PROJECT: {name}  (path {proj})

{plan_block}

=== PROJECT.md ===
{read(os.path.join(mem, 'PROJECT.md'))}

=== PROGRESS.md (already done - do NOT repeat) ===
{read(os.path.join(mem, 'PROGRESS.md'))}

=== current backlog (do NOT duplicate) ===
{read(backlog)}

=== graph memory ===
{graph_context(proj)}

=== repository ===
{repo_overview(proj)}
"""

    llm = LLM(model=f"openai/{MODEL}", base_url=PROXY, api_key=KEY)

    architect = Agent(
        role="Software Architect",
        goal=f"Propose the {n} most valuable next steps for {name}",
        backstory=("You study a real repository and decide what genuinely moves it "
                   "forward. If the owner wrote a plan, it overrides your own ideas. "
                   "You never invent features nobody asked for."),
        llm=llm, verbose=False, allow_delegation=False)

    engineer = Agent(
        role="Engineer",
        goal="Say honestly which proposals are actually implementable in THIS repo",
        backstory=("You know the code and the tooling. You object when a proposal is "
                   "vague, needs tools this repo does not have, cannot be verified, or "
                   "would break something. You say plainly what you would do instead."),
        llm=llm, verbose=False, allow_delegation=False)

    reviewer = Agent(
        role="Engineering Reviewer",
        goal="Turn the discussion into a final task list that one agent can execute",
        backstory=("You settle the debate. Each surviving task must be one concrete "
                   "action, doable in a single sitting, and checkable afterwards."),
        llm=llm, verbose=False, allow_delegation=False)

    t1 = Task(
        description=f"Study this project and propose the {n} most valuable next steps. "
                    f"Explain WHY each one matters.\n\n{context}",
        expected_output=f"{n} proposals, each with a one-line reason.",
        agent=architect)

    t2 = Task(
        description=(f"Review the architect's proposals for {name} as the engineer who "
                     "has to implement them. For each, say KEEP, CHANGE or DROP and why. "
                     "Be blunt about anything not verifiable in this repo."),
        expected_output="A verdict and reason for each proposal.",
        agent=engineer, context=[t1])

    t3 = Task(
        description=(
            f"Settle the discussion for {name} and write the final task list.\n"
            "Honour the owner's plan above all. Drop anything vague, duplicated, "
            "already done, or too big.\n"
            "Output ONLY the final task lines, each starting with '- [ ] ' and nothing "
            "else. No preamble, no numbering, no headings."),
        expected_output="Lines starting with '- [ ] '",
        agent=reviewer, context=[t1, t2])

    crew = Crew(agents=[architect, engineer, reviewer], tasks=[t1, t2, t3], verbose=False)
    out = str(crew.kickoff())

    # --- save the discussion so the owner can read it ---
    disc_dir = os.path.join(mem, "discussions")
    os.makedirs(disc_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    disc_path = os.path.join(disc_dir, f"{stamp}.md")
    parts = [f"# Planning meeting — {name}", f"_{datetime.now():%Y-%m-%d %H:%M}_", ""]
    if user_plan:
        parts += ["## The plan they were given", user_plan, ""]
    for label, t in (("Architect proposes", t1), ("Engineer responds", t2),
                     ("Reviewer decides", t3)):
        body = ""
        try:
            body = (t.output.raw if t.output else "").strip()
        except Exception:
            pass
        parts += [f"## {label}", body or "(no output)", ""]
    open(disc_path, "w", encoding="utf-8").write("\n".join(parts))

    tasks = [l.strip() for l in out.splitlines() if l.strip().startswith("- [ ]")]
    if not tasks:
        print(f"planner produced no usable tasks. Discussion saved: {disc_path}")
        return 1

    existing = read(backlog, 100000)
    added = 0
    with open(backlog, "a", encoding="utf-8") as f:
        for t in tasks:
            body = t[5:].strip()
            if body and body not in existing:
                f.write(t + "\n")
                added += 1
    print(f"{name}: added {added} task(s)")
    for t in tasks:
        print("  " + t)
    print(f"discussion saved: {disc_path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    sys.exit(plan(sys.argv[1], n))
