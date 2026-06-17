/* C4 Performance Lab shell — shared team login, hash-routed sections, the Races library +
   RaceDefinition review view. Vanilla JS, no build. Slice 1: Races is functional; the other
   sections are placeholders that describe what they'll do. */
"use strict";

const Lab = { token: sessionStorage.getItem("c4lab.token") || null, races: null, sel: null };

const PHASE_ORDER = ["pre_entry", "pre_start", "start", "in_race", "at_gate", "at_finish", "post_race"];
const PHASE_LABEL = {
  pre_entry: "Before entry", pre_start: "Pre-start / registration", start: "Start",
  in_race: "While racing", at_gate: "At the gate", at_finish: "At the finish", post_race: "Post-race",
};
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- auth ---------- */
async function unlock() {
  const err = document.getElementById("gateErr");
  const pw = document.getElementById("pw").value.trim();
  if (!pw) { err.textContent = "Enter the team password."; return; }
  err.textContent = "Checking…";
  try {
    const res = await fetch("/api/auth", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }) });
    if (!res.ok) { err.textContent = "Wrong team password."; return; }
    Lab.token = (await res.json()).token;
    sessionStorage.setItem("c4lab.token", Lab.token);
    document.getElementById("gate").style.display = "none";
    start();
  } catch (e) { err.textContent = "Login failed — Lab unreachable."; }
}
function logout() { sessionStorage.removeItem("c4lab.token"); Lab.token = null; location.reload(); }
async function apiFetch(path) {
  const res = await fetch(path, { headers: Lab.token ? { Authorization: "Bearer " + Lab.token } : {} });
  if (res.status === 401) { logout(); throw new Error("unauthorized"); }
  return res;
}
function boot() {
  if (Lab.token) { document.getElementById("gate").style.display = "none"; start(); }
}
window.addEventListener("DOMContentLoaded", boot);

/* ---------- router ---------- */
function start() {
  window.addEventListener("hashchange", route);
  route();
}
function route() {
  const sec = (location.hash || "#races").slice(1);
  document.querySelectorAll("#tabs a").forEach((a) =>
    a.classList.toggle("active", a.getAttribute("href") === "#" + sec));
  if (sec === "races") return renderRaces();
  renderPlaceholder(sec);
}

/* ---------- Races ---------- */
async function renderRaces() {
  const view = document.getElementById("view");
  if (!Lab.races) {
    view.innerHTML = '<div class="loading">Loading race library…</div>';
    try { Lab.races = (await (await apiFetch("/api/races")).json()).races || []; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load races.</div>'; return; }
  }
  if (!Lab.sel && Lab.races.length) Lab.sel = Lab.races[0].race_id;
  view.innerHTML = `<div class="races">
    <div><div class="card"><h3>Race library</h3>
      <div class="racelist">${Lab.races.map(raceItem).join("") ||
        '<div class="muted">No races yet — ingest one (coming next).</div>'}</div>
      <div class="muted" style="margin-top:12px;font-size:12px">Ingest a new race (auto-find URL ·
        paste link · upload PDF) — coming next.</div>
    </div></div>
    <div id="raceDetail" class="detail"><div class="placeholder">Select a race.</div></div>
  </div>`;
  if (Lab.sel) loadRace(Lab.sel);
}
function raceItem(r) {
  const rev = r.errors ? `<span class="pill bad">${r.errors} errors</span>`
    : (r.warnings ? `<span class="pill warn">${r.warnings} to review</span>`
      : `<span class="pill ok">reviewed</span>`);
  return `<div class="raceitem ${r.race_id === Lab.sel ? "sel" : ""}" onclick="selectRace('${esc(r.race_id)}')">
    <div class="nm">${esc(r.name)}</div>
    <div class="meta">${esc(r.region || "")} · ${esc(r.start_date || r.year)}</div>
    <div class="pills"><span class="pill">${r.courses} courses</span>
      <span class="pill">${r.requirements} checklist</span>
      <span class="pill">${r.ipad_items} →iPad</span>${rev}</div></div>`;
}
function selectRace(id) { Lab.sel = id; renderRaces(); }

async function loadRace(id) {
  const box = document.getElementById("raceDetail");
  if (!box) return;
  box.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const d = await (await apiFetch("/api/races/" + encodeURIComponent(id))).json();
    const v = await (await apiFetch("/api/races/" + encodeURIComponent(id) + "/validate")).json();
    box.innerHTML = renderDetail(d, v);
  } catch (e) { box.innerHTML = '<div class="placeholder">Failed to load race.</div>'; }
}

function renderDetail(d, v) {
  const errs = (v.errors || []), warns = (v.warnings || []);
  const banner = errs.length
    ? `<div class="banner review"><b>${errs.length} errors</b> — must be fixed before activation.</div>`
    : warns.length
      ? `<div class="banner review"><b>Needs human review (${warns.length}):</b> ${warns.map(esc).join(" · ")}</div>`
      : `<div class="banner ok">Validated — ready for review sign-off.</div>`;
  return `<div class="dhead">
      <h2>${esc(d.name)}</h2>
      <div class="dmeta">${esc(d.organizing_authority || "")}<br>
        Start ${esc(d.start_date || "")} · ${esc(d.start_area || "")} · ${esc(d.region || "")}</div>
    </div>${banner}
    ${(d.courses || []).map(courseCard).join("")}
    ${checklistCard(d.requirements || [])}
    ${rulesCard(d.rules_profile || {})}
    ${provenanceCard(d.provenance || {})}`;
}

function courseCard(c) {
  const marks = (c.marks || []).map((m) => `<tr>
    <td class="mono">${m.seq}</td><td>${esc(m.name)}</td><td>${esc(m.type)}</td>
    <td>${esc(m.rounding)}</td>
    <td class="mono">${m.lat == null ? '<span class="need">needs review</span>'
      : esc(m.lat.toFixed(4) + ", " + m.lon.toFixed(4))}</td></tr>`).join("");
  const fin = c.finish ? `<tr><td class="mono">F</td><td>Finish (${esc(c.finish.type)})</td>
    <td>finish</td><td>${esc(c.finish.crossing || "")}</td>
    <td class="mono">${(c.finish.points || []).map((p) => esc(p.lat.toFixed(4) + "," + p.lon.toFixed(4))).join(" → ")}</td></tr>` : "";
  return `<div class="card"><h3>Course — ${esc(c.name)}</h3>
    <div class="muted" style="margin-bottom:8px">Divisions ${esc((c.applies_to_divisions || []).join(", "))}${
      c.distance_nm ? " · " + c.distance_nm + " nm" : ""}</div>
    <table><thead><tr><th>#</th><th>Mark</th><th>Type</th><th>Leave</th><th>Lat, Lon</th></tr></thead>
    <tbody>${marks}${fin}</tbody></table></div>`;
}

function checklistCard(reqs) {
  if (!reqs.length) return "";
  const byPhase = {};
  reqs.forEach((r) => (byPhase[r.phase] = byPhase[r.phase] || []).push(r));
  const groups = PHASE_ORDER.filter((p) => byPhase[p]).map((p) => `<div class="phasegrp">
    <h4>${PHASE_LABEL[p] || p}</h4>
    ${byPhase[p].map(reqRow).join("")}</div>`).join("");
  const ipad = reqs.filter((r) => r.deliver_to_ipad).length;
  return `<div class="card"><h3>Rules, Safety &amp; Checklists — ${reqs.length} items
    (${ipad} pushed to the iPad)</h3>${groups}</div>`;
}
function reqRow(r) {
  const tags = [`<span class="tag cat">${esc(r.category)}</span>`];
  if (r.critical) tags.push('<span class="tag crit">critical</span>');
  if (r.deliver_to_ipad) tags.push(`<span class="tag ipad">→iPad${
    r.trigger_detail ? " · " + esc(r.trigger_detail) : ""}</span>`);
  return `<div class="req"><div class="body">${esc(r.text)}
    <div class="src">${esc(r.source || "")}</div></div>
    <div class="pills">${tags.join("")}</div></div>`;
}

function rulesCard(rp) {
  const mods = (rp.modifications || []).map((m) =>
    `<tr><td>${esc(m.ref)}</td><td>${esc(m.rule)}</td><td>${esc(m.summary)}</td></tr>`).join("");
  const sc = rp.scoring || {};
  return `<div class="card"><h3>Rules &amp; scoring</h3>
    <div class="muted" style="margin-bottom:10px">RRS ${esc(rp.rrs_edition || "")} ·
      Appendix WP: ${rp.appendix_wp ? "yes" : "no"} ·
      Tracker permitted: ${rp.tracker_permitted === true ? "yes" : rp.tracker_permitted === false ? "no" : "—"}</div>
    <table><thead><tr><th>Ref</th><th>Rule</th><th>Modification</th></tr></thead>
      <tbody>${mods}</tbody></table>
    <div style="margin-top:12px"><b>Scoring:</b> ${esc(sc.system || "")} —
      ${esc(sc.method || "")}<div class="muted" style="font-size:12px">${esc(sc.decided || "")}</div></div>
    </div>`;
}

function provenanceCard(p) {
  const srcs = (p.sources || []).map((s) =>
    `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a>
     <span class="muted">${esc(s.retrieved || "")}</span></li>`).join("");
  return `<div class="card"><h3>Provenance &amp; review</h3>
    <ul style="margin:0 0 10px;padding-left:18px">${srcs}</ul>
    <div class="muted" style="font-size:12px">${esc(p.si_status || "")}</div>
    <div class="need" style="font-size:12px;margin-top:6px">${esc(p.review_status || "")}</div></div>`;
}

/* ---------- placeholders ---------- */
const SOON = {
  course: ["Course & Marks", "Map view to review and sign off the extracted geometry (gate/finish/marks/zones), geocode the islands flagged for review, and set rounding sides."],
  rules: ["Rules, Safety & Checklists", "The full prep checklist the team works through — every SER + procedural item, with the race-time subset flagged to push to the iPad."],
  fleet: ["Fleet", "Competitor roster + ORC handicaps (entry-list import, MMSI matching) for handicap-aware, corrected-time tactics."],
  learnings: ["Learnings", "The boat-level library (refined polars, crossovers, calibration, fatigue/helm-skill) and what's applied to this regatta."],
  gameplan: ["Gameplan / Optimizer", "Run the multi-model optimization, review scenarios, and build the branching playbook with rationale and tradeoffs."],
  deploy: ["Lock-in & Deploy", "Freeze the playbook and push the homework (course, checklists, playbook) to the Pi / Orin for the race."],
  monitor: ["Monitor", "Shore-side live view during the race (the boat itself uses the onboard console)."],
  debrief: ["Debrief", "The post-race judge loop — regret analysis and write-back review that feeds the next prep."],
};
function renderPlaceholder(sec) {
  const [title, desc] = SOON[sec] || ["Section", "Coming soon."];
  document.getElementById("view").innerHTML =
    `<div class="placeholder"><h2>${esc(title)}</h2><p>${esc(desc)}</p>
     <p class="muted">Coming soon — the Races tab is live now.</p></div>`;
}
