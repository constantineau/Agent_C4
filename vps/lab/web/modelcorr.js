/* Model corrections (#384): per-model turn / speed / time shift, with an optional fade back to the
   raw forecast. Sits under the Gameplan "Weather models" checkboxes; kept per race in this browser
   and sent with every route run; the route, the PDF and the plume say when a correction was used.
   Server: app/wind/corrections.py (applied in the wind field before the blend).
   Uses app.js helpers and state (Opt, esc, qhelp, optTz, zonedWallToEpoch). */
"use strict";

const MC_KEY = "c4lab.modelcorr.v1";
const MC_FIELDS = ["rotate_deg", "scale_pct", "shift_h", "fade_min", "fade_at"];
const Mc = { open: false };
// the server's limits (app/wind/corrections.py): a value past one is used at the limit
const MC_LIMITS = { rotate_deg: [-90, 90], scale_pct: [25, 300], shift_h: [-24, 24], fade_min: [0, 10080] };
const MC_UNIT = { rotate_deg: "°", scale_pct: "%", shift_h: " h", fade_min: " min" };

function mcAll() {
  try { return JSON.parse(localStorage.getItem(MC_KEY) || "{}") || {}; } catch (e) { return {}; }
}
function mcSaveAll(all) {
  try { localStorage.setItem(MC_KEY, JSON.stringify(all)); } catch (e) { /* private window */ }
}
// this race's corrections: {model: {rotate_deg, scale_pct, shift_h, fade_min, fade_at}}
function mcFor(raceId) { return (raceId && mcAll()[raceId]) || {}; }

function mcNum(v) {
  if (v === "" || v == null) return null;
  const x = Number(v);
  return Number.isFinite(x) ? x : null;
}
// a field's number held to the server's limits (null when blank)
function mcClamp(field, v) {
  const x = mcNum(v), lim = MC_LIMITS[field];
  return x == null || !lim ? x : Math.min(lim[1], Math.max(lim[0], x));
}
// "Speed 400% is past the limit: 300% is used." or "" when the value is inside it
function mcClampNote(field, v) {
  const x = mcNum(v), c = mcClamp(field, v);
  if (x == null || x === c) return "";
  const name = { rotate_deg: "Turn", scale_pct: "Speed", shift_h: "Later", fade_min: "Fade" }[field];
  return `${name} ${x}${MC_UNIT[field]} is past the limit: ${c}${MC_UNIT[field]} is used.`;
}
// a correction that changes nothing (blank, 0°, 100 %, 0 h) is not a correction
function mcActive(c) {
  if (!c) return false;
  const r = mcClamp("rotate_deg", c.rotate_deg), s = mcClamp("scale_pct", c.scale_pct), h = mcClamp("shift_h", c.shift_h);
  return (r != null && r !== 0) || (s != null && s !== 100) || (h != null && h !== 0);
}

// Plain words, the same as the server's (corrections.describe_one): "GFS: 10° right, speed 80%".
function mcText(model, c) {
  const bits = [];
  const r = mcClamp("rotate_deg", c.rotate_deg), s = mcClamp("scale_pct", c.scale_pct),
    h = mcClamp("shift_h", c.shift_h), f = mcClamp("fade_min", c.fade_min);
  if (r) bits.push(`${Math.abs(r)}° ${r > 0 ? "right" : "left"}`);
  if (s != null && s !== 100) bits.push(`speed ${s}%`);
  if (h) bits.push(`${Math.abs(h)} h ${h > 0 ? "later" : "earlier"}`);
  if (f && f > 0) bits.push(`back to raw over ${f} min from ${c.fade_at ? String(c.fade_at).replace("T", " ") : "start"}`);
  return `${String(model).toUpperCase()}: ${bits.join(", ")}`;
}
function mcLines(corr, models) {
  return Object.keys(corr || {}).sort()
    .filter((m) => (!models || models.includes(m)) && mcActive(corr[m]))
    .map((m) => mcText(m, corr[m]));
}

// "YYYY-MM-DDThh:mm" in the race's zone (UTC when none) → epoch s, or null
function mcWallToEpoch(s, tz) {
  const m = String(s || "").trim().match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!m) return null;
  const [, y, mo, d, h, mi] = m.map(Number);
  return tz && typeof zonedWallToEpoch === "function" ? zonedWallToEpoch(y, mo, d, h, mi, tz)
    : Date.UTC(y, mo - 1, d, h, mi) / 1000;
}

// The request block for the chosen models: {model: {rotate_deg, scale_pct, shift_h, fade_min, fade_from}}.
function mcRequest(raceId, chosen, tz) {
  const corr = mcFor(raceId), out = {};
  for (const m of chosen || []) {
    const c = corr[m];
    if (!mcActive(c)) continue;
    const row = { rotate_deg: mcClamp("rotate_deg", c.rotate_deg) || 0,
      scale_pct: mcClamp("scale_pct", c.scale_pct) ?? 100, shift_h: mcClamp("shift_h", c.shift_h) || 0 };
    const f = mcClamp("fade_min", c.fade_min);
    if (f && f > 0) { row.fade_min = f; row.fade_from = mcWallToEpoch(c.fade_at, tz); }
    out[m] = row;
  }
  return Object.keys(out).length ? out : null;
}

function mcSet(model, field, value) {
  if (!MC_FIELDS.includes(field) || !Opt.raceId) return;
  const all = mcAll(), race = all[Opt.raceId] || {}, c = race[model] || {};
  c[field] = value;
  race[model] = c;
  if (!MC_FIELDS.some((k) => c[k] !== "" && c[k] != null)) delete race[model];
  all[Opt.raceId] = race;
  mcSaveAll(all);
  const n = document.getElementById("mcCount");
  if (n) n.textContent = mcCountLabel();
  const note = document.getElementById("mcNote");
  if (note) note.textContent = mcClampNote(field, value);
}
function mcClear() {
  const all = mcAll(); delete all[Opt.raceId]; mcSaveAll(all); mcRepaint();
}
function mcToggle() { Mc.open = !Mc.open; mcRepaint(); }
function mcCountLabel() {
  const n = mcLines(mcFor(Opt.raceId), Opt.chosen).length;
  return n ? `(${n} on)` : "";
}
function mcRepaint() {
  const el = document.getElementById("mcBox");
  if (el) el.innerHTML = mcControlsInner();
}

/* ---- Gameplan controls (under the Weather models checkboxes) ---- */
function mcControls() { return `<div id="mcBox" class="mc">${mcControlsInner()}</div>`; }
function mcControlsInner() {
  const head = `<button type="button" class="mini" onclick="mcToggle()">${Mc.open ? "Hide" : "Correct"} models</button>
    <span id="mcCount" class="muted" style="font-size:12px">${esc(mcCountLabel())}</span>`;
  if (!Mc.open) return head;
  const corr = mcFor(Opt.raceId);
  const val = (c, k) => esc(c && c[k] != null ? c[k] : "");
  const inp = (m, k, ph, step, w) => `<input type="number" step="${step}" placeholder="${ph}" style="width:${w}px"
      min="${MC_LIMITS[k][0]}" max="${MC_LIMITS[k][1]}"
      value="${val(corr[m], k)}" aria-label="${esc(m.toUpperCase())} ${k}" oninput="mcSet('${esc(m)}','${k}',this.value)">`;
  const rows = (Opt.chosen || []).map((m) => `<tr><td>${esc(m.toUpperCase())}</td>
      <td>${inp(m, "rotate_deg", "0", "1", 56)}</td><td>${inp(m, "scale_pct", "100", "5", 60)}</td>
      <td>${inp(m, "shift_h", "0", "0.5", 56)}</td><td>${inp(m, "fade_min", "—", "15", 60)}</td>
      <td><input type="datetime-local" value="${val(corr[m], "fade_at")}" aria-label="${esc(m.toUpperCase())} fade from"
        oninput="mcSet('${esc(m)}','fade_at',this.value)"></td></tr>`).join("");
  return head + qhelp("Fix a forecast you think is off. Turn: degrees, + = wind shifted right. Speed: % of the forecast. Later: hours the weather comes after the forecast. Fade: back to the plain forecast over that many minutes, from the time given (blank = start). Applies to the next route.") +
    `<table class="mc-table"><thead><tr><th>Model</th><th>Turn °</th><th>Speed %</th><th>Later h</th><th>Fade min</th><th>From</th></tr></thead>
     <tbody>${rows || `<tr><td colspan="6" class="muted">Pick a model first.</td></tr>`}</tbody></table>
     <div id="mcNote" class="muted" style="font-size:12px" aria-live="polite"></div>
     <button type="button" class="mini ghost" onclick="mcClear()">Clear all</button>`;
}

/* ---- what every output says ---- */
// route summary: the result's own record of what it used (the server's words)
function mcResultNote(r) {
  const c = r && r.corrections;
  if (!c || !c.items) return "";
  const used = c.applied ? `<div class="pill warn">Corrected forecast: ${(c.text || []).map(esc).join("; ")}.</div>` : "";
  const unused = c.items.filter((i) => i.unused).map((i) => `${esc(String(i.model).toUpperCase())} gave no wind, so its correction wasn't used.`);
  return used + (unused.length ? `<div class="muted" style="font-size:12px">${unused.join(" ")}</div>` : "");
}
// plume: its lines are the raw forecasts — say so when this race routes with corrections
function mcPlumeNote(raceId) {
  const lines = mcLines(mcFor(raceId));
  return lines.length ? `<div class="muted" style="font-size:12px">Raw forecasts, without your corrections (${lines.map(esc).join("; ")}).</div>` : "";
}
