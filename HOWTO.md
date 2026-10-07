# Agent Team — the short version

## Every session, one command

    bash ~/agent-team/up.sh

Starts the dashboard, the pixel office and the graph memory (only what is not
already running) and prints the links. Run it any time; it is safe to repeat.

---

## The three things you look at

| What | Where | Shows |
|---|---|---|
| **Control panel** | http://localhost:8765 | two tabs: Operations (plans, tasks, meetings, logs, buttons) and Workspace (free-model chat, API keys) |
| **Pixel office** | click "Pixel Office" on the panel | Claude and agy as characters at desks |
| **Graph memory** | http://localhost:7474 | facts and commits (neo4j / agentteam123) |

Always open the pixel office from the **panel button** — the token changes on
every restart, so a bookmarked URL goes stale.

---

## Doing work

The Operations tab opens with a sentence saying what the crew is doing, your
focus note, and the whole-team buttons: **Auto cycle** (meet where work ran out,
then work), **Run team**, **Pixel office**, **Add project** and **Stop everything**
(kills every agent and turns off the timers).

Below it, pick a project from the list on the left (on a phone, tap it; **All
projects** goes back). Its page has:

| Button | Meaning |
|---|---|
| **Claude** / **agy** | that agent does one task now |
| **Both** | both agents, taking turns on the project |
| **Meeting** | both agents discuss and agree the next tasks (10-20 min) |
| **Stop** | stop that project's agents |
| **Add** | type a task straight into the backlog |
| **Plan** tab, **Edit plan** | what you want them to do; **Save plan** to keep it |
| **Tasks** tab | every task, done, in progress and waiting |
| **Ask** tab | ask Claude or agy about the project, or **What happened?** to read the logs |
| **Details** tab | meeting transcripts, work logs, recent commits |
| **Build graph** / **View graph** | rebuild or open the code knowledge graph |
| **Commit** / **Push** | commit what the agents left, publish the branch (asks first) |

Typed text, draft plans and the conversation survive the page's refresh and
switching between projects.

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

## The Workspace tab

A private chat for yourself, on the free models, without spending Claude or
agy quota.

- **Pick what you're working on** with the chips: Code, Reasoning, Images,
  Cyber / CTF, Quick or Writing. AGX uses the model made for that work, and the
  line underneath names the model and why. **Cyber / CTF** stays on a local
  Ollama model first, so nothing about a target leaves the machine.
- **Any · auto** instead lets you choose the size — **Small / Big / Vision** —
  and AGX picks the best free model for it. Every reply names the model that
  wrote it, as `model via provider`.
- **Pick a model** opens the full list — every model each provider with a
  working key offers — to pin one exact model instead of the task pick. A model
  that is not answering is marked and skipped.
- **Attach image** (the + in the box, or paste one) and ask about it. Images
  always go to a vision model, the same as `python3 router.py see <image>`.
- Type in the glowing box and press **Ctrl+Enter** or the arrow to send.
- **Models and keys**: open a provider, paste its key and press **Save key**. It
  is written to `config.env` (mode 600) and never shown in the browser again.
  **Test** makes one tiny call and lists the model it would use for each size.
  With no keys at all, everything runs on Ollama if it is up. Providers: NVIDIA
  NIM (free — GLM-5.x, Kimi K3, Nemotron 550B), OpenRouter (free tier), GitHub
  Models (free GPT-4.1, DeepSeek, Llama with a GitHub token), Google Gemini,
  Z.ai, Groq, Cerebras and Mistral.
- **Make an image** works only after a provider passes **Test images**
  with a real image. NVIDIA NIM and Gemini are the candidates. Ollama refuses
  image models over its API, so it is never offered.

---

## From your phone (Tailscale)

The dashboard only listens on this machine. Tailscale publishes it over HTTPS
to your own devices, nowhere else.

    sudo tailscale up                       # once, if Tailscale is not connected
    bash ~/agent-team/tailnet.sh on         # serve https://<machine>.<tailnet>.ts.net
    bash ~/agent-team/tailnet.sh link       # the address, the token, a one-tap sign-in link
    bash ~/agent-team/tailnet.sh off        # stop serving it

Each device signs in once with the token (the sign-in link does it in one tap;
it contains the token, so open it only on your own devices). If `tailnet.sh on`
says access denied, allow your user once with
`sudo tailscale set --operator=$USER`. If it asks you to enable HTTPS, open the
link it prints, then run it again.

To sign every device out, make a new token:

    rm ~/agent-team/state/token && systemctl --user restart agent-dashboard.service

---

## What the agents are allowed to do

- **Claude** runs with a fixed list of allowed tools (`permissions.sh`), not with
  permission checks skipped. A worker may read and edit files inside the
  project, run `git status/diff/add/commit/log`, and run the project's test
  command. A meeting may only read and run `git log/diff/show`. Everything else
  - `rm`, `git push`, `git reset`, network tools, files outside the project -
  is refused.
- The test command is guessed from the repo (`package.json`, `Cargo.toml`,
  `go.mod`, pytest config, a Makefile `test:` target). Set it yourself by
  writing one line to `<project>/.agent-team/test-command`, e.g. `npm run test:unit`.
- **agy** has no allowlist option, so it still runs with
  `--dangerously-skip-permissions`. Point it only at projects on a branch you
  can throw away.

---

## The dashboard token

`state/token` (mode 600) is made the first time the dashboard starts. The page
sends it with every button press, so another website open in your browser
cannot press buttons for you. The dashboard also refuses requests addressed to
any name but `localhost`, `127.0.0.1` or your tailnet name.

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

After updating AGX, restart the dashboard so it picks up the change:

    systemctl --user restart agent-dashboard.service
