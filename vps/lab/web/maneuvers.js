/* Tacks and gybes (#375): every manoeuvre in the debriefed track, what it cost, and averages per
   side and per sail setup. Server: app/maneuvers.py, GET /api/debrief/maneuvers.
   Uses app.js helpers (apiGet, esc, qhelp). The Debrief tab calls manCard(report, Deb) while it
   paints; the card fills itself in when the answer arrives, without repainting the tab.

   For other views (replay map, strip chart): the last answer per recording is kept in
   ManData.byKey, and each answer is announced as a "c4:maneuvers" event on document with
   {key, race_id, recording, data}. Each manoeuvre has id, type, t, t_start, t_end, window,
   lat, lon, loss_s, loss_m, counted. */
"use strict";

const ManData = { byKey: {}, pending: {} };

function manKey(raceId, recording) { return (raceId == null ? "@norace" : raceId) + "|" + (recording == null ? "" : Math.round(recording)); }

// Which stored track the card is about: the open debrief's recording, else a loaded GPX/tracker
// track for the race. null = nothing to show.
function manTarget(r, deb) {
  if (r && r.available) {
    const rec = (r.recording || {}).start_ts;
    if (r.no_race) return rec == null ? null : { raceId: null, recording: rec };
    const rid = r.race_id || (deb || {}).raceId;
    return rid ? { raceId: rid, recording: rec == null ? null : rec } : null;
  }
  const t = (deb || {}).track;
  if (t && t.available && (deb || {}).raceId) return { raceId: deb.raceId, recording: t.recording == null ? null : t.recording };
  return null;
}

function manUrl(tg) {
  const q = tg.raceId == null ? "no_race=1" : "race_id=" + encodeURIComponent(tg.raceId);
  return "/api/debrief/maneuvers?" + q + (tg.recording != null ? "&recording=" + encodeURIComponent(tg.recording) : "");
}

function manCard(r, deb) {
  const tg = manTarget(r, deb);
  if (!tg) return "";
  const key = manKey(tg.raceId, tg.recording);
  const have = ManData.byKey[key];
  if (!have && !ManData.pending[key]) manLoad(tg, key);
  return `<div class="card" id="manCard" data-key="${esc(key)}">${have ? manBody(have) : manShell('<div class="loading">Looking for tacks and gybes…</div>')}</div>`;
}

async function manLoad(tg, key) {
  ManData.pending[key] = true;
  let d;
  try { d = await (await apiGet(manUrl(tg))).json(); }
  catch (e) { d = { available: false, note: "Could not load tacks and gybes.", maneuvers: [] }; }
  delete ManData.pending[key];
  ManData.byKey[key] = d;
  try {
    document.dispatchEvent(new CustomEvent("c4:maneuvers",
      { detail: { key, race_id: tg.raceId, recording: tg.recording, data: d } }));
  } catch (e) { /* no listeners is fine */ }
  const el = document.getElementById("manCard");
  if (el && el.dataset.key === key) el.innerHTML = manBody(d);
}

function manShell(inner) {
  return `<h3>Tacks and gybes <span class="muted" style="font-weight:400">— time lost per turn</span></h3>${inner}`;
}

const manF = (v, nd) => (v == null ? "—" : Number(v).toFixed(nd == null ? 0 : nd));
const manClock = (t) => new Date(t * 1000).toISOString().slice(11, 19) + "Z";
const manSide = (s) => (s === "port" ? "P" : "S");

function manAvgRow(label, g) {
  return `<tr><td>${label}</td><td>${g.n}</td><td><b>${manF(g.loss_s)}</b></td><td>${manF(g.loss_m)}</td>
    <td>${manF(g.recover_s)}</td><td>${manF(g.entry_kn, 1)} / ${manF(g.min_kn, 1)} / ${manF(g.exit_kn, 1)}</td></tr>`;
}

function manBody(d) {
  if (!d || !d.available) return manShell(`<div class="placeholder">${esc((d && d.note) || "No track for this recording.")}</div>`);
  const ms = d.maneuvers || [];
  const s = d.summary || {};
  const warn = d.basis === "gps" || d.basis === "mixed"
    ? `<div class="banner warn" style="font-size:12px;margin-bottom:6px">${esc(d.note || "Wind guessed from the track. Losses are rough.")}</div>`
    : (d.note ? `<div class="muted" style="font-size:12px;margin-bottom:6px">${esc(d.note)}</div>` : "");
  if (!ms.length) return manShell(warn || `<div class="placeholder">No tacks or gybes found.</div>`);
  const plural = { tack: "Tacks", gybe: "Gybes" };
  const avg = [];
  ["tack", "gybe"].forEach((k) => {
    const g = s[k];
    if (!g || !g.n) return;
    avg.push(manAvgRow(`<b>${plural[k]}</b>`, g));
    (s.groups || []).filter((x) => x.type === k && x.by === "side")
      .forEach((x) => avg.push(manAvgRow(`<span class="muted">onto ${esc(x.key)}</span>`, x)));
    const cfgs = (s.groups || []).filter((x) => x.type === k && x.by === "config");
    if (cfgs.length > 1 || (cfgs.length === 1 && cfgs[0].key)) {
      cfgs.forEach((x) => avg.push(manAvgRow(`<span class="muted">${esc(x.key || "sails not logged")}</span>`, x)));
    }
  });
  const left = (s.found || 0) - (s.counted || 0);
  const rows = ms.map((m) => `<tr${m.counted ? "" : ' class="muted"'}>
      <td>${manClock(m.t)}</td><td>${m.type === "tack" ? "Tack" : "Gybe"}</td>
      <td>${manSide(m.from_side)}→${manSide(m.to_side)}</td>
      <td><b>${manF(m.loss_s)}</b></td><td>${manF(m.loss_m)}</td>
      <td>${manF(m.entry_kn, 1)} / ${manF(m.min_kn, 1)} / ${manF(m.exit_kn, 1)}</td>
      <td>${manF(m.recover_s)}</td><td>${esc(m.config || "")}</td>
      <td style="font-size:11px">${esc((m.flags || []).join("; "))}</td></tr>`).join("");
  return manShell(`${warn}
    ${avg.length ? `<table class="fleet-tbl"><thead><tr><th></th><th>Count</th><th>Lost (s)</th><th>Lost (m)</th><th>Back to speed (s)</th><th>Speed in / low / out (kts)</th></tr></thead><tbody>${avg.join("")}</tbody></table>`
      : `<div class="placeholder">None of these turns could be measured.</div>`}
    ${left > 0 ? `<div class="muted" style="font-size:11px;margin-top:4px">${left} of ${s.found} not in the averages. See the notes below.</div>` : ""}
    <details style="margin-top:8px"><summary>All ${ms.length} turns</summary>
      <table class="fleet-tbl"><thead><tr><th>Time</th><th>Type</th><th>Side</th><th>Lost (s)</th><th>Lost (m)</th><th>Speed in / low / out</th><th>Back to speed (s)</th><th>Sails</th><th>Note</th></tr></thead><tbody>${rows}</tbody></table>
    </details>
    <div class="muted" style="font-size:11px;margin-top:6px">Lost = distance to windward (or downwind) vs the steady speed before and after.
      ${qhelp("We compare the distance made toward the wind (away from it on a run) over about a minute around each turn with what the boat made sailing steady just before and just after. Lost (s) is how long it takes to sail that distance back. Back to speed: seconds from head to wind (or the gybe) until the boat is at 90% of its speed before the turn. Side: P→S is a tack or gybe from port onto starboard.")}</div>`);
}
