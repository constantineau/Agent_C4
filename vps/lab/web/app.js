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
  if (sec === "fleet") return renderFleet();
  if (sec === "rules") return renderRules();
  if (sec === "learnings") return renderLearnings();
  if (sec === "monitor") return renderMonitor();
  if (sec === "debrief") return renderDebrief();
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
  const setMsg = (t, color) => {
    msg.textContent = t; msg.style.color = color || "";
    msg.style.fontWeight = color ? "600" : ""; msg.style.fontSize = "13px";
  };
  if (!Lab.sources.length && !files.length) { setMsg("Add a document URL or upload a PDF first.", "var(--warn)"); return; }
  btn.disabled = true; btn.textContent = "Extracting…";
  setMsg("⏳ Extracting with Opus — reading the documents. A large SI/NOR can take 1–2 min; keep this tab open.", "var(--accent)");
  try {
    let res;
    if (files.length) {
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      res = await api("/api/ingest/upload", { method: "POST", body: fd });
    } else {
      res = await apiPost("/api/ingest", { urls: Lab.sources });
    }
    const txt = await res.text();
    let resp;
    try { resp = JSON.parse(txt); }
    catch (e) {
      if (res.status === 502 || res.status === 504 || /^\s*</.test(txt))
        setMsg("✕ Timed out at the gateway — the document was too large/slow to process. Try uploading the PDF directly, or fewer/smaller docs.", "var(--bad)");
      else setMsg("✕ Ingest failed — server returned " + res.status + " (non-JSON response).", "var(--bad)");
      btn.disabled = false; btn.textContent = "Extract →"; return;
    }
    if (resp.detail) {   // the backend detail often already reads "ingest failed: …" — don't double it
      const d = /^ingest failed/i.test(resp.detail) ? resp.detail : "Ingest failed: " + resp.detail;
      setMsg("✕ " + d, "var(--bad)"); btn.disabled = false; btn.textContent = "Extract →"; return;
    }
    Lab.draft = resp; Lab.editDef = resp.definition;
    Lab.editVal = { errors: resp.errors || [], warnings: resp.warnings || [] }; Lab.isDraft = true;
    const nm = (resp.definition && resp.definition.name) || "the race";
    const nwarn = (resp.warnings || []).length, nerr = (resp.errors || []).length;
    setMsg("✓ Draft extracted for “" + nm + "” — review it on the right"
      + (nwarn ? " (" + nwarn + " item" + (nwarn > 1 ? "s" : "") + " flagged for review)" : "")
      + (nerr ? " (" + nerr + " error" + (nerr > 1 ? "s" : "") + " to fix)" : "")
      + ", then click SAVE to add it to the library. It is NOT saved yet.", "var(--accent2)");
    paintDetail();
  } catch (e) { setMsg("✕ Ingest failed: " + (e && e.message ? e.message : e), "var(--bad)"); }
  btn.disabled = false; btn.textContent = "Extract →";
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
    detailDelegated(d) +
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
// The rules/scoring/checklist + fleet now live in their own tabs — the Races view stays the ingest +
// library + sign-off hub and just points to them (avoids the duplicate editors, issue #28).
function detailDelegated(d) {
  const reqs = (d.requirements || []).length;
  const ipad = (d.requirements || []).filter((r) => r.deliver_to_ipad).length;
  const mods = ((d.rules_profile || {}).modifications || []).length;
  const fleet = (d.fleet || []).length;
  return `<div class="card"><h3>Rules, scoring, checklist &amp; fleet</h3>
    <div class="muted" style="font-size:12px;margin-bottom:8px">Reviewed and edited in their own tabs (kept out of here so the Races view stays the ingest + library + sign-off hub):</div>
    <div class="dep-grid">
      <div class="dep-row"><a href="#rules">Rules, Safety &amp; Checklists →</a> <span class="muted">${mods} RRS modification${mods === 1 ? "" : "s"} + scoring · ${reqs} checklist item${reqs === 1 ? "" : "s"} (${ipad} →iPad)</span></div>
      <div class="dep-row"><a href="#fleet">Fleet →</a> <span class="muted">${fleet} competitor${fleet === 1 ? "" : "s"} + ORC handicaps</span></div>
      <div class="dep-row"><a href="#course">Course &amp; Marks →</a> <span class="muted">geometry on a map</span></div>
    </div></div>`;
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
    <div id="map${ci}" class="coursemap"></div>
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
// Course & Marks chart — a real slippy map (OSM + OpenSeaMap seamarks) so geocoded marks can be
// sanity-checked against actual geography (issue #26). Labeled markers per start/mark/gate/finish,
// island disks (with the rounding side), gate/finish lines, auto-fit to the course.
function drawCourseMap(id, course) {
  const el = document.getElementById(id);
  if (!el || !window.L) return;
  Lab._courseMaps = Lab._courseMaps || {};
  if (Lab._courseMaps[id]) { try { Lab._courseMaps[id].remove(); } catch (e) {} delete Lab._courseMaps[id]; }
  const map = L.map(el); Lab._courseMaps[id] = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
  L.tileLayer("https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png", { maxZoom: 18, opacity: 0.9 }).addTo(map);
  const pts = [];
  const pin = (lat, lon, label, color) => {
    if (lat == null) return;
    L.circleMarker([lat, lon], { radius: 6, color: "#0b0b0e", weight: 1, fillColor: color, fillOpacity: 0.95 })
      .bindTooltip(label, { permanent: true, direction: "right", className: "cm-pin" }).addTo(map);
    pts.push([lat, lon]);
  };
  if (course.start && course.start.lat != null) pin(course.start.lat, course.start.lon, "Start", "#2ecc71");
  (course.marks || []).forEach((m) => {
    if (m.type === "island" && m.lat != null) {
      L.circle([m.lat, m.lon], { radius: (m.radius_nm || 0.5) * 1852, color: "#ff8a5c", weight: 1,
        fillColor: "#ff8a5c", fillOpacity: 0.16 }).addTo(map);
      pin(m.lat, m.lon, m.name + " · island, leave " + (m.rounding && m.rounding !== "none" ? m.rounding : "either"), "#ff8a5c");
    } else if (m.type === "gate" && m.lat != null && m.lat2 != null) {
      L.polyline([[m.lat, m.lon], [m.lat2, m.lon2]], { color: "#f5b13d", weight: 3 }).addTo(map);
      pin(m.lat, m.lon, m.name, "#f5b13d"); pin(m.lat2, m.lon2, m.name + " (NE)", "#f5b13d");
    } else if (m.lat != null) {
      pin(m.lat, m.lon, m.name + (m.rounding && m.rounding !== "none" ? " · " + m.rounding : ""), "#2f9bff");
    }
  });
  const fp = ((course.finish || {}).points || []).filter((p) => p && p.lat != null);
  if (fp.length === 2) L.polyline([[fp[0].lat, fp[0].lon], [fp[1].lat, fp[1].lon]], { color: "#f5c451", weight: 3 }).addTo(map);
  fp.forEach((p, k) => pin(p.lat, p.lon, k === 0 ? "Finish" : "Finish (2)", "#f5c451"));
  if (pts.length) map.fitBounds(pts, { padding: [42, 42], maxZoom: 12 });
  else { map.setView([44.5, -82.5], 6); L.popup().setLatLng([44.5, -82.5]).setContent("No coordinates yet — fill marks below.").openOn(map); }
  setTimeout(() => { try { map.invalidateSize(); } catch (e) {} }, 80);   // settle tiles after the innerHTML layout
}

/* ---------- placeholders (all sections are now built) ---------- */
const SOON = {};
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

// Parse the "Start" field — the datetime-local picker's "YYYY-MM-DDThh:mm" value (a space separator
// and an optional :ss are also accepted). Interpreted in the race's local timezone when one is known,
// else as UTC. Returns epoch seconds, or null if blank/invalid.
function optStartEpoch() {
  const m = (Opt.start || "").trim().match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::\d{2})?$/);
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
    ? "Pick the start date & time in " + startTz + " race-local time — converted to UTC behind the scenes."
    : "Pick the start date & time (UTC).") + " Leave blank for the freshest forecast cycle.";
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
          <label>Start (${startTz ? "race local" : "UTC"}) <input type="datetime-local" id="optStart"
            title="${esc(startTitle)}" value="${esc(Opt.start || "")}" oninput="Opt.start=this.value"></label>
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
        <label class="optchk" title="Degrade boatspeed for sea state (waves) — routes/ETAs on achievable speed. Conservative model + a low-Hs deadband; uncheck for flat-water (polar) routing. (Helm % still applies — that's crew efficiency, not waves.)"><input type="checkbox" id="optWaves" checked> Sea-state (waves)</label>
        <label class="optchk" title="Fold water current (set &amp; drift) into the route + leg ETAs — the boat crabs into a cross stream and rides a fair/foul current. Source: NOAA LMHOFS. Uncheck to route in still water, then re-run checked to see what the current is worth."><input type="checkbox" id="optCurrent" checked> Water current</label>
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
    <label title="Fraction of the flat-water ORC polar this crew actually sails — the optimizer routes on achievable speed. 100% = sails the book.">Helm % <input type="number" id="optHelm" step="1" min="30" max="100" value="${Math.round((ab.helm_factor ?? 1) * 100)}" style="width:58px" onchange="optSaveHelm()"></label>
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
async function optSaveHelm() {
  let pct = parseFloat(document.getElementById("optHelm").value);
  if (isNaN(pct)) return;
  pct = Math.max(30, Math.min(100, pct));                          // clamp to the modelled range
  const full = (await (await apiGet("/api/boats/" + encodeURIComponent(Opt.activeBoat))).json()).boat || {};
  full.helm_factor = Math.round(pct) / 100;
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
  out.innerHTML = boatModelCard(m);
}

// Sail palette (mirrors the .sail-bg-* CSS) — used to hatch the toss-up overlap bands in two colors.
const SAIL_COLORS = { J1: "#36b3ff", J2: "#66a9e0", J3: "#9b8cff", A2: "#7ee0a8", A3: "#f5c451", S2: "#ff8042" };
const sailColor = (s) => SAIL_COLORS[s] || "#8899a6";
function hexRgba(hex, a) {
  const h = String(hex).replace("#", "");
  const n = parseInt(h.length === 3 ? h.replace(/(.)/g, "$1$1") : h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

// The boat-model card HTML (crossover bands + jib change-downs + polar grid). Returned as a string so
// both the Gameplan review panel and the Learnings library can embed it.
function boatModelCard(m) {
  const tws = m.tws_buckets || [];
  // crossover bands: a horizontal 0–180° TWA axis per TWS, colored per sail
  const bands = tws.map((t) => {
    const zones = (m.crossovers[String(t)] || []).map((z) => {
      const left = (z.twa_min / 180 * 100).toFixed(1);
      const w = ((z.twa_max - z.twa_min) / 180 * 100).toFixed(1);
      return `<div class="xo-zone sail-bg-${esc(z.short)}" style="left:${left}%;width:${w}%" title="${esc(z.label)} ${z.twa_min}–${z.twa_max}°">${esc(z.short)}</div>`;
    }).join("");
    // toss-up overlays: two sails within ~2% of target → a translucent two-colour diagonal hatch drawn
    // over the solid zones (the zones still show through the gaps), so a sail the winner-take-all bands
    // erased on a near-tie (e.g. A2 at 14–16 kts) reappears in its own colour.
    const overlaps = ((m.overlaps || {})[String(t)] || []).map((o) => {
      const left = (o.twa_min / 180 * 100).toFixed(1);
      const w = ((o.twa_max - o.twa_min) / 180 * 100).toFixed(1);
      const c1 = hexRgba(sailColor(o.sails[0]), 0.72), c2 = hexRgba(sailColor(o.sails[1]), 0.72);
      const bg = `repeating-linear-gradient(45deg, ${c1} 0 4px, transparent 4px 8px, ${c2} 8px 12px, transparent 12px 16px)`;
      return `<div class="xo-ol" style="left:${left}%;width:${w}%;background:${bg}" title="Toss-up: ${esc(o.sails.join(" ≈ "))} within ~1.5% of target ${o.twa_min}–${o.twa_max}° — carry either / peel is optional">≈</div>`;
    }).join("");
    return `<div class="xo-row"><span class="xo-tws">${t} kts</span><div class="xo-track">${zones}${overlaps}</div></div>`;
  }).join("");
  const inv = (m.inventory || []).map((s) => `<span class="sail sail-${esc(s)}">${esc(s)}</span>`).join(" ");
  return `<div class="card boatmodel">
    <h3>Boat model — polars &amp; sail crossovers</h3>
    <div class="muted" style="font-size:12px;margin-bottom:10px">Source: ${esc(m.source || "—")}. This is the boat sail model the optimizer attaches per leg and <b>freezes into the playbook bundle</b> — what the onboard copilot loads to ground its sail calls. Review before lock-in.</div>
    <div class="bm-inv">Inventory: ${inv}</div>
    ${renderJibCrossovers(m)}
    <h4>Sail crossovers (optimal sail by TWA, per TWS)</h4>
    <div class="muted" style="font-size:11px;margin-bottom:4px">From the ORC cert (one headsail = the jib slot; specialised to J1/J2/J3 by the wind bands above). <b>Hatched ≈</b> = a toss-up: two sails within ~1.5% of target speed, where the winner-take-all bands can't show a tie — carry either (the erased sail reappears in its own colour, e.g. A2 on the reach at 14–16 kts).</div>
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
    return `<div class="xo-zone sail-bg-${esc(b.sail)}" style="left:${left}%;width:${w}%" title="${esc(b.sail)} ${rng} kts">${esc(b.sail)} ${esc(rng)}</div>`;
  }).join("");
  // editable boundaries (assumes ordered J1,J2,J3 with the two interior thresholds)
  const t1 = jc[0] && jc[0].tws_max, t2 = jc[1] && jc[1].tws_max;
  return `<h4>Upwind jib change-downs (by wind strength)</h4>
    <div class="muted" style="font-size:11px;margin-bottom:6px">The ORC cert rates only the J1 — these J1/J2/J3 change-downs are your crew/sailmaker thresholds (editable), and drive which jib each upwind leg carries.</div>
    <div class="xo-axis"><span>0</span><span>~9</span><span>~17</span><span>~26</span><span>35 kts</span></div>
    <div class="xo"><div class="xo-row"><span class="xo-tws">TWS</span><div class="xo-track">${bars}</div></div></div>
    <div class="jib-edit">
      <label>J1→J2 at <input type="number" id="jibT1" value="${t1 ?? ""}" min="2" max="34" step="0.5" style="width:58px"> kts</label>
      <label>J2→J3 at <input type="number" id="jibT2" value="${t2 ?? ""}" min="2" max="34" step="0.5" style="width:58px"> kts</label>
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
    if ((location.hash || "").slice(1) === "learnings") { renderLearnings(); }
    else { Opt.showBoatModel = true; renderGameplan(); }
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
  const wavesEl = document.getElementById("optWaves");
  const currentEl = document.getElementById("optCurrent");
  const resEl = document.getElementById("optRes");
  Opt.resolution = resEl ? resEl.value : "auto";
  Opt.useWaves = wavesEl ? wavesEl.checked : true;
  Opt.useCurrent = currentEl ? currentEl.checked : true;
  const body = { race_id: Opt.raceId, course_id: Opt.courseId, models: Opt.chosen, ensemble_members: ens,
    avoid_land: avoidEl ? avoidEl.checked : true, per_model: pmEl ? pmEl.checked : false,
    resolution: Opt.resolution, use_waves: Opt.useWaves, use_current: Opt.useCurrent };
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
        ${r.total_peels != null ? `<div title="Sail changes (peels) the route makes — held a sub-optimal sail rather than peel when it didn't pay (2g)"><b>${r.total_peels}</b><span>sail peels</span></div>` : ""}
        <div><b class="conf ${confCls}">${conf == null ? "—" : conf}</b><span>conf (min ${r.min_confidence == null ? "—" : r.min_confidence})</span></div>
        <div><b class="conf ${covCls}">${cov == null ? "—" : Math.round(cov * 100) + "%"}</b><span>wind cov</span></div>
        ${optCurrentStat(r)}
        ${optRealizedStat(r)}
      </div>
      <div class="rail-actions"><button id="pdfBtn" onclick="downloadGameplanPdf(this)" title="Download a PDF report of this gameplan — route summary + schematic + leg table + briefing + branching playbook — to email the crew">⬇ PDF report</button></div>
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
      ${optModelSkill(r)}
    </aside></div>`;
  MapView.render("optMap", r);
}
function optLegRow(l, i) {
  const w = l.wind || {};
  const c = w.confidence, cc = c == null ? "" : c >= 0.6 ? "ok" : c >= 0.4 ? "warn" : "bad";
  return `<tr class="legrow" onclick="MapView.focusLeg(${i})" title="Show this leg on the map">
    <td>${esc(l.to)}</td><td>${l.leg_minutes}</td><td>${esc(l.point_of_sail || "—")}</td>
    <td>${l.sail ? `<span class="sail sail-${esc(l.sail)}">${esc(l.sail)}</span>` : "—"}${l.peels > 0 ? ` <span class="peelbadge" title="${l.peels} sail change(s)/peel(s) on this leg">⛵${l.peels}</span>` : ""}</td>
    <td>${l.tacks > 0 ? `<span class="tackbadge" title="${l.tacks} tack/gybe(s) worked into this leg">⇄ ${l.tacks}</span>` : "0"}</td><td>${w.tws ?? "—"}</td><td>${w.twd ?? "—"}°</td>
    <td><span class="conf ${cc}">${c ?? "—"}</span></td></tr>`;
}

// CSV export of the leg table — Expedition's "email the crew" pattern, fully client-side.
function exportLegsCsv() {
  const r = Opt.result;
  if (!r || !r.legs) return;
  const hdr = ["leg", "to", "minutes", "eta_utc", "point_of_sail", "sail", "peels",
    "tacks", "direct_nm", "sailed_nm", "tws_kn", "twd_deg", "confidence"];
  const rows = r.legs.map((l, i) => {
    const w = l.wind || {};
    const eta = l.eta_epoch ? new Date(l.eta_epoch * 1000).toISOString().replace(".000Z", "Z") : "";
    return [i + 1, l.to, l.leg_minutes, eta, l.point_of_sail || "", l.sail || "", l.peels ?? 0,
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

/* PDF report of the whole gameplan — the optimize result + (if synthesized) the branching playbook.
   Server-rendered (reportlab) so it's a clean, consistent, shareable document, streamed back as a blob
   and downloaded (same "email the crew" pattern as the CSV export, but the full report). */
async function downloadGameplanPdf(btn) {
  const r = Opt.result;
  if (!r || !r.legs) { alert("Run the optimizer first — there's no route to report yet."); return; }
  const orig = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Rendering…"; }
  const raceName = (Lab.editDef && (Lab.editDef.name || Lab.editDef.race_id)) || Opt.raceId || "C4";
  try {
    const res = await apiPost("/api/gameplan/pdf",
      { result: r, playbook: (Pb && Pb.result && Pb.result.variants) ? Pb.result : null,
        race_name: raceName, boat: (r.boat && r.boat.name) || r.boat_name || "" });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.detail || ("HTTP " + res.status));
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = ("gameplan_" + raceName + ".pdf").replace(/[^\w.\-]/g, "_");
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert("PDF report failed — " + String((e && e.message) || e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig; }
  }
}

function optRealizedStat(r) {
  const rz = r.realized;
  if (!rz) return "";
  const pct = Math.round((rz.realized_pct ?? 1) * 100);
  const helm = Math.round((rz.helm_factor ?? 1) * 100);
  const hs = rz.sea_state_hs_mean || 0;
  const sea = hs > 0.05 ? ` · sea ~${hs.toFixed(1)}m` : "";
  const cls = pct >= 92 ? "ok" : pct >= 82 ? "warn" : "bad";
  // reshape gate: did the sea bend the route (⤳) or only tax the ETA (=)?
  const rsh = (hs > 0.05 && "wave_reshape" in rz)
    ? ` <span class="muted" title="${attr(rz.wave_reshape_note || "")}">${rz.wave_reshape ? "⤳ reshaped" : "= ETA-only"}</span>` : "";
  return `<div title="Achievable speed: the route is computed at this fraction of the flat-water polar (helm skill × sea state). The gap to 100% is the boatspeed left to find."><b class="conf ${cls}">${pct}%</b><span>realized · helm ${helm}%${sea}${rsh}</span></div>`;
}

function optCurrentStat(r) {
  const c = r.current;
  if (!c || !c.loaded) return "";
  const src = (c.source || "current").toUpperCase();
  const peak = r.current_grid && r.current_grid.peak_drift_kn;
  const sub = c.source === "constant"
    ? `${c.drift_kn ?? "?"} kts @ ${c.set_deg ?? "?"}°`
    : `${c.slices ?? "?"} slices${peak ? ` · pk ${peak} kts` : ""}`;
  return `<div title="Water current folded into the leg ETAs (set & drift). Source ${esc(src)}."><b>${esc(src)}</b><span>current · ${esc(sub)}</span></div>`;
}

// Venue weather-model skill panel: each model weighted by its MEASURED past accuracy at this venue
// (forecast-vs-observed), recency-weighted across seasons. Weights are applied automatically, so we
// always show them + the RMSE that earned them + a button to deepen the history (pre-2021 archives).
function optModelSkill(r) {
  const s = r.model_skill;
  if (!s) return "";
  const btn = `<button class="mini" onclick="runModelSkillBackfill()" id="mskBackfill"
    title="Pull the pre-2021 GRIB archives (HRRR 2015+, GEFS reforecast 2005+) for this venue — minutes, runs once, then cached.">⏳ Deepen history (2005+)</button>`;
  if (!s.enabled) {
    return `<details class="rail-sec"><summary>Model skill</summary>
      <div class="muted" style="font-size:12px">${esc(s.note || s.reason || "not weighting")} —
      routing on static model priors.</div>
      ${s.venue_key ? `<div class="muted" style="font-size:11px;margin-top:4px">Venue ${esc(s.venue_key)}${s.station ? " · " + esc(s.station) : ""}</div>${btn}` : ""}
    </details>`;
  }
  const rows = (s.table || []).map((t) => {
    const db = (t.bias_speed_kn || t.bias_dir_deg)
      ? `<span class="muted" title="bias removed before blending">${t.bias_dir_deg > 0 ? "+" : ""}${t.bias_dir_deg || 0}°</span>` : "—";
    const yrs = t.n_years ? ` <span class="muted" style="font-size:10px">${t.n_years}y</span>` : "";
    const wcell = t.reference
      ? `<span class="muted" title="tracked for reference — not in the routing blend">ref</span>`
      : `<b class="conf ${(t.weight >= 1.15) ? "ok" : (t.weight <= 0.85) ? "bad" : ""}">×${t.weight == null ? 1 : t.weight}</b>`;
    return `<tr><td>${esc(t.model.toUpperCase())}${yrs}</td><td>${t.vector_rmse_kn}</td>
      <td>${wcell}</td><td>${db}</td><td class="muted">${t.n}</td></tr>`;
  }).join("");
  const deep = s.deep ? `<span class="pill ok" title="Includes pre-2021 reforecast archives (HRRR 2015+, GEFS 2005+)">deep history</span>` : "";
  const total = (s.table || []).reduce((a, t) => a + (t.n || 0), 0);
  const nSt = (s.station || "").split("+").filter(Boolean).length;
  return `<details class="rail-sec" open><summary>Model skill — venue backtest ${deep}</summary>
    <div class="muted" style="font-size:11px;margin-bottom:8px">
      Each model's <b>past forecasts vs the observed wind</b> at this venue (a forecast-vs-observed
      backtest, not model agreement). Lower vector-RMSE ⇒ more trusted; each model's persistent
      veer/speed bias is removed before blending. <b>Weights auto-apply to the route above.</b></div>
    <div class="rail-stats opt-stats" style="margin-bottom:8px">
      <div title="${esc(s.station_name || s.station || "")}"><b>${nSt || "—"}</b><span>obs station${nSt === 1 ? "" : "s"}</span></div>
      <div><b>${s.n_years || 1}</b><span>seasons</span></div>
      <div title="race-window ±21 days, each season"><b>${s.window ? esc(s.window[0]) + "–" + esc(s.window[1]) : "—"}</b><span>years</span></div>
      <div title="total matched forecast–observation pairs across models"><b>${total.toLocaleString()}</b><span>comparisons</span></div>
      <div title="recency half-life: how fast older seasons are down-weighted (models drift)"><b>${s.recency_halflife_y}y</b><span>recency t½</span></div>
    </div>
    <div class="muted" style="font-size:10px;margin-bottom:3px">obs: ${esc(s.station || "—")} (${esc(s.obs_source || "")}) · forecast: Open-Meteo 2021+ ${s.deep ? "+ HRRR/GEFS reforecast archives" : ""}</div>
    <table class="legs"><thead><tr>
      <th title="model (with seasons of data)">Model</th><th title="vector RMSE, kn — headline skill (speed+direction error)">RMSE</th>
      <th title="blend weight applied (×priority); 'ref' = tracked but not routed">Weight</th>
      <th title="persistent direction bias removed before blending">Bias</th>
      <th title="matched forecast–obs pairs">n</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <div style="margin-top:6px">${btn}</div>
    <div id="mskMsg" class="muted" style="font-size:11px;margin-top:4px"></div>
  </details>`;
}

async function runModelSkillBackfill() {
  const b = document.getElementById("mskBackfill"); const msg = document.getElementById("mskMsg");
  if (!b) return;
  b.disabled = true; b.textContent = "Deepening… (pulling GRIB archives, minutes)";
  if (msg) msg.textContent = "Fetching pre-2021 HRRR + GEFS-reforecast archives for this venue…";
  const body = { race_id: Opt.raceId, course_id: Opt.courseId };
  const se = optStartEpoch(); if (se != null) body.start_epoch = se;
  try {
    const res = await apiPost("/api/model-skill/backfill", body);
    const p = await jsonOrFriendly(res);
    if (Opt.result) { Opt.result.model_skill = p; renderGameplan(); }
  } catch (e) {
    if (msg) msg.textContent = "Backfill failed: " + esc(String(e));
    b.disabled = false; b.textContent = "⏳ Deepen history (2005+)";
  }
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
  const wavesEl = document.getElementById("optWaves");
  const body = { race_id: Opt.raceId, course_id: Opt.courseId, models: Opt.chosen, ensemble_members: ens,
    use_waves: wavesEl ? wavesEl.checked : true };
  const startEpoch = optStartEpoch();
  if (startEpoch != null) body.start_epoch = startEpoch;
  Pb.running = true; Pb.result = null;
  const b = document.getElementById("pbRun"); if (b) { b.disabled = true; b.textContent = "Synthesizing… (models + perturbation scenarios)"; }
  out.innerHTML = '<div class="loading" style="margin-top:10px">Routing each forecast scenario (per-model + the v2 perturbation fan: shifts, pressure, timing), dedup-ing to plays, and writing the playbook… (~10 min; runs server-side, safe to keep working)</div>';
  try {
    const start = await (await apiPost("/api/playbook/synthesize", body)).json();
    if (start.ok === false) throw new Error(start.note || "synthesis busy");
    // background job — poll for the bundle (the fan runs far past the gateway timeout)
    while (true) {
      await new Promise((r) => setTimeout(r, 10000));
      const st = await (await apiGet("/api/playbook/synthesize/status")).json();
      if (st.state === "done") { Pb.result = st.bundle; break; }
      if (st.state === "error") { Pb.result = { available: false, note: st.error }; break; }
      if (st.state === "idle") { Pb.result = { available: false, note: "synthesis job vanished (container restarted?)" }; break; }
    }
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
    ${pbPlaysSection(b)}
    ${pbDecisionTree(b)}
    <div class="pb-freeze">
      ${sig ? pbSigBox(b) :
        `<button id="pbFreeze" onclick="freezePlaybook()" ${Pb.freezing ? "disabled" : ""}>${Pb.freezing ? "Freezing…" : "🔒 Freeze & sign for onboard"}</button>
         <span class="muted" style="font-size:12px">Signs the bundle (sha256) + saves it as the frozen, onboard-loadable homework. RRS 41: frozen at the gun.</span>`}
    </div></div>`;
}

// ---- Playbook v2: the scenario PLAY LIBRARY (docs/PLAYBOOK_V2.md) -------------------------------
function pbPlaysSection(b) {
  const plays = b.plays || [];
  const robust = ((b.nominal || {}).robustness) || [];
  const cor = b.corridor;
  if (!plays.length && !robust.length) return "";
  const corLine = cor ? `<div class="muted" style="font-size:12px;margin:4px 0">
      <b>${cor.verdict === "geometry" ? "⚖ The line matters here" : "🎯 Execution race"}:</b>
      ${esc(cor.note || "")} (fan spread p90 ${cor.corridor_p90_nm ?? "—"} nm · stakes up to ${cor.stakes_min ?? 0} min)</div>` : "";
  const robustLine = robust.length ? `<div class="muted" style="font-size:12px;margin:4px 0">
      ✅ Nominal holds under: ${robust.map((r) => esc(r.name || r.scenario)).join(" · ")}</div>` : "";
  const vs = b.venue_stats;
  const vsLine = vs ? `<div class="muted" style="font-size:11px;margin:4px 0">Venue fleet-normal (from ${(vs.races || []).length} archived race(s), ${vs.n_boats} boats): XTE ${vs.xte_median_nm}/${vs.xte_p90_nm} nm (median/p90) · behind-own-plan ${vs.behind_median_min}/${vs.behind_p90_min} min — frozen into the bundle for onboard phrasing.</div>` : "";
  return `<div style="margin-top:12px">
    <h4 style="margin:4px 0">Plays — pre-routed answers to scenarios (${plays.length})</h4>
    ${corLine}${robustLine}${vsLine}
    ${plays.map(pbPlayCard).join("")}
  </div>`;
}

function pbPlayCard(p) {
  const d = p.scenario || {};
  const preds = ((p.conditions || {}).predicates || [])
    .map((x) => `${esc(x.signal)} ${esc(x.op)} ${esc(String(x.value))}${x.sustain_min ? ` (≥${x.sustain_min} min)` : ""}`).join(" AND ");
  return `<details class="pb-var" style="margin-top:6px">
    <summary><b>${esc(p.name)}</b>
      <span class="pill">${esc(p.category)}</span>
      <span class="pill ${p.stakes_min >= 60 ? "warn" : ""}">stakes ~${p.stakes_min ?? 0} min</span>
      ${p.favored_side ? `<span class="pill">${esc(p.favored_side)}</span>` : ""}
      <span class="muted" style="font-size:12px">${esc(p.summary || "")}</span></summary>
    <div class="pb-row"><span class="pb-lbl">You'd observe</span><span>${esc((p.conditions || {}).narrative || "")}</span></div>
    ${preds ? `<div class="pb-row"><span class="pb-lbl">Arms when</span><span class="mono" style="font-size:11px">${preds}</span></div>` : ""}
    ${p.rationale ? `<div class="pb-row"><span class="pb-lbl">Why</span><span>${esc(p.rationale)}</span></div>` : ""}
    ${p.tradeoffs ? `<div class="pb-row"><span class="pb-lbl">Tradeoffs</span><span>${esc(p.tradeoffs)}</span></div>` : ""}
    ${p.what_flips_it ? `<div class="pb-row flips"><span class="pb-lbl">Hands back when</span><span>${esc(p.what_flips_it)}</span></div>` : ""}
    ${(p.response || {}).route ? `<div class="pb-row"><span class="pb-lbl">Route</span><span>${(p.response.route.legs || []).length} legs · ${p.response.route.total_sailed_nm ?? "—"} nm · ${p.response.route.total_tacks ?? "—"} tacks · sails ${esc(((p.response.route.sail_plan || []).map((s) => s.sail || s)).join("→") || "—")}</span></div>` : ""}
    ${(p.response || {}).guidance ? `<div class="pb-row"><span class="pb-lbl">The call</span><span><b>${esc(p.response.guidance)}</b></span></div>` : ""}
  </details>`;
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

/* ---------- Fleet (roster + ORC handicaps → corrected-time tactics homework) ---------- */
const fleetNum = (x) => { const v = parseFloat(String(x).replace(/[^0-9.\-]/g, "")); return isNaN(v) ? null : v; };

async function renderFleet() {
  const view = document.getElementById("view");
  if (!Lab.races) {
    view.innerHTML = '<div class="loading">Loading…</div>';
    try { Lab.races = (await (await apiGet("/api/races")).json()).races || []; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  if (!Lab.sel && Lab.races.length) Lab.sel = Lab.races[0].race_id;
  if (!Lab.sel) { view.innerHTML = '<div class="placeholder">No race — ingest one in the Races tab.</div>'; return; }
  if (!Lab.editDef || Lab.editDef.race_id !== Lab.sel) {
    view.innerHTML = '<div class="loading">Loading fleet…</div>';
    try { Lab.editDef = await (await apiGet("/api/races/" + encodeURIComponent(Lab.sel))).json(); Lab.fleetMsg = ""; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  if (stale("fleet")) return;
  if (!Array.isArray(Lab.editDef.fleet)) Lab.editDef.fleet = [];
  if (!Lab.editDef.own || typeof Lab.editDef.own !== "object") Lab.editDef.own = {};
  Lab.fleetDirty = false;                 // freshly loaded from disk = in sync
  Lab.fleetEdit = !Lab.editDef.fleet.length;   // empty roster → open the editor; populated → show the read-only view
  paintFleet();
}

async function fleetPickRace(id) { Lab.sel = id; Lab.editDef = null; Lab.fleetMsg = ""; renderFleet(); }
function fleetSet(i, f, v) { if (Lab.editDef && Lab.editDef.fleet[i]) { Lab.editDef.fleet[i][f] = v; Lab.fleetDirty = true; } }
function fleetOwnSet(f, v) { if (!Lab.editDef) return; if (!Lab.editDef.own) Lab.editDef.own = {}; Lab.editDef.own[f] = v; Lab.fleetDirty = true; }
function fleetAddRow() { Lab.editDef.fleet.push({ boat: "", division: "", cls: "", owner: "", sail: "", orc_gph: null, rating: null, mmsi: "", source: "manual" }); Lab.fleetDirty = true; Lab.fleetEdit = true; paintFleet(); }
function fleetRemove(i) { Lab.editDef.fleet.splice(i, 1); Lab.fleetDirty = true; paintFleet(); }

// Auto-import the roster from public data. source: 'yb' = the tracker entry list, 'both' = YB → ORC,
// 'website' = a regatta-site URL/pasted text → ORC, 'orc' = enrich the current roster. extra = {url|text}
// for the website source. Replaces the draft roster for review (nothing is saved until Save).
function _fleetCC() { return (document.getElementById("fleetOrcCC") || {}).value || "USA"; }
function _fleetApplyResult(r) {
  if (!r.ok) { Lab.fleetMsg = r.note || r.detail || "import failed — no boats found"; Lab.fleetMsgErr = true; return; }
  Lab.editDef.fleet = r.entries || [];
  Lab.fleetDirty = true; Lab.fleetMsgErr = false;
  const bits = [`${r.count} boats`];
  if (r.matched != null) bits.push(`${r.matched}/${r.total} matched to ORC certs`);
  if (r.unmatched && r.unmatched.length) bits.push(`${r.unmatched.length} need a handicap by hand`);
  if (r.seeded_mmsi) bits.push(`${r.seeded_mmsi} kept a seeded MMSI`);
  if (r.orc_error) bits.push("ORC lookup failed: " + r.orc_error);
  Lab.fleetMsg = "Imported " + bits.join(" · ") + " — review below, then click Save roster.";
}
async function fleetAutoImport(source, extra) {
  Lab.fleetBusy = true;
  // a big fleet from a slow entry-list site (e.g. Bayview Mackinac ~200 boats via bycmack's embedded
  // entries page) + ORC enrichment can take a minute or two — set expectations so the wait isn't a mystery.
  Lab.fleetMsg = "Importing from public data… (a large fleet or a slow site can take a minute or two)";
  paintFleet();
  try {
    const r = await (await apiPost("/api/fleet/import", Object.assign(
      { race_id: Lab.sel, source, country: _fleetCC() }, extra || {}))).json();
    _fleetApplyResult(r);
  } catch (e) { Lab.fleetMsg = "import failed: " + e.message; }
  Lab.fleetBusy = false; paintFleet();
}
function fleetWebImport() {
  const url = (document.getElementById("fleetWebUrl") || {}).value || "";
  const text = (document.getElementById("fleetWebText") || {}).value || "";
  if (!url.trim() && !text.trim()) { Lab.fleetMsg = "Paste a regatta entry-list URL or the entry-list text."; return paintFleet(); }
  fleetAutoImport("website", url.trim() ? { url: url.trim() } : { text });
}
async function fleetUploadEntry() {
  const f = (document.getElementById("fleetEntryPdf") || {}).files[0];
  if (!f) { Lab.fleetMsg = "Choose an entry-list PDF first."; return paintFleet(); }
  Lab.fleetBusy = true; Lab.fleetMsg = "Extracting the entry list from the PDF…"; paintFleet();
  try {
    const fd = new FormData(); fd.append("file", f);
    const q = "?race_id=" + encodeURIComponent(Lab.sel) + "&country=" + encodeURIComponent(_fleetCC());
    const r = await (await api("/api/fleet/import/upload" + q, { method: "POST", body: fd })).json();
    _fleetApplyResult(r);
  } catch (e) { Lab.fleetMsg = "upload failed: " + e.message; }
  Lab.fleetBusy = false; paintFleet();
}

// Parse a pasted entry list — one boat per line, comma- or tab-separated:
// boat, division, rating, GPH, MMSI  (a header row is skipped; only boat is required).
function fleetImport() {
  const ta = document.getElementById("fleetPaste");
  const text = ta ? ta.value : "";
  let added = 0;
  for (let line of text.split(/\r?\n/)) {
    line = line.trim(); if (!line) continue;
    const c = line.split(/\t|,/).map((s) => s.trim());
    if (/^(boat|name|yacht)$/i.test(c[0])) continue;        // header
    if (!c[0]) continue;
    Lab.editDef.fleet.push({ boat: c[0], division: c[1] || "", cls: "", owner: "", sail: "",
      rating: fleetNum(c[2]), orc_gph: fleetNum(c[3]), mmsi: (c[4] || "").trim(), source: "manual" });
    added++;
  }
  if (ta) ta.value = "";
  if (added) { Lab.fleetDirty = true; Lab.fleetEdit = true; }
  Lab.fleetMsg = added ? `Appended ${added} boat${added === 1 ? "" : "s"} — review below, then click Save roster.`
                       : "No boats parsed — check the format (one boat per line).";
  Lab.fleetMsgErr = !added;
  paintFleet();
}

async function fleetSave() {
  // coerce numerics so the persisted JSON is clean (orc_gph/rating numbers|null)
  Lab.editDef.fleet = Lab.editDef.fleet.filter((e) => (e.boat || "").trim()).map((e) => ({
    boat: e.boat.trim(), division: e.division || "", cls: e.cls || "", owner: e.owner || "",
    sail: (e.sail || "").trim(), source: e.source || "manual",
    orc_gph: fleetNum(e.orc_gph), rating: fleetNum(e.rating), mmsi: (e.mmsi || "").trim() }));
  const own = Lab.editDef.own || {};
  Lab.editDef.own = { boat: (own.boat || "").trim(), division: own.division || "",
    orc_gph: fleetNum(own.orc_gph), rating: fleetNum(own.rating), mmsi: (own.mmsi || "").trim() };
  Lab.fleetMsg = "Saving…"; Lab.fleetMsgErr = false; paintFleet();
  try {
    const r = await (await apiPost("/api/races", { definition: Lab.editDef })).json();
    if (r.saved) { Lab.fleetDirty = false; Lab.fleetMsgErr = false;
      Lab.fleetMsg = `Saved — ${Lab.editDef.fleet.length} boat${Lab.editDef.fleet.length === 1 ? "" : "s"} in the roster.`; }
    else { Lab.fleetMsg = "Save failed."; Lab.fleetMsgErr = true; }
  } catch (e) { Lab.fleetMsg = "Save failed: " + e.message; Lab.fleetMsgErr = true; }
  paintFleet();
}

function fleetSrcBadge(e) {
  const s = e.source || "";
  if (!s || s === "manual") return "";
  const orc = s.includes("orc");
  return ` <span class="pill ${orc ? "ok" : "warn"}" title="${esc(orc ? "handicap from ORC cert" : "entry only; no ORC handicap — fill by hand")}" style="font-size:9px;padding:1px 5px">${orc ? "ORC" : "entry"}</span>`;
}

// Read-only "fleet at a glance" — the obvious list of the ingested roster (sorted by division, boat),
// with a coverage summary so a missing-handicap boat stands out. The editable grid lives in a details.
function fleetGlance(roster) {
  if (!roster.length) return "";
  const byDiv = {};
  roster.forEach((e) => { const k = (e.division || "—").trim() || "—"; byDiv[k] = (byDiv[k] || 0) + 1; });
  const divSummary = Object.entries(byDiv).sort().map(([k, n]) => `${esc(k)} ${n}`).join(" · ");
  const rated = roster.filter((e) => e.rating != null || e.orc_gph != null).length;
  const sorted = [...roster].sort((a, b) =>
    (a.division || "").localeCompare(b.division || "") || (a.boat || "").localeCompare(b.boat || ""));
  const li = (e) => `<tr>
    <td><b>${esc(e.boat || "—")}</b>${fleetSrcBadge(e)}</td>
    <td class="muted">${esc(e.sail || "")}</td>
    <td class="muted">${esc(e.cls || "")}</td>
    <td>${esc(e.division || "")}</td>
    <td>${e.rating != null ? esc(e.rating) : '<span class="conf bad" title="no ORC handicap — fill it in">—</span>'}</td>
    <td class="muted">${e.orc_gph != null ? esc(e.orc_gph) : ""}</td></tr>`;
  return `<div class="muted" style="font-size:12px;margin-bottom:6px"><b>${roster.length}</b> boat${roster.length === 1 ? "" : "s"} · <b>${rated}</b> with an ORC handicap${rated < roster.length ? ` · <b class="conf bad">${roster.length - rated} missing a rating</b>` : ""} · by division: ${divSummary}</div>
    <table class="fleet-tbl"><thead><tr><th>Boat</th><th>Sail #</th><th>Class</th><th>Div</th><th>Rating</th><th>GPH</th></tr></thead>
    <tbody>${sorted.map(li).join("")}</tbody></table>`;
}

function paintFleet() {
  const d = Lab.editDef, roster = d.fleet || [], own = d.own || {};
  const rp = d.rules_profile || {}, sc = rp.scoring || {}, tr = d.tracker || {};
  const inp = (val, ph, oninput, w) => `<input class="ein" style="width:${w}px" placeholder="${esc(ph)}" value="${esc(val == null ? "" : val)}" oninput="${oninput}">`;
  const row = (e, i) => `<tr>
    <td>${inp(e.boat, "boat name", `fleetSet(${i},'boat',this.value)`, 140)}${fleetSrcBadge(e)}</td>
    <td>${inp(e.sail, "sail #", `fleetSet(${i},'sail',this.value)`, 70)}</td>
    <td>${inp(e.division, "div", `fleetSet(${i},'division',this.value)`, 48)}</td>
    <td>${inp(e.cls, "class", `fleetSet(${i},'cls',this.value)`, 64)}</td>
    <td>${inp(e.owner, "owner", `fleetSet(${i},'owner',this.value)`, 110)}</td>
    <td>${inp(e.orc_gph, "GPH", `fleetSet(${i},'orc_gph',this.value)`, 56)}</td>
    <td>${inp(e.rating, "rating", `fleetSet(${i},'rating',this.value)`, 56)}</td>
    <td>${inp(e.mmsi, "MMSI", `fleetSet(${i},'mmsi',this.value)`, 90)}</td>
    <td><button class="mini" onclick="fleetRemove(${i})" title="remove">✕</button></td></tr>`;
  const permitted = !!rp.tracker_permitted;
  const ybCfg = (tr.provider || "").match(/yb|bycmack|ybtracking|yellowbrick/i);

  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Fleet <span class="muted" style="font-weight:400">— competitor roster + ORC handicaps (corrected-time tactics homework)</span></h3>
      <div class="opt-controls">
        <label>Race <select id="fleetRace" onchange="fleetPickRace(this.value)">
          ${Lab.races.map((x) => `<option value="${esc(x.race_id)}" ${x.race_id === Lab.sel ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
        <span class="muted" style="font-size:12px">Frozen at the gun → matched to AIS / the public tracker onboard for who-beats-whom on corrected time.</span>
      </div>
    </div>

    <div class="card">
      <h3>Our boat <span class="muted" style="font-weight:400">— the reference for corrected-time deltas</span></h3>
      <div class="opt-controls">
        <label>Boat ${inp(own.boat, "SR33 \"C4\"", "fleetOwnSet('boat',this.value)", 150)}</label>
        <label>Division ${inp(own.division, "div", "fleetOwnSet('division',this.value)", 70)}</label>
        <label>ORC GPH ${inp(own.orc_gph, "GPH", "fleetOwnSet('orc_gph',this.value)", 70)}</label>
        <label>Rating ${inp(own.rating, "ToT/ToD", "fleetOwnSet('rating',this.value)", 80)}</label>
        <label>MMSI ${inp(own.mmsi, "MMSI", "fleetOwnSet('mmsi',this.value)", 100)}</label>
      </div>
    </div>

    <div class="card">
      <h3>Auto-import from public data <span class="muted" style="font-weight:400">— entry list (YB) + ORC handicaps</span></h3>
      ${Lab.fleetMsg ? `<div class="banner ${Lab.fleetMsgErr ? "warn" : "ok"}" style="margin-bottom:8px">${esc(Lab.fleetMsg)}</div>` : ""}
      <div class="muted" style="font-size:12px;margin-bottom:8px">Pull the roster automatically: the <b>YB tracker RaceSetup</b> supplies the entry list (boat, sail #, owner, class) and the public <b>ORC certificate database</b> fills each boat's GPH + rating, matched by sail number / name. Both public; the result is a draft you review &amp; Save. ${ybCfg ? "" : '<b>This race has no YB tracker configured</b> (set it in the Rules/ingest step) — ORC-enrich the existing roster instead, or paste an entry list below.'}</div>
      <div class="opt-controls">
        <span class="muted" style="font-size:12px">Entry list from the <b>YB tracker</b>:</span>
        <button onclick="fleetAutoImport('both')" ${Lab.fleetBusy || !ybCfg ? "disabled" : ""} title="${ybCfg ? "" : "no YB tracker configured for this race"}">${Lab.fleetBusy ? "Importing…" : "YB entry list + ORC handicaps"}</button>
        <button class="mini" onclick="fleetAutoImport('yb')" ${Lab.fleetBusy || !ybCfg ? "disabled" : ""}>YB entry list only</button>
        <label>ORC country <input id="fleetOrcCC" class="ein" style="width:54px" value="USA"></label>
        <button class="mini" onclick="fleetAutoImport('orc')" ${Lab.fleetBusy ? "disabled" : ""}>Enrich current roster from ORC</button>
      </div>
      <div style="margin-top:10px;border-top:1px solid var(--line);padding-top:10px">
        <div class="muted" style="font-size:12px;margin-bottom:6px">Or from the <b>regatta website</b> (for races with no YB tracker — most races). <b>A YachtScoring or Regatta Network event link works directly</b> — paste the <code>yachtscoring.com/emenu/&lt;id&gt;</code> or <code>regattanetwork.com/…?regatta_id=&lt;id&gt;</code> URL. Most other regatta sites are JavaScript-rendered, so a URL fetch can't see the list — for those, <b>paste the entry-list text</b> (select it on the page → copy → paste below) or upload the PDF. ORC then fills the handicaps:</div>
        <div class="opt-controls">
          <input id="fleetWebUrl" class="ein" style="width:340px" placeholder="YachtScoring or Regatta Network event URL">
          <button onclick="fleetWebImport()" ${Lab.fleetBusy ? "disabled" : ""}>Fetch &amp; extract + ORC</button>
          <label>PDF <input type="file" id="fleetEntryPdf" accept=".pdf,application/pdf"></label>
          <button class="mini" onclick="fleetUploadEntry()" ${Lab.fleetBusy ? "disabled" : ""}>Upload entry-list PDF</button>
        </div>
        <textarea id="fleetWebText" rows="3" style="width:100%;box-sizing:border-box;margin-top:6px" placeholder="…or paste the entry-list text here (best for JS-rendered hubs like YachtScoring/Regatta Network where a fetch can't reach the list) — then Fetch &amp; extract"></textarea>
      </div>
      <div class="muted" style="font-size:11px;margin-top:6px">Auto-import REPLACES the draft roster with the imported boats (Save to persist). Unmatched boats keep their identity — fill the handicap by hand. Sail # is the ORC match key, so check it on any unmatched boat. <b>MMSIs you've seeded are preserved</b> across a re-import (entry lists never carry MMSI; a seeded MMSI is what makes an AIS target match a roster boat exactly).</div>
    </div>

    <div class="card">
      <h3>Roster <span class="muted" style="font-weight:400">— the ingested fleet</span>
        ${roster.length ? (Lab.fleetDirty ? '<span class="pill warn" style="margin-left:8px">unsaved draft — click Save roster</span>' : '<span class="pill ok" style="margin-left:8px">saved</span>') : ""}</h3>
      ${roster.length ? fleetGlance(roster) : `<div class="banner warn">No boats in the roster yet. Auto-import from the YB tracker or the regatta website above (then <b>Save roster</b>), or add/paste boats manually. An import only fills a draft here — it isn't stored until you Save.</div>`}
      <details ${Lab.fleetEdit ? "open" : ""} style="margin-top:10px" ontoggle="Lab.fleetEdit=this.open">
        <summary style="cursor:pointer;font-size:13px">Edit roster ${roster.length ? `(${roster.length} — add / remove / fix handicaps)` : "(add boats)"}</summary>
        ${roster.length ? `<table class="fleet-tbl" style="margin-top:8px"><thead><tr>
          <th>Boat</th><th>Sail #</th><th>Div</th><th>Class</th><th>Owner</th><th>ORC GPH</th><th>Rating</th><th>MMSI</th><th></th>
          </tr></thead><tbody>${roster.map(row).join("")}</tbody></table>` : ""}
        <div style="margin-top:8px"><button class="mini" onclick="fleetAddRow()">+ Add boat</button></div>
      </details>
    </div>

    <div class="card">
      <h3>Import entry list <span class="muted" style="font-weight:400">— manual paste (fallback)</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:6px">Paste one boat per line — <code>boat, division, rating, GPH, MMSI</code> (comma or tab separated; a header row is skipped; only the boat name is required).</div>
      <textarea id="fleetPaste" rows="5" style="width:100%;box-sizing:border-box" placeholder="Il Mostro, I, 0.9123, 650.2, 366123456&#10;Windquest, I, 0.9456, 638.0"></textarea>
      <div style="margin-top:8px"><button class="mini" onclick="fleetImport()">Parse &amp; append</button></div>
    </div>

    <div class="card">
      <h3>Scoring &amp; tracker <span class="muted" style="font-weight:400">— set during ingest / Rules</span></h3>
      <div class="dep-grid">
        <div class="dep-row"><span class="pill ${sc.method ? "ok" : "warn"}">${sc.method ? "✓" : "⚠"} Scoring</span><span class="muted">${esc(sc.system || "")} ${esc(sc.method || "not set")}</span></div>
        <div class="dep-row"><span class="pill ${tr.provider ? "ok" : "warn"}">${tr.provider ? "✓" : "⚠"} Tracker</span><span class="muted">${tr.provider ? esc(tr.provider) + (tr.race ? " · " + esc(tr.race) : "") + (permitted ? " · permitted onboard" : " · not permitted onboard") : "no public tracker configured"}</span></div>
      </div>
    </div>

    <div class="dactions"><button onclick="fleetSave()">${Lab.fleetDirty ? "Save roster ●" : "Save roster"}</button>
      <span class="muted" style="font-size:12px">${Lab.fleetDirty ? "you have unsaved changes" : (roster.length ? "roster saved" : "")}</span></div>
  </div>`;
}

/* ---------- Rules, Safety & Checklists ---------- */
/* Reuses the top-of-file constants (REQ_CATEGORIES / REQ_PHASES / TRIGGER_TYPES / PHASE_ORDER /
   PHASE_LABEL) and the path-binding helpers (ein / esel / etri / echk / eset). */
async function renderRules() {
  const view = document.getElementById("view");
  if (!Lab.races) {
    view.innerHTML = '<div class="loading">Loading…</div>';
    try { Lab.races = (await (await apiGet("/api/races")).json()).races || []; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  if (!Lab.sel && Lab.races.length) Lab.sel = Lab.races[0].race_id;
  if (!Lab.sel) { view.innerHTML = '<div class="placeholder">No race — ingest one in the Races tab.</div>'; return; }
  if (!Lab.editDef || Lab.editDef.race_id !== Lab.sel) {
    view.innerHTML = '<div class="loading">Loading checklist…</div>';
    try { Lab.editDef = await (await apiGet("/api/races/" + encodeURIComponent(Lab.sel))).json(); }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  try { Lab.checked = (await (await apiGet("/api/checklist?race_id=" + encodeURIComponent(Lab.sel))).json()).checked || {}; }
  catch (e) { Lab.checked = {}; }
  if (stale("rules")) return;
  if (!Lab.editDef.rules_profile || typeof Lab.editDef.rules_profile !== "object") Lab.editDef.rules_profile = {};
  if (!Array.isArray(Lab.editDef.rules_profile.modifications)) Lab.editDef.rules_profile.modifications = [];
  if (!Lab.editDef.rules_profile.scoring) Lab.editDef.rules_profile.scoring = {};
  if (!Array.isArray(Lab.editDef.requirements)) Lab.editDef.requirements = [];
  paintRules();
}

async function rulesPickRace(id) { Lab.sel = id; Lab.editDef = null; renderRules(); }
function modAdd() { Lab.editDef.rules_profile.modifications.push({ ref: "", rule: "", summary: "" }); paintRules(); }
function modRemove(i) { Lab.editDef.rules_profile.modifications.splice(i, 1); paintRules(); }
function reqAdd() {
  const ids = new Set(Lab.editDef.requirements.map((r) => r.id));
  let n = Lab.editDef.requirements.length + 1, id = "req-" + n;
  while (ids.has(id)) { n++; id = "req-" + n; }
  Lab.editDef.requirements.push({ id, category: "safety", phase: "pre_start", text: "",
    trigger_type: "none", trigger_detail: "", deliver_to_ipad: false, critical: false, source: "" });
  paintRules();
}
function reqRemove(i) { Lab.editDef.requirements.splice(i, 1); paintRules(); }

async function checkToggle(id, on) {
  Lab.checked = Lab.checked || {};
  if (on) Lab.checked[id] = true; else delete Lab.checked[id];
  // persist progress (labstate-backed, separate from the RaceDefinition)
  try { await apiPost("/api/checklist", { race_id: Lab.sel, checked: Lab.checked }); } catch (e) {}
  paintRules();
}

async function rulesSave() {
  const msg = document.getElementById("rulesMsg");
  if (msg) msg.textContent = "Saving…";
  try {
    const r = await (await apiPost("/api/races", { definition: Lab.editDef })).json();
    paintRules();
    const m2 = document.getElementById("rulesMsg");
    if (m2) m2.textContent = r.saved ? `Saved · ${(r.errors || []).length} error(s), ${(r.warnings || []).length} warning(s).` : "Save failed.";
  } catch (e) { const m2 = document.getElementById("rulesMsg"); if (m2) m2.textContent = "Save failed: " + e.message; }
}

function paintRules() {
  const d = Lab.editDef, rp = d.rules_profile, sc = rp.scoring || {}, mods = rp.modifications || [];
  const reqs = d.requirements, checked = Lab.checked || {};
  const ipadN = reqs.filter((r) => r.deliver_to_ipad).length;
  const critN = reqs.filter((r) => r.critical).length;
  const doneN = reqs.filter((r) => checked[r.id]).length;

  const reqBlock = (r, i) => `<div class="req-block ${r.critical ? "crit" : ""}">
    <div class="req-l1">
      <label class="req-chk"><input type="checkbox" ${checked[r.id] ? "checked" : ""} onchange="checkToggle('${attr(r.id)}',this.checked)"></label>
      ${ein(`requirements.${i}.text`, r.text, "requirement text")}
      <button class="mini" onclick="reqRemove(${i})" title="remove">✕</button>
    </div>
    <div class="req-l2">
      ${esel(`requirements.${i}.category`, r.category, REQ_CATEGORIES)}
      ${esel(`requirements.${i}.phase`, r.phase, REQ_PHASES)}
      ${esel(`requirements.${i}.trigger_type`, r.trigger_type || "none", TRIGGER_TYPES)}
      ${ein(`requirements.${i}.trigger_detail`, r.trigger_detail, "trigger detail")}
      ${ein(`requirements.${i}.source`, r.source, "source §")}
      ${echk(`requirements.${i}.deliver_to_ipad`, r.deliver_to_ipad, "→iPad")}
      ${echk(`requirements.${i}.critical`, r.critical, "critical")}
    </div></div>`;

  let checklistHtml = "";
  PHASE_ORDER.forEach((ph) => {
    const items = reqs.map((r, i) => ({ r, i })).filter((x) => x.r.phase === ph);
    if (!items.length) return;
    checklistHtml += `<div class="phase-h">${esc(PHASE_LABEL[ph] || ph)} <span class="muted">· ${items.length}</span></div>`;
    checklistHtml += items.map((x) => reqBlock(x.r, x.i)).join("");
  });
  const orphans = reqs.map((r, i) => ({ r, i })).filter((x) => !PHASE_ORDER.includes(x.r.phase));
  if (orphans.length) checklistHtml += `<div class="phase-h">Other</div>` + orphans.map((x) => reqBlock(x.r, x.i)).join("");

  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Rules, Safety &amp; Checklists <span class="muted" style="font-weight:400">— the prep checklist + the race rules layer</span></h3>
      <div class="opt-controls">
        <label>Race <select onchange="rulesPickRace(this.value)">
          ${Lab.races.map((x) => `<option value="${attr(x.race_id)}" ${x.race_id === Lab.sel ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
      </div>
    </div>

    <div class="card">
      <h3>Rules profile <span class="muted" style="font-weight:400">— RRS-41 carve-out, scoring, modifications</span></h3>
      <div class="opt-controls">
        <label>RRS edition ${ein("rules_profile.rrs_edition", rp.rrs_edition, "2025-2028")}</label>
        <label>Tracker permitted onboard? ${etri("rules_profile.tracker_permitted", rp.tracker_permitted)}</label>
      </div>
      <div class="dep-grid" style="margin-top:8px">
        ${echk("rules_profile.info_available_to_all_permitted", rp.info_available_to_all_permitted, "Info available to all boats permitted (RRS 41 §2.1(d) carve-out)")}
        ${echk("rules_profile.customized_advice_while_underway_prohibited", rp.customized_advice_while_underway_prohibited, "Customized advice while underway prohibited")}
        ${echk("rules_profile.appendix_wp", rp.appendix_wp, "World Sailing Appendix WP (waypoint racing) in force")}
      </div>
      <div class="opt-controls" style="margin-top:8px">
        <label>Scoring system ${ein("rules_profile.scoring.system", sc.system, "ORC")}</label>
        <label>Method ${ein("rules_profile.scoring.method", sc.method, "Single-Number ToT")}</label>
        <label>Ref ${ein("rules_profile.scoring.ref", sc.ref, "NOR §13")}</label>
      </div>
      <div style="margin-top:10px"><b style="font-size:12px">RRS modifications</b>
        <div class="muted" style="font-size:11px;margin:2px 0 6px">Ref + rule on top, the full modification text below (wraps — nothing truncated).</div>
        ${mods.length ? mods.map((m, i) => `<div class="req-block">
            <div class="req-l1">${ein(`rules_profile.modifications.${i}.ref`, m.ref, "ref (e.g. NOR §2.1)")}
              ${ein(`rules_profile.modifications.${i}.rule`, m.rule, "rule (e.g. RRS 41)")}
              <button class="mini" onclick="modRemove(${i})" title="remove">✕</button></div>
            <div style="margin-top:5px">${etxt(`rules_profile.modifications.${i}.summary`, m.summary, "what the modification says (full text)")}</div>
          </div>`).join("")
          : `<div class="muted" style="font-size:12px">No modifications recorded.</div>`}
        <div style="margin-top:6px"><button class="mini" onclick="modAdd()">+ Add modification</button></div>
      </div>
    </div>

    <div class="card">
      <h3>Checklist <span class="muted" style="font-weight:400">— ${reqs.length} items · ${ipadN} →iPad · ${critN} critical · ${doneN}/${reqs.length} checked</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:8px">The team's prep list (check-off persists separately from the race definition). <b>→iPad</b> items compile into the playbook and surface on the onboard console at their trigger.</div>
      ${checklistHtml || '<div class="muted">No requirements yet.</div>'}
      <div style="margin-top:10px"><button class="mini" onclick="reqAdd()">+ Add requirement</button></div>
    </div>

    <div class="dactions"><button onclick="rulesSave()">Save rules &amp; checklist</button>
      <span id="rulesMsg" class="muted" style="font-size:12px"></span>
      <span class="muted" style="font-size:11px">(check-off saves on its own)</span></div>
  </div>`;
}

/* ---------- Monitor (shore-side live view: fleet via tracker + our boat via cloud telemetry) ---------- */
const Mon = { raceId: null, demo: false, auto: false, data: null, map: null, fleetLayer: null, ownLayer: null, timer: null, lastAt: null };

async function renderMonitor() {
  if (Mon.timer) { clearInterval(Mon.timer); Mon.timer = null; }
  const view = document.getElementById("view");
  if (!Lab.races) {
    view.innerHTML = '<div class="loading">Loading…</div>';
    try { Lab.races = (await (await apiGet("/api/races")).json()).races || []; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  if (!Mon.raceId) Mon.raceId = Lab.sel || (Lab.races[0] && Lab.races[0].race_id) || null;
  if (!Mon.raceId) { view.innerHTML = '<div class="placeholder">No race — ingest one in the Races tab.</div>'; return; }
  await monFetch();
  if (stale("monitor")) return;
  paintMonitor();
}

async function monFetch() {
  try {
    Mon.data = await (await apiGet(`/api/monitor?race_id=${encodeURIComponent(Mon.raceId)}${Mon.demo ? "&demo=true" : ""}`)).json();
    Mon.lastAt = Date.now();
  } catch (e) { Mon.data = { error: String(e) }; }
}

async function monPickRace(id) { Mon.raceId = id; Lab.sel = id; renderMonitor(); }
async function monToggleDemo(on) { Mon.demo = on; await monFetch(); paintMonitor(); }
async function monRefresh() {
  if ((location.hash || "").slice(1) !== "monitor") { if (Mon.timer) { clearInterval(Mon.timer); Mon.timer = null; } return; }
  await monFetch(); paintMonitor();
}
function monToggleAuto(on) {
  Mon.auto = on;
  if (Mon.timer) { clearInterval(Mon.timer); Mon.timer = null; }
  if (on) Mon.timer = setInterval(monRefresh, 30000);
}

function paintMonitor() {
  const data = Mon.data || {};
  const fl = data.fleet || { fixes: [], reason: data.error || "no data" };
  const own = data.own || { available: false, reason: data.error || "no data" };
  const fixes = fl.fixes || [];
  const ageMin = (t) => t ? Math.max(0, Math.round((Date.now() / 1000 - t) / 60)) : null;
  const ownLine = own.available
    ? `<span class="pill ok">● live</span> ${own.lat.toFixed(3)}, ${own.lon.toFixed(3)} · SOG ${own.sog != null ? own.sog.toFixed(1) : "?"} kts · HDG ${own.heading != null ? Math.round(own.heading) : "?"}°${own.tws != null ? " · TWS " + own.tws.toFixed(0) + " kts" : ""} · ${own.age_s != null ? Math.round(own.age_s) + "s ago" : ""}${own.stale ? " <span class=\"pill warn\">stale</span>" : ""}`
    : `<span class="pill warn">○ no live boat</span> <span class="muted">${esc(own.reason || "unavailable")}</span>`;
  const flLine = fixes.length
    ? `<span class="pill ok">${fixes.length} boats</span> via ${esc(fl.provider || "tracker")}${fl.delay_min ? " · ~" + fl.delay_min + " min delayed" : ""}${fl.demo || fl.provider === "sample" ? " <span class=\"pill warn\">demo</span>" : ""}`
    : `<span class="pill warn">no fleet</span> <span class="muted">${esc(fl.reason || "unavailable")}</span>`;
  const permNote = fl.onboard_permitted ? "permitted onboard (in-race)" : "shore-side only (not permitted onboard for this race)";

  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Monitor <span class="muted" style="font-weight:400">— shore-side live view (the boat uses the onboard console in-race)</span></h3>
      <div class="opt-controls">
        <label>Race <select onchange="monPickRace(this.value)">
          ${Lab.races.map((x) => `<option value="${attr(x.race_id)}" ${x.race_id === Mon.raceId ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
        <button class="mini" onclick="monRefresh()">↻ Refresh</button>
        <label class="req-flag"><input type="checkbox" ${Mon.auto ? "checked" : ""} onchange="monToggleAuto(this.checked)"> auto (30s)</label>
        <label class="req-flag"><input type="checkbox" ${Mon.demo ? "checked" : ""} onchange="monToggleDemo(this.checked)"> demo fleet</label>
        <span class="muted" style="font-size:11px">${Mon.lastAt ? "updated " + new Date(Mon.lastAt).toLocaleTimeString() : ""}</span>
      </div>
      <div class="dep-grid" style="margin-top:8px">
        <div class="dep-row"><b style="min-width:78px;display:inline-block">Our boat</b> ${ownLine}</div>
        <div class="dep-row"><b style="min-width:78px;display:inline-block">Fleet</b> ${flLine} <span class="muted">· ${esc(permNote)}</span></div>
      </div>
    </div>
    <section class="cockpit-map"><div id="monMap" class="routemap routemap-hero"></div></section>
    ${fixes.length ? `<div class="card"><h3>Fleet positions <span class="muted" style="font-weight:400">— aged from the public tracker</span></h3>
      <table class="fleet-tbl"><thead><tr><th>Boat</th><th>SOG</th><th>COG</th><th>DTF</th><th>Age</th></tr></thead><tbody>
      ${fixes.slice().sort((a, b) => (a.dtf_nm ?? 1e9) - (b.dtf_nm ?? 1e9)).map((f) => `<tr>
        <td>${esc(f.name || "?")}</td><td>${f.sog != null ? f.sog.toFixed(1) + " kts" : ""}</td>
        <td>${f.cog != null ? Math.round(f.cog) + "°" : ""}</td><td>${f.dtf_nm != null ? f.dtf_nm.toFixed(1) + " nm" : ""}</td>
        <td>${ageMin(f.time) != null ? ageMin(f.time) + " min" : ""}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
  </div>`;
  initMonitorMap();
}

function initMonitorMap() {
  if (Mon.map) { try { Mon.map.remove(); } catch (e) {} Mon.map = null; }
  const el = document.getElementById("monMap");
  if (!el || !window.L) return;
  const map = L.map(el, { preferCanvas: true }); Mon.map = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
  const data = Mon.data || {}, fixes = (data.fleet || {}).fixes || [], own = data.own || {};
  const pts = [];
  fixes.forEach((f) => {
    L.circleMarker([f.lat, f.lon], { radius: 5, color: "#36b3ff", fillColor: "#36b3ff", fillOpacity: 0.65, weight: 1 })
      .bindTooltip(`${f.name || "?"}${f.sog != null ? " · " + f.sog.toFixed(1) + " kts" : ""}`).addTo(map);
    pts.push([f.lat, f.lon]);
  });
  if (own.available) {
    L.circleMarker([own.lat, own.lon], { radius: 8, color: "#f5c451", fillColor: "#f5c451", fillOpacity: 0.95, weight: 2 })
      .bindTooltip(`Our boat${own.sog != null ? " · " + own.sog.toFixed(1) + " kts" : ""}`).addTo(map);
    pts.push([own.lat, own.lon]);
  }
  if (pts.length) map.fitBounds(pts, { padding: [30, 30], maxZoom: 11 });
  else map.setView([44.2, -82.5], 7);   // Lake Huron default
}

/* ---------- Debrief (Lab-4 post-race judge loop) ---------- */
const Deb = { raceId: null, playbookId: null, playbooks: null, running: false, report: null, track: null, trackBusy: false, trackMsg: "" };
// Fleet-retro card state (docs/RETRO_STUDY.md — debrief across the WHOLE fleet of a past race)
const Retro = { ybId: "bayviewmack2025", msg: "", busy: false, races: null, job: null, report: null, poll: null };

async function renderDebrief() {
  const view = document.getElementById("view");
  if (!Lab.races) {
    view.innerHTML = '<div class="loading">Loading…</div>';
    try { Lab.races = (await (await apiGet("/api/races")).json()).races || []; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  if (!Deb.raceId) Deb.raceId = Lab.sel || (Lab.races[0] && Lab.races[0].race_id) || null;
  if (!Deb.raceId) { view.innerHTML = '<div class="placeholder">No race — ingest one in the Races tab.</div>'; return; }
  try { Deb.playbooks = ((await (await apiGet("/api/playbooks")).json()).playbooks || []).filter((b) => b.race_id === Deb.raceId); }
  catch (e) { Deb.playbooks = []; }
  if (stale("debrief")) return;
  if (!Deb.playbookId || !Deb.playbooks.some((b) => b.id === Deb.playbookId)) Deb.playbookId = (Deb.playbooks[0] || {}).id || null;
  try { Deb.track = await (await apiGet("/api/debrief/track?race_id=" + encodeURIComponent(Deb.raceId))).json(); }
  catch (e) { Deb.track = null; }
  await retroRefresh();
  try {
    Retro.job = await (await apiGet("/api/retro/run/status")).json();
    if (Retro.job.state === "running" && !Retro.poll) Retro.poll = setInterval(retroPoll, 8000);
  } catch (e) { /* card renders without job state */ }
  if (stale("debrief")) return;
  paintDebrief();
}

// Boat-track card: upload a GPX or fetch our YB track, then "Run debrief" scores helm vs optimal.
function debTrackCard() {
  const t = Deb.track || {};
  const status = t.available
    ? `<span class="pill ok">track loaded</span> <span class="muted">${esc(t.source === "yb" ? "YB" : "GPX")}${t.boat ? " · " + esc(t.boat) : ""} · ${t.n} fixes${t.matched_by ? " · " + esc(t.matched_by) : ""}</span>`
    : `<span class="muted">No boat track yet — upload a GPX export or fetch our YB track.</span>`;
  return `<div class="card">
    <h3>Boat track <span class="muted" style="font-weight:400">— the real sailed line, scored vs the oracle (helm execution)</span></h3>
    <div style="margin:4px 0 8px">${status} ${t.available ? '<button class="mini" onclick="debClearTrack()">Remove</button>' : ""}</div>
    <div class="opt-controls">
      <label>GPX <input type="file" id="debGpx" accept=".gpx,application/gpx+xml,text/xml"></label>
      <button onclick="debUploadGpx()" ${Deb.trackBusy ? "disabled" : ""}>Upload GPX</button>
      <span class="muted">or</span>
      <label>Our boat <input id="debBoat" placeholder="boat name in the YB feed" style="width:200px"></label>
      <button onclick="debFetchYb()" ${Deb.trackBusy ? "disabled" : ""}>${Deb.trackBusy ? "Fetching…" : "Fetch from YB tracker"}</button>
    </div>
    <div class="muted" style="font-size:12px;margin-top:4px">GPX: export the track from Expedition / a Vakaros / your instruments / a phone (offline, always works). YB: pulls our boat's full track from the permitted public tracker (shore-side debrief use). ${Deb.trackMsg ? '<b>' + esc(Deb.trackMsg) + '</b>' : ""}</div>
  </div>`;
}

async function debUploadGpx() {
  const f = document.getElementById("debGpx").files[0];
  if (!f) { Deb.trackMsg = "Choose a .gpx file first."; return paintDebrief(); }
  Deb.trackBusy = true; Deb.trackMsg = ""; paintDebrief();
  try {
    const fd = new FormData(); fd.append("file", f);
    const r = await (await api("/api/debrief/track/upload?race_id=" + encodeURIComponent(Deb.raceId), { method: "POST", body: fd })).json();
    Deb.trackMsg = r.ok ? `Loaded ${r.n} fixes from ${f.name}.` : (r.detail || "upload failed");
  } catch (e) { Deb.trackMsg = "upload failed"; }
  Deb.trackBusy = false;
  Deb.track = await (await apiGet("/api/debrief/track?race_id=" + encodeURIComponent(Deb.raceId))).json();
  paintDebrief();
}

async function debFetchYb() {
  const boat = document.getElementById("debBoat").value.trim();
  Deb.trackBusy = true; Deb.trackMsg = ""; paintDebrief();
  try {
    const r = await (await apiPost("/api/debrief/track/fetch", { race_id: Deb.raceId, boat })).json();
    Deb.trackMsg = r.ok ? `Fetched ${r.n} fixes for ${esc(r.boat || boat)} (${r.matched_by || "matched"}).`
      : (r.note || r.detail || "fetch failed") + (r.boats ? " — boats: " + r.boats.slice(0, 20).join(", ") : "");
  } catch (e) { Deb.trackMsg = "fetch failed"; }
  Deb.trackBusy = false;
  Deb.track = await (await apiGet("/api/debrief/track?race_id=" + encodeURIComponent(Deb.raceId))).json();
  paintDebrief();
}

async function debClearTrack() {
  await apiPost("/api/debrief/track/clear", { race_id: Deb.raceId });
  Deb.track = { available: false }; Deb.trackMsg = "Track removed."; paintDebrief();
}

async function debPickRace(id) { Deb.raceId = id; Lab.sel = id; Deb.report = null; Deb.playbookId = null; renderDebrief(); }
async function debRun() {
  if (!Deb.playbookId) return;
  Deb.running = true; paintDebrief();
  try { Deb.report = await (await apiPost("/api/debrief/run", { race_id: Deb.raceId, playbook_id: Deb.playbookId })).json(); }
  catch (e) { Deb.report = { available: false, note: String(e) }; }
  Deb.running = false; paintDebrief();
}
async function debApply() {
  const ta = document.getElementById("debLearn"); if (!ta) return;
  const msg = document.getElementById("debApplyMsg"); if (msg) msg.textContent = "Saving…";
  try {
    const r = await (await apiPost("/api/debrief/apply", { race_id: Deb.raceId, learnings: ta.value })).json();
    if (msg) msg.textContent = r.saved ? "Promoted to Learnings ✓" : ("Failed: " + (r.note || ""));
  } catch (e) { if (msg) msg.textContent = "Failed: " + e.message; }
}

function paintDebrief() {
  const r = Deb.report;
  const pbOpts = (Deb.playbooks || []).map((b) => `<option value="${attr(b.id)}" ${b.id === Deb.playbookId ? "selected" : ""}>${esc((b.signed ? "🔒 " : "") + (b.headline || b.id).slice(0, 56))} · ${b.n_variants} var</option>`).join("");
  let body = "";
  if (Deb.running) body = '<div class="card"><div class="loading">Running the judge — building the actual-wind field, routing the oracle, writing the critique… (~1–2 min)</div></div>';
  else if (r && r.available) body = debReport(r);
  else if (r && !r.available) body = `<div class="card"><div class="placeholder">${esc(r.note || "debrief unavailable")}</div></div>`;

  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Debrief <span class="muted" style="font-weight:400">— post-race judge loop: oracle re-route → regret → critique → write-back</span></h3>
      <div class="opt-controls">
        <label>Race <select onchange="debPickRace(this.value)">
          ${Lab.races.map((x) => `<option value="${attr(x.race_id)}" ${x.race_id === Deb.raceId ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
        ${(Deb.playbooks || []).length ? `<label>Playbook <select onchange="Deb.playbookId=this.value" style="min-width:300px">${pbOpts}</select></label>
          <button onclick="debRun()" ${Deb.running ? "disabled" : ""}>${Deb.running ? "Judging…" : "Run debrief"}</button>`
          : `<span class="muted">No frozen playbook for this race — freeze one in Gameplan to judge against.</span>`}
      </div>
      <div class="muted" style="font-size:12px;margin-top:4px">The optimizer re-routes the course on the wind that actually blew (oracle) and compares it to the plan you carried — which side paid, the regret vs perfect foresight, and a coach's critique you can promote into Learnings.</div>
    </div>
    ${debTrackCard()}
    ${body}
    ${retroCard()}
  </div>`;
}

// ---- Fleet retro (docs/RETRO_STUDY.md): past-race archive + per-boat optimizer backtest ---------
function retroCard() {
  const races = Retro.races || [];
  const j = Retro.job || {};
  const running = j.state === "running";
  const archived = races.map((x) =>
    `<button class="pill${x.race_id === Retro.ybId ? " ok" : ""}" style="cursor:pointer;border:none"
       title="Select this archived race and load its report"
       onclick="retroPick('${attr(x.race_id)}')">${esc(x.race_id)} · ${x.entries} boats · ${x.tracks} tracks · ${x.polars} polars · ${x.runs} runs</button>`).join(" ")
    || '<span class="muted">nothing archived yet</span>';
  const jobLine = running
    ? `<div class="muted" style="font-size:12px;margin-top:6px">Fleet batch running… <span class="loading-dot"></span><br>${esc((j.progress || []).slice(-3).join(" · "))}</div>`
    : (j.state === "done" && j.result ? `<div class="muted" style="font-size:12px;margin-top:6px"><b>Batch done:</b> ${j.result.ran} boats scored${(j.result.failed || []).length ? ", " + j.result.failed.length + " failed" : ""}.</div>`
       : (j.state === "error" ? `<div class="muted" style="font-size:12px;margin-top:6px"><b>Batch error:</b> ${esc(j.error || "")}</div>` : ""));
  return `<div class="card">
    <h3>Fleet retro <span class="muted" style="font-weight:400">— past race, every boat: its ORC polar + the forecast at ITS gun vs the line it actually sailed</span></h3>
    <div style="margin:4px 0 8px">${archived}</div>
    <div class="opt-controls">
      <label>YB race id <input id="retroYb" value="${attr(Retro.ybId)}" style="width:170px"></label>
      <button class="mini" onclick="retroIngest()" ${Retro.busy ? "disabled" : ""}>1 · Ingest race</button>
      <button class="mini" onclick="retroPolars()" ${Retro.busy ? "disabled" : ""}>2 · Match ORC polars</button>
      <button class="mini" onclick="retroRun()" ${Retro.busy || running ? "disabled" : ""}>${running ? "Batch running…" : "3 · Run fleet batch"}</button>
      <button class="mini" onclick="retroReport()" ${Retro.busy ? "disabled" : ""}>4 · Report</button>
      ${Retro.msg ? `<span class="muted" style="font-size:12px">${esc(Retro.msg)}</span>` : ""}
    </div>
    ${jobLine}
    ${retroReportHtml()}
    <div class="muted" style="font-size:12px;margin-top:6px">Everything gathered is kept in the retro archive (tracks, results, certs/polars, runs, pinned GRIBs). Correlation, not causation — a good crew both routes well and sails fast; correlations run within divisions to blunt rating luck.</div>
  </div>`;
}

function retroReportHtml() {
  const r = Retro.report;
  if (!r) return "";
  if (!r.ok) return `<div class="placeholder" style="margin-top:8px">${esc(r.note || "no report")}</div>`;
  const rho = (v) => v === null || v === undefined ? "—" : `<b style="color:${v > 0.25 ? "var(--ok, #2e7d32)" : (v < -0.25 ? "var(--warn, #b26a00)" : "inherit")}">${v}</b>`;
  const pooled = r.pooled ? `<div style="margin:6px 0"><b>Pooled (${r.pooled.n} boats):</b>
      behind-own-optimal vs rank ρ=${rho(r.pooled.rho_behind_min)} ·
      XTE vs rank ρ=${rho(r.pooled.rho_xte)} ·
      polar% vs rank ρ=${rho(r.pooled.rho_polar_pct)}
      <span class="muted" style="font-size:12px">(ρ&gt;0 for behind/XTE = closer to the optimizer line went with a better finish; ρ&lt;0 for polar% = faster execution went with a better finish)</span></div>` : "";
  const divs = (r.divisions || []).map((d) => `
    <details style="margin-top:6px"><summary><b>${esc(d.division)}</b> — n=${d.n} ·
      behind ρ=${rho(d.rho_behind_min)} · XTE ρ=${rho(d.rho_xte)} · extra-dist ρ=${rho(d.rho_extra_distance)} · polar ρ=${rho(d.rho_polar_pct)}
      · top-third side: ${esc(Object.entries(d.top_third_sides || {}).map(([s, n]) => s + "×" + n).join(", ") || "—")}</summary>
      <table class="tbl" style="margin-top:4px"><tr><th>#</th><th>boat</th><th>behind own opt</th><th>XTE mean</th><th>extra dist</th><th>polar %</th><th>side</th></tr>
      ${d.boats.map((b) => `<tr><td>${b.rank}</td><td>${esc(b.boat || "?")}</td><td>${b.behind_min == null ? "—" : Math.round(b.behind_min) + " min"}</td><td>${b.xte_nm == null ? "—" : b.xte_nm + " nm"}</td><td>${b.extra_pct == null ? "—" : b.extra_pct + "%"}</td><td>${b.polar_pct == null ? "—" : b.polar_pct}</td><td>${esc(b.side || "—")}</td></tr>`).join("")}
      </table></details>`).join("");
  return `<div style="margin-top:8px">${pooled}${divs}</div>`;
}

async function retroRefresh() {
  try { Retro.races = (await (await apiGet("/api/retro/races")).json()).races || []; } catch (e) { Retro.races = []; }
}
async function retroPick(raceId) {
  // click an archived-race pill → select it + load its report (no ids to remember)
  Retro.ybId = raceId; Retro.report = null;
  paintDebrief();
  await retroReport();
}
async function retroIngest() {
  Retro.ybId = document.getElementById("retroYb").value.trim();
  Retro.busy = true; Retro.msg = "ingesting (tracks + results)…"; paintDebrief();
  try {
    const r = await (await api("/api/retro/ingest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ race_id: Retro.ybId }) })).json();
    Retro.msg = r.ok ? `ingested: ${r.tracks} tracks, ${r.results} results, ${r.divisions} divisions` : (r.note || r.detail || "ingest failed");
  } catch (e) { Retro.msg = "ingest failed"; }
  Retro.busy = false; await retroRefresh(); paintDebrief();
}
async function retroPolars() {
  Retro.ybId = document.getElementById("retroYb").value.trim();
  Retro.busy = true; Retro.msg = "matching ORC certs (USA + CAN)…"; paintDebrief();
  try {
    let total = 0;
    for (const cc of ["USA", "CAN"]) {
      const r = await (await api("/api/retro/polars", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ race_id: Retro.ybId, country: cc }) })).json();
      if (r.ok) total += r.matched;
    }
    Retro.msg = `${total} boats matched to ORC polars`;
  } catch (e) { Retro.msg = "polar match failed"; }
  Retro.busy = false; await retroRefresh(); paintDebrief();
}
async function retroRun() {
  Retro.ybId = document.getElementById("retroYb").value.trim();
  Retro.msg = "";
  try {
    const r = await (await api("/api/retro/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ race_id: Retro.ybId }) })).json();
    if (!r.ok && r.note) { Retro.msg = r.note; paintDebrief(); return; }
    Retro.job = { state: "running", progress: [] };
    if (Retro.poll) clearInterval(Retro.poll);
    Retro.poll = setInterval(retroPoll, 8000);
  } catch (e) { Retro.msg = "batch start failed"; }
  paintDebrief();
}
async function retroPoll() {
  try { Retro.job = await (await apiGet("/api/retro/run/status")).json(); } catch (e) { return; }
  if (Retro.job.state !== "running" && Retro.poll) { clearInterval(Retro.poll); Retro.poll = null; await retroRefresh(); }
  if (location.hash.replace("#", "") === "debrief") paintDebrief();
}
async function retroReport() {
  Retro.ybId = document.getElementById("retroYb").value.trim();
  Retro.busy = true; Retro.msg = "building report…"; paintDebrief();
  try { Retro.report = await (await apiGet("/api/retro/report?race_id=" + encodeURIComponent(Retro.ybId))).json(); Retro.msg = ""; }
  catch (e) { Retro.msg = "report failed"; }
  Retro.busy = false; paintDebrief();
}

function debTrackScore(at) {
  if (!at.available) {
    return `<div class="card"><h3>Helm vs optimal</h3><div class="placeholder">${esc(at.note || "Upload a GPX or fetch our YB track above, then run the debrief.")}</div></div>`;
  }
  const tb = at.time_behind_optimal_min;
  const tbTxt = tb == null ? "—" : (tb >= 0 ? tb + " min behind optimal" : Math.abs(tb) + " min faster than the oracle line");
  const row = (label, val) => `<div class="dep-row"><b style="min-width:150px;display:inline-block">${label}</b> ${val}</div>`;
  // >100% can be real (soft rating / sailing above the cert) once the current is removed — colour it
  // ok when current-corrected, warn when not (likely a fair tide), and a runaway >120 always warns.
  const over = at.polar_pct > 108;
  const polCls = at.polar_pct == null ? "" : (at.polar_pct > 120 ? "warn" : over ? (at.current_corrected ? "ok" : "warn") : (at.polar_pct >= 90 ? "ok" : "bad"));
  const overNote = over ? (at.current_corrected ? " · &gt;100% = above cert (rated soft?)" : " · &gt;100% — likely current (uncorrected)") : "";
  const pol = at.polar_pct == null ? null
    : `<span class="pill ${polCls}">${at.polar_pct}% of polar</span> <span class="muted">(${at.polar_samples} samples${overNote})</span>`;
  const ccNote = at.current_corrected
    ? `<span class="muted"> · current-corrected (mean ${at.current_mean_kn ?? "?"} kts)</span>`
    : (at.polar_pct != null ? '<span class="muted"> · no current correction (SOG vs polar)</span>' : "");
  // helm_pct = flat-water-equivalent (sea-state loss removed) — the number the learning loop refines
  // helm_factor from. Show it when the seaway materially depressed the raw polar%.
  const helmGap = (at.wave_corrected && at.helm_pct != null && at.polar_pct != null) ? (at.helm_pct - at.polar_pct) : 0;
  const helmRow = helmGap >= 3
    ? row("Helm (flat-water-equiv)", `<span class="pill ok">${at.helm_pct}%</span> <span class="muted">sea state ~${at.sea_state_hs_mean ?? "?"} m excused ~${helmGap} pts — this is the crew number; polar% above still includes the waves</span>`)
    : "";
  const cav = (at.caveats || []).length
    ? `<div class="banner warn" style="margin-top:8px;font-size:12px">${at.caveats.map(esc).join("<br>")}</div>` : "";
  return `<div class="card">
    <h3>Helm vs optimal <span class="muted" style="font-weight:400">— ${esc(at.source === "yb" ? "YB track" : "GPX track")}${at.boat ? " · " + esc(at.boat) : ""} · ${at.fixes_scored}/${at.fixes_total} fixes scored</span></h3>
    <div class="dep-grid">
      ${row("Time behind optimal", `${tbTxt} <span class="muted">(sailed ${at.elapsed_hours != null ? at.elapsed_hours.toFixed(1) + " h" : "—"} vs oracle ${at.oracle_hours != null ? at.oracle_hours.toFixed(1) + " h" : "—"})</span>`)}
      ${row("Distance sailed", `${at.sailed_nm} nm` + (at.extra_distance_pct != null ? ` · <b>${at.extra_distance_pct}% over</b> the optimal ${at.optimal_nm != null ? at.optimal_nm + " nm" : ""}` : "") + (at.rhumb_nm ? ` <span class="muted">(rhumb ${at.rhumb_nm} nm)</span>` : ""))}
      ${row("Cross-track off optimal", at.xte_mean_nm != null ? `mean ${at.xte_mean_nm} nm · p90 ${at.xte_p90_nm} nm · max ${at.xte_max_nm} nm` : "—")}
      ${row("First beat worked", `<b>${esc(at.side_worked || "—")}</b>`)}
      ${pol ? row("Polar achieved", pol + ccNote) : ""}
      ${helmRow}
    </div>
    ${cav}
    <div class="muted" style="font-size:11px;margin-top:6px">The boat never sails the optimal line exactly — these are coaching deltas. Oversail + cross-track = steering/tactics; polar% = helm/trim vs conditions. The critique above separates the causes.</div>
  </div>`;
}

function debReport(r) {
  const reg = r.regret, o = r.oracle, pb = r.playbook, c = r.critique || {};
  const pill = reg.side_matched ? '<span class="pill ok">side held</span>' : '<span class="pill warn">side missed</span>';
  const regMin = reg.minutes != null ? (reg.minutes >= 0 ? reg.minutes + " min slower than optimal" : Math.abs(reg.minutes) + " min (plan beat the model)") : "—";
  const vrows = (pb.variants || []).map((v) => `<tr class="${v.side === reg.side_paid ? "winrow" : ""}">
      <td>${esc(v.side || "?")}${v.side === pb.recommended ? " ★" : ""}</td>
      <td>${v.total_hours != null ? v.total_hours.toFixed(1) + " h" : ""}</td>
      <td>${v.share != null ? Math.round(v.share * 100) + "%" : ""}</td>
      <td>${v.side === reg.side_paid ? "✓ paid" : ""}</td></tr>`).join("");
  return `<div class="card">
      <h3>Result ${pill}</h3>
      <div class="dep-grid">
        <div class="dep-row"><b style="min-width:130px;display:inline-block">Side that paid</b> <b>${esc(reg.side_paid)}</b> · recommended <b>${esc(reg.recommended_side || "—")}</b> ${reg.side_matched ? "(matched)" : "(missed)"}</div>
        <div class="dep-row"><b style="min-width:130px;display:inline-block">Oracle optimal</b> ${o.total_hours != null ? o.total_hours.toFixed(1) + " h" : "—"} · plan predicted ${pb.predicted_hours != null ? pb.predicted_hours.toFixed(1) + " h" : "—"}</div>
        <div class="dep-row"><b style="min-width:130px;display:inline-block">Regret</b> ${esc(regMin)}</div>
      </div>
      <table class="fleet-tbl" style="margin-top:8px"><thead><tr><th>Variant</th><th>Predicted</th><th>Agreement</th><th>Outcome</th></tr></thead><tbody>${vrows}</tbody></table>
      <div class="muted" style="font-size:11px;margin-top:6px">★ = recommended · highlighted = the side that paid. ${esc(r.caveat || "")}</div>
    </div>
    <div class="card">
      <h3>Coach's critique <span class="muted" style="font-weight:400">— ${esc(c.model || "deterministic")}</span></h3>
      <p style="margin:4px 0">${esc(c.assessment || "")}</p>
      ${c.key_lesson ? `<p style="margin:4px 0"><b>Lesson:</b> ${esc(c.key_lesson)}</p>` : ""}
      ${c.brain_edit ? `<p style="margin:4px 0"><b>Onboard-brain edit:</b> ${esc(c.brain_edit)}</p>` : ""}
      ${c.boat_model_note ? `<p style="margin:4px 0"><b>Boat model:</b> ${esc(c.boat_model_note)}</p>` : ""}
    </div>
    ${debTrackScore(r.actual_track || {})}
    <div class="card">
      <h3>Write-back → Learnings <span class="muted" style="font-weight:400">— review, then promote to the next prep</span></h3>
      <textarea id="debLearn" rows="3" style="width:100%;box-sizing:border-box">${esc(c.proposed_learnings || "")}</textarea>
      <div style="margin-top:8px"><button onclick="debApply()">Promote to Learnings</button> <span id="debApplyMsg" class="muted" style="font-size:12px"></span></div>
    </div>`;
}

/* ---------- Learnings (boat-level library + per-regatta notes) ---------- */
async function renderLearnings() {
  const view = document.getElementById("view");
  view.innerHTML = '<div class="loading">Loading learnings…</div>';
  try {
    await reloadBoats();
    Opt.boatModel = await (await apiGet("/api/crossovers")).json();
    Opt.polarGrid = await (await apiGet("/api/polars")).json();
    if (!Lab.races) Lab.races = (await (await apiGet("/api/races")).json()).races || [];
  } catch (e) { view.innerHTML = '<div class="placeholder">Failed to load learnings.</div>'; return; }
  if (!Lab.sel && Lab.races.length) Lab.sel = Lab.races[0].race_id;
  if (Lab.sel && (!Lab.editDef || Lab.editDef.race_id !== Lab.sel)) {
    try { Lab.editDef = await (await apiGet("/api/races/" + encodeURIComponent(Lab.sel))).json(); } catch (e) {}
  }
  await learnReload();
  if (stale("learnings")) return;
  paintLearnings();
}

async function learnPickRace(id) { Lab.sel = id; Lab.editDef = null; renderLearnings(); }
async function learnSaveNotes() {
  if (!Lab.editDef) return;
  Lab.editDef.learnings_notes = document.getElementById("learnNotes").value;
  const msg = document.getElementById("learnMsg"); if (msg) msg.textContent = "Saving…";
  try {
    const r = await (await apiPost("/api/races", { definition: Lab.editDef })).json();
    if (msg) msg.textContent = r.saved ? "Saved." : "Save failed.";
  } catch (e) { if (msg) msg.textContent = "Save failed: " + e.message; }
}

function paintLearnings() {
  const ab = (Opt.boats || []).find((b) => b.boat_id === Opt.activeBoat) || {};
  const m = Opt.boatModel;
  const card = (m && m.crossovers) ? boatModelCard(m) : `<div class="card"><div class="placeholder">Boat model unavailable.</div></div>`;
  const d = Lab.editDef || {};
  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Learnings <span class="muted" style="font-weight:400">— the boat-level library (carries across races) + what's applied to this regatta</span></h3>
      <div class="muted" style="font-size:12px">Active boat: <b>${esc(ab.name || Opt.activeBoat || "—")}</b>${ab.draft_ft != null ? " · draft " + ab.draft_ft + " ft" : ""} · switch boat / draft in <a href="#gameplan">Gameplan</a>. The polars + sail crossovers below are the refined boat model the optimizer routes on and freezes into the playbook.</div>
    </div>
    ${card}
    <div class="card">
      <h3>Applied to this regatta</h3>
      <div class="opt-controls">
        <label>Race <select onchange="learnPickRace(this.value)">
          ${(Lab.races || []).map((x) => `<option value="${attr(x.race_id)}" ${x.race_id === Lab.sel ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
      </div>
      <div class="muted" style="font-size:12px;margin:8px 0 4px">Venue / conditions knowledge, calibration reminders, what to apply for this race (free text, saved with the race).</div>
      <textarea id="learnNotes" rows="6" style="width:100%;box-sizing:border-box" placeholder="e.g. Cove Island current sets NE on the ebb; J2 crossover felt early last year — try 15 kts; main calibration +2° at the masthead…">${esc(d.learnings_notes || "")}</textarea>
      <div style="margin-top:8px"><button onclick="learnSaveNotes()">Save notes</button> <span id="learnMsg" class="muted" style="font-size:12px"></span></div>
    </div>
    ${learnRefineCard(ab)}
    ${learnWaveCard(ab)}
    ${learnTrendCard()}
    ${learnArchiveCard()}
  </div>`;
}

// Lab-4: human-reviewed boat-model refinement. propose() suggests; nothing lands until the human
// approves — the polars/helm only change on an explicit Approve click.
function learnRefineCard(ab) {
  const L = Lab.learning || {};
  const pending = (L.proposals || []).find((p) => p.status === "proposed" && (p.kind || "boat_model") === "boat_model");
  const adjN = (ab.polar_adjustments || []).length;
  const applied = (ab.helm_factor != null && Math.abs(ab.helm_factor - 1) > 1e-6) || adjN
    ? `<div class="muted" style="font-size:12px">Currently applied: helm factor <b>${ab.helm_factor != null ? ab.helm_factor : 1}</b>${ab.helm_factor > 1 ? " (above cert — rated soft)" : ""}${adjN ? ` · <b>${adjN}</b> polar-cell adjustments` : " · no polar overlay"}.</div>` : "";
  let body;
  if (L.busy) body = '<div class="loading">Working…</div>';
  else if (pending) body = learnProposalReview(pending);
  else body = `<div class="muted" style="font-size:12px">Generate a refinement proposal from the archived debriefs for <b>${esc(ab.name || "this boat")}</b>. It suggests a refined helm factor + per-(TWS,TWA) polar adjustments — <b>nothing changes until you approve</b>.</div>
    <div style="margin-top:8px"><button onclick="learnPropose()">Propose refinements</button> ${L.msg ? `<span class="muted" style="font-size:12px">${esc(L.msg)}</span>` : ""}</div>`;
  return `<div class="card">
    <h3>Refine the boat model <span class="muted" style="font-weight:400">— learning loop · human-approved</span></h3>
    ${applied}
    ${body}
    <div class="muted" style="font-size:11px;margin-top:8px">The ORC cert stays the canonical polar; approved tweaks are an explicit overlay the optimizer applies. The helm factor sets the overall achievable level; per-cell multipliers capture where the boat is relatively weak/strong by angle.</div>
  </div>`;
}

function learnProposalReview(p) {
  const s = p.summary || {};
  const pos = s.by_point_of_sail || {};
  const rows = (p.adjustments || []).map((a, i) => `<tr>
    <td><input type="checkbox" data-adj="${i}" checked></td>
    <td>${a.tws} kts</td><td>${a.twa}°</td><td>${esc(a.point_of_sail)}</td>
    <td><b class="conf ${a.mult < 1 ? "bad" : "ok"}">×${a.mult}</b></td>
    <td class="muted">${esc(a.basis || "")}</td></tr>`).join("");
  return `<div class="dep-grid" style="margin-bottom:8px">
      <div class="dep-row"><b style="min-width:150px;display:inline-block">Overall achieved</b> ${s.overall_pct}% of polar over ${s.n_samples} samples · ${(s.races || []).length} race(s)</div>
      <div class="dep-row"><b style="min-width:150px;display:inline-block">Helm factor</b> ${p.helm_current} → <b>${p.helm_proposed}</b>${p.helm_proposed > 1 ? ' <span class="muted">(above cert — rated soft / sailing above the polar)</span>' : ""} <input id="learnHelmEdit" type="number" step="0.01" min="0.5" max="1.2" value="${p.helm_proposed}" style="width:70px;margin-left:8px"> <span class="muted">(editable)</span></div>
      ${Object.keys(pos).length ? `<div class="dep-row"><b style="min-width:150px;display:inline-block">By point of sail</b> ${Object.entries(pos).map(([k, v]) => `${k} ${v}%`).join(" · ")}</div>` : ""}
    </div>
    ${rows ? `<div class="muted" style="font-size:12px;margin-bottom:4px">Proposed polar-cell adjustments — untick any you don't want to apply:</div>
    <table class="fleet-tbl" id="learnAdjTbl"><thead><tr><th></th><th>TWS</th><th>TWA</th><th>Point of sail</th><th>×mult</th><th>Basis</th></tr></thead><tbody>${rows}</tbody></table>`
      : '<div class="muted" style="font-size:12px">No per-cell adjustments proposed (the boat tracks the polar shape well) — just the helm factor.</div>'}
    <div style="margin-top:10px">
      <button onclick="learnApply(${p.id})">✓ Approve &amp; apply</button>
      <button class="mini" onclick="learnReject(${p.id})">Reject</button>
      <span id="learnApplyMsg" class="muted" style="font-size:12px"></span>
    </div>`;
}

// Lab-4 condition attribution: calibrate the sea-state degradation coefficients (ROUTE_WAVE_K_* per
// point of sail) from the boat's realized-polar archive. Same human-approved discipline as helm/polars.
function learnWaveCard(ab) {
  const L = Lab.learning || {};
  const pending = (L.proposals || []).find((p) => p.status === "proposed" && p.kind === "wave_coeffs");
  const wc = ab.wave_coeffs || {};
  const appliedTxt = Object.keys(wc).length
    ? `<div class="muted" style="font-size:12px">Applied wave model: up <b>${wc.k_up}</b>/m · reach <b>${wc.k_reach}</b>/m · run <b>${wc.k_down}</b>/m (deadband ${wc.hs_deadband} m, floor ${wc.floor}).</div>`
    : `<div class="muted" style="font-size:12px">Using the conservative default (env) wave priors — calibrate to fit this boat.</div>`;
  let body;
  if (L.waveBusy) body = '<div class="loading">Fitting…</div>';
  else if (pending) body = learnWaveReview(pending);
  else body = `<div class="muted" style="font-size:12px">Fit how much the boat slows per metre of wave height (by point of sail) from the archived debriefs — needs races sailed across a RANGE of sea states. <b>Nothing changes until you approve.</b></div>
    <div style="margin-top:8px"><button onclick="learnCalibrateWaves()">Calibrate from archive</button> ${L.waveMsg ? `<span class="muted" style="font-size:12px">${esc(L.waveMsg)}</span>` : ""}</div>`;
  return `<div class="card">
    <h3>Calibrate the sea-state model <span class="muted" style="font-weight:400">— condition attribution · human-approved</span></h3>
    ${appliedTxt}
    ${body}
    <div class="muted" style="font-size:11px;margin-top:8px">The wave model carries the sea-state speed loss so <b>helm_factor stays a flat-water number</b> (they don't double-count). Coefficients are fit as k = −slope/intercept of achieved-%-of-polar vs excess Hs, clamped to a sane band; a point of sail with no wave spread keeps its prior.</div>
  </div>`;
}

function learnWaveReview(p) {
  const s = p.summary || {};
  const pos = s.by_point_of_sail || {};
  const prop = p.wave || {};
  const label = { upwind: "Upwind (k_up)", reaching: "Reaching (k_reach)", downwind: "Running (k_down)" };
  const rows = Object.entries(pos).map(([k, d]) => `<tr>
    <td>${esc(label[k] || k)}</td>
    <td>${d.k_current}</td>
    <td><b class="conf ${d.confidence > 0 ? "ok" : ""}">${d.k_proposed}</b>${d.confidence > 0 ? "" : ' <span class="muted">(kept)</span>'}</td>
    <td>${d.n_cells} / ${d.hs_spread} m</td>
    <td>${d.confidence != null ? Math.round(d.confidence * 100) + "%" : "—"}${d.r2 != null ? ` · r²${d.r2}` : ""}</td>
    <td class="muted" style="font-size:11px">${esc(d.note || (d.helm_level != null ? "helm level " + d.helm_level : ""))}</td></tr>`).join("");
  const db = s.deadband || {};
  const dbRow = db.proposed != null
    ? `<div class="dep-row" style="margin:2px 0"><b style="min-width:150px;display:inline-block">Deadband (knee)</b> ${db.current} → <b>${db.proposed}</b> m <span class="muted">(${esc(db.source === "fit" ? "fit from the data" : "held prior")}${db.floor != null ? ` · floor ${db.floor} held` : ""})</span></div>`
    : "";
  return `<div class="muted" style="font-size:12px;margin:4px 0">Fit over ${p.n_bins} cells / ${(s.races || []).length} race(s). Proposed per-metre speed-loss coefficients:</div>
    ${dbRow}
    <table class="fleet-tbl"><thead><tr><th>Point of sail</th><th>Now</th><th>Proposed</th><th>Cells / Hs spread</th><th>Confidence</th><th></th></tr></thead><tbody>${rows}</tbody></table>
    <div style="margin-top:10px">
      <button onclick="learnApplyWave(${p.id})">✓ Approve &amp; apply</button>
      <button class="mini" onclick="learnReject(${p.id})">Reject</button>
      <span id="learnWaveMsg" class="muted" style="font-size:12px"></span>
    </div>`;
}

// Lab-4 condition attribution: multi-race trend — see the model improving over a season.
function learnTrendCard() {
  const t = (Lab.learning || {}).trend || {};
  const s = t.series || [];
  if (s.length < 1) return "";
  const bar = (v, lo, hi, cls) => {
    if (v == null) return '<span class="muted">—</span>';
    const f = Math.max(0, Math.min(1, (v - lo) / (hi - lo)));
    return `<span class="tbar"><span class="tbar-fill ${cls}" style="width:${Math.round(f * 100)}%"></span></span>`;
  };
  const rows = s.map((d) => `<tr>
    <td>${d.created_at ? new Date(d.created_at * 1000).toLocaleDateString() : ""}</td>
    <td>${esc(d.race_name || d.race_id || "")}</td>
    <td>${d.helm_pct != null ? d.helm_pct + "%" : "—"} ${bar(d.helm_pct, 70, 105, "ok")}</td>
    <td>${d.polar_pct != null ? d.polar_pct + "%" : "—"}</td>
    <td>${d.time_behind_min != null ? d.time_behind_min + " min" : "—"}</td>
    <td>${d.regret_min != null ? d.regret_min + " min" : "—"}</td>
    <td>${d.sea_state_hs_mean != null ? d.sea_state_hs_mean + " m" : "—"}</td></tr>`).join("");
  const ms = (t.milestones || []).map((m) => {
    const a = m.applied || {};
    const what = a.wave_coeffs ? "sea-state model" : (a.helm_factor != null ? `helm ${a.helm_factor}` + ((a.polar_adjustments || []).length ? ` + ${a.polar_adjustments.length} polar cells` : "") : "refinement");
    const when = m.decided_at ? new Date(m.decided_at * 1000).toLocaleDateString() : "";
    return `<li>${when} — approved <b>${esc(what)}</b></li>`;
  }).join("");
  return `<div class="card">
    <h3>Performance trend <span class="muted" style="font-weight:400">— ${s.length} race(s), oldest → newest</span></h3>
    <table class="fleet-tbl"><thead><tr><th>Date</th><th>Race</th><th>Helm (flat-water)</th><th>Polar% (raw)</th><th>vs optimal</th><th>Regret</th><th>Sea state</th></tr></thead><tbody>${rows}</tbody></table>
    ${ms ? `<div class="muted" style="font-size:12px;margin-top:8px">Applied refinements:</div><ul class="muted" style="font-size:12px;margin:4px 0">${ms}</ul>` : ""}
    <div class="muted" style="font-size:11px;margin-top:6px">Helm % is the flat-water-equivalent (sea-state loss removed) — the number the refinement loop tracks; Polar% (raw) still includes the seaway, so the gap between them is the conditions. Watch helm trend up and regret/time-behind shrink as approved refinements land.</div>
  </div>`;
}

function learnArchiveCard() {
  const dbs = (Lab.learning || {}).debriefs || [];
  if (!dbs.length) return `<div class="card"><h3>Performance archive</h3><div class="muted" style="font-size:12px">No archived debriefs yet — run a debrief with a boat track (in the <a href="#debrief">Debrief</a> tab) and it's recorded here for future review.</div></div>`;
  const rows = dbs.map((d) => `<tr>
    <td>${d.created_at ? new Date(d.created_at * 1000).toLocaleDateString() : ""}</td>
    <td>${esc(d.race_name || d.race_id || "")}</td>
    <td>${d.regret_min != null ? d.regret_min + " min" : "—"}</td>
    <td>${d.time_behind_min != null ? d.time_behind_min + " min" : "—"}</td>
    <td>${d.oversail_pct != null ? d.oversail_pct + "%" : "—"}</td>
    <td>${d.polar_pct != null ? d.polar_pct + "%" : "—"}</td>
    <td>${d.helm_pct != null ? d.helm_pct + "%" : "—"}</td>
    <td>${esc(d.side_worked || "—")}${d.side_matched != null ? (d.side_matched ? " ✓" : " ✗") : ""}</td>
    <td>${esc(d.track_source || "—")}</td></tr>`).join("");
  return `<div class="card">
    <h3>Performance archive <span class="muted" style="font-weight:400">— ${dbs.length} debrief(s), kept for review</span></h3>
    <table class="fleet-tbl"><thead><tr><th>Date</th><th>Race</th><th>Regret</th><th>vs optimal</th><th>Oversail</th><th>Polar%</th><th>Helm% (flat)</th><th>Side</th><th>Track</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="muted" style="font-size:11px;margin-top:6px">Every debrief is archived to the ongoing learning database (helm-vs-optimal metrics + observed-vs-polar bins) so race performance is reviewable across the season and feeds the refinement proposals above.</div>
  </div>`;
}

async function learnPropose() {
  Lab.learning = Object.assign({}, Lab.learning, { busy: true, msg: "" }); paintLearnings();
  try {
    const r = await (await apiPost("/api/learning/propose", {})).json();
    if (!r.ok) Lab.learning = Object.assign({}, Lab.learning, { busy: false, msg: r.note || "no proposal" });
    else await learnReload();
  } catch (e) { Lab.learning = Object.assign({}, Lab.learning, { busy: false, msg: "propose failed" }); }
  paintLearnings();
}

async function learnApply(pid) {
  const helm = parseFloat(document.getElementById("learnHelmEdit").value);
  const keep = [...document.querySelectorAll("#learnAdjTbl input[data-adj]")];
  const prop = ((Lab.learning || {}).proposals || []).find((p) => p.id === pid) || {};
  const adjustments = (prop.adjustments || []).filter((_, i) => (keep[i] ? keep[i].checked : true));
  document.getElementById("learnApplyMsg").textContent = "Applying…";
  try {
    const r = await (await apiPost(`/api/learning/proposals/${pid}/apply`, { helm_factor: helm, adjustments })).json();
    if (!r.ok) { document.getElementById("learnApplyMsg").textContent = r.note || "apply failed"; return; }
    await reloadBoats(); await learnReload(); paintLearnings();
  } catch (e) { document.getElementById("learnApplyMsg").textContent = "apply failed"; }
}

async function learnReject(pid) {
  await apiPost(`/api/learning/proposals/${pid}/reject`, {});
  await learnReload(); paintLearnings();
}

async function learnCalibrateWaves() {
  Lab.learning = Object.assign({}, Lab.learning, { waveBusy: true, waveMsg: "" }); paintLearnings();
  try {
    const r = await (await apiPost("/api/learning/calibrate-waves", {})).json();
    if (!r.ok) Lab.learning = Object.assign({}, Lab.learning, { waveBusy: false, waveMsg: r.note || "not enough data" });
    else await learnReload();
  } catch (e) { Lab.learning = Object.assign({}, Lab.learning, { waveBusy: false, waveMsg: "calibrate failed" }); }
  Lab.learning = Object.assign({}, Lab.learning, { waveBusy: false }); paintLearnings();
}

async function learnApplyWave(pid) {
  const msg = document.getElementById("learnWaveMsg"); if (msg) msg.textContent = "Applying…";
  try {
    const r = await (await apiPost(`/api/learning/proposals/${pid}/apply`, {})).json();
    if (!r.ok) { if (msg) msg.textContent = r.note || "apply failed"; return; }
    await reloadBoats(); await learnReload(); paintLearnings();
  } catch (e) { if (msg) msg.textContent = "apply failed"; }
}

async function learnReload() {
  try {
    const [props, dbs, trend] = await Promise.all([
      (await apiGet("/api/learning/proposals")).json(),
      (await apiGet("/api/learning/debriefs")).json(),
      (await apiGet("/api/learning/trend")).json(),
    ]);
    Lab.learning = { busy: false, msg: "", waveBusy: false, waveMsg: "",
      proposals: props.proposals || [], debriefs: dbs.debriefs || [], trend };
  } catch (e) { Lab.learning = Object.assign({ busy: false }, Lab.learning); }
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

