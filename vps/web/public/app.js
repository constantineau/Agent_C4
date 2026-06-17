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
};

/* ---------- gate ---------- */
function unlock() {
  // Client-side stub for now (real shared-password auth is the 5.0 follow-up). Accept any
  // non-empty entry so the bench is usable; structure is here for a server check later.
  const pw = document.getElementById('pw').value.trim();
  if (!pw) { document.getElementById('gateErr').textContent = 'Enter the boat password.'; return; }
  document.getElementById('gate').style.display = 'none';
  start();
}

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
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  App.ws = new WebSocket(`${proto}://${location.host}/ws`);
  App.ws.onmessage = (e) => { const m = JSON.parse(e.data); addMsg(m.role, m.text); };
  App.ws.onclose = () => { addMsg('system', 'disconnected — reconnecting…'); setTimeout(connect, 2000); };
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

/* ---------- live poll: link health, fatigue, position ---------- */
async function refresh() {
  try {
    const c = await (await fetch('/api/conditions')).json();
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
    const c = await (await fetch('/api/conditions/full')).json();
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
