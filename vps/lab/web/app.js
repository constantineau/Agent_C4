/* C4 Performance Lab shell — shared team login, hash-routed sections, the Races library +
   RaceDefinition review view + dual-input ingestion (URL / paste-link / upload → Opus → review →
   save). Vanilla JS, no build. */
"use strict";

const Lab = { token: sessionStorage.getItem("c4lab.token") || null,
  races: null, sel: null, sources: [], draft: null,
  editDef: null, editVal: null, isDraft: false };

/* ---------- editable-field binding (the review form writes straight into Lab.editDef) ----------
   Text/select/checkbox edits write in place via eset() with NO repaint, so focus + scroll are
   never lost mid-type; only structural changes (add/remove a row) or Save/Approve repaint. */
const MARK_TYPES = ["start", "waypoint", "gate", "island", "buoy", "finish"];
const ROUNDINGS = ["none", "port", "starboard", "gate"];
const REQ_CATEGORIES = ["safety", "structural", "crew_safety", "navigation", "communications",
  "registration", "procedure", "reporting", "environmental", "rules"];
const REQ_PHASES = ["pre_entry", "pre_start", "start", "in_race", "at_gate", "at_finish", "post_race"];
const TRIGGER_TYPES = ["none", "time", "event", "location"];

function eset(path, value) {
  const parts = path.split(".");
  let o = Lab.editDef;
  for (let i = 0; i < parts.length - 1 && o != null; i++) {
    const k = parts[i]; o = o[/^\d+$/.test(k) ? +k : k];
  }
  if (o != null) o[parts[parts.length - 1]] = value;
}
function esetNum(path, raw) {
  const t = String(raw).trim();
  if (t === "") return eset(path, null);
  const v = Number(t); if (!Number.isNaN(v)) eset(path, v);
}
const attr = (s) => esc(s).replace(/'/g, "&#39;");
function ein(path, val, ph) {
  return `<input class="ein" value="${attr(val == null ? "" : val)}" placeholder="${attr(ph || "")}"
    oninput="eset('${path}', this.value)">`;
}
function enm(path, val, ph) {
  return `<input class="ein num" value="${val == null ? "" : val}" placeholder="${attr(ph || "")}"
    inputmode="decimal" oninput="esetNum('${path}', this.value)">`;
}
function etxt(path, val, ph) {
  return `<textarea class="ein ta" placeholder="${attr(ph || "")}"
    oninput="eset('${path}', this.value)">${esc(val == null ? "" : val)}</textarea>`;
}
function esel(path, val, opts) {
  return `<select class="esel" onchange="eset('${path}', this.value)">` +
    opts.map((o) => `<option ${o === val ? "selected" : ""} value="${attr(o)}">${esc(o)}</option>`).join("") +
    `</select>`;
}
function etri(path, val) {   /* yes / no / unknown tri-state for tracker_permitted etc. */
  const cur = val === true ? "yes" : val === false ? "no" : "unknown";
  return `<select class="esel" onchange="eset('${path}', this.value==='yes'?true:this.value==='no'?false:null)">` +
    ["unknown", "yes", "no"].map((o) => `<option ${o === cur ? "selected" : ""}>${o}</option>`).join("") + `</select>`;
}
function echk(path, val, label) {
  return `<label class="echk"><input type="checkbox" ${val ? "checked" : ""}
    onchange="eset('${path}', this.checked)"> ${esc(label)}</label>`;
}

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
/* Parse a response as JSON, but turn an HTML error page (a gateway 502/504 timeout — what you get
   when a long route/weather download outlives the proxy) into a clear message instead of a raw
   "Unexpected token '<'" JSON SyntaxError. */
async function jsonOrFriendly(res) {
  const txt = await res.text();
  try { return JSON.parse(txt); } catch (e) {
    if (res.status === 502 || res.status === 504 || /^\s*</.test(txt))
      throw new Error("the request timed out at the gateway — a weather source is likely slow/rate-limited. Try fewer models (uncheck ECMWF) and re-run.");
    throw new Error(`server returned ${res.status}${res.statusText ? " " + res.statusText : ""} (non-JSON response)`);
  }
}
function boot() { if (Lab.token) { document.getElementById("gate").style.display = "none"; start(); } }
window.addEventListener("DOMContentLoaded", boot);

/* ---------- router ---------- */
function start() { window.addEventListener("hashchange", route); route(); }
// The section currently being viewed. Async renderers fetch then paint #view; if the user switched
// tabs mid-fetch the late paint would land in the wrong tab — so each renderer checks stale() after
// its awaits before writing.
let activeSec = "";
const stale = (s) => activeSec !== s;
function route() {
  const sec = (location.hash || "#races").slice(1);
  activeSec = sec;
  document.querySelectorAll("#tabs a").forEach((a) =>
    a.classList.toggle("active", a.getAttribute("href") === "#" + sec));
  if (sec === "races") return renderRaces();
  if (sec === "course") return renderCourse();
  if (sec === "gameplan") return renderGameplan();
  if (sec === "deploy") return renderDeploy();
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
  if (stale("races")) return;
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
  const rev = r.reviewed ? `<span class="pill ok">✓ approved</span>`
    : r.errors ? `<span class="pill bad">${r.errors} errors</span>`
    : (r.warnings ? `<span class="pill warn">${r.warnings} to review</span>`
      : `<span class="pill warn">awaiting approval</span>`);
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
    Lab.editDef = d; Lab.editVal = v; Lab.isDraft = false;
    paintDetail();
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
    Lab.draft = resp; Lab.editDef = resp.definition;
    Lab.editVal = { errors: resp.errors || [], warnings: resp.warnings || [] }; Lab.isDraft = true;
    msg.textContent = "Draft extracted — review/edit it on the right, then save.";
    paintDetail();
  } catch (e) { msg.textContent = "Ingest failed."; }
  btn.disabled = false;
}
async function saveDraft() {
  if (!Lab.editDef) return;
  try {
    const r = await (await apiPost("/api/races", { definition: Lab.editDef })).json();
    if (!r.saved) { alert("Save failed: " + (r.detail || "unknown")); return; }
    Lab.races = null; Lab.sel = r.race_id; Lab.draft = null; Lab.sources = []; Lab.isDraft = false;
    renderRaces();
  } catch (e) { alert("Save failed."); }
}

/* ---------- detail rendering (editable review form, bound to Lab.editDef) ----------
   Text/select/checkbox edits write in place (eset, no repaint → focus preserved); only structural
   changes (add/remove a row) and Save/Approve repaint. Field paths address into Lab.editDef. */
function paintDetail() {
  const box = document.getElementById("raceDetail");
  if (!box) return;
  const d = Lab.editDef, v = Lab.editVal || {};
  if (!d) { box.innerHTML = '<div class="placeholder">Select a race.</div>'; return; }
  box.innerHTML = detailActions(d, v) + detailBanner(v) + detailHead(d) +
    (d.courses || []).map((c, i) => detailCourseCard(c, i)).join("") +
    detailChecklist(d.requirements || []) +
    detailRules(d.rules_profile || {}) +
    detailProvenance(d.provenance || {});
}
function detailBanner(v) {
  const errs = (v.errors || []), warns = (v.warnings || []);
  return errs.length
    ? `<div class="banner review"><b>${errs.length} errors</b>: ${errs.map(esc).join(" · ")}</div>`
    : warns.length
      ? `<div class="banner review"><b>Needs human review (${warns.length}):</b> ${warns.map(esc).join(" · ")}</div>`
      : `<div class="banner ok">Validated — ready for review sign-off.</div>`;
}
function detailActions(d, v) {
  if (Lab.isDraft) {
    return `<div class="banner draft"><b>DRAFT — machine-extracted, needs human review.</b>
      Edit any field below, then save to the library.
      <button class="mini" onclick="saveDraft()">Save to library</button></div>`;
  }
  const hasErrs = (v.errors || []).length > 0;
  const sign = d.reviewed
    ? `<span class="pill ok">✓ Approved${d.reviewed_at ? " · " + esc(d.reviewed_at) : ""}</span>
       <button class="mini" onclick="detailApprove(false)">Un-approve</button>`
    : `<button class="mini" onclick="detailApprove(true)"${hasErrs ? " disabled title='fix the errors first'" : ""}>Approve &amp; sign off</button>`;
  return `<div class="dactions">
    <button id="detailSaveBtn" onclick="detailSave()">Save edits</button>
    ${sign}<span id="detailMsg" class="muted" style="font-size:12px"></span></div>`;
}
function detailHead(d) {
  return `<div class="dhead"><h2>${ein("name", d.name, "Race name")}</h2>
      <div class="dmeta">${ein("organizing_authority", d.organizing_authority, "organizing authority")}<br>
        Start ${ein("start_date", d.start_date, "YYYY-MM-DD")} ·
        ${ein("start_area", d.start_area, "start area")} ·
        ${ein("region", d.region, "region")}</div></div>`;
}
function detailCourseCard(c, ci) {
  const marks = (c.marks || []).map((m, mi) => `<tr>
    <td class="mono">${m.seq}</td>
    <td>${ein(`courses.${ci}.marks.${mi}.name`, m.name, "name")}</td>
    <td>${esel(`courses.${ci}.marks.${mi}.type`, m.type, MARK_TYPES)}</td>
    <td>${esel(`courses.${ci}.marks.${mi}.rounding`, m.rounding || "none", ROUNDINGS)}</td>
    <td>${enm(`courses.${ci}.marks.${mi}.lat`, m.lat, "lat")} ${enm(`courses.${ci}.marks.${mi}.lon`, m.lon, "lon")}</td>
    <td><button class="mini" title="remove" onclick="detailRmMark(${ci},${mi})">✕</button></td></tr>`).join("");
  const fin = c.finish ? `<tr><td class="mono">F</td><td>Finish (${esc(c.finish.type)})</td>
    <td colspan="2">${esc(c.finish.crossing || "")}</td>
    <td class="mono">${(c.finish.points || []).map((p) =>
      p && p.lat != null ? esc(p.lat.toFixed(4) + "," + p.lon.toFixed(4)) : "—").join(" → ")}</td><td></td></tr>` : "";
  return `<div class="card"><h3>Course — ${ein(`courses.${ci}.name`, c.name, "course name")}</h3>
    <div class="muted" style="margin-bottom:8px">Divisions ${esc((c.applies_to_divisions || []).join(", "))}${
      c.distance_nm ? " · " + c.distance_nm + " nm" : ""}</div>
    <table><thead><tr><th>#</th><th>Mark</th><th>Type</th><th>Leave</th><th>Lat / Lon</th><th></th></tr></thead>
    <tbody>${marks}${fin}</tbody></table>
    <button class="mini" onclick="detailAddMark(${ci})">+ Add mark</button>
    <div class="muted" style="font-size:12px;margin-top:6px">Geocode marks + see them on a map in the
      <a href="#course">Course &amp; Marks</a> tab.</div></div>`;
}
function detailChecklist(reqs) {
  const ipad = reqs.filter((r) => r.deliver_to_ipad).length;
  const rows = reqs.map((r, i) => detailReqRow(r, i)).join("");
  return `<div class="card"><h3>Rules, Safety &amp; Checklists — ${reqs.length} items
      (${ipad} pushed to the iPad)</h3>
    ${rows || '<div class="muted">No checklist items.</div>'}
    <button class="mini" onclick="detailAddReq()">+ Add item</button></div>`;
}
function detailReqRow(r, i) {
  return `<div class="req edit"><div class="body">
      ${etxt(`requirements.${i}.text`, r.text, "requirement text")}
      <div class="reqmeta">
        ${esel(`requirements.${i}.category`, r.category, REQ_CATEGORIES)}
        ${esel(`requirements.${i}.phase`, r.phase, REQ_PHASES)}
        ${esel(`requirements.${i}.trigger_type`, r.trigger_type || "none", TRIGGER_TYPES)}
        ${ein(`requirements.${i}.trigger_detail`, r.trigger_detail, "trigger detail")}
        ${echk(`requirements.${i}.critical`, r.critical, "critical")}
        ${echk(`requirements.${i}.deliver_to_ipad`, r.deliver_to_ipad, "→iPad")}
      </div>
      ${ein(`requirements.${i}.source`, r.source, "source (NOR/SER §)")}</div>
    <div class="pills"><button class="mini" title="remove" onclick="detailRmReq(${i})">✕</button></div></div>`;
}
function detailRules(rp) {
  const mods = (rp.modifications || []).map((m, i) =>
    `<tr><td>${ein(`rules_profile.modifications.${i}.ref`, m.ref, "ref")}</td>
      <td>${ein(`rules_profile.modifications.${i}.rule`, m.rule, "rule")}</td>
      <td>${ein(`rules_profile.modifications.${i}.summary`, m.summary, "modification")}</td>
      <td><button class="mini" title="remove" onclick="detailRmMod(${i})">✕</button></td></tr>`).join("");
  const sc = rp.scoring || {};
  return `<div class="card"><h3>Rules &amp; scoring</h3>
    <div class="reqmeta" style="margin-bottom:10px">
      <label class="muted">RRS ${ein("rules_profile.rrs_edition", rp.rrs_edition, "edition")}</label>
      ${echk("rules_profile.appendix_wp", rp.appendix_wp, "Appendix WP")}
      <label class="muted">Tracker permitted ${etri("rules_profile.tracker_permitted", rp.tracker_permitted)}</label>
    </div>
    <table><thead><tr><th>Ref</th><th>Rule</th><th>Modification</th><th></th></tr></thead>
      <tbody>${mods}</tbody></table>
    <button class="mini" onclick="detailAddMod()">+ Add modification</button>
    <div style="margin-top:12px"><b>Scoring:</b>
      ${ein("rules_profile.scoring.system", sc.system, "system")}
      ${ein("rules_profile.scoring.method", sc.method, "method")}
      ${etxt("rules_profile.scoring.decided", sc.decided, "how / when decided")}</div></div>`;
}
function detailProvenance(p) {
  const srcs = (p.sources || []).map((s) =>
    `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a>
     <span class="muted">${esc(s.retrieved || "")}</span></li>`).join("");
  return `<div class="card"><h3>Provenance &amp; review</h3>
    <ul style="margin:0 0 10px;padding-left:18px">${srcs || '<li class="muted">No sources recorded.</li>'}</ul>
    <label class="muted" style="font-size:12px">SI status ${ein("provenance.si_status", p.si_status, "SI status")}</label>
    <label class="muted" style="font-size:12px">Review notes ${etxt("provenance.review_status", p.review_status, "review notes")}</label></div>`;
}

/* ---------- detail edit: structural mutations (repaint), then save / approve ---------- */
function detailAddMark(ci) {
  const marks = (Lab.editDef.courses[ci].marks = Lab.editDef.courses[ci].marks || []);
  marks.push({ seq: marks.length + 1, name: "New mark", type: "waypoint", rounding: "none",
    lat: null, lon: null, coords_source: "needs_review" });
  paintDetail();
}
function detailRmMark(ci, mi) {
  const marks = Lab.editDef.courses[ci].marks;
  marks.splice(mi, 1);
  marks.forEach((m, i) => { m.seq = i + 1; });   // re-sequence
  paintDetail();
}
function detailAddReq() {
  (Lab.editDef.requirements = Lab.editDef.requirements || []).push(
    { id: "req_" + Date.now(), text: "New requirement", category: "procedure", phase: "pre_start",
      trigger_type: "none", critical: false, deliver_to_ipad: false, source: "" });
  paintDetail();
}
function detailRmReq(i) { Lab.editDef.requirements.splice(i, 1); paintDetail(); }
function detailAddMod() {
  const rp = (Lab.editDef.rules_profile = Lab.editDef.rules_profile || {});
  (rp.modifications = rp.modifications || []).push({ ref: "", rule: "", summary: "" });
  paintDetail();
}
function detailRmMod(i) { Lab.editDef.rules_profile.modifications.splice(i, 1); paintDetail(); }

async function detailSave(silent) {
  const btn = document.getElementById("detailSaveBtn");
  const setMsg = (t) => { const m = document.getElementById("detailMsg"); if (m) m.textContent = t; };
  if (btn) btn.disabled = true;
  if (!silent) setMsg("Saving…");
  try {
    const r = await (await apiPost("/api/races", { definition: Lab.editDef })).json();
    if (!r.saved) { setMsg("Save failed: " + (r.detail || "?")); return false; }
    Lab.editVal = { errors: r.errors || [], warnings: r.warnings || [] };
    Lab.races = null;
    if (!silent) {
      paintDetail(); refreshRaceList();
      setMsg(`Saved. ${(r.warnings || []).length} item(s) still flagged for review.`);
    }
    return true;
  } catch (e) { setMsg("Save failed."); return false; }
  finally { if (btn) btn.disabled = false; }
}
async function detailApprove(approved) {
  const setMsg = (t) => { const m = document.getElementById("detailMsg"); if (m) m.textContent = t; };
  try {
    const r = await (await apiPost(`/api/races/${encodeURIComponent(Lab.editDef.race_id)}/approve`,
      { definition: Lab.editDef, approved })).json();
    if (r.detail) { paintDetail(); setMsg(r.detail); return; }   // e.g. blocked by validation errors
    Lab.editDef.reviewed = r.reviewed; Lab.editDef.reviewed_at = r.reviewed_at;
    Lab.editVal = { errors: r.errors || [], warnings: r.warnings || [] };
    Lab.races = null;
    paintDetail(); refreshRaceList();
    setMsg(approved ? "Approved — signed off for race use." : "Approval cleared.");
  } catch (e) { setMsg("Approve failed."); }
}
async function refreshRaceList() {
  try {
    Lab.races = (await (await apiGet("/api/races")).json()).races || [];
    const el = document.getElementById("racelist");
    if (el) el.innerHTML = Lab.races.map(raceItem).join("");
  } catch (e) {}
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
  if (stale("course")) return;
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
  chosen: null, running: false, result: null, resolution: "auto", start: "" };

// The race venue's IANA timezone, if the loaded RaceDefinition carries one.
function optTz() { return (Opt.def && Opt.def.timezone) || ""; }

// Offset (ms) of `tz` at `date`: (the same wall-clock read as if it were UTC) − the real UTC.
function tzOffsetMs(tz, date) {
  const dtf = new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const p = {};
  for (const part of dtf.formatToParts(date)) p[part.type] = part.value;
  const asUTC = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  return asUTC - date.getTime();
}

// A wall-clock time (y/mo/d/h/mi) interpreted in `tz` → epoch seconds (DST-aware, two-pass).
function zonedWallToEpoch(y, mo, d, h, mi, tz) {
  const naive = Date.UTC(y, mo - 1, d, h, mi);
  const o1 = tzOffsetMs(tz, new Date(naive));
  const o2 = tzOffsetMs(tz, new Date(naive - o1));   // settle DST-boundary cases
  return (naive - o2) / 1000;
}

// Parse the "Start" field — a locale-independent 24h "YYYY-MM-DD HH:MM" (space or T).
// Interpreted in the race's local timezone when one is known, else as UTC.
// Returns epoch seconds, or null if blank/invalid.
function optStartEpoch() {
  const m = (Opt.start || "").trim().match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})$/);
  if (!m) return null;
  const [, y, mo, d, h, mi] = m.map(Number);
  const tz = optTz();
  return tz ? zonedWallToEpoch(y, mo, d, h, mi, tz)
            : Date.UTC(y, mo - 1, d, h, mi) / 1000;
}

async function renderGameplan() {
  const view = document.getElementById("view");
  if (!Opt.races) {
    view.innerHTML = '<div class="loading">Loading…</div>';
    try {
      Opt.races = (await (await apiGet("/api/races")).json()).races || [];
      const md = await (await apiGet("/api/models")).json();
      Opt.models = md.models; Opt.defaultModels = md.default;
      Opt.chosen = Opt.chosen || md.default.slice();
      await reloadBoats();
    } catch (e) { view.innerHTML = '<div class="placeholder">Failed to load optimizer.</div>'; return; }
  }
  if (!Opt.raceId && Opt.races.length) await optPickRace(Opt.races[0].race_id, false);
  if (stale("gameplan")) return;
  const startTz = optTz();
  const startTitle = (startTz
    ? "24-hour " + startTz + " local time, e.g. 2026-07-18 06:30 — converted to UTC behind the scenes."
    : "24-hour UTC, e.g. 2026-07-19 20:05 for 8:05pm.") + " Leave blank for the freshest forecast cycle.";
  view.innerHTML = `<div class="opt">
    <div class="card">
      <h3>Gameplan / Optimizer <span class="muted" style="font-weight:400">— multi-model GRIB route (Lab-1)</span></h3>
      <div class="opt-groups">
        <div class="opt-group">
          <div class="opt-group-h">Course</div>
          <label>Race
            <select id="optRace" onchange="optPickRace(this.value)">
              ${Opt.races.map((r) => `<option value="${esc(r.race_id)}" ${r.race_id === Opt.raceId ? "selected" : ""}>${esc(r.name)}</option>`).join("")}
            </select></label>
          <label>Course <select id="optCourse" onchange="Opt.courseId=this.value">${optCourseOpts()}</select></label>
          <label>Start (${startTz ? "race local" : "UTC"}) <input type="text" id="optStart" inputmode="numeric"
            placeholder="YYYY-MM-DD HH:MM" pattern="\\d{4}-\\d{2}-\\d{2}[ T]\\d{2}:\\d{2}"
            title="${esc(startTitle)}"
            value="${esc(Opt.start || "")}" oninput="Opt.start=this.value" style="width:150px"></label>
        </div>
        <div class="opt-group">
          <div class="opt-group-h">Boat &amp; charts</div>
          ${optBoatControls()}
          <button class="mini" onclick="toggleBoatModel()">${Opt.showBoatModel ? "Hide" : "Review"} boat model — polars &amp; sail crossovers</button>
          <span class="muted" style="font-size:11px">per-leg sail plan + draft frozen into the playbook → loaded onto the copilot</span>
        </div>
        <div class="opt-group">
          <div class="opt-group-h">Weather models</div>
          <div class="opt-models">${optModelChecks()}</div>
          <label>Ensemble members <input type="number" id="optEns" value="0" min="0" style="width:64px" oninput="updateEnsembleControl()"></label>
          <span id="optEnsHint" class="muted" style="font-size:12px"></span>
          <div id="optEnsCost" class="muted" style="font-size:12px;margin-top:2px"></div>
        </div>
      </div>
      <div id="boatModelOut"></div>
      <div class="opt-run">
        <label class="optchk"><input type="checkbox" id="optAvoid" checked> Avoid land/islands/zones</label>
        <label class="optchk" title="Also route each weather model separately and overlay the candidate routes — the confidence fan made visible (slower)"><input type="checkbox" id="optPerModel"> Per-model route fan <span class="muted">(slower)</span></label>
        <label class="optchk" title="Routing resolution: Fine = finer heading fan + shorter steps (sharper near shore, slower); Fast = coarser (quicker).">Resolution
          <select id="optRes" oninput="updateResHint()">
            <option value="fast"${Opt.resolution === "fast" ? " selected" : ""}>Fast</option>
            <option value="auto"${(Opt.resolution || "auto") === "auto" ? " selected" : ""}>Auto</option>
            <option value="fine"${Opt.resolution === "fine" ? " selected" : ""}>Fine</option>
          </select></label>
        <button id="optRun" onclick="runOptimize()" ${Opt.running ? "disabled" : ""}>${Opt.running ? "Optimizing…" : "Run optimizer →"}</button>
        <span id="optResHint" class="muted" style="font-size:11px"></span>
      </div>
      <div class="muted" style="font-size:12px;margin-top:6px">Downloads live GRIB from NOAA NOMADS / ECMWF and routes the course on the SR33 polars. First run ~30–60 s (then cached). Pre-race cloud homework — frozen at the gun (RRS 41).</div>
    </div>
    <div id="optOut"></div>
    <div class="card">
      <h3>Branching playbook <span class="muted" style="font-weight:400">— forecast fan-out → strategic variants (Lab-2b)</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:8px">Fans the optimizer across each weather model, clusters the routes into <b>which side of the first beat</b> they favor, then has Opus write each variant's rationale / tradeoffs / <b>what-flips-it</b> + a decision tree. <b>Freeze &amp; sign</b> the bundle to carry it onboard (the copilot's <code>PLAYBOOK_PATH</code>) — frozen at the gun.</div>
      <button id="pbRun" onclick="synthPlaybook()" ${Pb.running ? "disabled" : ""}>${Pb.running ? "Synthesizing…" : "Synthesize branching playbook →"}</button>
      <div id="pbOut"></div>
    </div></div>`;
  if (Opt.showBoatModel) renderBoatModel();
  updateEnsembleControl();
  updateResHint();
  if (Opt.result) renderOptResult(Opt.result);
  if (Pb.result) renderPbResult(Pb.result);
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
  updateEnsembleControl();
}

/* The chosen ensemble sources + their real member counts (from /api/models). The ensemble-members
   field only does anything when one is selected, and the request caps each source at this count —
   so the cap is the LARGEST selected ensemble (a smaller source just maxes out internally). */
function optEnsembleInfo() {
  const m = Opt.models || {};
  const ens = Opt.chosen.filter((k) => m[k] && m[k].kind === "ensemble");
  const max = ens.reduce((a, k) => Math.max(a, m[k].members || 0), 0);
  return { names: ens.map((k) => k.toUpperCase()), max };
}

/* Enable/disable + cap + explain the ensemble field reactively (Orca-style clutter-removal: a
   control that's inert until it's meaningful, with a dynamic cap + a cost/diminishing-returns hint).
   Called on render and whenever a model checkbox toggles. */
const RES_HINTS = {
  fast: "Fast — coarse heading fan + longer steps; quickest, less sharp near shore.",
  auto: "Auto — balanced heading fan + step (the default).",
  fine: "Fine — finer heading fan + shorter steps; sharper near shore + tight marks, slower.",
};
function updateResHint() {
  const sel = document.getElementById("optRes"), hint = document.getElementById("optResHint");
  if (!sel || !hint) return;
  hint.textContent = RES_HINTS[sel.value] || "";
}

function updateEnsembleControl() {
  const inp = document.getElementById("optEns");
  const hint = document.getElementById("optEnsHint");
  const cost = document.getElementById("optEnsCost");
  if (!inp) return;
  const { names, max } = optEnsembleInfo();
  const on = max > 0;
  inp.disabled = !on;
  inp.max = on ? max : 0;
  if (!on) { inp.value = "0"; }
  else if (parseInt(inp.value || "0", 10) > max) { inp.value = String(max); }
  inp.style.opacity = on ? "1" : "0.5";
  if (hint) hint.textContent = on
    ? `0 = deterministic; up to ${max} members of ${names.join(" + ")}`
    : "select GEFS or ECMWF-ENS to use ensemble members";
  if (cost) {
    const n = parseInt(inp.value || "0", 10) || 0;
    cost.textContent = on && n > 0
      ? `≈ ${n} member${n > 1 ? "s" : ""} × forecast frames in extra GRIB downloads — 10–20 already samples the spread well; more rarely changes the route.`
      : "";
  }
}

/* Boat profile ([B]): active boat + editable draft (feet) + chart source. Draft sets the ENC
   depth no-go, so it drives the route. */
async function reloadBoats() {
  const bd = await (await apiGet("/api/boats")).json();
  Opt.boats = bd.boats || []; Opt.activeBoat = bd.active; Opt.chartSource = bd.chart_source;
}
function optBoatControls() {
  const ab = (Opt.boats || []).find((b) => b.boat_id === Opt.activeBoat) || {};
  const draftFt = ab.draft_ft != null ? ab.draft_ft : "";
  const depthFt = ab.safety_depth_m != null ? (ab.safety_depth_m / 0.3048).toFixed(1) : "";
  const isEnc = Opt.chartSource === "enc";
  return `<label>Boat
      <select id="optBoat" onchange="optPickBoat(this.value)">
        ${(Opt.boats || []).map((b) => `<option value="${esc(b.boat_id)}" ${b.boat_id === Opt.activeBoat ? "selected" : ""}>${esc(b.name)} (${b.draft_ft != null ? b.draft_ft + " ft" : "?"})</option>`).join("")}
      </select></label>
    <label>Draft (ft) <input type="number" id="optDraft" step="0.1" min="0" value="${draftFt}" style="width:64px" onchange="optSaveDraft()"></label>
    <label>Charts
      <select id="optChart" onchange="optSetChart(this.value)">
        <option value="natural_earth" ${!isEnc ? "selected" : ""}>Natural Earth (coarse)</option>
        <option value="enc" ${isEnc ? "selected" : ""}>NOAA ENC (draft-aware)</option>
      </select></label>
    <span class="muted">${isEnc ? "depth no-go &lt; " + depthFt + " ft (draft + margin)" : "global coastline backstop"}</span>`;
}
async function optPickBoat(id) {
  Opt.activeBoat = id;
  await apiPost("/api/boats/active", { boat_id: id });
  await reloadBoats(); renderGameplan();
}
async function optSetChart(src) {
  Opt.chartSource = src;
  await apiPost("/api/boats/active", { chart_source: src });
  renderGameplan();
}
async function optSaveDraft() {
  const ft = parseFloat(document.getElementById("optDraft").value);
  if (isNaN(ft) || ft <= 0) return;
  const full = (await (await apiGet("/api/boats/" + encodeURIComponent(Opt.activeBoat))).json()).boat || {};
  full.draft_m = Math.round(ft * 0.3048 * 10000) / 10000;          // store metres, enter feet
  await apiPost("/api/boats", full);
  await reloadBoats(); renderGameplan();
}
/* Boat-model review: the polars + sail crossovers frozen into the playbook → loaded onto the copilot */
async function toggleBoatModel() {
  Opt.showBoatModel = !Opt.showBoatModel;
  if (Opt.showBoatModel && !Opt.boatModel) {
    try {
      Opt.boatModel = await (await apiGet("/api/crossovers")).json();
      Opt.polarGrid = await (await apiGet("/api/polars")).json();
    } catch (e) { Opt.boatModel = { error: String(e) }; }
  }
  renderGameplan();
}

function renderBoatModel() {
  const out = document.getElementById("boatModelOut"); if (!out) return;
  const m = Opt.boatModel;
  if (!m) { out.innerHTML = '<div class="loading">Loading boat model…</div>'; return; }
  if (m.error || !m.crossovers) { out.innerHTML = `<div class="placeholder">Boat model unavailable — ${esc(m.error || "no crossover data")}</div>`; return; }
  const tws = m.tws_buckets || [];
  // crossover bands: a horizontal 0–180° TWA axis per TWS, colored per sail
  const bands = tws.map((t) => {
    const zones = (m.crossovers[String(t)] || []).map((z) => {
      const left = (z.twa_min / 180 * 100).toFixed(1);
      const w = ((z.twa_max - z.twa_min) / 180 * 100).toFixed(1);
      return `<div class="xo-zone sail-bg-${esc(z.short)}" style="left:${left}%;width:${w}%" title="${esc(z.label)} ${z.twa_min}–${z.twa_max}°">${esc(z.short)}</div>`;
    }).join("");
    return `<div class="xo-row"><span class="xo-tws">${t} kn</span><div class="xo-track">${zones}</div></div>`;
  }).join("");
  const inv = (m.inventory || []).map((s) => `<span class="sail sail-${esc(s)}">${esc(s)}</span>`).join(" ");
  out.innerHTML = `<div class="card boatmodel">
    <h3>Boat model — polars &amp; sail crossovers</h3>
    <div class="muted" style="font-size:12px;margin-bottom:10px">Source: ${esc(m.source || "—")}. This is the boat sail model the optimizer attaches per leg and <b>freezes into the playbook bundle</b> — what the onboard copilot loads to ground its sail calls. Review before lock-in.</div>
    <div class="bm-inv">Inventory: ${inv}</div>
    ${renderJibCrossovers(m)}
    <h4>Sail crossovers (optimal sail by TWA, per TWS)</h4>
    <div class="muted" style="font-size:11px;margin-bottom:4px">From the ORC cert (one headsail = the jib slot; specialised to J1/J2/J3 by the wind bands above).</div>
    <div class="xo-axis"><span>0°</span><span>45°</span><span>90°</span><span>135°</span><span>180°</span></div>
    <div class="xo">${bands}</div>
    ${renderPolarGrid()}
  </div>`;
}

/* Upwind jib change-downs by TWS (J1/J2/J3) — NOT in the ORC cert (it rates one headsail), so
   these are editable crew/sailmaker thresholds. Two boundaries: J1→J2 and J2→J3. */
function renderJibCrossovers(m) {
  const jc = m.jib_crossovers || [];
  if (!jc.length) return `<div class="muted" style="font-size:12px">No upwind jib change-downs set — the optimizer uses the single cert jib (J1).</div>`;
  const max = 35;     // axis ceiling for the TWS bars (kn)
  const bars = jc.map((b) => {
    const lo = b.tws_min != null ? b.tws_min : 0;
    const hi = b.tws_max != null ? b.tws_max : max;
    const left = (lo / max * 100).toFixed(1), w = ((hi - lo) / max * 100).toFixed(1);
    const rng = b.tws_min == null ? `<${b.tws_max}` : b.tws_max == null ? `${b.tws_min}+` : `${b.tws_min}–${b.tws_max}`;
    return `<div class="xo-zone sail-bg-${esc(b.sail)}" style="left:${left}%;width:${w}%" title="${esc(b.sail)} ${rng} kn">${esc(b.sail)} ${esc(rng)}</div>`;
  }).join("");
  // editable boundaries (assumes ordered J1,J2,J3 with the two interior thresholds)
  const t1 = jc[0] && jc[0].tws_max, t2 = jc[1] && jc[1].tws_max;
  return `<h4>Upwind jib change-downs (by wind strength)</h4>
    <div class="muted" style="font-size:11px;margin-bottom:6px">The ORC cert rates only the J1 — these J1/J2/J3 change-downs are your crew/sailmaker thresholds (editable), and drive which jib each upwind leg carries.</div>
    <div class="xo-axis"><span>0</span><span>~9</span><span>~17</span><span>~26</span><span>35 kn</span></div>
    <div class="xo"><div class="xo-row"><span class="xo-tws">TWS</span><div class="xo-track">${bars}</div></div></div>
    <div class="jib-edit">
      <label>J1→J2 at <input type="number" id="jibT1" value="${t1 ?? ""}" min="2" max="34" step="0.5" style="width:58px"> kn</label>
      <label>J2→J3 at <input type="number" id="jibT2" value="${t2 ?? ""}" min="2" max="34" step="0.5" style="width:58px"> kn</label>
      <button class="mini" onclick="saveJibCrossovers()">Save change-downs</button>
      <span id="jibSaveMsg" class="muted" style="font-size:11px"></span>
    </div>`;
}

async function saveJibCrossovers() {
  const t1 = parseFloat(document.getElementById("jibT1").value);
  const t2 = parseFloat(document.getElementById("jibT2").value);
  const msg = document.getElementById("jibSaveMsg");
  if (isNaN(t1) || isNaN(t2) || t1 >= t2) { if (msg) msg.textContent = "J1→J2 must be below J2→J3."; return; }
  const bands = [{ sail: "J1", tws_max: t1 }, { sail: "J2", tws_min: t1, tws_max: t2 }, { sail: "J3", tws_min: t2 }];
  if (msg) msg.textContent = "Saving…";
  try {
    await apiPost("/api/boats/jib-crossovers", { jib_crossovers: bands });
    Opt.boatModel = await (await apiGet("/api/crossovers")).json();   // refetch so the bars update
    Opt.showBoatModel = true;
    renderGameplan();
  } catch (e) { if (msg) msg.textContent = "Save failed."; }
}

function renderPolarGrid() {
  const g = Opt.polarGrid; if (!g || !g.grid) return "";
  const tws = g.tws_buckets, twa = g.twa_buckets;
  const head = `<tr><th>TWA \\ TWS</th>${tws.map((t) => `<th>${t}</th>`).join("")}</tr>`;
  const rows = twa.map((a) => `<tr><td class="pg-twa">${a}°</td>${tws.map((t) => {
    const v = g.grid[String(t)][String(a)];
    return `<td>${v == null ? "" : v.toFixed(1)}</td>`;
  }).join("")}</tr>`).join("");
  return `<h4>Polar grid — target boatspeed (kn), TWS × TWA</h4>
    <div class="pg-wrap"><table class="pg">${head}${rows}</table></div>
    <div class="muted" style="font-size:11px;margin-top:4px">${g.n_points} points · Best-Performance ORC envelope (the speed the optimizer routes on).</div>`;
}

async function optPickRace(id, rerender = true) {
  Opt.raceId = id; Opt.courseId = null; Opt.result = null; Pb.result = null;
  try { Opt.def = await (await apiGet("/api/races/" + encodeURIComponent(id))).json(); }
  catch (e) { Opt.def = null; }
  if (rerender) renderGameplan();
}

async function runOptimize() {
  const out = document.getElementById("optOut");
  const ens = parseInt(document.getElementById("optEns").value || "0", 10) || 0;
  const avoidEl = document.getElementById("optAvoid");
  const pmEl = document.getElementById("optPerModel");
  const resEl = document.getElementById("optRes");
  Opt.resolution = resEl ? resEl.value : "auto";
  const body = { race_id: Opt.raceId, course_id: Opt.courseId, models: Opt.chosen, ensemble_members: ens,
    avoid_land: avoidEl ? avoidEl.checked : true, per_model: pmEl ? pmEl.checked : false,
    resolution: Opt.resolution };
  const startEpoch = optStartEpoch();
  if (startEpoch != null) body.start_epoch = startEpoch;
  Opt.running = true;
  document.getElementById("optRun").disabled = true;
  document.getElementById("optRun").textContent = "Optimizing… (downloading GRIB + routing)";
  out.innerHTML = '<div class="card"><div class="loading">Building the multi-model wind field and routing the course…</div></div>';
  try {
    const res = await apiPost("/api/optimize", body);
    const r = await jsonOrFriendly(res);
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
  const cov = r.wind_coverage;
  const covCls = cov == null ? "" : cov >= 0.9 ? "ok" : cov >= 0.6 ? "warn" : "bad";
  // Tier 3.2 — map-led "cockpit": the slippy map is the hero; the stats + collapsible result
  // sections (legs / briefing / wind field) live in a side rail beside it (stacks under on narrow).
  out.innerHTML = `<div class="opt-cockpit">
    <section class="cockpit-map"><div id="optMap" class="routemap routemap-hero"></div></section>
    <aside class="cockpit-rail">
      <div class="rail-stats opt-stats">
        <div><b>${r.total_hours}</b><span>hours</span></div>
        <div><b>${r.total_sailed_nm}</b><span>nm sailed</span></div>
        <div><b>${r.total_direct_nm}</b><span>nm direct</span></div>
        <div><b>${r.total_tacks}</b><span>tacks/gybes</span></div>
        <div><b class="conf ${confCls}">${conf == null ? "—" : conf}</b><span>conf (min ${r.min_confidence == null ? "—" : r.min_confidence})</span></div>
        <div><b class="conf ${covCls}">${cov == null ? "—" : Math.round(cov * 100) + "%"}</b><span>wind cov</span></div>
      </div>
      ${optDegradedBanner(r)}
      ${r.timed_out ? '<div class="pill warn">routing hit the time budget — route is best-effort</div>' : ""}
      <details class="rail-sec" open><summary>Legs</summary>
        <div class="legs-head"><span class="muted" style="font-size:11px">click a leg → highlight on map + jump forecast to its ETA</span>
          <button class="mini" onclick="exportLegsCsv()" title="Download the leg table as CSV (email the crew)">⬇ CSV</button></div>
        ${optSailPlan(r)}
        <table class="legs"><thead><tr><th>To</th><th>Min</th><th>Point of sail</th><th>Sail</th><th>Tacks</th><th>TWS</th><th>TWD</th><th>Conf</th></tr></thead>
        <tbody>${r.legs.map((l, i) => optLegRow(l, i)).join("")}</tbody></table></details>
      <details class="rail-sec" open><summary>Briefing</summary><pre class="briefing">${esc(r.briefing || "")}</pre></details>
      <details class="rail-sec"><summary>Wind field &amp; obstacles</summary>
        ${optObstacleNote(r)}
        ${(r.skipped_marks || []).length ? `<div class="muted" style="font-size:12px;margin-top:6px">Marks skipped (no coords — review in Course &amp; Marks): ${r.skipped_marks.map(esc).join(", ")}</div>` : ""}
        <div class="muted" style="font-size:12px;margin-top:6px">${(r.windfield.models || []).map((m) =>
          `${esc(m.model.toUpperCase())} ${esc(m.cycle)} — ${m.frames}${m.expected_frames ? "/" + m.expected_frames : ""} frames` +
          (m.cycle_fallbacks ? ` <span class="conf warn">(−${m.cycle_fallbacks} cycle)</span>` : "")).join(" · ")} · ${r.windfield.total_frames} frames total</div>
      </details>
    </aside></div>`;
  MapView.render("optMap", r);
}
function optLegRow(l, i) {
  const w = l.wind || {};
  const c = w.confidence, cc = c == null ? "" : c >= 0.6 ? "ok" : c >= 0.4 ? "warn" : "bad";
  return `<tr class="legrow" onclick="MapView.focusLeg(${i})" title="Show this leg on the map">
    <td>${esc(l.to)}</td><td>${l.leg_minutes}</td><td>${esc(l.point_of_sail || "—")}</td>
    <td>${l.sail ? `<span class="sail sail-${esc(l.sail)}">${esc(l.sail)}</span>` : "—"}</td>
    <td>${l.tacks > 0 ? `<span class="tackbadge" title="${l.tacks} tack/gybe(s) worked into this leg">⇄ ${l.tacks}</span>` : "0"}</td><td>${w.tws ?? "—"}</td><td>${w.twd ?? "—"}°</td>
    <td><span class="conf ${cc}">${c ?? "—"}</span></td></tr>`;
}

// CSV export of the leg table — Expedition's "email the crew" pattern, fully client-side.
function exportLegsCsv() {
  const r = Opt.result;
  if (!r || !r.legs) return;
  const hdr = ["leg", "to", "minutes", "eta_utc", "point_of_sail", "sail",
    "tacks", "direct_nm", "sailed_nm", "tws_kn", "twd_deg", "confidence"];
  const rows = r.legs.map((l, i) => {
    const w = l.wind || {};
    const eta = l.eta_epoch ? new Date(l.eta_epoch * 1000).toISOString().replace(".000Z", "Z") : "";
    return [i + 1, l.to, l.leg_minutes, eta, l.point_of_sail || "", l.sail || "",
      l.tacks, l.direct_nm, l.sailed_nm, w.tws ?? "", w.twd ?? "", w.confidence ?? ""];
  });
  const csv = [hdr, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `route_${esc(r.course_id || "course")}.csv`.replace(/[^\w.\-]/g, "_");
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function csvCell(v) {
  const s = String(v == null ? "" : v);
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function optSailPlan(r) {
  const sp = r.sail_plan || [];
  if (!sp.length) return "";
  const seq = sp.map((s) => `<span class="sail sail-${esc(s.sail)}">${esc(s.sail)}</span>`).join(' <span class="muted">→</span> ');
  return `<div class="sailplan"><span class="muted">Sail plan:</span> ${seq}</div>`;
}

/* ---------- Branching playbook (Lab-2b) ---------- */
const Pb = { running: false, result: null, freezing: false };

async function synthPlaybook() {
  const out = document.getElementById("pbOut");
  const ens = parseInt((document.getElementById("optEns") || {}).value || "0", 10) || 0;
  const body = { race_id: Opt.raceId, course_id: Opt.courseId, models: Opt.chosen, ensemble_members: ens };
  const startEpoch = optStartEpoch();
  if (startEpoch != null) body.start_epoch = startEpoch;
  Pb.running = true; Pb.result = null;
  const b = document.getElementById("pbRun"); if (b) { b.disabled = true; b.textContent = "Synthesizing… (fanning forecasts + routing each)"; }
  out.innerHTML = '<div class="loading" style="margin-top:10px">Routing each forecast scenario, clustering into strategic variants, and writing the playbook…</div>';
  try {
    const res = await apiPost("/api/playbook/synthesize", body);
    Pb.result = await jsonOrFriendly(res);
  } catch (e) {
    Pb.result = { available: false, note: String(e) };
  }
  Pb.running = false; renderGameplan();
}

function renderPbResult(b) {
  const out = document.getElementById("pbOut"); if (!out) return;
  if (b.available === false || !b.variants) {
    out.innerHTML = `<div class="placeholder" style="margin-top:10px">No playbook: ${esc(b.note || b.detail || "unavailable")}</div>`;
    return;
  }
  const agree = b.agreement == null ? "—" : Math.round(b.agreement * 100) + "%";
  const rec = (b.variants.find((v) => v.id === b.recommended) || {}).name || b.recommended || "—";
  const sig = b.signature;
  out.innerHTML = `<div class="pb">
    <div class="pb-head">
      <div class="pb-headline">${esc(b.headline || "")}</div>
      <div class="pb-meta">
        <span class="pill ok">Start: ${esc(rec)}</span>
        <span class="pill">${agree} model agreement</span>
        <span class="pill ${(b.decision_spread_min || 0) >= 15 ? "warn" : ""}">stakes ~${Math.round(b.decision_spread_min || 0)} min</span>
        <span class="pill">${b.variants.length} variants · ${(b.provenance || {}).n_scenarios || "?"} scenarios</span>
        <span class="muted" style="font-size:11px">via ${esc(((b.provenance || {}).synth_model || "").replace("claude-", ""))}</span>
      </div>
      ${pbBoatModelNote(b)}
    </div>
    <div class="pb-variants">${b.variants.map((v) => pbVariantCard(v, v.id === b.recommended)).join("")}</div>
    ${pbDecisionTree(b)}
    <div class="pb-freeze">
      ${sig ? pbSigBox(b) :
        `<button id="pbFreeze" onclick="freezePlaybook()" ${Pb.freezing ? "disabled" : ""}>${Pb.freezing ? "Freezing…" : "🔒 Freeze & sign for onboard"}</button>
         <span class="muted" style="font-size:12px">Signs the bundle (sha256) + saves it as the frozen, onboard-loadable homework. RRS 41: frozen at the gun.</span>`}
    </div></div>`;
}

function pbVariantCard(v, recommended) {
  const conf = v.route_confidence, cc = conf == null ? "" : conf >= 0.6 ? "ok" : conf >= 0.4 ? "warn" : "bad";
  const share = v.share == null ? "—" : Math.round(v.share * 100) + "%";
  const rng = v.hours_range ? ` · ${v.hours_range[0]}–${v.hours_range[1]} h` : "";
  return `<div class="pb-var${recommended ? " rec" : ""}">
    <div class="pb-var-top">
      <b>${esc(v.name)}</b>${recommended ? '<span class="pill ok">default</span>' : ""}
      <span class="pill">${share}</span>
      <span class="muted" style="font-size:12px">${v.total_hours ?? "—"} h${rng} · <span class="conf ${cc}">conf ${conf ?? "—"}</span> · ${esc((v.supported_by || []).map((m) => m.toUpperCase()).join(", "))}</span>
    </div>
    <div class="pb-var-sum">${esc(v.summary || "")}</div>
    ${pbSailPlanRow(v)}
    ${v.rationale ? `<div class="pb-row"><span class="pb-lbl">Why</span><span>${esc(v.rationale)}</span></div>` : ""}
    ${v.tradeoffs ? `<div class="pb-row"><span class="pb-lbl">Tradeoffs</span><span>${esc(v.tradeoffs)}</span></div>` : ""}
    ${v.what_flips_it ? `<div class="pb-row flips"><span class="pb-lbl">What flips it</span><span>${esc(v.what_flips_it)}</span></div>` : ""}
  </div>`;
}

function pbBoatModelNote(b) {
  const m = b.boat_model; if (!m || !m.sail_inventory) return "";
  const inv = (m.sail_inventory || []).map((s) => `<span class="sail sail-${esc(s)}">${esc(s)}</span>`).join(" ");
  return `<div class="pb-boatmodel muted" style="font-size:12px;margin-top:6px">⛵ Boat model frozen in: ${inv} · draft ${m.draft_ft ?? "?"} ft · ${Object.keys(m.crossovers || {}).length} TWS crossover bands — loaded onto the copilot.</div>`;
}

function pbSailPlanRow(v) {
  const sp = v.sail_plan || [];
  if (!sp.length) return "";
  const seq = sp.map((s) => `<span class="sail sail-${esc(s.sail)}">${esc(s.sail)}</span>`).join(' → ');
  return `<div class="pb-row"><span class="pb-lbl">Sail plan</span><span>${seq}</span></div>`;
}

function pbDecisionTree(b) {
  const t = b.decision_tree || [];
  if (!t.length) return "";
  return `<div class="pb-tree"><h4>Decision tree</h4><ol>${t.map((n) => {
    const v = (b.variants.find((x) => x.id === n.variant) || {}).name || n.variant || "";
    return `<li><span class="pb-observe">${esc(n.observe || "")}</span> → <b>${esc(n.action || "")}</b>${v ? ` <span class="pill">${esc(v)}</span>` : ""}</li>`;
  }).join("")}</ol></div>`;
}

function pbSigBox(b) {
  const sig = b.signature;
  const pid = (b.race_id || "race") + "__" + Math.round(b.start_epoch || 0);
  return `<div class="pb-sig">
    <span class="pill ok">🔒 Frozen &amp; signed</span>
    <code title="sha256">${esc((sig.value || "").slice(0, 16))}…</code>
    <button class="mini" onclick="downloadPlaybook('${esc(pid)}')">Download bundle</button>
    <span class="muted" style="font-size:11px">Drop this at the copilot's <code>PLAYBOOK_PATH</code> onboard.</span>
  </div>`;
}

// The bundle download is an /api/* route (team-token gated), so a plain <a> navigation 401s — it
// can't carry the bearer header. Fetch it through the authed api() helper, then save the blob.
async function downloadPlaybook(pid) {
  try {
    const res = await apiGet("/api/playbooks/" + encodeURIComponent(pid) + "/download");
    if (!res.ok) throw new Error("download failed (" + res.status + ")");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = pid + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) { alert("Could not download the bundle: " + e.message); }
}

async function freezePlaybook() {
  if (!Pb.result) return;
  Pb.freezing = true;
  const b = document.getElementById("pbFreeze"); if (b) { b.disabled = true; b.textContent = "Freezing…"; }
  try {
    const res = await apiPost("/api/playbook/freeze", { bundle: Pb.result });
    const r = await res.json();
    if (r.frozen) Pb.result = r.bundle;
  } catch (e) { /* leave the draft; user can retry */ }
  Pb.freezing = false; renderGameplan();
}

/* ---------- Lock-in & Deploy ---------- */
const Dep = { races: null, raceId: null, ready: null, sel: null, busy: false };

async function renderDeploy() {
  const view = document.getElementById("view");
  if (!Dep.races) {
    view.innerHTML = '<div class="loading">Loading…</div>';
    try { Dep.races = (await (await apiGet("/api/races")).json()).races || []; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  if (!Dep.raceId) Dep.raceId = Lab.sel || (Dep.races[0] && Dep.races[0].race_id) || null;
  if (!Dep.raceId) { view.innerHTML = '<div class="placeholder">No race — ingest one in the Races tab.</div>'; return; }
  try { Dep.ready = await (await apiGet("/api/deploy?race_id=" + encodeURIComponent(Dep.raceId))).json(); }
  catch (e) { view.innerHTML = '<div class="placeholder">Failed to load deploy state.</div>'; return; }
  if (stale("deploy")) return;
  paintDeploy();
}

async function depPickRace(id) { Dep.raceId = id; Lab.sel = id; Dep.sel = null; renderDeploy(); }

const depPill = (ok, label) => `<span class="pill ${ok ? "ok" : "warn"}">${ok ? "✓" : "⚠"} ${esc(label)}</span>`;

function paintDeploy() {
  const r = Dep.ready, lock = r.lock_in, pbs = r.playbooks || [], t = r.targets || {};
  if (!Dep.sel) Dep.sel = (lock && lock.playbook_id) || (pbs[0] && pbs[0].id) || "";
  const c = r.course, fl = r.fleet, ck = r.checklists;
  const pbReady = pbs.length > 0;
  const fmtDate = (e) => e ? new Date(e * 1000).toISOString().slice(0, 16).replace("T", " ") + "Z" : "—";
  const pbOpt = (b) => `<option value="${esc(b.id)}" ${b.id === Dep.sel ? "selected" : ""}>${
    esc((b.signed ? "🔒 " : "draft ") + (b.headline || b.id).slice(0, 60))} · ${b.n_variants} var · ${esc(fmtDate(b.generated_at))}</option>`;

  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Lock-in &amp; Deploy <span class="muted" style="font-weight:400">— freeze the homework + push it onboard</span></h3>
      <div class="opt-controls">
        <label>Race <select id="depRace" onchange="depPickRace(this.value)">
          ${Dep.races.map((x) => `<option value="${esc(x.race_id)}" ${x.race_id === Dep.raceId ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
      </div>

      <div class="dep-grid">
        <div class="dep-row">${depPill(r.reviewed, "Race reviewed")}<span class="muted">${r.reviewed ? "signed off for race use" : "approve it in the Races tab"}</span></div>
        <div class="dep-row">${depPill(c.ready, "Course")}<span class="muted">${esc(c.course_id || "—")} · ${c.marks} marks${c.skipped && c.skipped.length ? " · ⚠ " + c.skipped.length + " un-geocoded (" + c.skipped.map(esc).join(", ") + ")" : ""}</span></div>
        <div class="dep-row">${depPill(fl.ready, "Fleet")}<span class="muted">${fl.roster} boats${fl.scoring ? " · " + esc(fl.scoring) : ""}${fl.tracker_permitted ? " · tracker permitted" : ""}</span></div>
        <div class="dep-row">${depPill(ck.ready, "Checklists")}<span class="muted">${ck.total} items · ${ck.ipad} →iPad</span></div>
        <div class="dep-row">${depPill(pbReady, "Playbook")}<span class="muted">${pbReady ? pbs.length + " frozen for this race" : "synthesize + Freeze & sign one in Gameplan"}</span></div>
      </div>
    </div>

    <div class="card">
      <h3>Lock in the homework</h3>
      ${pbReady ? `
        <div class="opt-controls">
          <label>Playbook <select id="depPb" onchange="Dep.sel=this.value" style="min-width:340px">${pbs.map(pbOpt).join("")}</select></label>
          <button id="depLock" onclick="depLockIn()" ${Dep.busy ? "disabled" : ""}>${lock ? "Re-lock selected" : "🔒 Lock in selected playbook"}</button>
        </div>` : `<div class="muted">No frozen playbook yet — go to <a href="#gameplan">Gameplan</a>, synthesize the branching playbook, and <b>Freeze &amp; sign</b> it. Then lock it in here.</div>`}
      ${lock ? `<div class="banner ok" style="margin-top:10px">🔒 Locked in: <code>${esc(lock.playbook_id)}</code>${lock.signed ? ` · sig <code>${esc(lock.signature)}…</code>` : " · <b>unsigned</b>"} · ${esc(fmtDate(lock.locked_at))}</div>` : ""}
    </div>

    ${lock ? deployPanel(lock, t) : ""}
  </div>`;
}

function deployPanel(lock, t) {
  const rid = Dep.raceId, pid = lock.playbook_id;
  const hw = `homework_${rid}.json`, pb = `${pid}.json`;
  const cmds = [
    `# 1. Course + fleet → the Pi engine (Tailscale host: ${t.pi_host})`,
    `jq .course_load ${hw} | ssh ${t.pi_host} 'curl -sX POST ${t.pi_engine}/course/load -H "Content-Type: application/json" -d @-'`,
    `jq .fleet_load  ${hw} | ssh ${t.pi_host} 'curl -sX POST ${t.pi_engine}/fleet/load  -H "Content-Type: application/json" -d @-'`,
    ``,
    `# 2. Signed playbook → the Orin copilot (Tailscale host: ${t.orin_host})`,
    `scp ${pb} ${t.orin_host}:${t.orin_playbook_path}`,
    `ssh ${t.orin_host} 'echo CAN100 | sudo -S systemctl restart ${t.orin_service}'`,
    `#   (ensure PLAYBOOK_PATH=${t.orin_playbook_path} is set in /etc/sr33/copilot.env on the Orin)`,
  ].join("\n");
  return `<div class="card">
    <h3>Deploy onboard <span class="muted" style="font-weight:400">— frozen at the gun (RRS 41)</span></h3>
    <div class="muted" style="font-size:12px;margin-bottom:8px">The Lab has no line to the boat — download the two artifacts, then run the load commands from a machine on the boat's Tailscale net (course + fleet onto the Pi engine, the signed playbook onto the Orin copilot).</div>
    <div class="dep-actions">
      <button onclick="downloadPlaybook('${esc(pid)}')">⬇ Playbook bundle (→ Orin)</button>
      <button onclick="downloadHomework('${esc(rid)}')">⬇ Homework package (→ Pi)</button>
    </div>
    <pre class="dep-cmds">${esc(cmds)}</pre>
  </div>`;
}

async function depLockIn() {
  if (!Dep.sel) return;
  Dep.busy = true;
  try { await apiPost("/api/deploy/lock-in", { race_id: Dep.raceId, playbook_id: Dep.sel }); }
  catch (e) { alert("Lock-in failed: " + e.message); }
  Dep.busy = false; renderDeploy();
}

// Authed download of the homework package (the /api/* route needs the bearer; a plain <a> would 401).
async function downloadHomework(rid) {
  try {
    const res = await apiGet("/api/deploy/package/" + encodeURIComponent(rid) + "/download");
    if (!res.ok) throw new Error("download failed (" + res.status + ")");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "homework_" + rid + ".json";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) { alert("Could not download the homework package: " + e.message); }
}

function optDegradedBanner(r) {
  const w = r.warnings || [];
  if (!w.length) return "";
  const cls = r.degraded ? "bad" : "warn";
  const head = r.degraded ? "⚠ Degraded forecast — read before trusting this route" : "⚠ Notes";
  // when degraded, surface the common-error checklist inline so the cause is actionable (2.5)
  const checklist = r.degraded ? `<div style="margin-top:6px;font-size:12px;font-weight:400">
    <b>What usually fixes it:</b>
    <ul style="margin:3px 0 0 16px;padding:0">
      <li>The latest model cycle may not be fully posted yet — re-run in ~30 min, or add GFS/NAM (most reliable).</li>
      <li>Uncheck ECMWF if a source is rate-limited (it can stall the field).</li>
      <li>Long course past a model's horizon → HRRR only reaches 48 h on synoptic cycles; lean on GFS/NAM.</li>
      <li>Try <b>Auto</b> or <b>Fast</b> resolution if a Fine run timed out before finishing.</li>
    </ul></div>` : "";
  return `<div class="pill ${cls}" style="display:block;margin-bottom:8px;text-align:left">
    <b>${head}</b><ul style="margin:4px 0 0 16px;padding:0">${w.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>${checklist}</div>`;
}

function optObstacleNote(r) {
  const ob = r.obstacles || {};
  if (!ob.active) return '<div class="muted" style="font-size:12px;margin-top:6px">Obstacle avoidance off — route may cross land/islands.</div>';
  const L = ob.layers || {};
  const steps = r.obstacle_steps_avoided || 0;
  const boat = r.boat ? esc(r.boat.name) : "boat";
  if (ob.source === "enc") {
    return `<div class="muted" style="font-size:12px;margin-top:6px">⚓ <b>NOAA ENC charts</b> (draft-aware) — route steered around real land + shoals + obstructions
      (${steps} candidate steps rejected; cells: land ${L.coastline || 0}, shoal ${L.shoal || 0}, rocks/obstrns ${L.obstruction || 0}, zones ${L.zones || 0}).
      Shoal no-go = ${boat} draft + margin, depth &lt; <b>${ob.safety_depth_m ?? "?"} m</b>. NOAA GIS export = non-navigational; verify against the official chart.</div>`;
  }
  return `<div class="muted" style="font-size:12px;margin-top:6px">⛰ Natural Earth 1:10m (${esc(ob.data_version || "")}) + this race's islands/zones — route steered around land/islands/zones
    (${steps} candidate steps rejected; cells: coastline ${L.coastline || 0}, islands ${L.islands || 0}, zones ${L.zones || 0}).
    Coarse near shore + no depth/shoals — switch Charts to <b>NOAA ENC</b> above for draft-aware accuracy.</div>`;
}

