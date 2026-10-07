'use strict';
// AGX dashboard. Every card is built once; the 4-second refresh only rewrites
// the parts whose data changed, so typed text, open panels, chat history and
// scroll positions survive it.

const TOKEN = document.querySelector('meta[name="agx-token"]').content;
const HDR = {'Content-Type': 'application/json', 'X-AGX-Token': TOKEN};
const NAME = {claude: 'CLAUDE', agy: 'AGY'};
const GROUPS = [
  ['running', 'RUNNING NOW'], ['stalled', 'NEEDS ATTENTION'],
  ['waiting', 'READY - WAITING FOR AN AGENT'], ['idle', 'IDLE - NO WORK LEFT'],
  ['paused', 'PAUSED']];
const I = {
  play: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2.5l8.5 5.5-8.5 5.5z"/></svg>',
  chat: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 2.5h12v8.5H8l-4 3v-3H2z"/></svg>',
  graph: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 14V2M2 14h12M5 11l3-4 3 2 3-5"/></svg>',
  plan: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 2h7l3 3v9H3zM10 2v3h3"/></svg>',
  stop: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 3.5h9v9h-9z"/></svg>',
  doc: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2h6l2 2v10H4zM6 7h4M6 10h4"/></svg>',
  plus: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10"/></svg>',
};

let DATA = [], MODELS = {}, SET = {}, BUSY = false, TICKING = false;
const CHAT = {};           // project -> [{who, text}]
const CARDS = new Map();   // project -> {el, q: {part: element}, planDirty}
const OPEN = {};           // project + panel -> open?
let GROUP_EL = null;       // status -> {sec, count, grid}
let OV = null;             // {mode: 'project'|'file', name}

const $ = (s, r = document) => r.querySelector(s);
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
// Write only when the markup actually changed: no wiped inputs, no flicker.
function setHTML(el, html) {
  if (el && el._html !== html) { el.innerHTML = html; el._html = html; }
}
function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }

async function post(body) {
  const r = await fetch('/action', {method: 'POST', headers: HDR, body: JSON.stringify(body)});
  if (r.status === 401 || r.status === 403) {
    const j = await r.json().catch(() => ({}));
    return {ok: false, msg: j.msg || 'not signed in - reload the page'};
  }
  return r.json();
}
function toast(m, bad) {
  const t = $('#toast');
  t.textContent = m; t.className = bad ? 'err' : ''; t.style.display = 'block';
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.style.display = 'none'; }, 6500);
}

// One server action at a time, as before. Only action buttons are locked;
// panels, chat and the overlay stay usable while an action runs.
function lockActions(on) {
  document.querySelectorAll('button[data-act]').forEach(b => { b.disabled = on; });
}
async function act(a, p, x) {
  if (BUSY) { toast('one action at a time', true); return null; }
  BUSY = true; lockActions(true);
  let j = null;
  try {
    j = await post(Object.assign({action: a, project: p}, x || {}));
    toast(j.msg || (j.ok ? 'done' : 'failed'), !j.ok);
  } catch (e) { toast('request failed: ' + e, true); }
  BUSY = false; lockActions(false); tick();
  return j;
}
function runAct(a, p) {
  if (a === 'stop_all' && !confirm('Stop every agent and disable the timers?')) return;
  if (a === 'push' && !confirm('Push this branch to GitHub? This publishes the commits.')) return;
  if (a === 'commit') {
    const m = prompt('Commit message (leave blank and one will be written from the diff):', '');
    if (m === null) return;
    act('commit', p, {text: m});
    return;
  }
  act(a, p);
}

// ---------------------------------------------------------------- pieces
function agentRow(a, s) {
  const on = !!(s && s.running && s.state === 'working');
  const st = s ? s.state : 'never run';
  return `<div class="agent${on ? ' on' : ''}">
    <span class="who ${a}">${NAME[a]}</span>
    <span class="state">${esc(String(st).toUpperCase())}</span>
    <span class="what">${s && s.task ? esc(s.task) : ''}</span></div>`;
}
function bar(t) {
  const tot = Math.max(t.done + t.doing + t.todo, 1), N = Math.min(tot, 26);
  let o = '';
  for (let i = 0; i < N; i++) {
    const p = (i + 1) / N * tot;
    o += `<i class="${p <= t.done ? 'd' : p <= t.done + t.doing ? 'w' : ''}"></i>`;
  }
  return `<div class="bar" role="img" aria-label="${t.done} done, ${t.doing} in progress, ${t.todo} left">${o}</div>`;
}
function ghBar(p) {
  const g = p.github || {};
  if (!g.connected) {
    return `<div class="gh"><span class="ghtag">NO REMOTE</span>
      <span>not connected to GitHub - work stays on this machine only</span></div>`;
  }
  const repo = g.web.replace(/^https?:\/\/(www\.)?github\.com\//, '');
  const bits = [];
  if (g.ahead) bits.push(`<b class="ahead">&uarr; ${g.ahead} to push</b>`);
  if (g.behind) bits.push(`<b class="behind">&darr; ${g.behind} behind</b>`);
  if (g.dirty) bits.push(`<b class="dirty">${g.dirty} uncommitted</b>`);
  if (!bits.length) bits.push('<b class="sync">in sync</b>');
  return `<div class="gh"><span class="ghtag">GITHUB</span>
    <a href="${esc(g.web)}" target="_blank" rel="noopener">${esc(repo)}</a>
    <span class="ghbranch">${esc(g.branch)}</span>${bits.join('')}
    <span class="ghspacer"></span>
    ${g.dirty ? `<button data-act="commit" aria-label="Commit ${g.dirty} changed files">COMMIT</button>` : ''}
    ${g.ahead ? `<button class="b-go" data-act="push" aria-label="Push ${g.ahead} commits to GitHub">PUSH</button>` : ''}
  </div>`;
}
function fileLinks(arr, ic) {
  return arr.map(x => `<button class="ln linkish" data-file="${esc(x.file)}" data-label="${esc(x.label)}">${ic}<span>${esc(x.label)}</span></button>`).join('');
}
function stats(p) {
  const t = p.tasks, w = p.work || {};
  const cls = (n, base) => n ? base : 'c-zero';
  return `${bar(t)}
    <div class="counts"><b>${t.done}</b> done, <b>${t.doing}</b> doing, <b>${t.todo}</b> left</div>
    <div class="mini">
      <div class="${cls(w.claude, 'c-claude')}"><b>${w.claude || 0}</b><span>claude</span></div>
      <div class="${cls(w.agy, 'c-agy')}"><b>${w.agy || 0}</b><span>agy</span></div>
      <div class="${cls(w.failed, 'c-bad')}"><b>${w.failed || 0}</b><span>no commit</span></div>
      <div class="${p.graph.up ? 'c-ok' : 'c-bad'}"><b>${p.graph.facts}</b><span>facts</span></div>
    </div>
    ${t.todo ? `<h3 class="sec">Next task</h3><code class="line">${esc(p.next_task)}</code>` : ''}`;
}
function planPreview(p) {
  return p.plan
    ? `<div class="plan">${esc(p.plan.slice(0, 900))}${p.plan.length > 900 ? '\n…' : ''}</div>`
    : `<div class="plan noplan">No plan written yet - the agents are deciding for themselves. Press PLAN and tell them what you want.</div>`;
}
function details(p) {
  return (p.talks.length ? `<h3 class="sec">Planning meetings</h3>${fileLinks(p.talks, I.chat)}` : '')
    + (p.logs.length ? `<h3 class="sec">Work logs</h3>${fileLinks(p.logs, I.doc)}` : '')
    + (p.commits.length ? `<h3 class="sec">Recent commits</h3>${p.commits.slice(0, 6).map(c => `<code class="line">${esc(c)}</code>`).join('')}` : '')
    || '<p class="hint">nothing recorded yet</p>';
}
function graphLink(p) {
  return p.codegraph.built
    ? `<a class="btn" href="/graph?p=${encodeURIComponent(p.name)}" target="_blank" rel="noopener">${I.graph}VIEW GRAPH (${p.codegraph.nodes})</a>`
    : '';
}

// ---------------------------------------------------------------- cards
function makeCard(name) {
  const n = esc(name);
  const el = document.createElement('section');
  el.className = 'frame card';
  el.dataset.p = name;
  el.innerHTML = `
    <div class="chead">
      <h2 class="pname"><button class="linkish" data-open="${n}" title="Open ${n} in full">${n}</button></h2>
      <div class="badge" data-k="badge"></div>
    </div>
    <div class="ppath" data-k="path"></div>
    <div data-k="gh"></div>
    <div class="cbody">
      <div class="colL">
        <div data-k="agents"></div>
        <div data-k="stats"></div>
      </div>
      <div class="colR">
        <h3 class="sec">The plan they follow</h3>
        <div data-k="plan"></div>
        <div class="row">
          <button class="b-claude" data-act="run_claude" aria-label="Run Claude on ${n}">${I.play}CLAUDE</button>
          <button class="b-agy" data-act="run_agy" aria-label="Run agy on ${n}">${I.play}AGY</button>
          <button data-act="meet" aria-label="Planning meeting for ${n}">${I.chat}MEETING</button>
          <button class="b-danger" data-act="stop" aria-label="Stop agents on ${n}">${I.stop}STOP</button>
        </div>
        <div class="row">
          <input type="text" class="task-in" data-k="task" placeholder="add a task…" aria-label="Add a task to ${n}" style="flex:1 1 180px">
          <button class="b-go" data-do="addTask" aria-label="Add task">${I.plus}ADD</button>
        </div>
        <div class="row">
          <button class="b-go" data-act="run_both" aria-label="Run both agents on ${n}">${I.play}BOTH</button>
          <button data-do="toggle" data-panel="chat" aria-expanded="false" aria-label="Ask an agent about ${n}">${I.chat}ASK</button>
          <button data-do="toggle" data-panel="det" aria-expanded="false">DETAILS</button>
          <button data-do="toggle" data-panel="pw" aria-expanded="false" aria-label="Edit plan for ${n}">${I.plan}PLAN</button>
          <button data-act="graphify" aria-label="Rebuild code graph for ${n}">${I.graph}BUILD GRAPH</button>
          <span data-k="graphlink"></span>
        </div>
        <div class="panel" data-k="panel_chat" hidden>
          <label class="label" for="q-${n}">Ask the agents about this project</label>
          <div class="chatlog" data-k="log" aria-live="polite"></div>
          <textarea id="q-${n}" class="chat-in" data-k="q" rows="2" placeholder="what did you change? why is agy failing? what is left?"></textarea>
          <div class="row">
            <button class="b-claude" data-do="ask" data-agent="claude">ASK CLAUDE</button>
            <button class="b-agy" data-do="ask" data-agent="agy">ASK AGY</button>
            <button data-do="explain">WHAT HAPPENED?</button>
            <span class="hint">ctrl+enter asks claude, replies take 10-60s</span>
          </div>
        </div>
        <div class="panel" data-k="panel_pw" hidden>
          <label class="label" for="plan-${n}">PLAN.md - agents obey this</label>
          <textarea id="plan-${n}" data-k="plan_ta" rows="10"></textarea>
          <div class="row"><button class="b-go" data-do="savePlan">SAVE PLAN</button></div>
        </div>
        <div class="panel" data-k="panel_det" hidden><div data-k="details"></div></div>
      </div>
    </div>`;
  const q = {};
  el.querySelectorAll('[data-k]').forEach(x => { q[x.dataset.k] = x; });
  const c = {el, q, planDirty: false};
  q.plan_ta.addEventListener('input', () => { c.planDirty = true; });
  return c;
}
function updateCard(c, p) {
  const q = c.q;
  q.badge.className = 'badge ' + p.status;
  setText(q.badge, p.status_text);
  setText(q.path, p.path);
  setHTML(q.gh, ghBar(p));
  setHTML(q.agents, agentRow('claude', p.agents.claude) + agentRow('agy', p.agents.agy));
  setHTML(q.stats, stats(p));
  setHTML(q.plan, planPreview(p));
  setHTML(q.graphlink, graphLink(p));
  if (OPEN[p.name + 'det']) setHTML(q.details, details(p));
  // The plan editor follows the file until you start typing in it.
  if (!c.planDirty && document.activeElement !== q.plan_ta && q.plan_ta.value !== (p.plan_raw || '')) {
    q.plan_ta.value = p.plan_raw || '';
  }
  if (BUSY) lockActions(true);
}
// Moving a card between groups must not steal focus from what you are typing.
function place(el, parent, before) {
  const a = document.activeElement, inside = a && el.contains(a);
  const sel = inside && typeof a.selectionStart === 'number' ? [a.selectionStart, a.selectionEnd] : null;
  parent.insertBefore(el, before);
  if (inside) {
    a.focus({preventScroll: true});
    if (sel) { try { a.setSelectionRange(sel[0], sel[1]); } catch (e) { /* not a text field */ } }
  }
}
function renderGroups() {
  const main = $('#grid');
  if (!DATA.length) {
    GROUP_EL = null; CARDS.clear();
    setHTML(main, `<div class="frame empty">No projects yet.<br><br>
      <code>bash ~/agent-team/add-project.sh /path/to/project</code></div>`);
    return;
  }
  if (!GROUP_EL) {
    main.innerHTML = ''; main._html = null; GROUP_EL = {};
    for (const [g, label] of GROUPS) {
      const s = document.createElement('section');
      s.className = 'group g-' + g;
      s.innerHTML = `<h2 class="gtitle"><span>${label} <span class="gcount num"></span></span></h2><div class="cards"></div>`;
      main.appendChild(s);
      GROUP_EL[g] = {sec: s, count: $('.gcount', s), grid: $('.cards', s)};
    }
  }
  const seen = new Set();
  for (const [g] of GROUPS) {
    const items = DATA.filter(p => p.status === g), G = GROUP_EL[g];
    G.sec.hidden = !items.length;
    setText(G.count, `(${items.length})`);
    items.forEach((p, i) => {
      seen.add(p.name);
      let c = CARDS.get(p.name);
      if (!c) { c = makeCard(p.name); CARDS.set(p.name, c); }
      updateCard(c, p);
      if (G.grid.children[i] !== c.el) place(c.el, G.grid, G.grid.children[i] || null);
    });
  }
  for (const [n, c] of CARDS) if (!seen.has(n)) { c.el.remove(); CARDS.delete(n); }
}

// ---------------------------------------------------------------- feed, totals, models
function renderFeed() {
  const rows = [];
  DATA.forEach(p => ['claude', 'agy'].forEach(a => {
    const s = p.agents[a];
    if (s && s.state === 'working' && s.running) rows.push({k: 0, a, p: p.name, t: s.task || 'working', st: 'WORKING'});
  }));
  DATA.forEach(p => { if (p.status === 'waiting' && p.next_task) rows.push({k: 1, a: '', p: p.name, t: p.next_task, st: 'NEXT UP'}); });
  DATA.forEach(p => (p.progress || []).slice(-1).forEach(l => {
    const m = l.match(/\|\s*(claude|agy)\s*\|\s*(.*)$/);
    if (m) rows.push({k: 2, a: m[1], p: p.name, t: m[2], st: 'DONE'});
  }));
  rows.sort((x, y) => x.k - y.k);
  setHTML($('#feed'), rows.length
    ? rows.slice(0, 8).map(r => `<div class="fline k${r.k}">
        <span class="fst">${r.st}</span>
        <span class="who ${r.a}">${r.a ? NAME[r.a] : ''}</span>
        <span class="fproj">${esc(r.p)}</span>
        <span class="ftask">${esc(r.t)}</span></div>`).join('')
    : '<div class="fline idle2">nothing running - press AUTO CYCLE to put the team to work</div>');
}
function renderHud() {
  const run = DATA.filter(p => p.status === 'running').length;
  const done = DATA.reduce((a, p) => a + p.tasks.done, 0);
  const left = DATA.reduce((a, p) => a + p.tasks.todo, 0);
  const fail = DATA.reduce((a, p) => a + ((p.work || {}).failed || 0), 0);
  const tile = (n, label, cls) => `<div class="frame ${cls || ''}"><b>${n}</b><span>${label}</span></div>`;
  setHTML($('#hud'),
    tile(run, 'RUNNING', run ? 'is-ok' : 'is-zero') + tile(DATA.length, 'PROJECTS')
    + tile(done, 'DONE') + tile(left, 'LEFT') + tile(fail, 'NO COMMIT', fail ? 'is-bad' : 'is-zero'));
}
function renderModels() {
  const box = $('#models');
  const sig = JSON.stringify(MODELS);
  if (box._sig !== sig) {
    box._sig = sig;
    box.innerHTML = ['claude', 'agy'].map(a => `<div class="mrow">
      <label for="m-${a}" class="who ${a}">${NAME[a]}</label>
      <select id="m-${a}" data-agent="${a}" aria-label="Model for ${NAME[a]}">
        <option value="">default</option>
        ${(MODELS[a] || []).map(m => `<option value="${esc(m.id)}">${esc(m.label)}${m.note ? ' - ' + esc(m.note) : ''}</option>`).join('')}
      </select></div>`).join('');
  }
  ['claude', 'agy'].forEach(a => {
    const s = $('#m-' + a), cur = SET['model_' + a] || '';
    if (!s || document.activeElement === s || s.value === cur) return;
    // A saved model the CLI no longer lists still shows, instead of a blank box.
    if (cur && ![...s.options].some(o => o.value === cur)) {
      s.add(new Option(cur + ' (saved, not listed now)', cur));
    }
    s.value = cur;
  });
  const n = $('#note');
  if (document.activeElement !== n && !n._dirty && n.value !== (SET.session_note || '')) n.value = SET.session_note || '';
}

// ---------------------------------------------------------------- overlay
function projectView(p) {
  const t = p.tasks, g = p.github || {};
  const links = (arr, ic) => arr.length ? fileLinks(arr, ic) : '<div class="muted">none yet</div>';
  const tasks = (p.all_tasks || []).map(x => {
    const cls = x.state === 'x' ? 't-done' : x.state === '~' ? 't-doing' : 't-todo';
    const mark = x.state === 'x' ? '✓' : x.state === '~' ? '▶' : '○';
    return `<div class="titem ${cls}"><span class="tm">${mark}</span><span>${esc(x.text)}</span></div>`;
  }).join('') || '<div class="muted">no tasks - run a meeting</div>';
  return `<div class="pv-path">${esc(p.path)}</div>
    ${ghBar(p)}
    <div>${agentRow('claude', p.agents.claude)}${agentRow('agy', p.agents.agy)}</div>
    <div class="row pv-actions">
      <button class="b-claude" data-act="run_claude">${I.play}Claude</button>
      <button class="b-agy" data-act="run_agy">${I.play}agy</button>
      <button class="b-go" data-act="run_both">${I.play}Both</button>
      <button data-act="meet">${I.chat}Meeting</button>
      <button class="b-danger" data-act="stop">${I.stop}Stop</button>
      ${g.dirty ? `<button data-act="commit">Commit ${g.dirty}</button>` : ''}
      ${g.ahead ? `<button class="b-go" data-act="push">Push ${g.ahead}</button>` : ''}
      ${graphLink(p).replace('VIEW GRAPH', 'View graph')}
    </div>
    <div class="pv-cols">
      <div>
        <h4>The plan</h4>
        <div class="plan" style="max-height:none">${p.plan ? esc(p.plan) : '<span class="muted">No plan yet. Press Plan on the card to write one.</span>'}</div>
        <h4>Tasks <span class="num">(${t.done} done, ${t.doing} doing, ${t.todo} left)</span></h4>
        <div>${tasks}</div>
      </div>
      <div>
        <h4>Recent commits</h4>
        <div>${(p.commits || []).slice(0, 10).map(c => `<code class="line">${esc(c)}</code>`).join('') || '<div class="muted">none</div>'}</div>
        <h4>Planning meetings</h4>${links(p.talks, I.chat)}
        <h4>Work logs</h4>${links(p.logs, I.doc)}
      </div>
    </div>`;
}
function showOv(title) {
  setText($('#ovt'), title);
  const ov = $('#ov');
  if (ov.hidden) { ov.hidden = false; document.body.style.overflow = 'hidden'; $('#ovx').focus(); }
}
function openProject(name) {
  const p = DATA.find(x => x.name === name);
  if (!p) return;
  OV = {mode: 'project', name};
  const c = $('#ovc');
  c.dataset.p = name; c._html = null;
  setHTML(c, projectView(p));
  showOv(name);
}
async function openFile(f, label) {
  OV = {mode: 'file', name: label};
  const c = $('#ovc');
  delete c.dataset.p; c._html = null;
  c.innerHTML = '<pre>loading…</pre>';
  showOv(label);
  try {
    const r = await fetch('/file?p=' + encodeURIComponent(f));
    $('pre', c).textContent = await r.text();
  } catch (e) { $('pre', c).textContent = 'failed: ' + e; }
}
function refreshOverlay() {
  if (!OV || OV.mode !== 'project' || $('#ov').hidden) return;
  const p = DATA.find(x => x.name === OV.name);
  if (p) setHTML($('#ovc'), projectView(p));
}
function closeOv() {
  const ov = $('#ov');
  if (ov.hidden) return;
  ov.hidden = true; OV = null; document.body.style.overflow = '';
}

// ---------------------------------------------------------------- chat
function renderChat(n) {
  const c = CARDS.get(n);
  if (!c) return;
  const el = c.q.log;
  el.innerHTML = (CHAT[n] || []).map(m => `<div class="msg ${m.who}${m.err ? ' err' : ''}">
    <b>${m.who === 'you' ? 'YOU' : esc(m.who.toUpperCase())}</b><div>${esc(m.text)}</div></div>`).join('');
  el.scrollTop = el.scrollHeight;
}
async function converse(n, agent, question, body) {
  const pending = {who: agent, text: question === null ? 'reading the logs…' : 'thinking…'};
  (CHAT[n] = CHAT[n] || []).push({who: 'you', text: question || 'What happened in the logs?'}, pending);
  renderChat(n);
  try {
    const j = await post(body);
    pending.text = j.reply || j.msg || 'no reply';
    pending.err = !j.ok;
  } catch (e) { pending.text = 'failed: ' + e; pending.err = true; }
  renderChat(n);
}
function ask(n, agent) {
  const c = CARDS.get(n), t = c && c.q.q;
  if (!t || !t.value.trim()) return;
  const q = t.value.trim();
  t.value = '';
  converse(n, agent, q, {action: 'chat', project: n, agent, text: q});
}
function explain(n) { converse(n, 'claude', null, {action: 'explain', project: n}); }

// ---------------------------------------------------------------- scanner
async function scanRepos() {
  const box = $('#scanbox');
  box.hidden = false;
  box.innerHTML = '<div class="hint">scanning ~/Projects and ~ …</div>';
  try {
    const j = await post({action: 'scan'});
    const f = j.found || [];
    if (!f.length) { box.innerHTML = '<div class="hint">no new git repos found - everything is already added</div>'; return; }
    const mine = f.filter(x => x.yours), other = f.filter(x => !x.yours);
    const row = x => `<div class="found${x.yours ? '' : ' third'}">
      <span class="fname">${esc(x.name)}</span>
      <span class="fmeta">${esc(x.branch)}, ${esc(x.commits)} commits, ${x.owner ? esc(x.owner) : 'local only'}</span>
      <span class="fpath">${esc(x.path)}</span>
      <button class="b-go" data-do="addProject" data-path="${esc(x.path)}">ADD</button></div>`;
    box.innerHTML = `<div class="scanclose"><button data-do="toggleScan" aria-label="Close the project scanner">CLOSE</button></div>`
      + (mine.length ? `<h3 class="sec">Your repos (${mine.length})</h3>${mine.map(row).join('')}` : '')
      + (other.length ? `<h3 class="sec">Cloned from others (${other.length}) - probably not yours</h3>${other.map(row).join('')}` : '');
  } catch (e) { box.innerHTML = '<div class="hint">scan failed: ' + esc(String(e)) + '</div>'; }
}
async function addProject(path) {
  await act('add_project', null, {path});
  if (!$('#scanbox').hidden) scanRepos();
}

// ---------------------------------------------------------------- events
const DO = {
  toggleScan(btn) {
    const box = $('#scanbox'), opener = $('[data-do="toggleScan"][aria-controls]');
    if (!box.hidden) { box.hidden = true; opener.setAttribute('aria-expanded', 'false'); return; }
    opener.setAttribute('aria-expanded', 'true');
    scanRepos();
  },
  addManual() { const i = $('#newpath'); if (i.value.trim()) { addProject(i.value.trim()); i.value = ''; } },
  addProject(btn) { addProject(btn.dataset.path); },
  async addTask(btn, p) {
    const c = CARDS.get(p), i = c && c.q.task;
    if (!i || !i.value.trim()) return;
    const text = i.value;
    const j = await act('add_task', p, {text});
    if (j && j.ok && i.value === text) i.value = '';
  },
  toggle(btn, p) {
    const k = btn.dataset.panel, c = CARDS.get(p);
    if (!c) return;
    const on = OPEN[p + k] = !OPEN[p + k];
    c.q['panel_' + k].hidden = !on;
    btn.setAttribute('aria-expanded', String(on));
    if (k === 'det') {
      btn.textContent = on ? 'HIDE DETAILS' : 'DETAILS';
      if (on) setHTML(c.q.details, details(DATA.find(x => x.name === p)));
    }
    if (k === 'chat' && on) c.q.q.focus();
    if (k === 'pw' && on) c.q.plan_ta.focus();
  },
  ask(btn, p) { ask(p, btn.dataset.agent); },
  explain(btn, p) { explain(p); },
  async savePlan(btn, p) {
    const c = CARDS.get(p);
    const j = await act('save_plan', p, {text: c.q.plan_ta.value});
    if (j && j.ok) c.planDirty = false;
  },
  closeOv,
};
document.addEventListener('click', e => {
  const t = e.target.closest('[data-act],[data-do],[data-open],[data-file]');
  if (!t || t.disabled) return;
  const holder = t.closest('[data-p]');
  const p = holder ? holder.dataset.p : null;
  if (t.dataset.file) { openFile(t.dataset.file, t.dataset.label); return; }
  if (t.dataset.open) { openProject(t.dataset.open); return; }
  if (t.dataset.act) { runAct(t.dataset.act, p); return; }
  if (DO[t.dataset.do]) DO[t.dataset.do](t, p, e);
});
$('#ov').addEventListener('click', e => { if (e.target.id === 'ov') closeOv(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeOv(); return; }
  const t = e.target, holder = t.closest && t.closest('[data-p]');
  if (t.classList.contains('task-in') && e.key === 'Enter') DO.addTask(t, holder.dataset.p);
  else if (t.classList.contains('chat-in') && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) ask(holder.dataset.p, 'claude');
  else if (t.id === 'newpath' && e.key === 'Enter') DO.addManual();
});
document.addEventListener('change', e => {
  const s = e.target;
  if (s.matches('#models select')) act('set_model', null, {agent: s.dataset.agent, model: s.value});
});
$('#note').addEventListener('input', e => {
  const n = e.target;
  n._dirty = true;
  clearTimeout(n._t);
  n._t = setTimeout(async () => {
    try { await post({action: 'set_note', text: n.value}); SET.session_note = n.value; } catch (err) { /* retried on next edit */ }
    n._dirty = false;
  }, 900);
});

// ---------------------------------------------------------------- workspace
// Chat with the free models (providers.py picks the model for each tier),
// ask about an image, manage API keys, and generate images where a provider
// has proved it can.
const WS = {tier: 'big', log: [], image: null, busy: false, state: null, loaded: false, rows: new Map()};
const TIER_NOTE = {small: 'fast and cheap', big: 'strongest reasoning', vision: 'understands images'};

function showTab(t) {
  const ws = t === 'ws';
  $('#view-ops').hidden = ws;
  $('#view-ws').hidden = !ws;
  $('#tab-ops').setAttribute('aria-selected', String(!ws));
  $('#tab-ws').setAttribute('aria-selected', String(ws));
  try { history.replaceState(null, '', location.pathname + (ws ? '#workspace' : '')); } catch (e) { /* file view */ }
  if (ws && !WS.loaded) wsRefresh();
}
async function wsRefresh() {
  WS.loaded = true;
  try {
    const j = await post({action: 'ws_state'});
    if (!j.ok) { toast(j.msg || 'workspace failed to load', true); return; }
    WS.state = j;
    renderWsState();
  } catch (e) { toast('workspace: ' + e, true); }
}
function renderWsState() {
  const s = WS.state;
  setHTML($('#tiers'), ['small', 'big', 'vision'].map(t => {
    const v = s.tiers[t] || {};
    return `<div class="tierrow"><b>${t}</b>${v.provider
      ? `<span>${esc(v.provider)}/${esc(v.model)}</span>` : '<span class="none">nothing available</span>'}</div>`;
  }).join(''));
  renderTierHint();
  const box = $('#provs');
  for (const r of s.providers) {
    let row = WS.rows.get(r.id);
    if (!row) { row = makeProvRow(r); WS.rows.set(r.id, row); box.appendChild(row.el); }
    updateProvRow(row, r);
  }
  renderImageGen();
}
function renderTierHint() {
  const s = WS.state, v = s && s.tiers[WS.tier];
  setText($('#tierhint'), !s ? 'checking which models are available…'
    : v && v.provider ? `${WS.tier} (${TIER_NOTE[WS.tier]}) is answered by ${v.provider}/${v.model} right now`
    : `nothing can answer the ${WS.tier} tier - add a key or start Ollama`);
}
function makeProvRow(r) {
  const el = document.createElement('div');
  el.className = 'prow';
  el.dataset.prov = r.id;
  el.innerHTML = `<div class="prow-head"><b>${esc(r.label)}</b><span class="pill" data-k="pill"></span></div>
    <p class="hint">${esc(r.blurb)}${r.local ? '' : ` - <a href="${esc(r.signup)}" target="_blank" rel="noopener">get a key</a>`}</p>
    <div class="row">
      ${r.env ? `<input type="password" autocomplete="off" spellcheck="false" data-k="key"
        placeholder="paste ${esc(r.env)}${r.prefix ? ' (' + esc(r.prefix) + '…)' : ''}" aria-label="${esc(r.label)} API key">
      <button data-do="keySave">SAVE</button>` : ''}
      <button data-do="keyTest">TEST</button>
      ${r.image === 'untested' || r.image === 'verified' ? '<button data-do="imgTest">TEST IMAGE</button>' : ''}
    </div>
    <div class="tres" data-k="res"></div>`;
  const q = {};
  el.querySelectorAll('[data-k]').forEach(x => { q[x.dataset.k] = x; });
  return {el, q};
}
function updateProvRow(row, r) {
  const t = r.test;
  const pill = r.local ? (r.ready ? ['ok', 'RUNNING'] : ['warn', 'NOT RUNNING'])
    : r.cooling ? ['warn', `RESTING ${r.cooling}s`]
    : !r.has_key ? ['', 'NO KEY']
    : t && t.ok ? ['ok', 'WORKS'] : t ? ['bad', 'TEST FAILED'] : ['', 'KEY SET, UNTESTED'];
  row.q.pill.className = 'pill ' + pill[0];
  setText(row.q.pill, pill[1]);
  let html = '';
  if (t) {
    html = `<div>${t.ok ? 'tested' : 'failed'} ${esc(t.at || '')}: ${esc(t.msg)}</div>`;
    if (t.tiers && Object.keys(t.tiers).length) {
      html += '<div class="tt">' + ['small', 'big', 'vision'].map(x => `${x}: ${esc(t.tiers[x] || 'none')}`).join(', ') + '</div>';
    }
  }
  if (r.image === 'verified') html += `<div>image generation works: ${esc(r.image_note)}</div>`;
  else if (r.image === 'never') html += `<div class="tt">${esc(r.image_note)}</div>`;
  else if (r.image === 'untested' && r.image_note) html += `<div class="tt">last image test failed: ${esc(r.image_note)}</div>`;
  row.q.res.className = 'tres' + (t && !t.ok ? ' bad' : '');
  setHTML(row.q.res, html);
}
function renderImageGen() {
  const rows = WS.state.providers || [], box = $('#imggen');
  const ok = rows.filter(r => r.image === 'verified');
  if (!ok.length) {
    box._ready = null;
    const cands = rows.filter(r => r.image === 'untested').map(r => r.label).join(' or ');
    setHTML(box, `<p class="hint">No provider has produced an image on this machine yet, so nothing is offered.
      Save a ${esc(cands)} key, then press TEST IMAGE next to it. Ollama refuses image models over its API,
      so it is not an option.</p>`);
    return;
  }
  const sig = ok.map(r => r.id + r.image_note).join();
  if (box._ready === sig) return;            // keep a half-typed prompt
  box._ready = sig; box._html = null;
  box.innerHTML = `<div class="imgrow">
    <select id="imgprov" aria-label="Image provider">${ok.map(r =>
      `<option value="${esc(r.id)}">${esc(r.label)}: ${esc((r.image_note || '').replace(/ returned an image$/, ''))}</option>`).join('')}</select>
    <textarea id="imgprompt" rows="2" placeholder="describe the image you want"></textarea>
    <div class="row"><button class="b-go" data-do="imgMake">GENERATE</button></div></div>`;
}
function renderWsLog() {
  const el = $('#wslog');
  el.innerHTML = WS.log.map(m => {
    const ext = m.image ? (m.image.match(/^data:image\/(\w+)/) || [, 'png'])[1].replace('jpeg', 'jpg') : '';
    return `<div class="wmsg${m.role === 'user' ? '' : ' ai'}${m.err ? ' err' : ''}">
      <div class="who2">${m.err ? 'ERROR' : m.role === 'user' ? 'YOU' : 'MODEL'}</div>
      ${m.content ? `<div class="body">${esc(m.content)}</div>` : ''}
      ${m.image ? `<img src="${esc(m.image)}" alt="${m.role === 'user' ? 'Attached image' : 'Generated image'}">` : ''}
      ${m.model ? `<div class="meta">answered by ${esc(m.model)}${m.tier ? ' (' + esc(m.tier) + ' tier)' : ''}</div>` : ''}
      ${m.kind === 'gen' && m.image && m.role !== 'user' ? `<a class="btn" href="${esc(m.image)}" download="agx-image.${ext}">SAVE IMAGE</a>` : ''}
    </div>`;
  }).join('');
  el.scrollTop = el.scrollHeight;
}
async function wsSend() {
  if (WS.busy) { toast('wait for the reply first', true); return; }
  const ta = $('#wsq'), text = ta.value.trim(), image = WS.image && WS.image.url;
  if (!text && !image) return;
  const history = WS.log.filter(m => !m.err && !m.pending && m.kind === 'text' && m.content)
    .slice(-14).map(m => ({role: m.role, content: m.content}));
  const pending = {role: 'assistant', content: image ? 'looking at the image…' : 'thinking…', pending: true};
  WS.log.push({role: 'user', content: text, image, kind: 'text'}, pending);
  ta.value = '';
  unattach();
  renderWsLog();
  WS.busy = true;
  try {
    const j = await post({action: 'ws_chat', tier: WS.tier, image,
                          messages: history.concat([{role: 'user', content: text}])});
    Object.assign(pending, j.ok ? {content: j.reply, model: j.model, tier: j.tier, kind: 'text'}
                                : {content: j.msg || 'failed', err: true});
  } catch (e) { Object.assign(pending, {content: 'request failed: ' + e, err: true}); }
  pending.pending = false;
  WS.busy = false;
  renderWsLog();
}
function readURL(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result); r.onerror = () => rej(r.error);
    r.readAsDataURL(file);
  });
}
// Phone photos run to several megabytes; models see 1600px just as well.
async function shrink(file) {
  const url = await readURL(file);
  const img = await new Promise((res, rej) => {
    const i = new Image(); i.onload = () => res(i); i.onerror = () => rej(new Error('not an image')); i.src = url;
  });
  const scale = Math.min(1, 1600 / Math.max(img.naturalWidth, img.naturalHeight));
  if (scale === 1 && file.size <= 2.5e6) return url;
  const c = document.createElement('canvas');
  c.width = Math.round(img.naturalWidth * scale); c.height = Math.round(img.naturalHeight * scale);
  const g = c.getContext('2d');
  g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);
  g.drawImage(img, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', 0.9);
}
async function attachFile(file) {
  if (!file) return;
  if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) { toast('attach a PNG, JPEG, WebP or GIF image', true); return; }
  try {
    WS.image = {url: await shrink(file), name: file.name || 'pasted image'};
  } catch (e) { toast('could not read the image: ' + e, true); return; }
  const a = $('#attach');
  $('img', a).src = WS.image.url;
  setText($('span', a), `${WS.image.name} - questions about it go to the vision tier`);
  a.hidden = false;
}
function unattach() { WS.image = null; $('#attach').hidden = true; $('#attach img').removeAttribute('src'); }
async function wsCall(btn, body, after) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'WORKING…';
  try {
    const j = await post(body);
    toast(j.msg || (j.ok ? 'done' : 'failed'), !j.ok);
    if (after) after(j);
  } catch (e) { toast('request failed: ' + e, true); }
  btn.disabled = false; btn.textContent = label;
  await wsRefresh();
}
Object.assign(DO, {
  tab(btn) { showTab(btn.dataset.tab); },
  tier(btn) {
    WS.tier = btn.dataset.tier;
    document.querySelectorAll('.seg button').forEach(b => b.setAttribute('aria-checked', String(b === btn)));
    renderTierHint();
  },
  wsSend,
  pickImage() { $('#wsfile').click(); },
  unattach,
  wsClear() { WS.log = []; renderWsLog(); },
  keySave(btn) {
    const row = btn.closest('.prow'), input = $('input', row), key = input.value.trim();
    if (!key) { input.focus(); return; }
    wsCall(btn, {action: 'ws_key_save', provider: row.dataset.prov, key}, j => { if (j.ok) input.value = ''; });
  },
  keyTest(btn) { wsCall(btn, {action: 'ws_key_test', provider: btn.closest('.prow').dataset.prov}); },
  imgTest(btn) {
    const prov = btn.closest('.prow').dataset.prov;
    wsCall(btn, {action: 'ws_image_test', provider: prov}, j => {
      if (j.ok && j.image) {
        WS.log.push({role: 'assistant', content: 'Image generation test', image: j.image,
                     model: prov + '/' + j.model, kind: 'gen'});
        renderWsLog();
      }
    });
  },
  async imgMake(btn) {
    const ta = $('#imgprompt'), prompt = ta.value.trim(), provider = $('#imgprov').value;
    if (!prompt) { ta.focus(); return; }
    const pending = {role: 'assistant', content: 'generating…', pending: true, kind: 'gen'};
    WS.log.push({role: 'user', content: 'Generate: ' + prompt, kind: 'gen'}, pending);
    renderWsLog();
    btn.disabled = true;
    try {
      const j = await post({action: 'ws_image', provider, prompt});
      Object.assign(pending, j.ok ? {content: '', image: j.image, model: j.model} : {content: j.msg, err: true});
    } catch (e) { Object.assign(pending, {content: 'request failed: ' + e, err: true}); }
    pending.pending = false; btn.disabled = false;
    renderWsLog();
  },
});
$('#wsfile').addEventListener('change', e => { attachFile(e.target.files[0]); e.target.value = ''; });
$('#wsq').addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); wsSend(); } });
$('#wsq').addEventListener('paste', e => {
  const f = [...(e.clipboardData || {}).files || []].find(x => x.type.startsWith('image/'));
  if (f) { e.preventDefault(); attachFile(f); }
});
if (location.hash === '#workspace') showTab('ws');

// ---------------------------------------------------------------- refresh
async function tick() {
  if (TICKING) return;
  TICKING = true;
  try {
    const r = await fetch('/api');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const raw = await r.json();
    DATA = raw.projects || []; MODELS = raw.models || {}; SET = raw.settings || {};
    renderModels(); renderHud(); renderFeed(); renderGroups(); refreshOverlay();
    setText($('#sub'), `${DATA.length} projects, updated ${new Date().toLocaleTimeString()}`);
  } catch (e) {
    setText($('#sub'), 'dashboard unreachable - retrying');
  } finally { TICKING = false; }
}
tick();
setInterval(tick, 4000);
