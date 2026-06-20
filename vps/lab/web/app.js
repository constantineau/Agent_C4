/* C4 Performance Lab shell — shared team login, hash-routed sections, the Races library +
   RaceDefinition review view + dual-input ingestion (URL / paste-link / upload → Opus → review →
   save). Vanilla JS, no build. */
"use strict";

const Lab = { token: sessionStorage.getItem("c4lab.token") || null,
  races: null, sel: null, sources: [], draft: null };

const PHASE_ORDER = ["pre_entry", "pre_start", "start", "in_race", "at_gate", "at_finish", "post_race"];
const PHASE_LABEL = {
  pre_entry: "Before entry", pre_start: "Pre-start / registration", start: "Start",
  in_race: "While racing", at_gate: "At the gate", at_finish: "At the finish", post_race: "Post-race",
};
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- auth + api ---------- */
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
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers, Lab.token ? { Authorization: "Bearer " + Lab.token } : {});
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) { logout(); throw new Error("unauthorized"); }
  return res;
}
const apiGet = (p) => api(p);
const apiPost = (p, body) => api(p, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body) });
function boot() { if (Lab.token) { document.getElementById("gate").style.display = "none"; start(); } }
window.addEventListener("DOMContentLoaded", boot);

/* ---------- router ---------- */
function start() { window.addEventListener("hashchange", route); route(); }
function route() {
  const sec = (location.hash || "#races").slice(1);
  document.querySelectorAll("#tabs a").forEach((a) =>
    a.classList.toggle("active", a.getAttribute("href") === "#" + sec));
  if (sec === "races") return renderRaces();
  if (sec === "course") return renderCourse();
  if (sec === "gameplan") return renderGameplan();
  renderPlaceholder(sec);
}
const clone = (o) => JSON.parse(JSON.stringify(o));

/* ---------- Races ---------- */
async function renderRaces() {
  const view = document.getElementById("view");
  if (!Lab.races) {
    view.innerHTML = '<div class="loading">Loading race library…</div>';
    try { Lab.races = (await (await apiGet("/api/races")).json()).races || []; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load races.</div>'; return; }
  }
  if (!Lab.sel && Lab.races.length) Lab.sel = Lab.races[0].race_id;
  view.innerHTML = `<div class="races">
    <div>
      ${ingestCard()}
      <div class="card"><h3>Race library</h3>
        <div class="racelist" id="racelist">${Lab.races.map(raceItem).join("") ||
          '<div class="muted">No races yet — ingest one above.</div>'}</div></div>
    </div>
    <div id="raceDetail" class="detail"><div class="placeholder">Select a race.</div></div>
  </div>`;
  renderSources();
  if (Lab.sel) loadRace(Lab.sel);
}
function raceItem(r) {
  const rev = r.errors ? `<span class="pill bad">${r.errors} errors</span>`
    : (r.warnings ? `<span class="pill warn">${r.warnings} to review</span>`
      : `<span class="pill ok">reviewed</span>`);
  return `<div class="raceitem ${r.race_id === Lab.sel ? "sel" : ""}" data-id="${esc(r.race_id)}"
      onclick="selectRace('${esc(r.race_id)}')">
    <div class="nm">${esc(r.name)}</div>
    <div class="meta">${esc(r.region || "")} · ${esc(r.start_date || r.year)}</div>
    <div class="pills"><span class="pill">${r.courses} courses</span>
      <span class="pill">${r.requirements} checklist</span>
      <span class="pill">${r.ipad_items} →iPad</span>${rev}</div></div>`;
}
function selectRace(id) {
  Lab.sel = id; Lab.draft = null;
  document.querySelectorAll(".raceitem").forEach((el) =>
    el.classList.toggle("sel", el.dataset.id === id));
  loadRace(id);
}

async function loadRace(id) {
  const box = document.getElementById("raceDetail");
  if (!box) return;
  box.innerHTML = '<div class="loading">Loading…</div>';
  try {
    const d = await (await apiGet("/api/races/" + encodeURIComponent(id))).json();
    const v = await (await apiGet("/api/races/" + encodeURIComponent(id) + "/validate")).json();
    box.innerHTML = renderDetail(d, v);
  } catch (e) { box.innerHTML = '<div class="placeholder">Failed to load race.</div>'; }
}

/* ---------- ingestion ---------- */
function ingestCard() {
  return `<div class="card"><h3>Ingest a race</h3>
    <div class="ing-row"><input id="ingUrl" placeholder="Race or document URL (auto-find)">
      <button class="mini" onclick="discover()">Find docs</button></div>
    <div id="ingCands" class="ing-cands"></div>
    <div class="ing-row"><input id="ingLink" placeholder="…or paste a direct PDF link">
      <button class="mini" onclick="addLink()">Add</button></div>
    <ul id="ingList" class="ing-list"></ul>
    <div class="ing-row"><label class="muted" style="font-size:12px">Or upload PDF(s):</label>
      <input type="file" id="ingFiles" multiple accept="application/pdf"></div>
    <button onclick="extractDraft()" id="ingBtn">Extract →</button>
    <div id="ingMsg" class="muted" style="font-size:12px;margin-top:8px"></div></div>`;
}
function renderSources() {
  const el = document.getElementById("ingList"); if (!el) return;
  el.innerHTML = Lab.sources.map((u, i) =>
    `<li>${esc(u)} <span class="rm" onclick="rmSource(${i})">✕</span></li>`).join("");
}
function addSource(u) { if (u && !Lab.sources.includes(u)) { Lab.sources.push(u); renderSources(); } }
function rmSource(i) { Lab.sources.splice(i, 1); renderSources(); }
function addLink() {
  const inp = document.getElementById("ingLink"); addSource(inp.value.trim()); inp.value = "";
}
async function discover() {
  const url = document.getElementById("ingUrl").value.trim();
  const cands = document.getElementById("ingCands");
  if (!url) return;
  cands.innerHTML = '<div class="muted" style="font-size:12px">Searching…</div>';
  try {
    const r = await (await apiPost("/api/ingest/discover", { url })).json();
    const list = r.candidates || [];
    cands.innerHTML = list.length
      ? list.slice(0, 12).map((c) =>
        `<div class="cand"><span>${esc(c.label)}</span>
         <button class="mini" onclick="addSource('${esc(c.url)}')">Add</button></div>`).join("")
      : '<div class="muted" style="font-size:12px">No PDFs found — paste a direct link or upload.</div>';
  } catch (e) { cands.innerHTML = '<div class="muted" style="font-size:12px">Discover failed.</div>'; }
}
async function extractDraft() {
  const msg = document.getElementById("ingMsg");
  const btn = document.getElementById("ingBtn");
  const files = document.getElementById("ingFiles").files;
  if (!Lab.sources.length && !files.length) { msg.textContent = "Add a document URL or upload a PDF first."; return; }
  btn.disabled = true; msg.textContent = "Extracting with Opus… (reading the documents, ~30–60s)";
  try {
    let resp;
    if (files.length) {
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      resp = await (await api("/api/ingest/upload", { method: "POST", body: fd })).json();
    } else {
      resp = await (await apiPost("/api/ingest", { urls: Lab.sources })).json();
    }
    if (resp.detail) { msg.textContent = "Ingest failed: " + resp.detail; btn.disabled = false; return; }
    Lab.draft = resp; msg.textContent = "Draft extracted — review it on the right, then save.";
    document.getElementById("raceDetail").innerHTML = renderDraft(resp);
  } catch (e) { msg.textContent = "Ingest failed."; }
  btn.disabled = false;
}
function renderDraft(resp) {
  return `<div class="banner draft"><b>DRAFT — machine-extracted, needs human review.</b>
      Check the geometry and checklist, then save to the library.
      <button class="mini" onclick="saveDraft()">Save to library</button></div>
    ${renderDetail(resp.definition, resp)}`;
}
async function saveDraft() {
  if (!Lab.draft) return;
  try {
    const r = await (await apiPost("/api/races", { definition: Lab.draft.definition })).json();
    if (!r.saved) { alert("Save failed: " + (r.detail || "unknown")); return; }
    Lab.races = null; Lab.sel = r.race_id; Lab.draft = null; Lab.sources = [];
    renderRaces();
  } catch (e) { alert("Save failed."); }
}

/* ---------- detail rendering ---------- */
function renderDetail(d, v) {
  const errs = (v.errors || []), warns = (v.warnings || []);
  const banner = errs.length
    ? `<div class="banner review"><b>${errs.length} errors</b>: ${errs.map(esc).join(" · ")}</div>`
    : warns.length
      ? `<div class="banner review"><b>Needs human review (${warns.length}):</b> ${warns.map(esc).join(" · ")}</div>`
      : `<div class="banner ok">Validated — ready for review sign-off.</div>`;
  return `<div class="dhead"><h2>${esc(d.name || "(unnamed)")}</h2>
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
    <td class="mono">${(c.finish.points || []).map((p) =>
      p && p.lat != null ? esc(p.lat.toFixed(4) + "," + p.lon.toFixed(4)) : "—").join(" → ")}</td></tr>` : "";
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
  const phases = PHASE_ORDER.filter((p) => byPhase[p])
    .concat(Object.keys(byPhase).filter((p) => !PHASE_ORDER.includes(p)));
  const groups = phases.map((p) => `<div class="phasegrp">
    <h4>${PHASE_LABEL[p] || p}</h4>${byPhase[p].map(reqRow).join("")}</div>`).join("");
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
    <div style="margin-top:12px"><b>Scoring:</b> ${esc(sc.system || "")} — ${esc(sc.method || "")}
      <div class="muted" style="font-size:12px">${esc(sc.decided || "")}</div></div></div>`;
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

/* ---------- Course & Marks review (map + edit + geocode + save) ---------- */
async function renderCourse() {
  const view = document.getElementById("view");
  if (!Lab.races) { try { Lab.races = (await (await apiGet("/api/races")).json()).races || []; } catch (e) {} }
  if (!Lab.sel && Lab.races && Lab.races.length) Lab.sel = Lab.races[0].race_id;
  if (!Lab.sel) { view.innerHTML = '<div class="placeholder">No race — ingest one in the Races tab.</div>'; return; }
  view.innerHTML = '<div class="loading">Loading course…</div>';
  try { Lab.editDef = await (await apiGet("/api/races/" + encodeURIComponent(Lab.sel))).json(); }
  catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  paintCourse();
}
/* Render the Course & Marks view from the in-memory Lab.editDef (no server refetch — so edits and
   geocoded coords are preserved). Callers set #courseMsg AFTER calling this. */
function paintCourse() {
  const d = Lab.editDef;
  document.getElementById("view").innerHTML = `<div class="dhead">
      <h2>Course &amp; Marks — ${esc(d.name)}</h2>
      <div class="dmeta">Review the geometry; fill any <span class="need">needs review</span> marks
        (type a lat/lon or Geocode), then Save. Reviewed copies override the bundled seed.</div></div>
    <div id="courseMsg" class="muted" style="font-size:12px;margin-bottom:10px"></div>
    ${(d.courses || []).map((c, i) => courseEditCard(c, i)).join("")}
    <button id="saveCourseBtn" onclick="saveCourse()">Save course geometry</button>
    <button class="mini" onclick="saveAndGameplan()">Save → run optimizer</button>`;
  (d.courses || []).forEach((c, i) => drawCourseMap("map" + i, c));
}
function courseEditCard(c, ci) {
  c.start = c.start || {};
  const rows = (c.marks || []).map((m, mi) => {
    const need = m.lat == null || m.coords_source === "needs_review";
    const isGate = m.type === "gate", isIsland = m.type === "island";
    return `<tr class="${need ? "needrow" : ""}">
      <td class="mono">${m.seq}</td>
      <td><input class="cin" style="width:120px" value="${esc(m.name || "")}"
        onchange="editMark(${ci},${mi},'name',this.value)"></td>
      <td><select onchange="editMark(${ci},${mi},'type',this.value)">
        ${["waypoint", "gate", "island", "buoy"].map((t) =>
          `<option ${m.type === t ? "selected" : ""}>${t}</option>`).join("")}</select></td>
      <td><select onchange="editMark(${ci},${mi},'rounding',this.value)">
        ${["none", "port", "starboard", "gate"].map((r) =>
          `<option ${m.rounding === r ? "selected" : ""}>${r}</option>`).join("")}</select></td>
      <td><input class="cin" value="${m.lat == null ? "" : m.lat}" placeholder="lat"
        onchange="editMark(${ci},${mi},'lat',this.value)"></td>
      <td><input class="cin" value="${m.lon == null ? "" : m.lon}" placeholder="lon"
        onchange="editMark(${ci},${mi},'lon',this.value)"></td>
      <td>${isGate ? `<input class="cin" value="${m.lat2 == null ? "" : m.lat2}" placeholder="lat2"
        onchange="editMark(${ci},${mi},'lat2',this.value)"><input class="cin" value="${m.lon2 == null ? "" : m.lon2}"
        placeholder="lon2" onchange="editMark(${ci},${mi},'lon2',this.value)">` : ""}</td>
      <td>${isIsland ? `<input class="cin" style="width:48px" value="${m.radius_nm == null ? "" : m.radius_nm}"
        placeholder="nm" onchange="editMark(${ci},${mi},'radius_nm',this.value)">` : ""}</td>
      <td>${esc(m.coords_source || "")}</td>
      <td>${need ? `<button class="mini" onclick="geocodeMark(${ci},${mi})">Geocode</button>` : ""}
        <button class="mini" title="remove" onclick="removeMark(${ci},${mi})">✕</button></td>
    </tr>`;
  }).join("");
  return `<div class="card"><h3>${esc(c.name)}</h3>
    <canvas id="map${ci}" class="coursemap" width="640" height="360"></canvas>
    <div style="font-size:13px;margin:8px 0">
      <b>Start</b>
      <input class="cin" value="${c.start.lat == null ? "" : c.start.lat}" placeholder="start lat"
        onchange="editStart(${ci},'lat',this.value)">
      <input class="cin" value="${c.start.lon == null ? "" : c.start.lon}" placeholder="start lon"
        onchange="editStart(${ci},'lon',this.value)">
      <span class="muted">${esc(c.start.coords_source || "")}</span>
    </div>
    <table><thead><tr><th>#</th><th>Mark</th><th>Type</th><th>Leave</th><th>Lat</th><th>Lon</th>
      <th>Gate 2nd pt</th><th>R&nbsp;nm</th><th>Source</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table>
    <button class="mini" onclick="addMark(${ci})">+ Add mark</button></div>`;
}
function editMark(ci, mi, field, val) {
  const m = Lab.editDef.courses[ci].marks[mi];
  if (["lat", "lon", "lat2", "lon2", "radius_nm"].includes(field)) {
    const n = parseFloat(val);
    m[field] = isNaN(n) ? null : n;
    if (m.lat != null && m.lon != null && m.coords_source === "needs_review") m.coords_source = "approx";
  } else { m[field] = val; }
  paintCourse();   // re-render from Lab.editDef (updates source cell + map; onchange = after blur)
}
function addMark(ci) {
  const marks = Lab.editDef.courses[ci].marks = (Lab.editDef.courses[ci].marks || []);
  marks.push({ seq: marks.length + 1, name: "New mark", type: "waypoint", rounding: "none",
    lat: null, lon: null, coords_source: "needs_review" });
  paintCourse();
  document.getElementById("courseMsg").textContent = "Added a mark — set its name + lat/lon, then Save.";
}
function removeMark(ci, mi) {
  const marks = Lab.editDef.courses[ci].marks;
  marks.splice(mi, 1);
  marks.forEach((m, i) => { m.seq = i + 1; });   // re-sequence
  paintCourse();
}
function editStart(ci, field, val) {
  const c = Lab.editDef.courses[ci]; c.start = c.start || {};
  const n = parseFloat(val);
  c.start[field] = isNaN(n) ? null : n;
  if (c.start.lat != null && c.start.lon != null &&
      (!c.start.coords_source || ["si_pending", "needs_review"].includes(c.start.coords_source)))
    c.start.coords_source = "approx";
  paintCourse();
}
async function saveAndGameplan() {
  await saveCourse();
  if (window.Opt) { Opt.raceId = Lab.sel; Opt.def = Lab.editDef; Opt.courseId = null; Opt.result = null; }
  location.hash = "#gameplan";
}
async function geocodeMark(ci, mi) {
  const m = Lab.editDef.courses[ci].marks[mi];
  const msg = document.getElementById("courseMsg");
  msg.textContent = `Geocoding "${m.name}"…`;
  try {
    // Query the place name alone — appending the body of water (e.g. "Lake Huron") makes Nominatim
    // miss. The human verifies the proposed hit on the map (the display_name is shown) before saving.
    const r = await (await apiPost("/api/geocode", { q: m.name })).json();
    const hit = (r.results || [])[0];
    if (!hit) { msg.textContent = `No geocode match for "${m.name}" — enter coords manually.`; return; }
    m.lat = hit.lat; m.lon = hit.lon; m.coords_source = "approx";
    paintCourse();   // re-render from Lab.editDef (preserves the edit) — then set the message
    document.getElementById("courseMsg").textContent =
      `"${m.name}" → ${hit.lat}, ${hit.lon} (${hit.display_name}). VERIFY on the map, then Save.`;
  } catch (e) { msg.textContent = "Geocode failed."; }
}
async function saveCourse() {
  const btn = document.getElementById("saveCourseBtn");
  const msg = document.getElementById("courseMsg");
  btn.disabled = true; msg.textContent = "Saving…";
  // mark the review touched in provenance
  Lab.editDef.provenance = Lab.editDef.provenance || {};
  Lab.editDef.provenance.review_status =
    "human-reviewed in the Course & Marks tab — verify before race use.";
  try {
    const r = await (await apiPost("/api/races", { definition: Lab.editDef })).json();
    Lab.races = null;
    msg.textContent = r.saved ? `Saved. ${r.warnings.length} item(s) still flagged for review.`
      : ("Save failed: " + (r.detail || ""));
  } catch (e) { msg.textContent = "Save failed."; }
  btn.disabled = false;
}
function drawCourseMap(id, course) {
  const cv = document.getElementById(id); if (!cv) return;
  const ctx = cv.getContext("2d"); const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const pts = [];
  if (course.start && course.start.lat != null)
    pts.push({ lat: course.start.lat, lon: course.start.lon, label: "Start", kind: "start" });
  (course.marks || []).forEach((m) => {
    if (m.lat != null) pts.push({ lat: m.lat, lon: m.lon, label: m.name, kind: m.type });
    if (m.lat2 != null) pts.push({ lat: m.lat2, lon: m.lon2, label: m.name + " (NE)", kind: "gate" });
  });
  ((course.finish || {}).points || []).forEach((p) => {
    if (p && p.lat != null) pts.push({ lat: p.lat, lon: p.lon, label: "Finish", kind: "finish" });
  });
  ctx.fillStyle = "#8aa0b4"; ctx.font = "12px system-ui";
  if (pts.length < 1) { ctx.fillText("No coordinates yet — fill marks below.", 16, 24); return; }
  const lats = pts.map((p) => p.lat), lons = pts.map((p) => p.lon);
  const meanlat = (Math.min(...lats) + Math.max(...lats)) / 2;
  const kx = Math.cos(meanlat * Math.PI / 180);
  const xs = pts.map((p) => p.lon * kx), ys = pts.map((p) => p.lat);
  let minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
  const pad = 50, spanx = (maxx - minx) || 0.01, spany = (maxy - miny) || 0.01;
  const sc = Math.min((W - 2 * pad) / spanx, (H - 2 * pad) / spany);
  const X = (p) => pad + (p.lon * kx - minx) * sc;
  const Y = (p) => H - (pad + (p.lat - miny) * sc);   // north up
  // gate / finish lines (pairs sharing a name)
  ctx.strokeStyle = "#7ee0a8"; ctx.lineWidth = 2;
  (course.marks || []).forEach((m) => {
    if (m.lat != null && m.lat2 != null) {
      ctx.beginPath(); ctx.moveTo(X({ lon: m.lon }), Y({ lat: m.lat }));
      ctx.lineTo(X({ lon: m.lon2 }), Y({ lat: m.lat2 })); ctx.stroke();
    }
  });
  const fp = ((course.finish || {}).points || []).filter((p) => p && p.lat != null);
  if (fp.length === 2) {
    ctx.strokeStyle = "#f5c451";
    ctx.beginPath(); ctx.moveTo(X(fp[0]), Y(fp[0])); ctx.lineTo(X(fp[1]), Y(fp[1])); ctx.stroke();
  }
  pts.forEach((p) => {
    ctx.fillStyle = p.kind === "finish" ? "#f5c451" : p.kind === "gate" ? "#7ee0a8" : "#36b3ff";
    ctx.beginPath(); ctx.arc(X(p), Y(p), 5, 0, 7); ctx.fill();
    ctx.fillStyle = "#e8eef4"; ctx.fillText(p.label, X(p) + 8, Y(p) + 4);
  });
}

/* ---------- placeholders ---------- */
const SOON = {
  rules: ["Rules, Safety & Checklists", "The full prep checklist the team works through — every SER + procedural item, with the race-time subset flagged to push to the iPad."],
  fleet: ["Fleet", "Competitor roster + ORC handicaps (entry-list import, MMSI matching) for handicap-aware, corrected-time tactics."],
  learnings: ["Learnings", "The boat-level library (refined polars, crossovers, calibration, fatigue/helm-skill) and what's applied to this regatta."],
  deploy: ["Lock-in & Deploy", "Freeze the playbook and push the homework (course, checklists, playbook) to the Pi / Orin for the race."],
  monitor: ["Monitor", "Shore-side live view during the race (the boat itself uses the onboard console)."],
  debrief: ["Debrief", "The post-race judge loop — regret analysis and write-back review that feeds the next prep."],
};
function renderPlaceholder(sec) {
  const [title, desc] = SOON[sec] || ["Section", "Coming soon."];
  document.getElementById("view").innerHTML =
    `<div class="placeholder"><h2>${esc(title)}</h2><p>${esc(desc)}</p>
     <p class="muted">Coming soon — the Races tab (ingest + review) is live now.</p></div>`;
}

/* ---------- Gameplan / Optimizer (Lab-1) ---------- */
const Opt = { races: null, models: null, raceId: null, def: null, courseId: null,
  chosen: null, running: false, result: null };

async function renderGameplan() {
  const view = document.getElementById("view");
  if (!Opt.races) {
    view.innerHTML = '<div class="loading">Loading…</div>';
    try {
      Opt.races = (await (await apiGet("/api/races")).json()).races || [];
      const md = await (await apiGet("/api/models")).json();
      Opt.models = md.models; Opt.defaultModels = md.default;
      Opt.chosen = Opt.chosen || md.default.slice();
    } catch (e) { view.innerHTML = '<div class="placeholder">Failed to load optimizer.</div>'; return; }
  }
  if (!Opt.raceId && Opt.races.length) await optPickRace(Opt.races[0].race_id, false);
  view.innerHTML = `<div class="opt">
    <div class="card">
      <h3>Gameplan / Optimizer <span class="muted" style="font-weight:400">— multi-model GRIB route (Lab-1)</span></h3>
      <div class="opt-controls">
        <label>Race
          <select id="optRace" onchange="optPickRace(this.value)">
            ${Opt.races.map((r) => `<option value="${esc(r.race_id)}" ${r.race_id === Opt.raceId ? "selected" : ""}>${esc(r.name)}</option>`).join("")}
          </select></label>
        <label>Course <select id="optCourse" onchange="Opt.courseId=this.value">${optCourseOpts()}</select></label>
        <label>Start (UTC) <input type="datetime-local" id="optStart"></label>
      </div>
      <div class="opt-models">${optModelChecks()}</div>
      <div class="opt-controls">
        <label>Ensemble members <input type="number" id="optEns" value="0" min="0" max="30" style="width:64px"> <span class="muted">(GEFS/ECMWF-ENS only; 0 = deterministic)</span></label>
        <label class="optchk"><input type="checkbox" id="optAvoid" checked> Avoid land/islands/zones</label>
        <button id="optRun" onclick="runOptimize()" ${Opt.running ? "disabled" : ""}>${Opt.running ? "Optimizing…" : "Run optimizer →"}</button>
      </div>
      <div class="muted" style="font-size:12px;margin-top:6px">Downloads live GRIB from NOAA NOMADS / ECMWF and routes the course on the SR33 polars. First run ~30–60 s (then cached). Pre-race cloud homework — frozen at the gun (RRS 41).</div>
    </div>
    <div id="optOut"></div></div>`;
  if (Opt.result) renderOptResult(Opt.result);
}

function optCourseOpts() {
  const cs = (Opt.def && Opt.def.courses) || [];
  if (!Opt.courseId && cs.length) Opt.courseId = cs[0].id;
  return cs.map((c) => `<option value="${esc(c.id)}" ${c.id === Opt.courseId ? "selected" : ""}>${esc(c.name || c.id)}</option>`).join("");
}
function optModelChecks() {
  const m = Opt.models || {};
  return Object.keys(m).map((k) => {
    const on = Opt.chosen.includes(k);
    const ens = m[k].kind === "ensemble" ? ` <span class="muted">(ens ${m[k].members})</span>` : "";
    return `<label class="optchk"><input type="checkbox" value="${k}" ${on ? "checked" : ""} onchange="optToggle('${k}',this.checked)"> ${esc(k.toUpperCase())}${ens}</label>`;
  }).join("");
}
function optToggle(k, on) {
  Opt.chosen = Opt.chosen.filter((x) => x !== k);
  if (on) Opt.chosen.push(k);
}
async function optPickRace(id, rerender = true) {
  Opt.raceId = id; Opt.courseId = null; Opt.result = null;
  try { Opt.def = await (await apiGet("/api/races/" + encodeURIComponent(id))).json(); }
  catch (e) { Opt.def = null; }
  if (rerender) renderGameplan();
}

async function runOptimize() {
  const out = document.getElementById("optOut");
  const ens = parseInt(document.getElementById("optEns").value || "0", 10) || 0;
  const startVal = document.getElementById("optStart").value;
  const avoidEl = document.getElementById("optAvoid");
  const body = { race_id: Opt.raceId, course_id: Opt.courseId, models: Opt.chosen, ensemble_members: ens,
    avoid_land: avoidEl ? avoidEl.checked : true };
  if (startVal) body.start_epoch = Date.parse(startVal + "Z") / 1000;
  Opt.running = true;
  document.getElementById("optRun").disabled = true;
  document.getElementById("optRun").textContent = "Optimizing… (downloading GRIB + routing)";
  out.innerHTML = '<div class="card"><div class="loading">Building the multi-model wind field and routing the course…</div></div>';
  try {
    const res = await apiPost("/api/optimize", body);
    const r = await res.json();
    Opt.result = r; Opt.running = false;
    renderGameplan();
  } catch (e) {
    Opt.running = false;
    out.innerHTML = '<div class="card"><div class="placeholder">Optimize failed — ' + esc(String(e)) + '</div></div>';
    const b = document.getElementById("optRun"); if (b) { b.disabled = false; b.textContent = "Run optimizer →"; }
  }
}

function renderOptResult(r) {
  const out = document.getElementById("optOut"); if (!out) return;
  if (!r.available) {
    out.innerHTML = `<div class="card"><div class="placeholder">No route: ${esc(r.note || "unavailable")}</div>
      ${r.log ? `<div class="muted" style="font-size:12px">${r.log.map(esc).join("<br>")}</div>` : ""}</div>`;
    return;
  }
  const conf = r.route_confidence;
  const confCls = conf == null ? "" : conf >= 0.6 ? "ok" : conf >= 0.4 ? "warn" : "bad";
  out.innerHTML = `<div class="opt-result">
    <div class="card">
      <h3>Optimal route</h3>
      <div class="opt-stats">
        <div><b>${r.total_hours}</b><span>hours</span></div>
        <div><b>${r.total_sailed_nm}</b><span>nm sailed</span></div>
        <div><b>${r.total_direct_nm}</b><span>nm direct</span></div>
        <div><b>${r.total_tacks}</b><span>tacks/gybes</span></div>
        <div><b class="conf ${confCls}">${conf == null ? "—" : conf}</b><span>confidence (min ${r.min_confidence == null ? "—" : r.min_confidence})</span></div>
      </div>
      <canvas id="optMap" class="coursemap" width="660" height="380"></canvas>
      ${optObstacleNote(r)}
      ${r.timed_out ? '<div class="pill warn">routing hit the time budget — route is best-effort</div>' : ""}
      ${(r.skipped_marks || []).length ? `<div class="muted" style="font-size:12px">Marks skipped (no coords — review in Course &amp; Marks): ${r.skipped_marks.map(esc).join(", ")}</div>` : ""}
    </div>
    <div class="card"><h3>Legs</h3>
      <table class="legs"><thead><tr><th>To</th><th>Min</th><th>Point of sail</th><th>Tacks</th><th>TWS</th><th>TWD</th><th>Conf</th></tr></thead>
      <tbody>${r.legs.map(optLegRow).join("")}</tbody></table></div>
    <div class="card"><h3>Briefing</h3><pre class="briefing">${esc(r.briefing || "")}</pre></div>
    <div class="card"><h3>Wind field</h3>
      <div class="muted" style="font-size:12px">${(r.windfield.models || []).map((m) =>
        `${esc(m.model.toUpperCase())} ${esc(m.cycle)} — ${m.frames} frames`).join(" · ")} · ${r.windfield.total_frames} frames total</div>
    </div></div>`;
  drawRoute("optMap", r);
}
function optLegRow(l) {
  const w = l.wind || {};
  const c = w.confidence, cc = c == null ? "" : c >= 0.6 ? "ok" : c >= 0.4 ? "warn" : "bad";
  return `<tr><td>${esc(l.to)}</td><td>${l.leg_minutes}</td><td>${esc(l.point_of_sail || "—")}</td>
    <td>${l.tacks}</td><td>${w.tws ?? "—"}</td><td>${w.twd ?? "—"}°</td>
    <td><span class="conf ${cc}">${c ?? "—"}</span></td></tr>`;
}

function optObstacleNote(r) {
  const ob = r.obstacles || {};
  if (!ob.active) return '<div class="muted" style="font-size:12px;margin-top:6px">Obstacle avoidance off — route may cross land/islands.</div>';
  const L = ob.layers || {};
  return `<div class="muted" style="font-size:12px;margin-top:6px">⛰ Obstacle avoidance ON — route steered around land/islands/zones
    (${r.obstacle_steps_avoided || 0} candidate steps rejected; layers: coastline ${L.coastline || 0}, islands ${L.islands || 0}, zones ${L.zones || 0} cells).
    Coastline is Natural Earth 1:10m (${esc(ob.data_version || "")}) + this race's islands/zones — coarse near shore; verify against the chart.</div>`;
}

function drawRoute(id, r) {
  const cv = document.getElementById(id); if (!cv) return;
  const ctx = cv.getContext("2d"); const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const path = r.path || [];
  if (path.length < 2) { ctx.fillStyle = "#8aa0b4"; ctx.fillText("No path.", 16, 24); return; }
  const lats = path.map((p) => p.lat), lons = path.map((p) => p.lon);
  const meanlat = (Math.min(...lats) + Math.max(...lats)) / 2;
  const kx = Math.cos(meanlat * Math.PI / 180);
  const xs = path.map((p) => p.lon * kx), ys = path.map((p) => p.lat);
  const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
  const pad = 46, spanx = (maxx - minx) || 0.01, spany = (maxy - miny) || 0.01;
  const sc = Math.min((W - 2 * pad) / spanx, (H - 2 * pad) / spany);
  const X = (p) => pad + (p.lon * kx - minx) * sc;
  const Y = (p) => H - (pad + (p.lat - miny) * sc);
  // obstacle overlay (land/islands/zones) drawn UNDER the track
  const geo = (r.obstacles || {}).geometry || {};
  const px = (lat, lon) => pad + (lon * kx - minx) * sc, py = (lat, lon) => H - (pad + (lat - miny) * sc);
  ctx.fillStyle = "rgba(120,134,148,0.28)"; ctx.strokeStyle = "rgba(150,164,178,0.5)"; ctx.lineWidth = 1;
  (geo.land_rings || []).forEach((ring) => {
    if (ring.length < 3) return; ctx.beginPath();
    ring.forEach((c, i) => i ? ctx.lineTo(px(c[0], c[1]), py(c[0], c[1])) : ctx.moveTo(px(c[0], c[1]), py(c[0], c[1])));
    ctx.closePath(); ctx.fill(); ctx.stroke();
  });
  (geo.islands || []).forEach((is) => {
    const cx = px(is.lat, is.lon), cy = py(is.lat, is.lon), rr = Math.max(3, is.radius_nm / 60 * sc);
    ctx.fillStyle = "rgba(245,150,70,0.20)"; ctx.strokeStyle = "rgba(245,150,70,0.7)"; ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.arc(cx, cy, rr, 0, 7); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#f5a64a"; ctx.font = "11px system-ui"; ctx.fillText(is.name, cx + rr + 3, cy + 4);
  });
  ctx.fillStyle = "rgba(255,90,90,0.16)"; ctx.strokeStyle = "rgba(255,90,90,0.8)"; ctx.lineWidth = 1.5;
  (geo.zones || []).forEach((z) => {
    const ring = z.ring || []; if (ring.length < 3) return; ctx.beginPath();
    ring.forEach((c, i) => i ? ctx.lineTo(px(c[0], c[1]), py(c[0], c[1])) : ctx.moveTo(px(c[0], c[1]), py(c[0], c[1])));
    ctx.closePath(); ctx.fill(); ctx.stroke();
  });
  // optimized track
  ctx.strokeStyle = "#36b3ff"; ctx.lineWidth = 2.5; ctx.beginPath();
  path.forEach((p, i) => i ? ctx.lineTo(X(p), Y(p)) : ctx.moveTo(X(p), Y(p)));
  ctx.stroke();
  // start + finish
  const s = path[0], f = path[path.length - 1];
  ctx.fillStyle = "#7ee0a8"; ctx.beginPath(); ctx.arc(X(s), Y(s), 6, 0, 7); ctx.fill();
  ctx.fillStyle = "#f5c451"; ctx.beginPath(); ctx.arc(X(f), Y(f), 6, 0, 7); ctx.fill();
  ctx.fillStyle = "#e8eef4"; ctx.font = "12px system-ui";
  ctx.fillText("Start", X(s) + 9, Y(s) + 4); ctx.fillText("Finish", X(f) + 9, Y(f) + 4);
}
