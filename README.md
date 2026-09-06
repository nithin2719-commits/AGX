# Agent Team

Two AI workers (Claude Code and Antigravity) plan, build, review and remember
work on all of your projects — on the quota you already pay for, with no API keys.

---

## The loop

    YOUR PLAN  ->  planning meeting  ->  backlog  ->  workers build  ->  graph memory
         ^                                                                    |
         +--------------------- feeds the next meeting ----------------------+

1. You write what you want in `PLAN.md`.
2. Three roles (Architect, Engineer, Reviewer) hold a **planning meeting** and
   agree on concrete tasks. The whole discussion is saved for you to read.
3. Claude and Antigravity pick tasks off the backlog and actually do them —
   editing files, running tests, committing to git.
4. What they learn goes into a **graph memory** every future run reads.

---

## Features

| Feature | What it means |
|---|---|
| **You set the plan** | `PLAN.md` per project. It outranks the agents' own ideas — both the planners and the workers obey it. |
| **Agents discuss** | Architect proposes, Engineer objects, Reviewer decides. Not one model guessing. |
| **You read every conversation** | Meetings and work logs are saved and readable in the GUI. |
| **Per-project memory** | Each project has its own `PROJECT.md`, `PROGRESS.md`, backlog and history. |
| **Graph memory** | Neo4j stores facts and commits per project; injected into every prompt. |
| **Session continuity** | Agents resume their previous conversation per project (`--continue`). |
| **No duplicate work** | Tasks are claimed (`- [~]`) and each project is locked to one agent at a time. |
| **Runs by itself** | systemd timers wake the planner and both workers automatically. |
| **Uses idle quota** | Timers fire after limit resets, so unused quota gets spent on real work. |
| **No API keys** | CLIProxyAPI turns your subscriptions into a local OpenAI-compatible API. |
| **Live GUI** | See who is working, on what, with the plan, tasks, meetings and commits. |

---

## Set up a new project (30 seconds)

    bash ~/agent-team/add-project.sh /path/to/project     # must be a git repo

Then write your plan:

    nano /path/to/project/.agent-team/PLAN.md

That is the whole setup. Everything else is automatic.

---

## Daily commands

    # REAL meeting: Claude and agy argue with each other, then agree the tasks
    bash ~/agent-team/meet.sh /path/to/project
    #   Round 1  Claude reads the code and proposes
    #   Round 2  agy reads Claude's proposal + the code, and pushes back
    #   Round 3  Claude weighs the objections and writes the agreed backlog
    #   full transcript saved in .agent-team/discussions/

    # faster single-model meeting (3 CrewAI roles via the proxy)
    ~/agent-team/.venv/bin/python ~/agent-team/planner.py /path/to/project

    # plan every project that is running low
    bash ~/agent-team/plan.sh

    # let the workers do a round
    bash ~/agent-team/team.sh

    # watch everything, read every conversation
    python3 ~/agent-team/dashboard.py        # http://localhost:8765
    #   plan, tasks, planning meetings, work logs, commits
    #   the "Pixel Office" button opens the pixel-art view

    # the pixel-art office (pixel-agents, watches your Claude sessions)
    pixel-agents --port 8790                 # http://127.0.0.1:8790

    # check every moving part is alive
    bash ~/agent-team/health.sh

    # teach the graph something permanent
    ~/agent-team/.venv/bin/python ~/agent-team/graph/memory.py \
        fact /path/to/project "Never break Qt5 compatibility"

---

## Full automation

    cp ~/agent-team/systemd/*.service ~/agent-team/systemd/*.timer ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now agent-proxy.service
    systemctl --user enable --now agent-planner.timer agent-claude.timer agent-agy.timer
    sudo loginctl enable-linger nithin

| Unit | Cadence | Job |
|---|---|---|
| `agent-proxy.service` | always on | subscriptions -> local API on :8317 |
| `agent-dashboard.service` | always on | dashboard on :8765 |
| `agent-pixel.service` | always on | pixel-art office on :8790 |
| `agent-planner.timer` | every 6h | refill backlogs that are running low |
| `agent-claude.timer` | every 5h | Claude does a task in every project |
| `agent-agy.timer` | every 2h | Antigravity does a task in every project |

Check and stop:

    systemctl --user list-timers
    journalctl --user -u agent-claude.service -f
    systemctl --user disable --now agent-claude.timer agent-agy.timer agent-planner.timer

---

## Files in a project

    <project>/.agent-team/
      PLAN.md        <- YOU write this. The team obeys it.
      PROJECT.md     what the project is (agents fill this in)
      PROGRESS.md    running log of everything done
      backlog.md     the task list  [ ] todo  [~] doing  [x] done
      discussions/   saved planning meetings - read these
      status-*.json  live state for the GUI

Everything except the transient status files is committed with your repo, so the
memory travels with the project.

---

## Where things live

    ~/agent-team          this system
    ~/cliproxy            the proxy (subscriptions -> local API)
    ~/agent-team/graph    Neo4j graph memory (docker compose)
    http://localhost:8765 GUI
    http://localhost:7474 Neo4j browser (neo4j / agentteam123)

---

## Safety

- Workers auto-approve their own actions, so only register repos where
  auto-commits are acceptable. A dedicated branch is safest:
  `git checkout -b agent-work`
- They never push. Nothing reaches GitHub unless you push it.
- Everything is in git — review with `git log` and revert anything you dislike.
- Vague plans produce vague work. Be specific in `PLAN.md`.
