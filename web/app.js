'use strict';
// AGX dashboard. Everything is built once and the 4-second refresh patches
// only what changed, so typed text, open panes and chat history survive it.
// Each project's detail view is kept after it is built, so switching projects
// does not lose a half-typed task, a draft plan or the conversation.

const TOKEN = document.querySelector('meta[name="agx-token"]').content;
const HDR = {'Content-Type': 'application/json', 'X-AGX-Token': TOKEN};
const NAME = {claude: 'Claude', agy: 'agy'};
const GROUPS = [['running', 'Working now'], ['stalled', 'Needs you'],
  ['waiting', 'Ready for an agent'], ['idle', 'Out of work'], ['paused', 'Paused']];
const STATE = {working: 'Working', idle: 'Idle', done: 'Done', nocommit: 'No commit',
  stopped: 'Stopped', error: 'Error', waiting: 'Waiting for the project'};
const WIDE = matchMedia('(min-width: 901px)');

let DATA = [], MODELS = {}, SET = {}, BUSY = false, TICKING = false, LOADED = false;
let SEL = null;              // project picked in the address (#/p/name)
const CHAT = {};             // project -> [{who, text, err}]
const PROJ = new Map();      // project -> {el, q, planDirty, pane, editing}
const ROWS = new Map();      // project -> list button
const GROUP_H = {};          // status -> list heading

const $ = (s, r = document) => r.querySelector(s);
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
function setHTML(el, html) { if (el && el._html !== html) { el.innerHTML = html; el._html = html; } }
function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;
const short = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s || '');
const byName = n => DATA.find(p => p.name === n);

async function post(body) {
  const r = await fetch('/action', {method: 'POST', headers: HDR, body: JSON.stringify(body)});
  if (r.status === 401 || r.status === 403) {
    const j = await r.json().catch(() => ({}));
    return {ok: false, msg: j.msg || 'Not signed in. Reload the page.'};
  }
  return r.json();
}
function toast(m, bad) {
  const t = $('#toast');
  t.textContent = m; t.className = bad ? 'err' : ''; t.style.display = 'block';
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.style.display = 'none'; }, 6500);
}

// One server action at a time. Only action buttons lock; navigation, panes
// and chat stay usable while an action runs.
function lockActions(on) { document.querySelectorAll('button[data-act]').forEach(b => { b.disabled = on; }); }
async function act(a, p, x) {
  if (BUSY) { toast('One action at a time. Wait for the last one to finish.', true); return null; }
  BUSY = true; lockActions(true);
  let j = null;
  try {
    j = await post(Object.assign({action: a, project: p}, x || {}));
    toast(j.msg || (j.ok ? 'Done' : 'Failed'), !j.ok);
  } catch (e) { toast('Request failed: ' + e, true); }
  BUSY = false; lockActions(false); tick();
  return j;
}
function runAct(a, p) {
  if (a === 'stop_all' && !confirm('Stop every agent and turn off the timers?')) return;
  if (a === 'push' && !confirm('Push this branch to GitHub? This publishes the commits.')) return;
  if (a === 'commit') {
    const m = prompt('Commit message. Leave it blank and one is written from the diff.', '');
    if (m === null) return;
    act('commit', p, {text: m});
    return;
  }
  act(a, p);
}

// ---------------------------------------------------------------- routing
function route() {
  const raw = location.hash.slice(1);
  if (raw === '/workspace') { showView('ws'); return; }
  showView('ops');
  SEL = raw.startsWith('/p/') ? decodeURIComponent(raw.slice(3)) : null;
  if (!LOADED) return;                       // the first refresh renders
  renderList(); renderDetail();
  if (SEL && !WIDE.matches) $('#detail').scrollIntoView({block: 'start'});
}
function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
function showView(v) {
  const ws = v === 'ws';
  $('#view-ops').hidden = ws; $('#view-ws').hidden = !ws;
  $('#tab-ops').setAttribute('aria-selected', String(!ws));
  $('#tab-ws').setAttribute('aria-selected', String(ws));
  if (ws && !WS.loaded) wsRefresh();
}
// The project shown in the detail: the one in the address, or on a wide
// screen the first in the list. On a phone, no address means the list.
function current() {
  if (SEL && byName(SEL)) return SEL;
  return WIDE.matches && DATA.length ? DATA[0].name : null;
}

// ---------------------------------------------------------------- words
function working(p) {
  return ['claude', 'agy'].filter(a => p.agents[a] && p.agents[a].state === 'working' && p.agents[a].running);
}
function statusText(p) {
  const w = working(p);
  if (p.status === 'running') return w.length > 1 ? 'Both working' : w.length ? `${NAME[w[0]]} working` : 'Working';
  if (p.status === 'stalled') return 'Claimed, nobody working';
  if (p.status === 'waiting') return `${p.tasks.todo} waiting`;
  if (p.status === 'paused') return 'Paused';
  return 'No work left';
}
const statusClass = p => (p.status === 'running' ? 'is-ok' : p.status === 'stalled' ? 'is-warn' : '');

// ---------------------------------------------------------------- hero
function renderHero() {
  const crew = [];
  DATA.forEach(p => working(p).forEach(a => crew.push({a, p: p.name, t: p.agents[a].task})));
  let html;
  if (!DATA.length) html = 'No projects yet. Add one to put the crew to work.';
  else if (!crew.length) html = 'Nobody is working right now.';
  else {
    html = crew.slice(0, 2).map(w => `<span class="t-${w.a}">${NAME[w.a]}</span> is working on ${esc(w.p)}`
      + (w.t ? `: <span class="task">${esc(short(w.t, 80))}</span>` : '') + '.').join(' ');
    if (crew.length > 2) html += ` ${crew.length - 2} more running.`;
    const free = ['claude', 'agy'].filter(a => !crew.some(w => w.a === a));
    if (free.length === 1) html += ` <span class="t-${free[0]}">${NAME[free[0]]}</span> is free.`;
  }
  setHTML($('#now'), html);
  if (!DATA.length) { setHTML($('#totals'), ''); return; }
  const done = DATA.reduce((n, p) => n + p.tasks.done, 0);
  const left = DATA.reduce((n, p) => n + p.tasks.todo, 0);
  const fail = DATA.reduce((n, p) => n + ((p.work || {}).failed || 0), 0);
  const stalled = DATA.filter(p => p.status === 'stalled');
  let t = `${plural(left, 'task')} waiting across ${plural(DATA.length, 'project')}, ${done} done so far.`;
  if (!crew.length && left) t += ' Auto cycle puts the crew to work.';
  if (!left && !crew.length) t += ' Hold a meeting on a project to plan more.';
  if (stalled.length) t += ` <span class="is-warn">${stalled.map(p => esc(p.name)).join(', ')} ${stalled.length > 1 ? 'have' : 'has'} a claimed task nobody is working on.</span>`;
  if (fail) t += ` <span class="is-bad">${plural(fail, 'run')} ended without a commit.</span>`;
  setHTML($('#totals'), t);
}

// ---------------------------------------------------------------- project list
// Put el at position i of parent, keeping focus if it moves between groups.
function put(el, parent, i) {
  if (parent.children[i] === el) return;
  const a = document.activeElement, inside = a && el.contains(a);
  parent.insertBefore(el, parent.children[i] || null);
  if (inside) a.focus({preventScroll: true});
}
function renderList() {
  const nav = $('#plist');
  if (!DATA.length) { setHTML(nav, '<p class="group-h">Nothing here yet.</p>'); ROWS.clear(); return; }
  if (nav._html) { nav.innerHTML = ''; nav._html = null; }
  const cur = current();
  let at = 0;
  const seen = new Set();
  for (const [g, label] of GROUPS) {
    const items = DATA.filter(p => p.status === g);
    if (!GROUP_H[g]) {
      GROUP_H[g] = document.createElement('h3');
      GROUP_H[g].className = 'group-h g-' + g;
      GROUP_H[g].textContent = label;
    }
    if (!items.length) { GROUP_H[g].remove(); continue; }
    put(GROUP_H[g], nav, at++);
    for (const p of items) {
      seen.add(p.name);
      let b = ROWS.get(p.name);
      if (!b) {
        b = document.createElement('button');
        b.className = 'pick';
        b.dataset.sel = p.name;
        b.innerHTML = '<span class="nm"></span><span class="st"></span><span class="meter"><i></i></span>';
        ROWS.set(p.name, b);
      }
      setText($('.nm', b), p.name);
      const st = $('.st', b);
      setText(st, statusText(p));
      st.className = 'st ' + statusClass(p);
      const tot = p.tasks.done + p.tasks.doing + p.tasks.todo;
      $('i', b).style.width = (tot ? p.tasks.done / tot * 100 : 0) + '%';
      b.setAttribute('aria-current', String(p.name === cur));
      b.setAttribute('aria-label', `${p.name}, ${statusText(p)}, ${p.tasks.done} of ${tot} tasks done`);
      put(b, nav, at++);
    }
  }
  for (const [n, b] of ROWS) if (!seen.has(n)) { b.remove(); ROWS.delete(n); }
}

// ---------------------------------------------------------------- one project
function gitLine(p) {
  const g = p.github || {};
  if (!g.connected) return '<p>Not connected to GitHub. Work stays on this machine.</p>';
  const repo = g.web.replace(/^https?:\/\/(www\.)?github\.com\//, '');
  const bits = [];
  if (g.ahead) bits.push(`<span class="is-warn">${plural(g.ahead, 'commit')} to push</span>`);
  if (g.behind) bits.push(`<span class="is-warn">${g.behind} behind GitHub</span>`);
  if (g.dirty) bits.push(`<span class="is-warn">${plural(g.dirty, 'uncommitted file')}</span>`);
  return `<p>On <a href="${esc(g.web)}" target="_blank" rel="noopener">${esc(repo)}</a>, branch
    <b>${esc(g.branch)}</b>: ${bits.length ? bits.join(', ') : 'in sync with GitHub'}.</p>`
    + (g.dirty || g.ahead ? `<div class="row">
        ${g.dirty ? `<button data-act="commit">Commit ${plural(g.dirty, 'file')}</button>` : ''}
        ${g.ahead ? `<button class="primary" data-act="push">Push ${plural(g.ahead, 'commit')}</button>` : ''}</div>` : '');
}
function agentRow(a, s) {
  const on = !!(s && s.running && s.state === 'working');
  const st = s ? (STATE[s.state] || s.state) : 'Never run';
  const bad = s && (s.state === 'error' || s.state === 'nocommit') ? ' is-bad' : '';
  return `<div class="agent${on ? ' on' : ''}"><span class="who t-${a}">${NAME[a]}</span>
    <span class="st${bad}">${esc(st)}</span><span class="task">${s && s.task ? esc(s.task) : ''}</span></div>`;
}
function stats(p) {
  const t = p.tasks, w = p.work || {}, tot = t.done + t.doing + t.todo;
  const pct = n => (tot ? (n / tot * 100).toFixed(1) : 0) + '%';
  return `<div class="meterbar" role="img" aria-label="${t.done} done, ${t.doing} in progress, ${t.todo} left">
      <span class="d" style="width:${pct(t.done)}"></span><span class="w" style="width:${pct(t.doing)}"></span></div>
    <p><b>${t.done}</b> done, <b>${t.doing}</b> in progress, <b>${t.todo}</b> left.
      <span class="t-claude">Claude</span> committed in ${plural(w.claude || 0, 'run')} and
      <span class="t-agy">agy</span> in ${w.agy || 0}${w.failed ? `; <span class="is-bad">${plural(w.failed, 'run')} ended without a commit</span>` : ''}.
      ${p.graph.up ? `Graph memory holds ${plural(p.graph.facts, 'fact')}.` : '<span class="is-bad">Graph memory is offline.</span>'}</p>`;
}
function fileButtons(arr) {
  return arr.map(x => `<button class="file" data-file="${esc(x.file)}" data-label="${esc(x.label)}">${esc(x.label)}</button>`).join('');
}
function details(p) {
  const out = (p.talks.length ? `<h3>Planning meetings</h3>${fileButtons(p.talks)}` : '')
    + (p.logs.length ? `<h3>Work logs</h3>${fileButtons(p.logs)}` : '')
    + (p.commits.length ? `<h3>Recent commits</h3><ol class="commits">${p.commits.map(c => `<li class="code">${esc(c)}</li>`).join('')}</ol>` : '');
  return out || '<p class="hint">No meetings, logs or commits yet.</p>';
}
function taskList(p) {
  const items = (p.all_tasks || []).map(x => {
    const cls = x.state === 'x' ? 'done' : x.state === '~' ? 'doing' : '';
    const mark = x.state === 'x' ? '✓' : x.state === '~' ? '▶' : '○';
    const what = x.state === 'x' ? 'Done: ' : x.state === '~' ? 'In progress: ' : '';
    return `<li class="${cls}"><span class="m" aria-hidden="true">${mark}</span><span><span class="sr">${what}</span>${esc(x.text)}</span></li>`;
  }).join('');
  return items ? `<ol class="tasklist">${items}</ol>` : '<p class="hint">No tasks yet. Hold a meeting, or add one above.</p>';
}
function makeProject(name) {
  const n = esc(name);
  const el = document.createElement('article');
  el.className = 'proj';
  el.dataset.p = name;
  el.innerHTML = `
    <button class="back plain" data-do="back">All projects</button>
    <header><h2>${n}</h2><p class="state" data-k="state"></p><p class="path code" data-k="path"></p></header>
    <div class="git" data-k="gh"></div>
    <div class="row act" role="group" aria-label="Agents on ${n}">
      <button class="b-claude" data-act="run_claude">Claude</button>
      <button class="b-agy" data-act="run_agy">agy</button>
      <button class="primary" data-act="run_both">Both</button>
      <button data-act="meet">Meeting</button>
      <button class="danger" data-act="stop">Stop</button>
    </div>
    <div class="crew" data-k="agents"></div>
    <div class="progress" data-k="stats"></div>
    <div class="next" data-k="next"></div>
    <div class="row addtask">
      <input type="text" class="task-in grow" data-k="task" placeholder="Add a task to the backlog" aria-label="New task for ${n}">
      <button data-do="addTask">Add</button>
    </div>
    <div class="tabs2" role="tablist" aria-label="${n}">
      <button role="tab" data-do="pane" data-pane="plan" aria-selected="true">Plan</button>
      <button role="tab" data-do="pane" data-pane="tasks" aria-selected="false">Tasks</button>
      <button role="tab" data-do="pane" data-pane="ask" aria-selected="false">Ask</button>
      <button role="tab" data-do="pane" data-pane="det" aria-selected="false">Details</button>
    </div>
    <div class="pane" data-k="pane_plan" role="tabpanel">
      <div class="plantext" data-k="plan"></div>
      <button data-do="editPlan" aria-expanded="false">Edit plan</button>
      <div data-k="editor" hidden>
        <label for="plan-${n}" class="sr">PLAN.md, which the agents obey</label>
        <p class="hint">PLAN.md. The agents obey it above everything else.</p>
        <textarea id="plan-${n}" data-k="plan_ta" rows="12"></textarea>
        <button class="primary" data-do="savePlan">Save plan</button>
      </div>
    </div>
    <div class="pane" data-k="pane_tasks" role="tabpanel" hidden><div data-k="tasks"></div></div>
    <div class="pane" data-k="pane_ask" role="tabpanel" hidden>
      <div class="chatlog" data-k="log" aria-live="polite"></div>
      <label for="q-${n}" class="sr">Question about ${n}</label>
      <textarea id="q-${n}" class="chat-in" data-k="q" rows="3" placeholder="What did you change? Why is agy failing? What is left?"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="b-claude" data-do="ask" data-agent="claude">Ask Claude</button>
        <button class="b-agy" data-do="ask" data-agent="agy">Ask agy</button>
        <button data-do="explain">What happened?</button>
      </div>
      <p class="hint" style="margin-top:8px">Ctrl+Enter asks Claude. Replies take 10 to 60 seconds.</p>
    </div>
    <div class="pane" data-k="pane_det" role="tabpanel" hidden><div data-k="details"></div></div>
    <div class="row graphrow">
      <button data-act="graphify">Build graph</button>
      <span data-k="graphlink"></span>
    </div>`;
  const q = {};
  el.querySelectorAll('[data-k]').forEach(x => { q[x.dataset.k] = x; });
  const c = {el, q, planDirty: false, pane: 'plan', editing: false};
  q.plan_ta.addEventListener('input', () => { c.planDirty = true; });
  return c;
}
function updateProject(c, p) {
  const q = c.q;
  setText(q.state, statusText(p));
  q.state.className = 'state ' + statusClass(p);
  setText(q.path, p.path);
  setHTML(q.gh, gitLine(p));
  setHTML(q.agents, agentRow('claude', p.agents.claude) + agentRow('agy', p.agents.agy));
  setHTML(q.stats, stats(p));
  setHTML(q.next, p.tasks.todo ? `<p><span>Next up:</span> ${esc(p.next_task)}</p>`
    : '<p class="quiet">Nothing waiting. A meeting plans the next tasks.</p>');
  setHTML(q.plan, p.plan ? esc(p.plan)
    : '<span class="none">No plan yet, so the agents decide for themselves. Edit the plan to tell them what you want.</span>');
  q.plan.classList.toggle('none', !p.plan);
  if (c.pane === 'tasks') setHTML(q.tasks, taskList(p));
  if (c.pane === 'det') setHTML(q.details, details(p));
  setHTML(q.graphlink, p.codegraph.built
    ? `<a class="btn" href="/graph?p=${encodeURIComponent(p.name)}" target="_blank" rel="noopener">View graph (${p.codegraph.nodes} nodes)</a>` : '');
  // The plan editor follows the file until you start typing in it.
  if (!c.planDirty && document.activeElement !== q.plan_ta && q.plan_ta.value !== (p.plan_raw || '')) {
    q.plan_ta.value = p.plan_raw || '';
  }
  if (BUSY) lockActions(true);
}
function renderDetail() {
  const box = $('#detail');
  if (!DATA.length) {
    setHTML(box, `<div class="empty"><h2>Give the crew a project</h2>
      <p>Press Add project above and pick one of your git repos, or run
      <code>bash ~/agent-team/add-project.sh /path/to/repo</code>.</p></div>`);
    return;
  }
  const name = current();
  document.body.classList.toggle('one', !!(SEL && byName(SEL)));
  if (!name) return;
  let c = PROJ.get(name);
  if (!c) { c = makeProject(name); PROJ.set(name, c); }
  if (box.firstElementChild !== c.el) { box.replaceChildren(c.el); box._html = null; }
  updateProject(c, byName(name));
}

// ---------------------------------------------------------------- side column
function renderFeed() {
  const rows = [];
  DATA.forEach(p => working(p).forEach(a => rows.push({k: 0, a, p: p.name, t: p.agents[a].task || 'working'})));
  DATA.forEach(p => { if (p.status === 'waiting' && p.next_task) rows.push({k: 1, a: '', p: p.name, t: p.next_task}); });
  DATA.forEach(p => (p.progress || []).slice(-1).forEach(l => {
    const m = l.match(/\|\s*(claude|agy)\s*\|\s*(.*)$/);
    if (m) rows.push({k: 2, a: m[1], p: p.name, t: m[2]});
  }));
  rows.sort((x, y) => x.k - y.k);
  const who = a => `<span class="t-${a}">${NAME[a]}</span>`;
  const head = r => (r.k === 0 ? `${who(r.a)} working in ${esc(r.p)}`
    : r.k === 1 ? `Next up in ${esc(r.p)}` : `Done by ${who(r.a)} in ${esc(r.p)}`);
  setHTML($('#feed'), rows.length ? rows.slice(0, 8).map(r => `<li>
      <span class="k${r.k ? '' : ' is-ok'}">${head(r)}</span>${esc(r.t)}</li>`).join('')
    : '<li class="quiet">Nothing has happened yet.</li>');
}
function renderModels() {
  const box = $('#models');
  const sig = JSON.stringify(MODELS);
  if (box._sig !== sig) {
    box._sig = sig;
    box.innerHTML = ['claude', 'agy'].map(a => `<div class="mrow">
      <label for="m-${a}" class="t-${a}">${NAME[a]}</label>
      <select id="m-${a}" data-agent="${a}">
        <option value="">Its default model</option>
        ${(MODELS[a] || []).map(m => `<option value="${esc(m.id)}">${esc(m.label)}${m.note ? ', ' + esc(m.note) : ''}</option>`).join('')}
      </select></div>`).join('');
  }
  ['claude', 'agy'].forEach(a => {
    const s = $('#m-' + a), cur = SET['model_' + a] || '';
    if (!s || document.activeElement === s || s.value === cur) return;
    // A saved model the CLI no longer lists still shows, instead of a blank box.
    if (cur && ![...s.options].some(o => o.value === cur)) s.add(new Option(cur + ' (saved, not listed now)', cur));
    s.value = cur;
  });
  const n = $('#note');
  if (document.activeElement !== n && !n._dirty && n.value !== (SET.session_note || '')) { n.value = SET.session_note || ''; grow(n); }
}
function grow(t) { t.style.height = 'auto'; t.style.height = t.scrollHeight + 2 + 'px'; }

// ---------------------------------------------------------------- overlay
async function openFile(f, label) {
  const c = $('#ovc');
  c.innerHTML = '<pre>Loading…</pre>';
  setText($('#ovt'), label);
  const ov = $('#ov');
  if (ov.hidden) { ov.hidden = false; document.body.style.overflow = 'hidden'; $('#ovx').focus(); }
  try {
    const r = await fetch('/file?p=' + encodeURIComponent(f));
    $('pre', c).textContent = await r.text();
  } catch (e) { $('pre', c).textContent = 'Could not load it: ' + e; }
}
function closeOv() {
  const ov = $('#ov');
  if (ov.hidden) return;
  ov.hidden = true; document.body.style.overflow = '';
}

// ---------------------------------------------------------------- ask the agents
function renderChat(n) {
  const c = PROJ.get(n);
  if (!c) return;
  const el = c.q.log;
  el.innerHTML = (CHAT[n] || []).map(m => `<div class="msg ${m.who}${m.err ? ' err' : ''}">
    <div class="by">${m.who === 'you' ? 'You' : esc(NAME[m.who] || m.who)}</div><div class="body">${esc(m.text)}</div></div>`).join('');
  el.scrollTop = el.scrollHeight;
}
async function converse(n, agent, question, body) {
  const pending = {who: agent, text: question === null ? 'Reading the logs…' : 'Thinking…'};
  (CHAT[n] = CHAT[n] || []).push({who: 'you', text: question || 'What happened in the logs?'}, pending);
  renderChat(n);
  try {
    const j = await post(body);
    pending.text = j.reply || j.msg || 'No reply.';
    pending.err = !j.ok;
  } catch (e) { pending.text = 'Request failed: ' + e; pending.err = true; }
  renderChat(n);
}
function ask(n, agent) {
  const c = PROJ.get(n), t = c && c.q.q;
  if (!t || !t.value.trim()) { if (t) t.focus(); return; }
  const q = t.value.trim();
  t.value = '';
  converse(n, agent, q, {action: 'chat', project: n, agent, text: q});
}

// ---------------------------------------------------------------- add a project
async function scanRepos() {
  const box = $('#scanlist');
  box.innerHTML = '<p class="hint" style="margin-top:14px">Looking through ~/Projects and your home folder…</p>';
  try {
    const j = await post({action: 'scan'});
    const f = j.found || [];
    if (!f.length) { box.innerHTML = '<p class="hint" style="margin-top:14px">No new git repos found. Everything is already added.</p>'; return; }
    const mine = f.filter(x => x.yours), other = f.filter(x => !x.yours);
    const row = x => `<div class="found${x.yours ? '' : ' theirs'}">
      <b>${esc(x.name)}</b>
      <span class="meta">${esc(x.branch)} branch, ${plural(+x.commits || 0, 'commit')}, ${x.owner ? 'owner ' + esc(x.owner) : 'local only'}</span>
      <span class="meta code">${esc(x.path)}</span>
      <button data-do="addProject" data-path="${esc(x.path)}">Add</button></div>`;
    box.innerHTML = (mine.length ? `<h3>Your repos</h3>${mine.map(row).join('')}` : '')
      + (other.length ? `<h3>Cloned from other people</h3>${other.map(row).join('')}` : '');
  } catch (e) { box.innerHTML = '<p class="hint">The scan failed: ' + esc(String(e)) + '</p>'; }
}
async function addProject(path) {
  await act('add_project', null, {path});
  if (!$('#scan').hidden) scanRepos();
}

// ---------------------------------------------------------------- workspace
// Chat with the free models (providers.py picks the model for each size),
// ask about an image, manage API keys, and make images where a provider has
// proved it can.
const WS = {tier: 'big', model: '', log: [], image: null, busy: false, state: null, loaded: false, rows: new Map()};
const TIER_NOTE = {small: 'fast and cheap', big: 'strongest reasoning', vision: 'understands images'};

async function wsRefresh() {
  WS.loaded = true;
  try {
    const j = await post({action: 'ws_state'});
    if (!j.ok) { toast(j.msg || 'The workspace did not load.', true); return; }
    WS.state = j;
    renderWsState();
  } catch (e) { toast('Workspace: ' + e, true); }
}
function renderWsState() {
  const s = WS.state;
  setHTML($('#tiers'), ['small', 'big', 'vision'].map(t => {
    const v = s.tiers[t] || {};
    return `<div><span>${t[0].toUpperCase() + t.slice(1)}</span>${v.provider
      ? `<span class="code" style="color:var(--paper)">${esc(v.provider)}/${esc(v.model)}</span>`
      : '<span class="none">Nothing available</span>'}</div>`;
  }).join(''));
  renderModelPicker();
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
  setText($('#tierhint'), !s ? 'Checking which models are available…'
    : WS.model ? `You picked ${WS.model.replace('|', '/')}. The size buttons apply to Auto only.`
    : v && v.provider ? `${WS.tier[0].toUpperCase() + WS.tier.slice(1)} (${TIER_NOTE[WS.tier]}) is answered by ${v.provider}/${v.model} right now.`
    : `Nothing can answer at this size. Add a key or start Ollama.`);
}
// Every model a ready provider offers, grouped by provider, so a specific one
// (say NVIDIA's newest GLM) can be picked by name.
function renderModelPicker() {
  const sel = $('#wsmodel'), list = WS.state.models || [];
  const groups = {};
  list.forEach(m => { (groups[m.label] = groups[m.label] || []).push(m); });
  setHTML(sel, '<option value="">Auto: the best free model for this size</option>'
    + Object.entries(groups).map(([label, ms]) => `<optgroup label="${esc(label)}">${ms.map(m =>
      `<option value="${esc(m.provider + '|' + m.model)}">${esc(m.model)} (${esc(m.tiers.join(', '))})</option>`).join('')}</optgroup>`).join(''));
  if (WS.model && !list.some(m => m.provider + '|' + m.model === WS.model)) WS.model = '';
  if (sel.value !== WS.model) sel.value = WS.model;
  const cloud = (WS.state.providers || []).some(r => !r.local && r.ready);
  const hint = $('#cloudhint');
  hint.hidden = cloud;
  setText(hint, 'Only models on this machine are listed. A free NVIDIA key (build.nvidia.com) adds GLM-5.x, '
    + 'Kimi K3 and Qwen3 Coder 480B. Paste it under Models and keys.');
}
function makeProvRow(r) {
  const el = document.createElement('details');
  el.className = 'prov';
  el.dataset.prov = r.id;
  el.innerHTML = `<summary><b>${esc(r.label)}</b><span class="pill" data-k="pill"></span></summary>
    <div class="in">
      <p class="hint">${esc(r.blurb)}.${r.local ? '' : ` <a href="${esc(r.signup)}" target="_blank" rel="noopener">Get a key</a>`}</p>
      <div class="row">
        ${r.env ? `<input type="password" autocomplete="off" spellcheck="false" data-k="key"
          placeholder="Paste ${esc(r.env)}${r.prefix ? ' (starts ' + esc(r.prefix) + ')' : ''}" aria-label="${esc(r.label)} API key">
        <button data-do="keySave">Save key</button>` : ''}
        <button data-do="keyTest">Test</button>
        ${r.image === 'untested' || r.image === 'verified' ? '<button data-do="imgTest">Test images</button>' : ''}
      </div>
      <div class="tres" data-k="res"></div>
    </div>`;
  const q = {};
  el.querySelectorAll('[data-k]').forEach(x => { q[x.dataset.k] = x; });
  return {el, q};
}
function updateProvRow(row, r) {
  const t = r.test;
  const pill = r.local ? (r.ready ? ['ok', 'Running'] : ['warn', 'Not running'])
    : r.cooling ? ['warn', `Resting ${r.cooling}s`]
    : !r.has_key ? ['', 'No key']
    : t && t.ok ? ['ok', 'Works'] : t ? ['bad', 'Test failed'] : ['', 'Key saved, not tested'];
  row.q.pill.className = 'pill ' + pill[0];
  setText(row.q.pill, pill[1]);
  let html = '';
  if (t) {
    html = `<div>${t.ok ? 'Tested' : 'Failed'} ${esc(t.at || '')}: ${esc(t.msg)}</div>`;
    if (t.tiers && Object.keys(t.tiers).length) {
      html += '<div class="tt">' + ['small', 'big', 'vision'].map(x => `${x}: ${esc(t.tiers[x] || 'none')}`).join(', ') + '</div>';
    }
  }
  if (r.image === 'verified') html += `<div>Images work: ${esc(r.image_note)}</div>`;
  else if (r.image === 'never') html += `<div class="tt">${esc(r.image_note)}</div>`;
  else if (r.image === 'untested' && r.image_note) html += `<div class="tt">The last image test failed: ${esc(r.image_note)}</div>`;
  row.q.res.className = 'tres' + (t && !t.ok ? ' bad' : '');
  setHTML(row.q.res, html);
}
function renderImageGen() {
  const rows = WS.state.providers || [], box = $('#imggen');
  const ok = rows.filter(r => r.image === 'verified');
  if (!ok.length) {
    box._ready = null;
    const cands = rows.filter(r => r.image === 'untested').map(r => r.label).join(' or ');
    setHTML(box, `<p class="hint">Nothing here yet: no provider has made an image on this machine. Save a
      ${esc(cands)} key, open it under Models and keys, and press Test images. Ollama refuses image
      models over its API, so it is not an option.</p>`);
    return;
  }
  const sig = ok.map(r => r.id + r.image_note).join();
  if (box._ready === sig) return;            // keep a half-typed prompt
  box._ready = sig; box._html = null;
  box.innerHTML = `<div class="imgrow">
    <label for="imgprov">Made by</label>
    <select id="imgprov">${ok.map(r =>
      `<option value="${esc(r.id)}">${esc(r.label)}, ${esc((r.image_note || '').replace(/ returned an image$/, ''))}</option>`).join('')}</select>
    <label for="imgprompt">Describe the image</label>
    <textarea id="imgprompt" rows="2"></textarea>
    <div class="row"><button class="primary" data-do="imgMake">Make image</button></div></div>`;
}
function renderWsLog() {
  const el = $('#wslog');
  el.innerHTML = WS.log.map(m => {
    const ext = m.image ? (m.image.match(/^data:image\/(\w+)/) || [, 'png'])[1].replace('jpeg', 'jpg') : '';
    return `<div class="wmsg${m.role === 'user' ? ' you' : ''}${m.err ? ' err' : ''}">
      <div class="by">${m.err ? 'Error' : m.role === 'user' ? 'You' : 'Model'}</div>
      ${m.content ? `<div class="body">${esc(m.content)}</div>` : ''}
      ${m.image ? `<img src="${esc(m.image)}" alt="${m.role === 'user' ? 'Attached image' : 'Made image'}">` : ''}
      ${m.model ? `<div class="meta">Answered by <code>${esc(m.model)}</code>${m.tier ? ', ' + esc(m.tier) : ''}</div>` : ''}
      ${m.kind === 'gen' && m.image && m.role !== 'user' ? `<a class="btn" style="margin-top:8px" href="${esc(m.image)}" download="agx-image.${ext}">Save image</a>` : ''}
    </div>`;
  }).join('');
  el.lastElementChild && el.lastElementChild.scrollIntoView({block: 'nearest'});
}
async function wsSend() {
  if (WS.busy) { toast('Wait for the reply first.', true); return; }
  const ta = $('#wsq'), text = ta.value.trim(), image = WS.image && WS.image.url;
  if (!text && !image) { ta.focus(); return; }
  const history = WS.log.filter(m => !m.err && !m.pending && m.kind === 'text' && m.content)
    .slice(-14).map(m => ({role: m.role, content: m.content}));
  const pending = {role: 'assistant', content: image ? 'Looking at the image…' : 'Thinking…', pending: true};
  WS.log.push({role: 'user', content: text, image, kind: 'text'}, pending);
  ta.value = '';
  unattach();
  renderWsLog();
  WS.busy = true;
  try {
    const j = await post({action: 'ws_chat', tier: WS.tier, model: WS.model, image,
                          messages: history.concat([{role: 'user', content: text}])});
    Object.assign(pending, j.ok ? {content: j.reply, model: j.model, tier: j.tier, kind: 'text'}
                                : {content: j.msg || 'Failed', err: true});
  } catch (e) { Object.assign(pending, {content: 'Request failed: ' + e, err: true}); }
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
  if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) { toast('Attach a PNG, JPEG, WebP or GIF image.', true); return; }
  try {
    WS.image = {url: await shrink(file), name: file.name || 'Pasted image'};
  } catch (e) { toast('Could not read the image: ' + e, true); return; }
  const a = $('#attach');
  $('img', a).src = WS.image.url;
  setText($('span', a), `${WS.image.name}. Questions about it go to a vision model.`);
  a.hidden = false;
}
function unattach() { WS.image = null; $('#attach').hidden = true; $('#attach img').removeAttribute('src'); }
async function wsCall(btn, body, after) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = 'Working…';
  try {
    const j = await post(body);
    toast(j.msg || (j.ok ? 'Done' : 'Failed'), !j.ok);
    if (after) after(j);
  } catch (e) { toast('Request failed: ' + e, true); }
  btn.disabled = false; btn.textContent = label;
  await wsRefresh();
}

// ---------------------------------------------------------------- events
const DO = {
  tab(btn) { go(btn.dataset.tab === 'ws' ? '#/workspace' : SEL ? '#/p/' + encodeURIComponent(SEL) : '#/'); },
  back() { go('#/'); },
  toggleScan() {
    const box = $('#scan'), opener = $('[data-do="toggleScan"][aria-controls]');
    box.hidden = !box.hidden;
    opener.setAttribute('aria-expanded', String(!box.hidden));
    if (!box.hidden) { $('#newpath').focus(); scanRepos(); }
  },
  addManual() { const i = $('#newpath'); if (i.value.trim()) { addProject(i.value.trim()); i.value = ''; } },
  addProject(btn) { addProject(btn.dataset.path); },
  async addTask(btn, p) {
    const c = PROJ.get(p), i = c && c.q.task;
    if (!i || !i.value.trim()) { if (i) i.focus(); return; }
    const text = i.value;
    const j = await act('add_task', p, {text});
    if (j && j.ok && i.value === text) i.value = '';
  },
  pane(btn, p) {
    const c = PROJ.get(p), k = btn.dataset.pane;
    if (!c) return;
    c.pane = k;
    btn.parentNode.querySelectorAll('button').forEach(b => b.setAttribute('aria-selected', String(b === btn)));
    ['plan', 'tasks', 'ask', 'det'].forEach(x => { c.q['pane_' + x].hidden = x !== k; });
    const d = byName(p);
    if (k === 'tasks' && d) setHTML(c.q.tasks, taskList(d));
    if (k === 'det' && d) setHTML(c.q.details, details(d));
    if (k === 'ask') c.q.q.focus();
  },
  editPlan(btn, p) {
    const c = PROJ.get(p);
    c.editing = !c.editing;
    c.q.editor.hidden = !c.editing;
    btn.textContent = c.editing ? 'Close editor' : 'Edit plan';
    btn.setAttribute('aria-expanded', String(c.editing));
    if (c.editing) c.q.plan_ta.focus();
  },
  async savePlan(btn, p) {
    const c = PROJ.get(p);
    const j = await act('save_plan', p, {text: c.q.plan_ta.value});
    if (j && j.ok) c.planDirty = false;
  },
  ask(btn, p) { ask(p, btn.dataset.agent); },
  explain(btn, p) { converse(p, 'claude', null, {action: 'explain', project: p}); },
  closeOv,
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
    const row = btn.closest('.prov'), input = $('input', row), key = input.value.trim();
    if (!key) { input.focus(); return; }
    wsCall(btn, {action: 'ws_key_save', provider: row.dataset.prov, key}, j => { if (j.ok) input.value = ''; });
  },
  keyTest(btn) { wsCall(btn, {action: 'ws_key_test', provider: btn.closest('.prov').dataset.prov}); },
  imgTest(btn) {
    const prov = btn.closest('.prov').dataset.prov;
    wsCall(btn, {action: 'ws_image_test', provider: prov}, j => {
      if (j.ok && j.image) {
        WS.log.push({role: 'assistant', content: 'Image test', image: j.image, model: prov + '/' + j.model, kind: 'gen'});
        renderWsLog();
      }
    });
  },
  async imgMake(btn) {
    const ta = $('#imgprompt'), prompt = ta.value.trim(), provider = $('#imgprov').value;
    if (!prompt) { ta.focus(); return; }
    const pending = {role: 'assistant', content: 'Making the image…', pending: true, kind: 'gen'};
    WS.log.push({role: 'user', content: 'Make an image: ' + prompt, kind: 'gen'}, pending);
    renderWsLog();
    btn.disabled = true;
    try {
      const j = await post({action: 'ws_image', provider, prompt});
      Object.assign(pending, j.ok ? {content: '', image: j.image, model: j.model} : {content: j.msg, err: true});
    } catch (e) { Object.assign(pending, {content: 'Request failed: ' + e, err: true}); }
    pending.pending = false; btn.disabled = false;
    renderWsLog();
  },
};
document.addEventListener('click', e => {
  const t = e.target.closest('[data-act],[data-do],[data-sel],[data-file]');
  if (!t || t.disabled) return;
  const holder = t.closest('[data-p]');
  const p = holder ? holder.dataset.p : null;
  if (t.dataset.file) { openFile(t.dataset.file, t.dataset.label); return; }
  if (t.dataset.sel) { go('#/p/' + encodeURIComponent(t.dataset.sel)); return; }
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
  else if (t.id === 'wsq' && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); wsSend(); }
});
document.addEventListener('change', e => {
  const s = e.target;
  if (s.matches('#models select')) act('set_model', null, {agent: s.dataset.agent, model: s.value});
});
$('#note').addEventListener('input', e => {
  const n = e.target;
  n._dirty = true;
  grow(n);
  clearTimeout(n._t);
  n._t = setTimeout(async () => {
    try { await post({action: 'set_note', text: n.value}); SET.session_note = n.value; } catch (err) { /* retried on next edit */ }
    n._dirty = false;
  }, 900);
});
$('#wsmodel').addEventListener('change', e => { WS.model = e.target.value; renderTierHint(); });
$('#wsfile').addEventListener('change', e => { attachFile(e.target.files[0]); e.target.value = ''; });
$('#wsq').addEventListener('paste', e => {
  const f = [...((e.clipboardData || {}).files || [])].find(x => x.type.startsWith('image/'));
  if (f) { e.preventDefault(); attachFile(f); }
});
addEventListener('hashchange', route);
WIDE.addEventListener('change', () => { renderList(); renderDetail(); });

// ---------------------------------------------------------------- refresh
async function tick() {
  if (TICKING) return;
  TICKING = true;
  try {
    const r = await fetch('/api');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const raw = await r.json();
    DATA = raw.projects || []; MODELS = raw.models || {}; SET = raw.settings || {};
    LOADED = true;
    renderHero(); renderList(); renderDetail(); renderFeed(); renderModels();
    setText($('#sub'), 'Updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}));
  } catch (e) {
    setText($('#sub'), 'Lost the dashboard. Retrying…');
  } finally { TICKING = false; }
}
route();
tick();
setInterval(tick, 4000);
