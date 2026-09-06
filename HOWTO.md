# Agent Team — the short version

## Every session, one command

    bash ~/agent-team/up.sh

Starts the dashboard, the pixel office and the graph memory (only what is not
already running) and prints the links. Run it any time; it is safe to repeat.

---

## The three things you look at

| What | Where | Shows |
|---|---|---|
| **Control panel** | http://localhost:8765 | plans, tasks, meetings, logs, buttons |
| **Pixel office** | click "Pixel Office" on the panel | Claude and agy as characters at desks |
| **Graph memory** | http://localhost:7474 | facts and commits (neo4j / agentteam123) |

Always open the pixel office from the **panel button** — the token changes on
every restart, so a bookmarked URL goes stale.

---

## Doing work

Everything is a button on the control panel:

| Button | Meaning |
|---|---|
| **▶ Claude** | Claude does one task now |
| **▶ agy** | agy does one task now |
| **💬 Meeting** | both agents discuss and agree the next tasks (10-20 min) |
| **📈 Graph** | rebuild the code knowledge graph |
| **✎ Plan** | edit what you want them to do, then Save |
| **+ Task** | type a task straight into the backlog |
| **■ Stop** | stop that project's agents |
| **■ STOP EVERYTHING** | kill every agent and disable the timers |

Same thing from the terminal:

    bash ~/agent-team/team.sh                    # both agents, one round
    bash ~/agent-team/meet.sh <project-path>     # planning meeting
    bash ~/agent-team/stop.sh                    # stop everything

---

## Choosing what they work on

    bash ~/agent-team/focus.sh SignFlow    # only SignFlow
    bash ~/agent-team/focus.sh all         # every project
    bash ~/agent-team/focus.sh             # show what is active

Paused projects stay registered - they are just skipped.

---

## Adding a project

    bash ~/agent-team/add-project.sh /path/to/project    # must be a git repo
    nano /path/to/project/.agent-team/PLAN.md            # tell them what you want
    bash ~/agent-team/build-graph.sh /path/to/project    # optional: code graph

Put it on its own branch first so your `main` stays clean:

    git -C /path/to/project checkout -b agent-work

---

## The normal loop

1. `bash ~/agent-team/up.sh`
2. Write what you want in **✎ Plan**
3. Hit **💬 Meeting** — Claude and agy argue and agree a task list
4. Hit **▶ Claude** and **▶ agy** — or let the timers do it
5. Watch the pixel office, read the meeting transcript on the card
6. Review their commits: `git -C <project> log --oneline`

---

## If something looks stuck

    bash ~/agent-team/health.sh     # what is up and what is down
    bash ~/agent-team/stop.sh       # stop all agents (force-kills if needed)
    bash ~/agent-team/up.sh         # bring the services back

Agents print nothing while thinking. A quiet terminal for a few minutes is
normal, not a hang. `health.sh` is the honest answer.

---

## Make it survive reboots (do this once)

    cp ~/agent-team/systemd/*.service ~/agent-team/systemd/*.timer ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now agent-dashboard.service agent-pixel.service
    systemctl --user enable --now agent-claude.timer agent-agy.timer

Until you do this, the dashboard and pixel office die whenever the shell that
started them exits - which is why they keep disappearing.
