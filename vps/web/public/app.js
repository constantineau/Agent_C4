/* SR33 Navigator — iPad crew companion shell (5.0).
   WebSocket chat + live polling, automatic day/night, race/practice mode, fatigue chip,
   and the all-channels slide-over. Later steps (5.1–5.4) hang the sail dial, course plot,
   navigator and routing onto this shell. Vanilla JS, no build step. */
"use strict";

const App = {
  ws: null,
  lastPos: null,         // {lat, lon} from telemetry, for the sun/day-night calc
  theme: localStorage.getItem('sr33.theme') || 'auto',   // auto | day | night
  mode: localStorage.getItem('sr33.mode') || 'practice',  // practice | race
  pollTimer: null,
  token: sessionStorage.getItem('sr33.token') || null,    // shared-password bearer token
};

/* ---------- gate (server-side shared-password auth) ---------- */
async function unlock() {
  const errEl = document.getElementById('gateErr');
  const pw = document.getElementById('pw').value.trim();
  if (!pw) { errEl.textContent = 'Enter the boat password.'; return; }
  errEl.textContent = 'Checking…';
  try {
    const res = await fetch('/api/auth', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pw }),
    });
    if (!res.ok) { errEl.textContent = 'Wrong boat password.'; return; }
    const data = await res.json();
    App.token = data.token;
    sessionStorage.setItem('sr33.token', App.token);
    errEl.textContent = '';
    document.getElementById('pw').value = '';
    document.getElementById('gate').style.display = 'none';
    start();
  } catch (e) {
    errEl.textContent = 'Login failed — agent unreachable.';
  }
}

/* Authenticated REST helper: inject the bearer token; on 401 (missing/expired) re-gate. */
async function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, opts.headers,
    App.token ? { Authorization: 'Bearer ' + App.token } : {});
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) { relock('Session expired — sign in again.'); throw new Error('unauthorized'); }
  return res;
}

/* Drop the session and return to the gate (token expired or rejected). */
function relock(msg) {
  App.token = null;
  sessionStorage.removeItem('sr33.token');
  if (App.pollTimer) { clearInterval(App.pollTimer); App.pollTimer = null; }
  if (App.ws) { try { App.ws.close(); } catch (e) {} App.ws = null; }
  const gate = document.getElementById('gate');
  if (gate) gate.style.display = '';
  const errEl = document.getElementById('gateErr');
  if (errEl) errEl.textContent = msg || '';
}

/* Auto-resume a stored session on load (a stale token re-gates on the first 401). */
function boot() {
  if (App.token) { document.getElementById('gate').style.display = 'none'; start(); }
}
window.addEventListener('DOMContentLoaded', boot);

function start() {
  applyTheme();
  applyMode();
  connect();
  refresh();
  App.pollTimer = setInterval(refresh, 5000);
  // Re-evaluate auto day/night a couple times a minute (cheap; catches dusk/dawn).
  setInterval(() => { if (App.theme === 'auto') applyTheme(); }, 25000);
}

/* ---------- theme (auto day/night) ---------- */
function cycleTheme() {
  App.theme = { auto: 'day', day: 'night', night: 'auto' }[App.theme];
  localStorage.setItem('sr33.theme', App.theme);
  applyTheme();
}
function resolvedTheme() {
  if (App.theme !== 'auto') return App.theme;
  const now = new Date();
  if (App.lastPos) return Sun.isDaylight(App.lastPos.lat, App.lastPos.lon, now) ? 'day' : 'night';
  const h = now.getHours();                 // no fix yet → fall back to local clock
  return (h >= 6 && h < 20) ? 'day' : 'night';
}
function applyTheme() {
  const r = resolvedTheme();
  document.documentElement.setAttribute('data-theme', r);
  document.getElementById('themeLbl').textContent =
    App.theme === 'auto' ? `AUTO·${r === 'day' ? '☀' : '☾'}` : App.theme.toUpperCase();
}

/* ---------- race / practice mode ---------- */
function toggleMode() {
  App.mode = App.mode === 'race' ? 'practice' : 'race';
  localStorage.setItem('sr33.mode', App.mode);
  applyMode();
}
function applyMode() {
  document.getElementById('modeLbl').textContent = App.mode.toUpperCase();
  document.getElementById('modeBtn').classList.toggle('on', App.mode === 'race');
  document.body.dataset.mode = App.mode;   // tactical/routing panels read this (RRS 41 gate)
  window.dispatchEvent(new CustomEvent('sr33:mode', { detail: App.mode }));
}
function tacticsAllowed() { return App.mode !== 'race'; }

/* ---------- websocket chat ---------- */
function connect() {
  if (!App.token) return;   // locked — nothing to connect with
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  App.ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(App.token)}`);
  App.ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.role === 'alert') handleAlert(m); else addMsg(m.role, m.text);
  };
  App.ws.onclose = () => {
    if (!App.token) return;   // re-gated — don't reconnect-loop against a closed (1008) socket
    addMsg('system', 'disconnected — reconnecting…');
    setTimeout(connect, 2000);
  };
}
function addMsg(role, text) {
  const log = document.getElementById('log');
  const div = document.createElement('div');
  div.className = 'msg ' + role; div.textContent = text;
  log.appendChild(div); log.scrollTop = log.scrollHeight;
}
function send() {
  const inp = document.getElementById('input');
  const t = inp.value.trim();
  if (!t || !App.ws || App.ws.readyState !== 1) return;
  addMsg('user', t); App.ws.send(t); inp.value = '';
}
function ask(q) { addMsg('user', q); if (App.ws && App.ws.readyState === 1) App.ws.send(q); }

/* On-demand debrief: POST the window report and drop the narrative into the chat log. */
async function runDebrief() {
  addMsg('user', 'Debrief the last session.');
  addMsg('system', 'Generating debrief…');
  try {
    const r = await (await apiFetch('/api/debrief', { method: 'POST' })).json();
    addMsg('assistant', r.available ? r.summary : 'No telemetry in the window to debrief yet.');
  } catch (e) { addMsg('system', 'Debrief failed.'); }
}

/* ---------- live poll: link health, fatigue, position ---------- */
async function refresh() {
  try {
    const c = await (await apiFetch('/api/conditions')).json();
    const link = document.getElementById('link');
    if (!c.available) { link.className = 'dot down'; setFatigue(null, null); return; }
    link.className = 'dot ' + (c.stale ? 'stale' : 'live');
    if (typeof c.lat === 'number' && typeof c.lon === 'number') {
      App.lastPos = { lat: c.lat, lon: c.lon };
      if (App.theme === 'auto') applyTheme();
    }
    setFatigue(c.fatigue, c.fatigue_level);
    App.lastConditions = c;
    window.dispatchEvent(new CustomEvent('sr33:conditions', { detail: c }));  // for later panels
  } catch (e) { document.getElementById('link').className = 'dot down'; }
}
function setFatigue(index, level) {
  const chip = document.getElementById('fatigueChip');
  document.getElementById('fatigueVal').textContent =
    (index === null || index === undefined) ? '–' : Math.round(index);
  chip.dataset.level = level || '';
}

/* ---------- alert banner (server-pushed) ---------- */
App.alerts = {};                 // key -> banner element
App.dismissed = new Set();       // keys the crew dismissed; suppressed until they clear
function handleAlert(m) {
  const a = m.alert; if (!a || !a.key) return;
  if (m.event === 'cleared') { removeAlert(a.key); App.dismissed.delete(a.key); return; }
  if (App.dismissed.has(a.key)) return;   // dismissed and still active — keep it hidden
  renderAlert(a);
}
function renderAlert(a) {
  const box = document.getElementById('alerts');
  let el = App.alerts[a.key];
  if (!el) { el = document.createElement('div'); el.className = 'alert'; App.alerts[a.key] = el; box.appendChild(el); }
  el.dataset.sev = a.severity || 'info';
  el.innerHTML = `<span class="akind"></span><span class="amsg"></span><button class="ax" title="dismiss">×</button>`;
  el.querySelector('.akind').textContent = (a.kind || '').replace(/_/g, ' ');
  el.querySelector('.amsg').textContent = a.message || '';
  el.querySelector('.ax').onclick = () => { App.dismissed.add(a.key); removeAlert(a.key); };
}
function removeAlert(key) {
  const el = App.alerts[key];
  if (el) { el.remove(); delete App.alerts[key]; }
}

/* ---------- all-channels slide-over ---------- */
async function openChannels() {
  document.getElementById('scrim').classList.add('show');
  document.getElementById('channels').classList.add('open');
  renderChannels();
}
function closeChannels() {
  document.getElementById('scrim').classList.remove('show');
  document.getElementById('channels').classList.remove('open');
}
async function renderChannels() {
  const body = document.getElementById('channelsBody');
  try {
    const c = await (await apiFetch('/api/conditions/full')).json();
    if (!c.available) { body.innerHTML = '<div class="placeholder">No telemetry in window.</div>'; return; }
    const rows = Object.entries(c.channels).sort((a, b) => a[0].localeCompare(b[0])).map(([name, ch]) => {
      const pref = ch.preferred || {};
      const srcs = ch.readings.map(r =>
        `${r.source.split('.').pop()} ${r.value}${r.age_s > 30 ? ' ⚠' : ''}`).join(' · ');
      const dis = ch.disagreement ? ' disagree' : '';
      const fb = ch.fell_back ? ' ↩backup' : '';
      return `<div class="chrow${dis}">
        <span class="ch">${name}</span>
        <span class="pv">${pref.value ?? '–'}<span class="u">${ch.unit || ''}</span></span>
        <span class="srcs">${srcs}${fb}${ch.disagreement ? ' · sources disagree' : ''}</span></div>`;
    }).join('');
    body.innerHTML = rows || '<div class="placeholder">No channels.</div>';
  } catch (e) { body.innerHTML = '<div class="placeholder">Failed to load channels.</div>'; }
}

// auto-refresh channels while the sheet is open
setInterval(() => {
  if (document.getElementById('channels').classList.contains('open')) renderChannels();
}, 5000);
