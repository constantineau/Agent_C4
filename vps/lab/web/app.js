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
  return `<input class="ein num" value="${esc(val)}" placeholder="${attr(ph || "")}"
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
/* "?" toggle: keeps a hint short and puts the how-it-works detail one tap away. `html` is trusted
   markup written in this file (never data). The button is a 44px touch target; preventDefault so a
   tap inside a <summary> doesn't also open/close the <details>. Not for use inside a <label>. */
function qhelp(html) {
  return `<span class="qh"><button type="button" class="ghost qh-btn" aria-expanded="false" aria-label="More detail"
    onclick="qhToggle(event, this)"><span aria-hidden="true">?</span></button><span class="qh-txt" hidden>${html}</span></span>`;
}
function qhToggle(ev, b) {
  ev.preventDefault(); ev.stopPropagation();
  const t = b.nextElementSibling, open = t.hidden;
  t.hidden = !open; b.setAttribute("aria-expanded", String(open));
}

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
  } catch (e) { err.textContent = "Can't reach the Lab. Try again."; }
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
      throw new Error("timed out. A weather source is slow. Uncheck ECMWF and try again.");
    throw new Error(`server error ${res.status}${res.statusText ? " " + res.statusText : ""}`);
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
let routeHash = "", routeAsking = false;   // the hash being shown; true while the unsaved prompt is up
async function route() {
  if (!location.hash) history.replaceState(null, "", "#races");   // so a later click on Races is a no-op, not a reload
  const target = location.hash;
  // Leaving a tab with unsaved edits: put the old hash back (replaceState fires no hashchange) and ask
  // first — Save / Discard / Stay — instead of the next renderer silently refetching over them.
  if (routeAsking) { history.replaceState(null, "", routeHash); return; }   // Back/Forward under the prompt
  if (routeHash && target !== routeHash) {
    routeAsking = true;
    history.replaceState(null, "", routeHash);
    const ok = await confirmLeave();   // settles pending saves first; asks only if still dirty
    routeAsking = false;
    if (!ok) { history.replaceState(null, "", routeHash); return; }
    history.replaceState(null, "", target);
  }
  routeHash = target;
  const sec = target.slice(1);
  activeSec = sec;
  syncDirty();   // the bar + markers belong to the tab being entered
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
  if (sec === "polar") return renderPolar();
  location.hash = "#races";          // unknown route → the library
}
const clone = (o) => JSON.parse(JSON.stringify(o));

/* ---------- unsaved edits — ONE mechanism for every form that holds edits (DIRTY_SOURCES) ----------
   Races, Course, Rules, Fleet and Learnings all edit the same in-memory RaceDefinition. markClean()
   snapshots it right after a load or a successful save; it is dirty when it no longer matches the
   snapshot (or is an unsaved ingest draft). While dirty: a fixed Save bar sits at the bottom of the
   screen (no scrolling to the end of a long page), the tab gets a ● marker, and leaving the tab,
   switching race or closing the page asks first. EDIT_SAVE maps each editing tab to its own save (so
   its normalisation + messages still apply); every save returns true/false and calls markClean(). */
const EDIT_SAVE = {
  races: () => (Lab.isDraft ? saveDraft() : detailSave()),
  course: () => saveCourse(),
  rules: () => rulesSave(),
  fleet: () => fleetSave(),
  learnings: () => learnSaveNotes(),
};
// snap = the JSON a save actually sent (taken before the await), so an edit typed while the request
// is in flight stays dirty; omitted = the editor as it is now (right after a load).
function markClean(snap) {
  Lab.cleanDef = snap !== undefined ? snap : (Lab.editDef ? JSON.stringify(Lab.editDef) : null);
  syncDirty();
}
// After a save resolves: only re-baseline if the same definition is still in the editor (the user
// may have switched race or discarded while the request was in flight).
function markSaved(snap, ed) { if (Lab.editDef === ed) markClean(snap); }
// Every RaceDefinition write goes through one queue: two overlapping saves (bar + in-page button)
// then persist and resolve in the order they were sent, so the last baseline set is what's on disk.
let saveChain = Promise.resolve();
function queueDef(fn) {
  const p = saveChain.then(fn);
  saveChain = p.catch(() => {});
  return p;
}
const postDef = (path, body) => queueDef(() => apiPost(path, body).then((r) => r.json()));
// Wait out any save still in flight (and its handler's repaint) before leaving or discarding, so a
// late response can't repaint another tab or persist edits the user just threw away.
async function settleSaves() {
  let c;
  do { c = saveChain; await c; await new Promise((r) => setTimeout(r, 0)); } while (c !== saveChain);
}
// The race definition as a dirty source: edited on the EDIT_SAVE tabs, saved by that tab's own save.
function raceDirty() {
  if (!EDIT_SAVE[activeSec] || !Lab.editDef) return false;
  return Lab.isDraft || JSON.stringify(Lab.editDef) !== Lab.cleanDef;
}
function discardRaceEdits() {
  if (Lab.isDraft) { Lab.draft = null; Lab.isDraft = false; Lab.editDef = null; Lab.editVal = null; }
  else Lab.editDef = Lab.cleanDef ? JSON.parse(Lab.cleanDef) : null;
  markClean();
}
// Everything on a tab that can hold unsaved edits. Each source: the tabs it lives on, dirty(), a label
// for the bar/prompt, save() → true/false (its own normalisation + messages), discard(). The Save bar,
// the ● marker, the leave prompt and beforeunload are driven by ALL of them — the bar's Save saves every
// dirty one, Discard drops every dirty one, so no form's edits are silently left behind.
const DIRTY_SOURCES = [
  { tabs: Object.keys(EDIT_SAVE), dirty: raceDirty,
    label: () => (Lab.editDef && (Lab.editDef.name || Lab.editDef.race_id)) || "this race",
    save: () => EDIT_SAVE[activeSec](), discard: discardRaceEdits },
  // the boat model's crew thresholds (jib change-downs, Code 0, reefs) — not part of any race
  { tabs: ["learnings", "gameplan"], dirty: () => bmDirty(), label: () => "boat model",
    save: () => saveBoatModel(), discard: () => bmDiscard() },
];
const dirtySources = () => DIRTY_SOURCES.filter((s) => s.tabs.includes(activeSec) && s.dirty());
function editDirty() { return dirtySources().length > 0; }
// Save every dirty source in turn; stop at the first failure (its message is on the page).
async function saveDirty() {
  for (const s of dirtySources()) { if ((await s.save()) !== true) return false; }
  return true;
}
function discardEdits() { dirtySources().forEach((s) => s.discard()); syncDirty(); }
// "Fastnet" / "Fastnet and boat model" — what the bar and the leave prompt name.
const dirtyLabels = (srcs) => srcs.map((s) => s.label());
const onlyDraft = (srcs) => Lab.isDraft && srcs.length === 1 && srcs[0].dirty === raceDirty;
// → true when it's OK to replace/drop the edits (clean, saved, or discarded); false = stay put.
async function confirmLeave() {
  await settleSaves();
  if (!editDirty()) return true;
  const choice = await askUnsaved();
  if (choice === "save") {
    if ((await saveDirty()) !== true) return false;
    return editDirty() ? confirmLeave() : true;   // edited again while the save ran → ask again
  }
  if (choice === "discard") { discardEdits(); return true; }
  return false;
}
// A small in-page dialog (big touch targets — native confirm() is tiny on an iPad). buttons =
// [{c, label, cls}]; resolves with the clicked c, or `dismiss` for a tap outside the box.
function askModal(title, bodyHtml, buttons, focus, dismiss) {
  return new Promise((resolve) => {
    const el = document.createElement("div");
    el.className = "modal-back";
    el.innerHTML = `<div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="modalH">
      <h3 id="modalH">${esc(title)}</h3><p>${bodyHtml}</p>
      <div class="modal-btns">${buttons.map((b) =>
        `<button class="${b.cls}" data-c="${b.c}">${esc(b.label)}</button>`).join("")}</div></div>`;
    el.addEventListener("click", (e) => {
      const c = e.target.dataset ? e.target.dataset.c : null;
      if (!c && e.target !== el) return;
      el.remove(); resolve(c || dismiss);
    });
    document.body.appendChild(el);
    el.querySelector(`[data-c=${focus}]`).focus();
  });
}
function askUnsaved() {
  const srcs = dirtySources(), names = dirtyLabels(srcs).map((n) => `<b>${esc(n)}</b>`).join(" and ");
  const body = onlyDraft(srcs) ? `The draft ${names} is not saved.`
    : `Your changes to ${names} are not saved.`;
  return askModal("Unsaved changes", body, [
    { c: "stay", label: "Stay here", cls: "ghost" },
    { c: "discard", label: "Discard", cls: "ghost danger" },
    { c: "save", label: "Save", cls: "primary" }], "save", "stay");
}
// Repaint the Save bar + the tab marker. Cheap enough to run after every input (rAF-coalesced).
let dirtyRaf = 0, savebarNote = "", savebarTimer = 0;
function syncDirty() {
  const bar = document.getElementById("savebar");
  if (!bar) return;
  const srcs = dirtySources(), dirty = srcs.length > 0;
  const show = dirty || !!savebarNote;
  bar.hidden = !show;
  bar.classList.toggle("ok", !dirty && !!savebarNote);
  document.body.classList.toggle("has-savebar", show);
  document.body.classList.toggle("is-dirty", dirty);   // drives the in-page .if-dirty / .if-clean markers
  document.querySelectorAll("#tabs a").forEach((a) =>
    a.classList.toggle("dirty", dirty && a.getAttribute("href") === "#" + activeSec));
  document.getElementById("savebarText").textContent = dirty
    ? (onlyDraft(srcs) ? "Draft not saved" : "Unsaved changes") + " — " + dirtyLabels(srcs).join(" + ")
    : savebarNote;
  document.getElementById("savebarBtns").hidden = !dirty;
}
function queueSyncDirty() { if (!dirtyRaf) dirtyRaf = requestAnimationFrame(() => { dirtyRaf = 0; syncDirty(); }); }
["input", "change", "click"].forEach((t) => document.addEventListener(t, queueSyncDirty, true));
window.addEventListener("beforeunload", (e) => { if (editDirty()) { e.preventDefault(); e.returnValue = ""; } });
function flashSavebar(note) {
  savebarNote = note; clearTimeout(savebarTimer);
  savebarTimer = setTimeout(() => { savebarNote = ""; syncDirty(); }, 2500);
  syncDirty();
}
async function savebarSave() {
  const btn = document.getElementById("savebarSave");
  if (!editDirty()) return;
  btn.disabled = true; btn.textContent = "Saving…";
  let ok = false;
  try { ok = (await saveDirty()) === true; } catch (e) { ok = false; }
  btn.disabled = false; btn.textContent = "Save";
  if (ok) flashSavebar("Saved ✓");
  else { document.getElementById("savebarText").textContent = "Save failed. See the message on the page."; }
}
async function savebarDiscard() {
  if (!confirm("Throw away your unsaved changes?")) return;
  await settleSaves();
  discardEdits();
  route();   // repaint the tab from the restored (or reloaded) definition
}

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
          '<div class="muted">No races yet. Add one above.</div>'}</div></div>
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
      : `<span class="pill warn">not approved</span>`);
  return `<div class="raceitem ${r.race_id === Lab.sel ? "sel" : ""}" data-id="${esc(r.race_id)}"
      onclick="selectRace('${esc(r.race_id)}')">
    <div class="nm">${esc(r.name)}</div>
    <div class="meta">${esc(r.region || "")} · ${esc(r.start_date || r.year)}</div>
    <div class="pills"><span class="pill">${r.courses} courses</span>
      <span class="pill">${r.requirements} checklist</span>
      <span class="pill">${r.ipad_items} to iPad</span>${rev}</div></div>`;
}
async function selectRace(id) {
  if (!(await confirmLeave())) return;
  Lab.sel = id; Lab.draft = null; Lab.isDraft = false;
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
    paintDetail(); markClean();   // after the paint: detailWatchCard fills in an empty watch_plan
  } catch (e) { box.innerHTML = '<div class="placeholder">Failed to load race.</div>'; }
}

/* ---------- ingestion ---------- */
function ingestCard() {
  return `<div class="card"><h3>Add a race</h3>
    <div class="ing-row"><input id="ingUrl" placeholder="Race website or document link">
      <button class="mini" onclick="discover()">Find docs</button></div>
    <div id="ingCands" class="ing-cands"></div>
    <div class="ing-row"><input id="ingLink" placeholder="Or paste a PDF link">
      <button class="mini" onclick="addLink()">Add</button></div>
    <ul id="ingList" class="ing-list"></ul>
    <div class="ing-row"><label class="muted" style="font-size:12px">Or upload PDFs:</label>
      <input type="file" id="ingFiles" multiple accept="application/pdf"></div>
    <button onclick="extractDraft()" id="ingBtn">Read documents →</button>
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
      : '<div class="muted" style="font-size:12px">No PDFs found. Paste a link or upload one.</div>';
  } catch (e) { cands.innerHTML = '<div class="muted" style="font-size:12px">Search failed.</div>'; }
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
  if (!(await confirmLeave())) return;   // a new draft replaces whatever is in the editor
  btn.disabled = true; btn.textContent = "Reading…";
  setMsg("⏳ Reading the documents. Takes up to 2 min. Keep this tab open.", "var(--accent)");
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
        setMsg("✕ Timed out. The documents were too big. Upload the PDF, or fewer documents.", "var(--bad)");
      else setMsg("✕ Couldn't read the documents (server error " + res.status + ").", "var(--bad)");
      btn.disabled = false; btn.textContent = "Read documents →"; return;
    }
    if (resp.detail) {   // the backend detail often already reads "ingest failed: …" — don't double it
      const d = /^ingest failed/i.test(resp.detail) ? resp.detail : "Couldn't read the documents: " + resp.detail;
      setMsg("✕ " + d, "var(--bad)"); btn.disabled = false; btn.textContent = "Read documents →"; return;
    }
    Lab.draft = resp; Lab.editDef = resp.definition;
    Lab.editVal = { errors: resp.errors || [], warnings: resp.warnings || [] }; Lab.isDraft = true;
    const nm = (resp.definition && resp.definition.name) || "the race";
    const nwarn = (resp.warnings || []).length, nerr = (resp.errors || []).length;
    setMsg("✓ Draft ready for “" + nm + "”. Check it on the right."
      + (nwarn ? " " + nwarn + " to check." : "")
      + (nerr ? " " + nerr + " error" + (nerr > 1 ? "s" : "") + " to fix." : "")
      + " Not saved yet. Click Save to keep it.", "var(--accent2)");
    paintDetail();
  } catch (e) { setMsg("✕ Couldn't read the documents: " + (e && e.message ? e.message : e), "var(--bad)"); }
  btn.disabled = false; btn.textContent = "Read documents →";
}
async function saveDraft() {
  if (!Lab.editDef) return false;
  const sent = JSON.stringify(Lab.editDef), ed = Lab.editDef;
  try {
    // draft: true → the server keeps the stored prep fields (roster, notes, watch plan…) that a
    // fresh extraction sends empty; an ordinary save stores exactly what it sends.
    const r = await postDef("/api/races", { definition: JSON.parse(sent), draft: true });
    if (!r.saved) { alert("Save failed: " + (r.detail || "unknown")); return false; }
    Lab.races = null;
    if (Lab.editDef !== ed) { refreshRaceList(); return true; }   // draft discarded/replaced mid-save
    // Re-read what the server stored: a re-ingest keeps fields the draft lacks (fleet, notes…). Queued
    // like a save, so leaving the tab waits for it and it can't land on another tab's edits.
    let stored = null;
    try { stored = await queueDef(() => apiGet("/api/races/" + encodeURIComponent(r.race_id)).then((x) => x.json())); }
    catch (e) {}
    if (Lab.editDef !== ed) { refreshRaceList(); return true; }
    let clean = null;   // null = take the editor as it stands after the paint (nothing pending)
    if (stored && stored.race_id === r.race_id) {
      // The stored copy is the truth; overlay only what was edited while the save ran.
      const was = JSON.parse(sent), storedJSON = JSON.stringify(stored);
      let changed = false;
      for (const k of Object.keys(ed)) {
        if (k === "race_id" || JSON.stringify(ed[k]) === JSON.stringify(was[k])) continue;
        stored[k] = ed[k]; changed = true;
      }
      Lab.editDef = stored;
      if (changed) clean = storedJSON;
    } else {
      // Saved, but the stored copy didn't come back: stay a draft (its empty roster/notes would
      // otherwise read as real values, and the next ordinary save would clear the stored ones).
      ed.race_id = r.race_id; Lab.sel = r.race_id; refreshRaceList();
      const m = document.getElementById("ingMsg");
      if (m) { m.textContent = "Saved, but couldn't reload it. Click Save again."; m.style.color = "var(--warn)"; }
      return false;
    }
    Lab.sel = r.race_id; Lab.draft = null; Lab.sources = []; Lab.isDraft = false;
    Lab.editVal = { errors: r.errors || [], warnings: r.warnings || [] };
    if (activeSec === "races") paintDetail();
    if (clean === null) markClean(); else markClean(clean);   // after the paint, as loadRace does
    if (activeSec === "races") {
      renderSources();
      const m = document.getElementById("ingMsg"); if (m) { m.textContent = "✓ Saved to the library."; m.style.color = "var(--accent2)"; }
    }
    refreshRaceList();
    return true;
  } catch (e) { alert("Save failed."); return false; }
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
    detailWatchCard(d) +
    detailProvenance(d.provenance || {});
  syncDirty();
}
function detailBanner(v) {
  const errs = (v.errors || []), warns = (v.warnings || []);
  return errs.length
    ? `<div class="banner review"><b>${errs.length} errors</b>: ${errs.map(esc).join(" · ")}</div>`
    : warns.length
      ? `<div class="banner review"><b>Check these (${warns.length}):</b> ${warns.map(esc).join(" · ")}</div>`
      : `<div class="banner ok">No problems found. Ready to approve.</div>`;
}
function detailActions(d, v) {
  if (Lab.isDraft) {
    return `<div class="banner draft"><b>Draft read from the documents. Check it.</b>
      Fix anything below, then save.
      <button class="mini" onclick="saveDraft()">Save to library</button></div>`;
  }
  const hasErrs = (v.errors || []).length > 0;
  const sign = d.reviewed
    ? `<span class="pill ok">✓ Approved${d.reviewed_at ? " · " + esc(d.reviewed_at) : ""}</span>
       <button class="mini" onclick="detailApprove(false)">Un-approve</button>`
    : `<button class="mini" onclick="detailApprove(true)"${hasErrs ? " disabled title='fix the errors first'" : ""}>Approve</button>`;
  return `<div class="dactions">
    <button id="detailSaveBtn" onclick="detailSave()">Save edits</button>
    ${sign}<span id="detailMsg" class="muted" style="font-size:12px"></span></div>`;
}
function detailHead(d) {
  return `<div class="dhead"><h2>${ein("name", d.name, "Race name")}</h2>
      <div class="dmeta">${ein("organizing_authority", d.organizing_authority, "organizer")}<br>
        Start ${ein("start_date", d.start_date, "YYYY-MM-DD")} ·
        ${ein("start_area", d.start_area, "start area")} ·
        ${ein("region", d.region, "region")}</div></div>`;
}
// Course geometry is edited in ONE place — the Course & Marks tab (map + geocode + gate/island
// fields). The Races detail just summarizes each course, same delegation pattern as rules/fleet.
function detailCourseCard(c, ci) {
  const marks = c.marks || [];
  const missing = marks.filter((m) => m.lat == null || m.lon == null).length;
  const review = marks.filter((m) => m.coords_source === "needs_review").length;
  const warn = missing || review
    ? `<span class="pill warn">${missing ? missing + " without a position" : ""}${missing && review ? " · " : ""}${review ? review + " to check" : ""}</span>`
    : `<span class="pill ok">all marks placed</span>`;
  return `<div class="card"><h3>Course — ${ein(`courses.${ci}.name`, c.name, "course name")}</h3>
    <div class="muted" style="margin-bottom:8px">Divisions ${esc((c.applies_to_divisions || []).join(", "))}${
      c.distance_nm ? " · " + c.distance_nm + " nm" : ""}</div>
    <div class="dep-row"><a href="#course">Course &amp; Marks →</a>
      <span class="muted">${marks.length} mark${marks.length === 1 ? "" : "s"}${c.finish ? " + finish" : ""}</span> ${warn}</div></div>`;
}
// The rules/scoring/checklist + fleet now live in their own tabs — the Races view stays the ingest +
// library + sign-off hub and just points to them (avoids the duplicate editors, issue #28).
function detailDelegated(d) {
  const reqs = (d.requirements || []).length;
  const ipad = (d.requirements || []).filter((r) => r.deliver_to_ipad).length;
  const mods = ((d.rules_profile || {}).modifications || []).length;
  const fleet = (d.fleet || []).length;
  return `<div class="card"><h3>Rules, scoring, checklist &amp; fleet</h3>
    <div class="muted" style="font-size:12px;margin-bottom:8px">Edit these in their own tabs:</div>
    <div class="dep-grid">
      <div class="dep-row"><a href="#rules">Rules &amp; Checklist →</a> <span class="muted">${mods} rule change${mods === 1 ? "" : "s"}, scoring · ${reqs} checklist item${reqs === 1 ? "" : "s"} (${ipad} to iPad)</span></div>
      <div class="dep-row"><a href="#fleet">Fleet →</a> <span class="muted">${fleet} boat${fleet === 1 ? "" : "s"} and handicaps</span></div>
      <div class="dep-row"><a href="#course">Course &amp; Marks →</a> <span class="muted">marks on a map</span></div>
    </div></div>`;
}

/* WATCH PLAN (crew rotation) — authored here, delivered aboard with the homework (seeds the
   engine's /watch store), edited live from the iPad CREW tile. Lives on the definition so it
   survives an SI re-ingest (preserve list). The BLOCK LIST is the canonical format; the
   pattern generator (shared/watchplan.py, via /api/watchplan/generate) is a convenience. */
function watchPlanOf(d) {
  const w = (d.watch_plan = d.watch_plan || {});
  w.teams = w.teams || {};
  w.teams.A = w.teams.A || { name: "A", members: [] };
  w.teams.B = w.teams.B || { name: "B", members: [] };
  w.blocks = w.blocks || [];
  return w;
}
function watchWallToEpoch(val, tz) {
  const m = (val || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return null;
  const [, y, mo, d, h, mi] = m.map(Number);
  return tz ? zonedWallToEpoch(y, mo, d, h, mi, tz) : new Date(y, mo - 1, d, h, mi).getTime() / 1000;
}
function watchTeamMembers(tid, val) {
  watchPlanOf(Lab.editDef).teams[tid].members =
    val.split(",").map((s) => s.trim()).filter(Boolean);
}
function watchBlockSet(i, field, val) {
  const w = watchPlanOf(Lab.editDef), b = w.blocks[i];
  if (!b) return;
  if (field === "start" || field === "end") {
    const e = watchWallToEpoch(val, Lab.editDef.timezone || "");
    if (e != null) b[field] = e;
  } else if (field === "on") b.on = val;
  else if (field === "note") b.note = val.trim() || null;
  if (field !== "note") paintDetail();
}
function watchBlockAdd() {
  const w = watchPlanOf(Lab.editDef);
  const last = w.blocks[w.blocks.length - 1];
  const start = last ? last.end : Math.floor(Date.now() / 1000);
  w.blocks.push({ start, end: start + 4 * 3600, on: last && last.on === "A" ? "B" : "A", note: null });
  paintDetail();
}
function watchBlockDel(i) { watchPlanOf(Lab.editDef).blocks.splice(i, 1); paintDetail(); }
function watchClearBlocks() {
  if (!confirm("Clear all watches?")) return;
  watchPlanOf(Lab.editDef).blocks = [];
  paintDetail();
}
async function watchGen() {
  const w = watchPlanOf(Lab.editDef), tz = Lab.editDef.timezone || "";
  const anchor = watchWallToEpoch((document.getElementById("wgAnchor") || {}).value, tz);
  const hours = Number((document.getElementById("wgHours") || {}).value) || 0;
  let pattern = (document.getElementById("wgPattern") || {}).value;
  if (pattern === "custom") {
    pattern = ((document.getElementById("wgCustom") || {}).value || "")
      .split(",").map((s) => Number(s.trim())).filter((n) => n > 0);
  }
  const firstOn = (document.getElementById("wgFirst") || {}).value || "A";
  const msg = document.getElementById("watchMsg");
  if (anchor == null || !hours) { if (msg) msg.textContent = "Set the start time and total hours first."; return; }
  if (w.blocks.length && !confirm("Replace the current watches?")) return;
  try {
    const r = await (await apiPost("/api/watchplan/generate",
      { anchor, total_hours: hours, pattern, first_on: firstOn })).json();
    if (!(r.blocks || []).length) { if (msg) msg.textContent = "No watches made. Check the pattern."; return; }
    w.blocks = r.blocks;
    paintDetail();
    const m2 = document.getElementById("watchMsg");
    if (m2) m2.textContent = `${r.blocks.length} watches made. Set ALL for the start, then save.`;
  } catch (e) { if (msg) msg.textContent = "Couldn't make watches: " + (e.message || e); }
}
function detailWatchCard(d) {
  const w = watchPlanOf(d), tz = d.timezone || "";
  const teamRow = (tid) => `<div class="dep-row">
      <label>Team ${tid} ${ein(`watch_plan.teams.${tid}.name`, w.teams[tid].name, tid)}</label>
      <input class="ein" style="flex:1;min-width:260px" value="${attr((w.teams[tid].members || []).join(", "))}"
        placeholder="crew names, comma-separated" onchange="watchTeamMembers('${tid}', this.value)"></div>`;
  const hours = (b) => ((b.end - b.start) / 3600).toFixed(1).replace(/\.0$/, "");
  const onSel = (i, on) => `<select class="esel" onchange="watchBlockSet(${i}, 'on', this.value)">` +
    ["A", "B", "ALL"].map((o) => `<option ${o === on ? "selected" : ""}>${o}</option>`).join("") + `</select>`;
  const rows = w.blocks.map((b, i) => `<tr>
      <td><input type="datetime-local" value="${epochToWall(b.start, tz)}" onchange="watchBlockSet(${i}, 'start', this.value)"></td>
      <td><input type="datetime-local" value="${epochToWall(b.end, tz)}" onchange="watchBlockSet(${i}, 'end', this.value)"></td>
      <td class="muted">${hours(b)}h</td>
      <td>${onSel(i, b.on)}</td>
      <td><input class="ein" value="${attr(b.note || "")}" placeholder="note" onchange="watchBlockSet(${i}, 'note', this.value)"></td>
      <td><button class="mini" onclick="watchBlockDel(${i})">✕</button></td></tr>`).join("");
  return `<div class="card">
    <h3>Watch plan <span class="muted" style="font-weight:400">— ${w.blocks.length} watches${w.blocks.length ? "" : " (optional)"}</span></h3>
    <div class="muted" style="font-size:12px;margin-bottom:8px">Two teams take turns. <b>ALL</b> = all hands. Times are ${tz ? "race local (" + esc(tz) + ")" : "this browser's time"}.
      ${qhelp("Goes to the boat with the rest of the race prep. The iPad crew screen shows who's on and can change it live.")}</div>
    ${teamRow("A")}${teamRow("B")}
    <div class="opt-controls" style="margin-top:8px">
      <label>Start <input type="datetime-local" id="wgAnchor"></label>
      <label>Total <input class="ein num" id="wgHours" value="48" style="width:52px"> h</label>
      <label>Pattern <select id="wgPattern" onchange="document.getElementById('wgCustom').style.display = this.value === 'custom' ? '' : 'none'">
        <option value="4on4off">4 on / 4 off</option><option value="3on3off">3 on / 3 off</option>
        <option value="6on6off">6 on / 6 off</option><option value="swedish">Swedish 4-4-4-6-6</option>
        <option value="custom">custom…</option></select></label>
      <input class="ein" id="wgCustom" placeholder="hours, e.g. 5,5,4,5,5" style="display:none;width:140px">
      <label>First on <select id="wgFirst"><option>A</option><option>B</option></select></label>
      <button class="mini" onclick="watchGen()">Make watches</button>
      ${w.blocks.length ? `<button class="mini" onclick="watchClearBlocks()">Clear</button>` : ""}
      <span id="watchMsg" class="muted" style="font-size:12px"></span>
    </div>
    ${w.blocks.length ? `<div class="tbl-scroll" style="margin-top:8px"><table class="legs">
      <thead><tr><th>from</th><th>until</th><th></th><th>team</th><th>note</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <button class="mini" style="margin-top:6px" onclick="watchBlockAdd()">+ watch</button>` : ""}
  </div>`;
}

function detailProvenance(p) {
  const srcs = (p.sources || []).map((s) =>
    `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a>
     <span class="muted">${esc(s.retrieved || "")}</span></li>`).join("");
  return `<div class="card"><h3>Sources &amp; notes</h3>
    <ul style="margin:0 0 10px;padding-left:18px">${srcs || '<li class="muted">No sources recorded.</li>'}</ul>
    <label class="muted" style="font-size:12px">SI status ${ein("provenance.si_status", p.si_status, "SI status")}</label>
    <label class="muted" style="font-size:12px">Review notes ${etxt("provenance.review_status", p.review_status, "review notes")}</label></div>`;
}

/* ---------- detail edit: structural mutations (repaint), then save / approve ---------- */
async function detailSave(silent) {
  const btn = document.getElementById("detailSaveBtn");
  const setMsg = (t) => { const m = document.getElementById("detailMsg"); if (m) m.textContent = t; };
  if (btn) btn.disabled = true;
  if (!silent) setMsg("Saving…");
  const sent = JSON.stringify(Lab.editDef), ed = Lab.editDef;
  try {
    const r = await postDef("/api/races", { definition: JSON.parse(sent) });
    if (!r.saved) { setMsg("Save failed: " + (r.detail || "?")); return false; }
    Lab.editVal = { errors: r.errors || [], warnings: r.warnings || [] };
    Lab.races = null; markSaved(sent, ed);
    if (!silent) {
      paintDetail(); refreshRaceList();
      setMsg(`Saved. ${(r.warnings || []).length} still to check.`);
    }
    return true;
  } catch (e) { setMsg("Save failed."); return false; }
  finally { if (btn) btn.disabled = false; }
}
async function detailApprove(approved) {
  const setMsg = (t) => { const m = document.getElementById("detailMsg"); if (m) m.textContent = t; };
  const sent = JSON.parse(JSON.stringify(Lab.editDef)), ed = Lab.editDef;
  try {
    const r = await postDef(`/api/races/${encodeURIComponent(sent.race_id)}/approve`,
      { definition: sent, approved });
    if (r.detail) { paintDetail(); setMsg(r.detail); return; }   // e.g. blocked by validation errors
    // approve persists the definition it was sent (+ the sign-off): that is the new clean baseline
    for (const o of [ed, sent]) {
      o.reviewed = r.reviewed; o.reviewed_at = r.reviewed_at;
      if (!approved) delete o.reviewed_at;
    }
    Lab.editVal = { errors: r.errors || [], warnings: r.warnings || [] };
    Lab.races = null; markSaved(JSON.stringify(sent), ed);
    paintDetail(); refreshRaceList();
    setMsg(approved ? "Approved for racing." : "Approval cleared.");
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
  if (!Lab.sel) { view.innerHTML = '<div class="placeholder">No race yet. Add one in the Races tab.</div>'; return; }
  view.innerHTML = '<div class="loading">Loading course…</div>';
  try { Lab.editDef = await (await apiGet("/api/races/" + encodeURIComponent(Lab.sel))).json(); }
  catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  if (stale("course")) return;
  paintCourse(); markClean();   // after the paint: courseEditCard fills in an empty start
}
/* Render the Course & Marks view from the in-memory Lab.editDef (no server refetch — so edits and
   geocoded coords are preserved). Callers set #courseMsg AFTER calling this. */
function paintCourse() {
  const d = Lab.editDef;
  document.getElementById("view").innerHTML = `<div class="dhead">
      <h2>Course &amp; Marks — ${esc(d.name)}</h2>
      <div class="dmeta">Check each mark. Fill in any marked <span class="need">needs review</span>
        (type lat/lon or tap Find), then Save.</div></div>
    <div id="courseMsg" class="muted" style="font-size:12px;margin-bottom:10px"></div>
    ${(d.courses || []).map((c, i) => courseEditCard(c, i)).join("")}
    <button id="saveCourseBtn" onclick="saveCourse()">Save course</button>
    <button class="mini" onclick="saveAndGameplan()">Save and route →</button>`;
  (d.courses || []).forEach((c, i) => drawCourseMap("map" + i, c));
  syncDirty();
}
function courseEditCard(c, ci) {
  c.start = c.start || {};
  const rows = (c.marks || []).map((m, mi) => {
    const need = m.lat == null || m.coords_source === "needs_review";
    const isGate = m.type === "gate", isIsland = m.type === "island";
    return `<tr class="${need ? "needrow" : ""}">
      <td class="mono">${esc(m.seq)}</td>
      <td><input class="cin" style="width:140px" value="${esc(m.name || "")}"
        onchange="editMark(${ci},${mi},'name',this.value)"></td>
      <td><select onchange="editMark(${ci},${mi},'type',this.value)">
        ${["waypoint", "gate", "island", "buoy"].map((t) =>
          `<option ${m.type === t ? "selected" : ""}>${t}</option>`).join("")}</select></td>
      <td><select onchange="editMark(${ci},${mi},'rounding',this.value)">
        ${["none", "port", "starboard", "gate"].map((r) =>
          `<option ${m.rounding === r ? "selected" : ""}>${r}</option>`).join("")}</select></td>
      <td><div class="cstack"><input class="cin" value="${esc(m.lat)}" placeholder="lat"
        onchange="editMark(${ci},${mi},'lat',this.value)"><input class="cin" value="${esc(m.lon)}"
        placeholder="lon" onchange="editMark(${ci},${mi},'lon',this.value)"></div></td>
      <td>${isGate ? `<div class="cstack"><input class="cin" value="${esc(m.lat2)}" placeholder="lat2"
        onchange="editMark(${ci},${mi},'lat2',this.value)"><input class="cin" value="${esc(m.lon2)}"
        placeholder="lon2" onchange="editMark(${ci},${mi},'lon2',this.value)"></div>` : ""}</td>
      <td>${isIsland ? `<input class="cin" style="width:48px" value="${esc(m.radius_nm)}"
        placeholder="nm" onchange="editMark(${ci},${mi},'radius_nm',this.value)">` : ""}</td>
      <td>${esc(m.coords_source || "")}</td>
      <td>${need ? `<button class="mini" onclick="geocodeMark(${ci},${mi})">Find</button>` : ""}
        <button class="mini" title="remove" onclick="removeMark(${ci},${mi})">✕</button></td>
    </tr>`;
  }).join("");
  return `<div class="card"><h3>${esc(c.name)}</h3>
    <div id="map${ci}" class="coursemap"></div>
    <div style="font-size:13px;margin:8px 0">
      <b>Start</b>
      <input class="cin" value="${esc(c.start.lat)}" placeholder="start lat"
        onchange="editStart(${ci},'lat',this.value)">
      <input class="cin" value="${esc(c.start.lon)}" placeholder="start lon"
        onchange="editStart(${ci},'lon',this.value)">
      <span class="muted">${esc(c.start.coords_source || "")}</span>
    </div>
    <div class="tablewrap"><table class="marktable"><thead><tr><th>#</th><th>Mark</th><th>Type</th><th>Leave</th><th>Lat / Lon</th>
      <th>Gate end 2</th><th>Radius&nbsp;nm</th><th>Source</th><th></th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <button class="mini" onclick="addMark(${ci})">+ Add mark</button>
    ${finishEditor(c, ci)}</div>`;
}
/* The finish: 1 point (a finish mark) or 2 (a line), each name + lat/lon, plus the source — edited
   in place on Lab.editDef like the start. Nothing is written on render: a course with no finish
   only gets one when the user adds a point. */
function finishEditor(c, ci) {
  const f = (c.finish && typeof c.finish === "object" && !Array.isArray(c.finish)) ? c.finish : {};
  const pts = Array.isArray(f.points) ? f.points : [];
  const need = !pts.length || pts.some((p) => !p || p.lat == null || p.lon == null)
    || ["needs_review", "si_pending"].includes(f.coords_source);
  const rows = pts.map((p, k) => `<div class="finrow">
      <span class="mono">${k === 0 ? "Finish" : "Finish (2)"}</span>
      <input class="cin" style="width:160px" value="${esc((p && p.name) || "")}" placeholder="point name"
        onchange="editFinish(${ci},${k},'name',this.value)">
      <input class="cin" value="${esc(p && p.lat)}" placeholder="lat"
        onchange="editFinish(${ci},${k},'lat',this.value)">
      <input class="cin" value="${esc(p && p.lon)}" placeholder="lon"
        onchange="editFinish(${ci},${k},'lon',this.value)">
      <button class="mini" title="remove finish point" onclick="removeFinishPoint(${ci},${k})">✕</button>
    </div>`).join("");
  return `<div class="finedit${need ? " needrow" : ""}">
    <div><b>Finish</b> <span class="muted">${pts.length === 2 ? "line between two points"
      : pts.length === 1 ? "single finish mark" : "not set"}${f.crossing ? " · cross " + esc(f.crossing) : ""}</span>
      ${pts.length ? `<label class="muted" style="margin-left:8px">source
        <select onchange="editFinishSource(${ci},this.value)">${[...new Set(["si", "approx", "needs_review",
          f.coords_source || "needs_review"])].map((v) =>
          `<option ${(f.coords_source || "needs_review") === v ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>` : ""}</div>
    ${rows}
    ${pts.length < 2 ? `<button class="mini" onclick="addFinishPoint(${ci})">+ Add finish point</button>` : ""}
  </div>`;
}
function finishPoints(ci) {
  const c = Lab.editDef.courses[ci];
  if (!c.finish || typeof c.finish !== "object" || Array.isArray(c.finish)) c.finish = { type: "line", points: [], coords_source: "needs_review" };
  if (!Array.isArray(c.finish.points)) c.finish.points = [];
  return c.finish;
}
function finishSourceAfterEdit(f) {
  const ok = f.points.length && f.points.every((p) => p && p.lat != null && p.lon != null);
  if (ok && (!f.coords_source || ["si_pending", "needs_review"].includes(f.coords_source)))
    f.coords_source = "approx";
}
function editFinish(ci, k, field, val) {
  const f = finishPoints(ci), cur = f.points[k];
  const p = f.points[k] = (cur && typeof cur === "object" && !Array.isArray(cur)) ? cur : {};
  if (field === "name") p.name = val;
  else { const n = parseFloat(val); p[field] = isNaN(n) ? null : n; finishSourceAfterEdit(f); }
  paintCourse();
}
function editFinishSource(ci, val) {
  finishPoints(ci).coords_source = val;
  paintCourse();
}
function addFinishPoint(ci) {
  const f = finishPoints(ci);
  if (f.points.length >= 2) return;
  f.points.push({ name: f.points.length ? "Finish line end 2" : "Finish", lat: null, lon: null });
  if (f.points.length === 2 && f.type === "mark") f.type = "line";
  paintCourse();
  document.getElementById("courseMsg").textContent = "Finish point added. Set its lat/lon, then Save.";
}
function removeFinishPoint(ci, k) {
  const f = finishPoints(ci);
  f.points.splice(k, 1);
  paintCourse();
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
  document.getElementById("courseMsg").textContent = "Mark added. Set its name and lat/lon, then Save.";
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
  if (!(await saveCourse())) return;
  if (window.Opt) { Opt.raceId = Lab.sel; Opt.def = Lab.editDef; Opt.courseId = null; Opt.result = null; }
  location.hash = "#gameplan";
}
async function geocodeMark(ci, mi) {
  const m = Lab.editDef.courses[ci].marks[mi];
  const msg = document.getElementById("courseMsg");
  msg.textContent = `Finding "${m.name}"…`;
  try {
    // Query the place name alone — appending the body of water (e.g. "Lake Huron") makes Nominatim
    // miss. The human verifies the proposed hit on the map (the display_name is shown) before saving.
    const r = await (await apiPost("/api/geocode", { q: m.name })).json();
    const hit = (r.results || [])[0];
    if (!hit) { msg.textContent = `Couldn't find "${m.name}". Type the lat/lon.`; return; }
    m.lat = hit.lat; m.lon = hit.lon; m.coords_source = "approx";
    paintCourse();   // re-render from Lab.editDef (preserves the edit) — then set the message
    document.getElementById("courseMsg").textContent =
      `Found "${m.name}" at ${hit.lat}, ${hit.lon} (${hit.display_name}). Check it on the map, then Save.`;
  } catch (e) { msg.textContent = "Search failed."; }
}
async function saveCourse() {
  const btn = document.getElementById("saveCourseBtn");
  const msg = document.getElementById("courseMsg");
  btn.disabled = true; msg.textContent = "Saving…";
  let ok = false;
  // mark the review touched in provenance
  Lab.editDef.provenance = Lab.editDef.provenance || {};
  Lab.editDef.provenance.review_status =
    "Checked in Course & Marks. Verify before racing.";
  const sent = JSON.stringify(Lab.editDef), ed = Lab.editDef;
  try {
    const r = await postDef("/api/races", { definition: JSON.parse(sent) });
    Lab.races = null; ok = !!r.saved;
    if (ok) markSaved(sent, ed);
    msg.textContent = r.saved ? `Saved. ${(r.warnings || []).length} still to check.`
      : ("Save failed: " + (r.detail || ""));
  } catch (e) { msg.textContent = "Save failed."; }
  btn.disabled = false;
  return ok;
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
  const pts = [], pins = [];
  const pin = (lat, lon, label, color, prio = 2) => {
    if (lat == null || lon == null) return;
    const mk = L.circleMarker([lat, lon], { radius: 6, color: "#0b0b0e", weight: 1, fillColor: color, fillOpacity: 0.95 })
      .bindTooltip(label, { permanent: true, direction: "right", className: "cm-pin" }).addTo(map);
    // a label hidden by the declutter comes back while the pin is hovered/tapped
    mk.on("mouseover click", () => { const t = mk.getTooltip(); t && t.getElement() && t.getElement().classList.add("cm-force"); });
    mk.on("mouseout", () => { const t = mk.getTooltip(); t && t.getElement() && t.getElement().classList.remove("cm-force"); });
    mk._cmPrio = prio; pins.push(mk); pts.push([lat, lon]);
  };
  if (course.start && course.start.lat != null) pin(course.start.lat, course.start.lon, "Start", "#2ecc71", 0);
  (course.marks || []).forEach((m) => {
    if (m.type === "island" && m.lat != null) {
      L.circle([m.lat, m.lon], { radius: (m.radius_nm || 0.5) * 1852, color: "#ff8a5c", weight: 1,
        fillColor: "#ff8a5c", fillOpacity: 0.16 }).addTo(map);
      pin(m.lat, m.lon, m.name + " · leave " + (m.rounding && m.rounding !== "none" ? m.rounding : "either"), "#ff8a5c");
    } else if (m.type === "gate" && m.lat != null && m.lat2 != null) {
      L.polyline([[m.lat, m.lon], [m.lat2, m.lon2]], { color: "#f5b13d", weight: 3 }).addTo(map);
      pin(m.lat, m.lon, m.name, "#f5b13d"); pin(m.lat2, m.lon2, m.name + " (NE)", "#f5b13d", 3);
    } else if (m.lat != null) {
      pin(m.lat, m.lon, m.name + (m.rounding && m.rounding !== "none" ? " · " + m.rounding : ""), "#2f9bff");
    }
  });
  const fpRaw = (course.finish || {}).points;
  const fp = (Array.isArray(fpRaw) ? fpRaw : []).filter((p) => p && p.lat != null && p.lon != null);
  if (fp.length === 2) L.polyline([[fp[0].lat, fp[0].lon], [fp[1].lat, fp[1].lon]], { color: "#f5c451", weight: 3 }).addTo(map);
  fp.forEach((p, k) => pin(p.lat, p.lon, k === 0 ? "Finish" : "Finish (2)", "#f5c451", k === 0 ? 1 : 3));
  // Declutter: in priority order (start, finish, marks in course order, 2nd finish/gate points) each
  // label goes right of its pin, else left, else is hidden until hovered/tapped. Re-run after every
  // zoom/pan so zooming in brings labels back.
  const declutter = () => {
    const kept = [];
    const hits = (r) => kept.some((k) => r.left < k.right + 2 && r.right > k.left - 2 && r.top < k.bottom + 2 && r.bottom > k.top - 2);
    pins.slice().sort((a, b) => a._cmPrio - b._cmPrio).forEach((mk) => {
      const t = mk.getTooltip(), el = t && t.getElement();
      if (!el) return;
      el.classList.remove("cm-hidden");
      let placed = null;
      for (const dir of ["right", "left"]) {
        if (t.options.direction !== dir) { t.options.direction = dir; t.update(); }
        const r = el.getBoundingClientRect();
        if (!r.width) return;
        if (!hits(r)) { placed = r; break; }
      }
      if (placed) kept.push(placed);
      else { t.options.direction = "right"; t.update(); el.classList.add("cm-hidden"); }
    });
  };
  map.on("zoomend moveend resize", declutter);
  if (pts.length) map.fitBounds(pts, { padding: [42, 42], maxZoom: 12 });
  else { map.setView([44.5, -82.5], 6); L.popup().setLatLng([44.5, -82.5]).setContent("No positions yet. Fill in the marks below.").openOn(map); }
  setTimeout(() => { try { map.invalidateSize(); declutter(); } catch (e) {} }, 80);   // settle tiles after the innerHTML layout
}

/* ---------- placeholders (all sections are now built) ---------- */

/* ---------- Gameplan / Optimizer (Lab-1) ---------- */
const Opt = { races: null, models: null, raceId: null, def: null, courseId: null,
  chosen: null, running: false, result: null, resolution: "auto", start: "", startDefault: null };

// What a BLANK Start routes from (#92) — the server decides (app/racestart.py): our division's gun,
// else the earliest gun; "now" only when there's no gun time or it has passed. Said in plain text
// under the field, not a tooltip.
async function optLoadStartDefault() {
  Opt.startDefault = null;
  if (!Opt.raceId) return;
  try {
    const r = await apiGet("/api/races/" + encodeURIComponent(Opt.raceId) + "/start");
    if (r.ok) Opt.startDefault = await r.json();
  } catch (e) { /* label falls back to the generic line */ }
  // re-word the note the moment the gun passes, if the screen is still open then
  clearTimeout(Opt.startTimer);
  const sd = Opt.startDefault, ms = sd && sd.race_start ? sd.race_start.epoch * 1000 - Date.now() : -1;
  if (sd && sd.source === "race_start" && ms > 0 && ms < 2 ** 31 - 1)
    Opt.startTimer = setTimeout(() => optStartInput(Opt.start), ms + 1000);
}
function optStartNote() {
  const sd = Opt.startDefault ? Object.assign({}, Opt.startDefault) : null, tz = optTz();
  // the screen can sit open past the gun — the server then routes from now, so say that
  if (sd && sd.source === "race_start" && sd.race_start.epoch <= Date.now() / 1000) sd.source = "now_passed";
  if ((Opt.start || "").trim())
    return "Routes from the time you picked. Clear it to route from "
      + (!sd || sd.source === "race_start" ? "the race start." : "now.");
  const when = (e) => epochToWall(e, tz || "UTC").replace("T", " ") + (tz ? " race local" : " UTC");
  if (!sd) return "Blank routes from the race start if a gun time is set, otherwise from now.";
  const rs = sd.race_start;
  if (sd.source === "race_start")
    return `Blank = race start: ${rs.division} gun, ${when(rs.epoch)}.`;
  if (sd.source === "now_passed")
    return `Blank = now. The ${rs.division} start (${when(rs.epoch)}) has passed.`;
  return "Blank = now. No gun time is set for this race. Add it on the Fleet tab.";
}
function optStartInput(v) {
  Opt.start = v;
  const n = document.getElementById("optStartNote"); if (n) n.textContent = optStartNote();
}

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
    } catch (e) { view.innerHTML = '<div class="placeholder">Couldn\'t load Gameplan.</div>'; return; }
  }
  if (!Opt.raceId && Opt.races.length) await optPickRace(Opt.races[0].race_id, false);
  await optLoadStartDefault();
  if (stale("gameplan")) return;
  const startTz = optTz();
  const startTitle = (startTz
    ? "Start time, race local (" + startTz + ")."
    : "Start time (UTC).") + " Leave blank to use the race start.";
  view.innerHTML = `<div class="opt">
    <div class="card">
      <h3>Gameplan <span class="muted" style="font-weight:400">— best route on the forecast</span></h3>
      <div class="opt-groups">
        <div class="opt-group">
          <div class="opt-group-h">Course</div>
          <label>Race
            <select id="optRace" onchange="optPickRace(this.value)">
              ${Opt.races.map((r) => `<option value="${esc(r.race_id)}" ${r.race_id === Opt.raceId ? "selected" : ""}>${esc(r.name)}</option>`).join("")}
            </select></label>
          <label>Course <select id="optCourse" onchange="Opt.courseId=this.value">${optCourseOpts()}</select></label>
          <label>Start (${startTz ? "race local" : "UTC"}) <input type="datetime-local" id="optStart"
            title="${esc(startTitle)}" value="${esc(Opt.start || "")}" oninput="optStartInput(this.value)"></label>
          <span id="optStartNote" class="opt-start-note">${esc(optStartNote())}</span>
        </div>
        <div class="opt-group">
          <div class="opt-group-h">Boat &amp; charts</div>
          ${optBoatControls()}
          <button class="mini" onclick="toggleBoatModel()">${Opt.showBoatModel ? "Hide" : "Show"} boat speeds &amp; sails</button>
        </div>
        <div class="opt-group">
          <div class="opt-group-h">Weather models</div>
          <div class="opt-models">${optModelChecks()}</div>
          <label>Ensemble runs <input type="number" id="optEns" value="0" min="0" style="width:64px" oninput="updateEnsembleControl()"></label>
          <span id="optEnsHint" class="muted" style="font-size:12px"></span>
          <div id="optEnsCost" class="muted" style="font-size:12px;margin-top:2px"></div>
        </div>
      </div>
      <div id="boatModelOut"></div>
      <div class="opt-run">
        <label class="optchk"><input type="checkbox" id="optAvoid" checked> Avoid land &amp; zones</label>
        <label class="optchk" title="Slows the boat in waves. Uncheck for flat water."><input type="checkbox" id="optWaves" checked> Waves</label>
        <label class="optchk" title="Includes water current. Run once without it to see what it's worth."><input type="checkbox" id="optCurrent" checked> Current</label>
        <label class="optchk" title="Also routes each weather model on its own and draws them all."><input type="checkbox" id="optPerModel"> Route per model <span class="muted">(slower)</span></label>
        <label class="optchk" title="Fine is sharper near shore but slower. Fast is quicker.">Detail
          <select id="optRes" oninput="updateResHint()">
            <option value="fast"${Opt.resolution === "fast" ? " selected" : ""}>Fast</option>
            <option value="auto"${(Opt.resolution || "auto") === "auto" ? " selected" : ""}>Auto</option>
            <option value="fine"${Opt.resolution === "fine" ? " selected" : ""}>Fine</option>
          </select></label>
        <button id="optRun" onclick="runOptimize()" ${Opt.running ? "disabled" : ""}>${Opt.running ? "Routing…" : "Find route →"}</button>
        <span id="optResHint" class="muted" style="font-size:11px"></span>
      </div>
      <div class="muted" style="font-size:12px;margin-top:6px">Routes the course on live weather. First run about 1 min.
        ${qhelp("Downloads forecasts from NOAA and ECMWF and sails them on the SR33 polar. Later runs reuse the download. Prep only: nothing changes after the start (rule 41).")}</div>
    </div>
    <div id="optOut"></div>
    <div class="card">
      <h3>Playbook <span class="muted" style="font-weight:400">— which side, and when to switch</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:8px">Routes many forecasts. Says which side to start and what would change it.
        ${qhelp("Groups the routes by which side of the first beat they favour. Claude writes the reasons, the cost and what would change the call, plus a decision tree. Freeze it to take it on the boat.")}</div>
      <label class="muted" style="font-size:12px" title="More scenarios cover more weather but take longer.">Depth
        <select id="pbFan" style="margin:0 8px 0 4px">
          <option value="quick">Quick: 6 scenarios, ~5 min (race morning)</option>
          <option value="standard">Standard: 9 scenarios, ~7 min</option>
          <option value="deep" selected>Deep: 15 scenarios, ~15 min (early in the week)</option>
        </select></label>
      <button id="pbRun" onclick="synthPlaybook()" ${Pb.running ? "disabled" : ""}>${Pb.running ? "Building…" : "Build playbook →"}</button>
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
    const ens = m[k].kind === "ensemble" ? ` <span class="muted">(${m[k].members} runs)</span>` : "";
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
  fast: "Fast: quickest, less sharp near shore.",
  auto: "Auto: balanced. The default.",
  fine: "Fine: sharpest near shore and tight marks. Slower.",
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
    ? `0 = main forecast only. Up to ${max} runs of ${names.join(" + ")}.`
    : "Tick GEFS or ECMWF-ENS to use this.";
  if (cost) {
    const n = parseInt(inp.value || "0", 10) || 0;
    cost.textContent = on && n > 0
      ? `${n} extra forecast${n > 1 ? "s" : ""} to download. 10–20 is plenty.`
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
    <label title="How much of the polar speed this crew sails. 100% = full polar.">Helm % <input type="number" id="optHelm" step="1" min="30" max="100" value="${Math.round((ab.helm_factor ?? 1) * 100)}" style="width:58px" onchange="optSaveHelm()"></label>
    <label>Charts
      <select id="optChart" onchange="optSetChart(this.value)">
        <option value="natural_earth" ${!isEnc ? "selected" : ""}>Basic coastline</option>
        <option value="enc" ${isEnc ? "selected" : ""}>NOAA charts (uses draft)</option>
      </select></label>
    <span class="muted">${isEnc ? "avoids water under " + depthFt + " ft" : "coastline only, no depths"}</span>`;
}
async function optPickBoat(id) {
  // the boat-model thresholds save to the ACTIVE boat — settle unsaved ones before it changes
  if (!(await confirmLeave())) return renderGameplan();   // Stay → repaint puts the picker back
  Opt.activeBoat = id;
  await apiPost("/api/boats/active", { boat_id: id });
  await reloadBoats();
  if (Opt.boatModel) {   // the card shows the new boat's model, not the old one's
    try { bmLoaded(await (await apiGet("/api/crossovers")).json()); } catch (e) { bmLoaded({ error: String(e) }); }
  }
  renderGameplan();
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
      bmLoaded(await (await apiGet("/api/crossovers")).json());
      Opt.polarGrid = await (await apiGet("/api/polars")).json();
    } catch (e) { bmLoaded({ error: String(e) }); }
  }
  renderGameplan();
}

function renderBoatModel() {
  const out = document.getElementById("boatModelOut"); if (!out) return;
  const m = Opt.boatModel;
  if (!m) { out.innerHTML = '<div class="loading">Loading boat model…</div>'; return; }
  if (m.error || !m.crossovers) { out.innerHTML = `<div class="placeholder">Boat model not available: ${esc(m.error || "no sail data")}</div>`; return; }
  out.innerHTML = boatModelCard(m);
}

// Sail palette (mirrors the .sail-bg-* CSS) — used to hatch the toss-up overlap bands in two colors.
const SAIL_COLORS = { J1: "#36b3ff", J2: "#66a9e0", J3: "#9b8cff", C0: "#4dd6c9", A3: "#f5c451", S1: "#ff8042" };
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
    // erased on a near-tie reappears in its own colour.
    const overlaps = ((m.overlaps || {})[String(t)] || []).map((o) => {
      const left = (o.twa_min / 180 * 100).toFixed(1);
      const w = ((o.twa_max - o.twa_min) / 180 * 100).toFixed(1);
      const c1 = hexRgba(sailColor(o.sails[0]), 0.72), c2 = hexRgba(sailColor(o.sails[1]), 0.72);
      const bg = `repeating-linear-gradient(45deg, ${c1} 0 4px, transparent 4px 8px, ${c2} 8px 12px, transparent 12px 16px)`;
      return `<div class="xo-ol" style="left:${left}%;width:${w}%;background:${bg}" title="Toss-up: ${esc(o.sails.join(" ≈ "))} at ${o.twa_min}–${o.twa_max}°. Either sail works.">≈</div>`;
    }).join("");
    return `<div class="xo-row"><span class="xo-tws">${t} kts</span><div class="xo-track">${zones}${overlaps}</div></div>`;
  }).join("");
  const inv = (m.inventory || []).map((s) => `<span class="sail sail-${esc(s)}">${esc(s)}</span>`).join(" ");
  return `<div class="card boatmodel">
    <h3>Boat model — speeds &amp; sail choice</h3>
    <div class="muted" style="font-size:12px;margin-bottom:10px">The route and the boat's sail calls use these. Check before you lock in. Source: ${esc(m.source || "—")}.</div>
    <div class="bm-inv">Sails: ${inv}</div>
    ${renderJibCrossovers(m)}
    ${renderSailConfig(m)}
    <div class="jib-edit bm-save">
      <button class="mini" onclick="bmSaveClick()">Save sail settings</button>
      <span id="bmSaveMsg" class="muted" style="font-size:11px">Saves jibs, Code 0 and reefs together.</span>
    </div>
    <h4>Best sail by wind angle and speed</h4>
    <div class="muted" style="font-size:11px;margin-bottom:4px">From the ORC certificate. <b>Hatched ≈</b> = toss-up, either sail works.
      ${qhelp("The certificate rates one jib. The J1/J2/J3 split comes from your wind settings above. Hatched means two sails are within 1.5% of each other.")}</div>
    <div class="xo-axis"><span>0°</span><span>45°</span><span>90°</span><span>135°</span><span>180°</span></div>
    <div class="xo">${bands}</div>
    ${renderPolarGrid()}
  </div>`;
}

/* Upwind jib change-downs by TWS (J1/J2/J3) — NOT in the ORC cert (it rates one headsail), so
   these are editable crew/sailmaker thresholds. Two boundaries: J1→J2 and J2→J3. */
function renderJibCrossovers(m) {
  const jc = m.jib_crossovers || [];
  if (!jc.length) return `<div class="muted" style="font-size:12px">No jib changes set. The route uses the J1 only.</div>`;
  const max = 35;     // axis ceiling for the TWS bars (kn)
  const bars = jc.map((b) => {
    const lo = b.tws_min != null ? b.tws_min : 0;
    const hi = b.tws_max != null ? b.tws_max : max;
    const left = (lo / max * 100).toFixed(1), w = ((hi - lo) / max * 100).toFixed(1);
    const rng = b.tws_min == null ? `<${b.tws_max}` : b.tws_max == null ? `${b.tws_min}+` : `${b.tws_min}–${b.tws_max}`;
    return `<div class="xo-zone sail-bg-${esc(b.sail)}" style="left:${left}%;width:${w}%" title="${esc(b.sail)} ${rng} kts">${esc(b.sail)} ${esc(rng)}</div>`;
  }).join("");
  const f = Opt.bmEdit || bmFormFrom(m);   // the inputs show the form (edits survive a repaint)
  return `<h4>Upwind jib changes by wind speed</h4>
    <div class="muted" style="font-size:11px;margin-bottom:6px">Your crew's change points. They set the jib on each upwind leg.</div>
    <div class="xo-axis"><span>0</span><span>~9</span><span>~17</span><span>~26</span><span>35 kts</span></div>
    <div class="xo"><div class="xo-row"><span class="xo-tws">TWS</span><div class="xo-track">${bars}</div></div></div>
    <div class="jib-edit">
      <label>J1→J2 at <input type="number" id="jibT1" value="${attr(f.t1)}" min="2" max="34" step="0.5" style="width:58px" oninput="bmSet('t1',this.value)"> kts</label>
      <label>J2→J3 at <input type="number" id="jibT2" value="${attr(f.t2)}" min="2" max="34" step="0.5" style="width:58px" oninput="bmSet('t2',this.value)"> kts</label>
    </div>`;
}

/* Code 0 band + mainsail reef points — crew sail-config overlays the ORC cert can't express (it
   rates neither). Editable here (Learnings + Gameplan share this card); they set the sail CALLS
   across routing legs, the crossover chart, the frozen bundle boat_model and the copilot digest —
   routing SPEED stays the rated envelope (no invented numbers). */
function renderSailConfig(m) {
  const f = Opt.bmEdit || bmFormFrom(m);
  return `<h4>Code 0 — light-air reaching</h4>
    <div class="muted" style="font-size:11px;margin-bottom:6px">When to fly the Code 0. Sets the sail call, not the boat speed.
      ${qhelp("The certificate doesn't rate the Code 0, so route speed stays the jib speed. The band shows as C0 in the chart below, in the leg sail calls and in the playbook.")}</div>
    <div class="jib-edit">
      <label><input type="checkbox" id="c0On" ${f.c0On ? "checked" : ""} onchange="bmSet('c0On',this.checked)"> fly the Code 0</label>
      <label>under <input type="number" id="c0Tws" value="${attr(f.c0Tws)}" min="3" max="20" step="0.5" style="width:58px" oninput="bmSet('c0Tws',this.value)"> kts</label>
      <label>TWA <input type="number" id="c0Alo" value="${attr(f.c0Alo)}" min="30" max="150" step="5" style="width:58px" oninput="bmSet('c0Alo',this.value)">–<input type="number" id="c0Ahi" value="${attr(f.c0Ahi)}" min="40" max="170" step="5" style="width:58px" oninput="bmSet('c0Ahi',this.value)">°</label>
    </div>
    <h4>Mainsail reef points</h4>
    <div class="muted" style="font-size:11px;margin-bottom:6px">Reef 1 to depower. A lower setting opens the slot when flying the A3.</div>
    <div class="jib-edit">
      <label>Reef 1 (depower) at <input type="number" id="r1Tws" value="${attr(f.r1)}" min="4" max="45" step="0.5" style="width:58px" oninput="bmSet('r1',this.value)"> kts</label>
      <label>Reef 1 with the A3 at <input type="number" id="r1Slot" value="${attr(f.slot)}" min="4" max="45" step="0.5" style="width:58px" oninput="bmSet('slot',this.value)"> kts</label>
    </div>`;
}

/* The boat-model thresholds form — jib change-downs + Code 0 band + reef points — is ONE form with
   ONE save, and one source in the Lab's unsaved-edits mechanism (DIRTY_SOURCES). Its inputs bind to
   Opt.bmEdit as you type; Opt.bmClean is the form as the server has it. Saving posts only the
   section(s) that changed, so an edit in one section is never reverted by saving the other, and a
   repaint redraws the inputs from Opt.bmEdit. Lives on Learnings and the Gameplan review panel. */
const BM_JIB = ["t1", "t2"], BM_SC = ["c0On", "c0Tws", "c0Alo", "c0Ahi", "r1", "slot"];
const bmStr = (v) => (v == null || v === "" ? "" : isNaN(Number(v)) ? String(v) : String(Number(v)));
function bmFormFrom(m) {
  const jc = (m && m.jib_crossovers) || [], c0 = (m && m.code0) || {}, mr = (m && m.main_reefs) || {};
  return { t1: bmStr(jc[0] && jc[0].tws_max), t2: bmStr(jc[1] && jc[1].tws_max),
    c0On: c0.enabled !== false && c0.tws_max != null, c0Tws: bmStr(c0.tws_max ?? 9),
    c0Alo: bmStr(c0.twa_min ?? 55), c0Ahi: bmStr(c0.twa_max ?? 110),
    r1: bmStr(mr.r1_tws_kn), slot: bmStr(mr.r1_a3_slot_tws_kn) };
}
// A fresh /api/crossovers: the new baseline. Unsaved edits (if any) are kept on top of it — but only
// for the same boat; another boat's model starts clean (its thresholds are not this boat's edits).
function bmLoaded(m) {
  const keep = bmDirty() && !!m && !!Opt.boatModel && m.boat_id === Opt.boatModel.boat_id;
  Opt.boatModel = m;
  const f = m && m.crossovers ? bmFormFrom(m) : null;
  Opt.bmClean = f ? JSON.stringify(f) : null;
  if (!keep) Opt.bmEdit = f;
}
function bmSet(k, v) { if (Opt.bmEdit) Opt.bmEdit[k] = typeof v === "boolean" ? v : bmStr(v); }
function bmDirty() { return !!(Opt.bmEdit && Opt.bmClean) && JSON.stringify(Opt.bmEdit) !== Opt.bmClean; }
function bmDiscard() { Opt.bmEdit = Opt.bmClean ? JSON.parse(Opt.bmClean) : null; repaintBoatModel(); }
function repaintBoatModel() {
  const el = document.querySelector(".card.boatmodel");
  if (el && Opt.boatModel && Opt.boatModel.crossovers) el.outerHTML = boatModelCard(Opt.boatModel);
  syncDirty();
}
function bmMsg(t, bad) {
  const m = document.getElementById("bmSaveMsg");
  if (m) { m.textContent = t; m.style.color = bad ? "var(--warn)" : ""; }
}
async function bmSaveClick() {
  if (!bmDirty()) { bmMsg("Nothing to save."); return; }
  if (await saveBoatModel()) flashSavebar("Saved ✓");
}
// The request bodies for the sections of form f that differ from baseline `was`, validated first so
// a bad value can't leave a half-save. → {jib, sc} (null = section unchanged) or {err}.
function bmBodies(f, was) {
  const changed = (keys) => keys.some((k) => f[k] !== was[k]);
  let jib = null, sc = null;
  if (changed(BM_JIB)) {
    const t1 = parseFloat(f.t1), t2 = parseFloat(f.t2);
    if (isNaN(t1) || isNaN(t2) || t1 <= 0 || t1 >= t2) return { err: "J1→J2 must be below J2→J3." };
    jib = { jib_crossovers: [{ sail: "J1", tws_max: t1 }, { sail: "J2", tws_min: t1, tws_max: t2 }, { sail: "J3", tws_min: t2 }] };
  }
  if (changed(BM_SC)) {
    const tws = parseFloat(f.c0Tws), alo = parseFloat(f.c0Alo), ahi = parseFloat(f.c0Ahi);
    // the same bounds /api/boats/sail-config enforces, checked before ANY post
    if (f.c0On && (isNaN(tws) || isNaN(alo) || isNaN(ahi) || alo >= ahi)) return { err: "Code 0: TWA min must be below max." };
    if (f.c0On && !(tws > 0 && tws <= 30 && alo >= 0 && ahi <= 180)) return { err: "Code 0: wind up to 30 kts, TWA within 0–180°." };
    const r1 = parseFloat(f.r1), slot = parseFloat(f.slot);
    if ([r1, slot].some((v) => !isNaN(v) && (v < 4 || v > 45))) return { err: "Reef points must be 4–45 kts (or blank)." };
    if (!isNaN(r1) && !isNaN(slot) && slot > r1) return { err: "The A3 reef should be at or below the depower reef." };
    sc = { code0: f.c0On ? { enabled: true, tws_max: tws, twa_min: alo, twa_max: ahi } : { enabled: false },
      main_reefs: { r1_tws_kn: isNaN(r1) ? null : r1, r1_a3_slot_tws_kn: isNaN(slot) ? null : slot } };
  }
  return { jib, sc };
}
// → true when saved (or nothing to save); false = a value is invalid or a post failed (message shown).
async function saveBoatModel() {
  if (!bmDirty()) return true;
  const f = JSON.parse(JSON.stringify(Opt.bmEdit)), sent = JSON.stringify(f);
  const pre = bmBodies(f, JSON.parse(Opt.bmClean));
  if (pre.err) { bmMsg(pre.err, true); return false; }
  bmMsg("Saving…");
  // The whole save — posts, the refetch and the new baseline — is ONE queued step, so leaving the tab
  // or Discard (both wait out the queue) can't run between the save and its baseline update.
  const ok = await queueDef(async () => {
    // diff against the baseline as it is NOW: an earlier save in the queue may have moved it
    const was = JSON.parse(Opt.bmClean), { jib, sc, err } = bmBodies(f, was);
    if (err) { bmMsg(err, true); return false; }
    const post = async (path, body) => {
      const r = await (await apiPost(path, body)).json();
      if (!r.saved) throw new Error(r.detail || "not saved");
      return r;
    };
    const clean = Object.assign({}, was);   // what the server holds so far
    try {
      if (jib) {
        await post("/api/boats/jib-crossovers", jib);
        BM_JIB.forEach((k) => { clean[k] = f[k]; });
      }
      if (sc) {
        await post("/api/boats/sail-config", sc);
        BM_SC.forEach((k) => { clean[k] = f[k]; });
      }
    } catch (e) {
      Opt.bmClean = JSON.stringify(clean);   // a section that did save is no longer dirty
      syncDirty(); bmMsg("Save failed: " + e.message, true);
      return false;
    }
    let fresh = null;
    try { fresh = await (await apiGet("/api/crossovers")).json(); } catch (e) {}
    if (fresh && fresh.crossovers) {
      Opt.boatModel = fresh;   // the bars + chart redraw from what the server now has
      if (JSON.stringify(Opt.bmEdit) === sent) { Opt.bmEdit = bmFormFrom(fresh); Opt.bmClean = JSON.stringify(Opt.bmEdit); }
      else Opt.bmClean = sent;   // edited again while the save ran → those edits stay unsaved
    } else Opt.bmClean = sent;
    return true;
  }).catch(() => { bmMsg("Save failed.", true); return false; });
  if (!ok) return false;
  repaintBoatModel();
  bmMsg("Saved.");
  return true;
}

function renderPolarGrid() {
  const g = Opt.polarGrid; if (!g || !g.grid) return "";
  const tws = g.tws_buckets, twa = g.twa_buckets;
  const head = `<tr><th>TWA \\ TWS</th>${tws.map((t) => `<th>${t}</th>`).join("")}</tr>`;
  const rows = twa.map((a) => `<tr><td class="pg-twa">${a}°</td>${tws.map((t) => {
    const v = g.grid[String(t)][String(a)];
    return `<td>${v == null ? "" : v.toFixed(1)}</td>`;
  }).join("")}</tr>`).join("");
  return `<h4>Target boat speed (kn) by wind speed and angle</h4>
    <div class="pg-wrap"><table class="pg">${head}${rows}</table></div>
    <div class="muted" style="font-size:11px;margin-top:4px">${g.n_points} points. The ORC speeds the route uses.</div>`;
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
  document.getElementById("optRun").textContent = "Routing… (getting weather)";
  out.innerHTML = '<div class="card"><div class="loading">Getting the weather and routing the course…</div>' +
    '<div class="muted" id="optProg" style="font-size:12px;margin-top:6px"></div></div>';
  try {
    // background job — a slow/rate-limited weather source used to push the synchronous request
    // past the gateway's ~300 s cap (the old "timed out at the gateway" failure). Start + poll,
    // like the synthesis fan; the status's progress tail shows which source it's waiting on.
    const start = await (await apiPost("/api/optimize", body)).json();
    if (start.detail) throw new Error(start.detail);
    if (start.ok === false) throw new Error(start.note || "a route is already running");
    let r;
    while (true) {
      await new Promise((res) => setTimeout(res, 3000));
      const st = await (await apiGet("/api/optimize/status")).json();
      if (st.state === "done") { r = st.result; break; }
      if (st.state === "error") throw new Error(st.error || "routing failed");
      if (st.state === "idle") throw new Error("the route job stopped. Run it again.");
      const p = document.getElementById("optProg");
      if (p && st.progress && st.progress.length) p.textContent = st.progress[st.progress.length - 1];
    }
    Opt.result = r; Opt.running = false;
    renderGameplan();
  } catch (e) {
    Opt.running = false;
    out.innerHTML = '<div class="card"><div class="placeholder">Routing failed: ' + esc(String(e)) + '</div></div>';
    const b = document.getElementById("optRun"); if (b) { b.disabled = false; b.textContent = "Find route →"; }
  }
}

function renderOptResult(r) {
  const out = document.getElementById("optOut"); if (!out) return;
  if (!r.available) {
    out.innerHTML = `<div class="card"><div class="placeholder">No route: ${esc(r.note || "unavailable")}</div>
      ${r.start && r.start.note ? `<div class="opt-start-note">${esc(r.start.note)}</div>` : ""}
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
        ${r.total_peels != null ? `<div title="Sail changes on the route. Skips a change when it doesn't pay."><b>${r.total_peels}</b><span>sail changes</span></div>` : ""}
        <div><b class="conf ${confCls}">${conf == null ? "—" : conf}</b><span>confidence (low ${r.min_confidence == null ? "—" : r.min_confidence})</span></div>
        <div><b class="conf ${covCls}">${cov == null ? "—" : Math.round(cov * 100) + "%"}</b><span>wind data</span></div>
        ${optCurrentStat(r)}
        ${optRealizedStat(r)}
      </div>
      <div class="rail-actions"><button id="pdfBtn" onclick="downloadGameplanPdf(this)" title="PDF of the route, legs, briefing and playbook, for the crew.">⬇ PDF report</button>
        <button id="shareBtn" onclick="shareGameplan(this)" title="A view-only link to the route player. No login needed.">🔗 Share player</button></div>
      ${r.start && r.start.note ? `<div class="opt-start-note">${esc(r.start.note)}</div>` : ""}
      ${optDegradedBanner(r)}
      ${r.timed_out ? '<div class="pill warn">Routing ran out of time. The route may be incomplete.</div>' : ""}
      <details class="rail-sec" open><summary>Legs</summary>
        <div class="legs-head"><span class="muted" style="font-size:11px">Tap a leg to show it on the map.</span>
          <button class="mini" onclick="exportLegsCsv()" title="Leg table as a spreadsheet file.">⬇ CSV</button></div>
        ${optSailPlan(r)}
        <table class="legs"><thead><tr><th>To</th><th>Min</th><th>Point of sail</th><th>Sail</th><th>Tacks</th><th>TWS</th><th>TWD</th><th>Conf</th></tr></thead>
        <tbody>${r.legs.map((l, i) => optLegRow(l, i)).join("")}</tbody></table></details>
      <details class="rail-sec" open><summary>Briefing</summary><pre class="briefing">${esc(r.briefing || "")}</pre></details>
      <details class="rail-sec"><summary>Weather data &amp; land</summary>
        ${optObstacleNote(r)}
        ${(r.skipped_marks || []).length ? `<div class="muted" style="font-size:12px;margin-top:6px">Marks skipped (no position, fix in Course &amp; Marks): ${r.skipped_marks.map(esc).join(", ")}</div>` : ""}
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
    <td>${l.sail ? `<span class="sail sail-${esc(l.sail)}">${esc(l.sail)}</span>` : "—"}${l.reef ? ` <span class="peelbadge" title="Main: ${esc(l.reef)}. ${esc(l.reef_why || "")}">▽${esc(l.reef)}</span>` : ""}${l.peels > 0 ? ` <span class="peelbadge" title="${l.peels} sail change(s) on this leg">⛵${l.peels}</span>` : ""}</td>
    <td>${l.tacks > 0 ? `<span class="tackbadge" title="${l.tacks} tack(s) or gybe(s) on this leg">⇄ ${l.tacks}</span>` : "0"}</td><td>${w.tws ?? "—"}</td><td>${w.twd ?? "—"}°</td>
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
    return [i + 1, l.to, l.leg_minutes, eta, l.point_of_sail || "", (l.sail || "") + (l.reef ? "+" + l.reef : ""), l.peels ?? 0,
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

/* Public share link — freezes this gameplan into a read-only route-player page anyone with the
   link can open (unguessable token, no team login; served at /share/<token>). The PDF report
   also creates one automatically and embeds the link + QR. */
async function shareGameplan(btn) {
  const r = Opt.result;
  if (!r || !r.legs) { alert("Find a route first."); return; }
  const orig = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Creating link…"; }
  const raceName = (Lab.editDef && (Lab.editDef.name || Lab.editDef.race_id)) || Opt.raceId || "C4";
  try {
    const res = await apiPost("/api/share",
      { result: r, race_name: raceName, boat: (r.boat && r.boat.name) || r.boat_name || "" });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(j.detail || ("HTTP " + res.status));
    let copied = false;
    try { await navigator.clipboard.writeText(j.url); copied = true; } catch (e) { /* http or no permission */ }
    prompt(copied ? "Link copied. Send it to the crew:"
                  : "Copy this link and send it to the crew:", j.url);
  } catch (e) {
    alert("Share failed: " + String((e && e.message) || e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig; }
  }
}

/* PDF report of the whole gameplan — the optimize result + (if synthesized) the branching playbook.
   Server-rendered (reportlab) so it's a clean, consistent, shareable document, streamed back as a blob
   and downloaded (same "email the crew" pattern as the CSV export, but the full report). */
async function downloadGameplanPdf(btn) {
  const r = Opt.result;
  if (!r || !r.legs) { alert("Find a route first."); return; }
  const orig = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Making PDF…"; }
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
    alert("PDF failed: " + String((e && e.message) || e));
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
  const sea = hs > 0.05 ? ` · waves ~${hs.toFixed(1)}m` : "";
  const cls = pct >= 92 ? "ok" : pct >= 82 ? "warn" : "bad";
  // reshape gate: did the sea bend the route (⤳) or only tax the ETA (=)?
  const rsh = (hs > 0.05 && "wave_reshape" in rz)
    ? ` <span class="muted" title="${attr(rz.wave_reshape_note || "")}">${rz.wave_reshape ? "⤳ waves change route" : "= waves slow only"}</span>` : "";
  return `<div title="Route speed as % of the full polar (helm and waves). The gap is speed left to find."><b class="conf ${cls}">${pct}%</b><span>of polar · helm ${helm}%${sea}${rsh}</span></div>`;
}

function optCurrentStat(r) {
  const c = r.current;
  if (!c || !c.loaded) return "";
  const src = (c.source || "current").toUpperCase();
  const peak = r.current_grid && r.current_grid.peak_drift_kn;
  const sub = c.source === "constant"
    ? `${c.drift_kn ?? "?"} kts @ ${c.set_deg ?? "?"}°`
    : `${peak ? `peak ${peak} kts` : "varies"}`;
  return `<div title="Water current included in leg times. Source ${esc(src)}."><b>${esc(src)}</b><span>current · ${esc(sub)}</span></div>`;
}

// Venue weather-model skill panel: each model weighted by its MEASURED past accuracy at this venue
// (forecast-vs-observed), recency-weighted across seasons. Weights are applied automatically, so we
// always show them + the RMSE that earned them + a button to deepen the history (pre-2021 archives).
function optModelSkill(r) {
  const s = r.model_skill;
  if (!s) return "";
  const btn = `<button class="mini" onclick="runModelSkillBackfill()" id="mskBackfill"
    title="Adds older forecasts (back to 2005) for this venue. Runs once.">⏳ Add older years (2005+)</button>`;
  if (!s.enabled) {
    return `<details class="rail-sec"><summary>Model skill</summary>
      <div class="muted" style="font-size:12px">${esc(s.note || s.reason || "not weighting")}.
      Using default model weights.</div>
      ${s.venue_key ? `<div class="muted" style="font-size:11px;margin-top:4px">Venue ${esc(s.venue_key)}${s.station ? " · " + esc(s.station) : ""}</div>${btn}` : ""}
    </details>`;
  }
  const rows = (s.table || []).map((t) => {
    const db = (t.bias_speed_kn || t.bias_dir_deg)
      ? `<span class="muted" title="usual direction error, corrected">${t.bias_dir_deg > 0 ? "+" : ""}${t.bias_dir_deg || 0}°</span>` : "—";
    const yrs = t.n_years ? ` <span class="muted" style="font-size:10px">${t.n_years}y</span>` : "";
    const wcell = t.reference
      ? `<span class="muted" title="Shown for reference. Not used in the route.">ref</span>`
      : `<b class="conf ${(t.weight >= 1.15) ? "ok" : (t.weight <= 0.85) ? "bad" : ""}">×${t.weight == null ? 1 : t.weight}</b>`;
    return `<tr><td>${esc(t.model.toUpperCase())}${yrs}</td><td>${t.vector_rmse_kn}</td>
      <td>${wcell}</td><td>${db}</td><td class="muted">${t.n}</td></tr>`;
  }).join("");
  const deep = s.deep ? `<span class="pill ok" title="Includes older forecasts (HRRR 2015+, GEFS 2005+)">older years</span>` : "";
  const total = (s.table || []).reduce((a, t) => a + (t.n || 0), 0);
  const nSt = (s.station || "").split("+").filter(Boolean).length;
  return `<details class="rail-sec" open><summary>Model skill — past accuracy here ${deep}</summary>
    <div class="muted" style="font-size:11px;margin-bottom:8px">
      How close each model's past forecasts were to the measured wind here. Better models count more in the route.
      ${qhelp("Lower error = more trusted. Each model's usual direction and speed error is corrected before mixing. The weights apply to the route above automatically.")}</div>
    <div class="rail-stats opt-stats" style="margin-bottom:8px">
      <div title="${esc(s.station_name || s.station || "")}"><b>${nSt || "—"}</b><span>obs station${nSt === 1 ? "" : "s"}</span></div>
      <div><b>${s.n_years || 1}</b><span>seasons</span></div>
      <div title="Race dates ±21 days, each year"><b>${s.window ? esc(s.window[0]) + "–" + esc(s.window[1]) : "—"}</b><span>years</span></div>
      <div title="Forecast vs measured pairs, all models"><b>${total.toLocaleString()}</b><span>comparisons</span></div>
      <div title="Older years count less. This is when a year counts half."><b>${s.recency_halflife_y}y</b><span>half weight after</span></div>
    </div>
    <div class="muted" style="font-size:10px;margin-bottom:3px">measured: ${esc(s.station || "—")} (${esc(s.obs_source || "")}) · forecasts: 2021+ ${s.deep ? "+ older years" : ""}</div>
    <table class="legs"><thead><tr>
      <th title="Model (years of data)">Model</th><th title="Typical wind error, kn. Lower is better.">Error kn</th>
      <th title="How much this model counts. 'ref' = not used.">Weight</th>
      <th title="Usual direction error, corrected">Bias</th>
      <th title="Number of comparisons">n</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <div style="margin-top:6px">${btn}</div>
    <div id="mskMsg" class="muted" style="font-size:11px;margin-top:4px"></div>
  </details>`;
}

/* The deep backfill is a server-side BACKGROUND JOB (a full seasonal span ~an hour) — start it,
   then poll /status; the button/note live-update and the panel refreshes when the weights land. */
async function runModelSkillBackfill() {
  const b = document.getElementById("mskBackfill"); const msg = document.getElementById("mskMsg");
  if (!b) return;
  b.disabled = true; b.textContent = "Adding older years… (you can keep working)";
  const setMsg = (t) => { const m = document.getElementById("mskMsg"); if (m) m.textContent = t; };
  setMsg("Starting…");
  const body = { race_id: Opt.raceId, course_id: Opt.courseId };
  const se = optStartEpoch(); if (se != null) body.start_epoch = se;
  try {
    const start = await jsonOrFriendly(await apiPost("/api/model-skill/backfill", body));
    if (start.detail || (start.ok === false && !start.job)) throw new Error(start.detail || start.note || "refused");
    const poll = setInterval(async () => {
      try {
        const st = await (await api("/api/model-skill/backfill/status")).json();
        if (st.state === "running") {
          const last = (st.progress || []).slice(-1)[0];
          setMsg("Running… " + (last || "") + " (up to an hour, you can leave this page)");
          return;
        }
        clearInterval(poll);
        if (st.state === "done") {
          if (Opt.result) Opt.result.model_skill = st.result;
          if ((location.hash || "#races").slice(1) === "gameplan") renderGameplan();
        } else {
          setMsg("Failed: " + esc(String(st.error || st.state)));
          const btn = document.getElementById("mskBackfill");
          if (btn) { btn.disabled = false; btn.textContent = "⏳ Add older years (2005+)"; }
        }
      } catch (e) { /* transient poll error — keep polling */ }
    }, 5000);
  } catch (e) {
    setMsg("Failed: " + esc(String(e.message || e)));
    b.disabled = false; b.textContent = "⏳ Add older years (2005+)";
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
    use_waves: wavesEl ? wavesEl.checked : true,
    fan_depth: (document.getElementById("pbFan") || {}).value || "standard" };
  const startEpoch = optStartEpoch();
  if (startEpoch != null) body.start_epoch = startEpoch;
  Pb.running = true; Pb.result = null;
  const b = document.getElementById("pbRun"); if (b) { b.disabled = true; b.textContent = "Building… (about 10 min)"; }
  out.innerHTML = '<div class="loading" style="margin-top:10px">Routing each forecast scenario and writing the playbook. About 10 min. You can keep working.</div>';
  try {
    const start = await (await apiPost("/api/playbook/synthesize", body)).json();
    if (start.ok === false) throw new Error(start.note || "a playbook is already building");
    // background job — poll for the bundle (the fan runs far past the gateway timeout)
    while (true) {
      await new Promise((r) => setTimeout(r, 10000));
      const st = await (await apiGet("/api/playbook/synthesize/status")).json();
      if (st.state === "done") { Pb.result = st.bundle; break; }
      if (st.state === "error") { Pb.result = { available: false, note: st.error }; break; }
      if (st.state === "idle") { Pb.result = { available: false, note: "the playbook job stopped. Run it again." }; break; }
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
        <span class="pill">${agree} of forecasts agree</span>
        <span class="pill ${(b.decision_spread_min || 0) >= 15 ? "warn" : ""}">worth ~${Math.round(b.decision_spread_min || 0)} min</span>
        <span class="pill">${b.variants.length} options · ${(b.provenance || {}).n_scenarios || "?"} scenarios</span>
      </div>
      ${pbBoatModelNote(b)}
    </div>
    <div class="pb-variants">${b.variants.map((v) => pbVariantCard(v, v.id === b.recommended)).join("")}</div>
    ${pbPlaysSection(b)}
    ${pbDecisionTree(b)}
    <div class="pb-freeze">
      ${sig ? pbSigBox(b) :
        `<button id="pbFreeze" onclick="freezePlaybook()" ${Pb.freezing ? "disabled" : ""}>${Pb.freezing ? "Freezing…" : "🔒 Freeze for the boat"}</button>
         <span class="muted" style="font-size:12px">Saves a locked copy to send to the boat. No changes after the start.</span>`}
    </div></div>`;
}

// ---- Playbook v2: the scenario PLAY LIBRARY (docs/PLAYBOOK_V2.md) -------------------------------
function pbPlaysSection(b) {
  const plays = b.plays || [];
  const robust = ((b.nominal || {}).robustness) || [];
  const cor = b.corridor;
  if (!plays.length && !robust.length) return "";
  const corLine = cor ? `<div class="muted" style="font-size:12px;margin:4px 0">
      <b>${cor.verdict === "geometry" ? "⚖ The side matters" : "🎯 Boat speed matters most"}:</b>
      ${esc(cor.note || "")} (most routes within ${cor.corridor_p90_nm ?? "—"} nm · worth up to ${cor.stakes_min ?? 0} min)</div>` : "";
  const robustLine = robust.length ? `<div class="muted" style="font-size:12px;margin:4px 0">
      ✅ Main plan still works if: ${robust.map((r) => esc(r.name || r.scenario)).join(" · ")}</div>` : "";
  const vs = b.venue_stats;
  const vsLine = vs ? `<div class="muted" style="font-size:11px;margin:4px 0">Typical here (${(vs.races || []).length} past race(s), ${vs.n_boats} boats): ${vs.xte_median_nm} nm off the plan (most within ${vs.xte_p90_nm}), ${vs.behind_median_min} min behind it (most within ${vs.behind_p90_min}).</div>` : "";
  return `<div style="margin-top:12px">
    <h4 style="margin:4px 0">Plays — ready answers if the weather changes (${plays.length})</h4>
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
      <span class="pill ${p.stakes_min >= 60 ? "warn" : ""}">worth ~${p.stakes_min ?? 0} min</span>
      ${p.favored_side ? `<span class="pill">${esc(p.favored_side)}</span>` : ""}
      <span class="muted" style="font-size:12px">${esc(p.summary || "")}</span></summary>
    <div class="pb-row"><span class="pb-lbl">You'll see</span><span>${esc((p.conditions || {}).narrative || "")}</span></div>
    ${preds ? `<div class="pb-row"><span class="pb-lbl">Triggers when</span><span class="mono" style="font-size:11px">${preds}</span></div>` : ""}
    ${p.rationale ? `<div class="pb-row"><span class="pb-lbl">Why</span><span>${esc(p.rationale)}</span></div>` : ""}
    ${p.tradeoffs ? `<div class="pb-row"><span class="pb-lbl">Cost</span><span>${esc(p.tradeoffs)}</span></div>` : ""}
    ${p.what_flips_it ? `<div class="pb-row flips"><span class="pb-lbl">Ends when</span><span>${esc(p.what_flips_it)}</span></div>` : ""}
    ${(p.response || {}).route ? `<div class="pb-row"><span class="pb-lbl">Route</span><span>${(p.response.route.legs || []).length} legs · ${p.response.route.total_sailed_nm ?? "—"} nm · ${p.response.route.total_tacks ?? "—"} tacks · sails ${esc(((p.response.route.sail_plan || []).map((s) => s.sail || s)).join("→") || "—")}</span></div>` : ""}
    ${(p.response || {}).guidance ? `<div class="pb-row"><span class="pb-lbl">The call</span><span><b>${esc(p.response.guidance)}</b></span></div>` : ""}
    ${p.boundary ? `<div class="pb-row"><span class="pb-lbl">Switch point</span><span>plan holds at ${p.boundary.holds}, changes by ${p.boundary.diverges}. Switch at <b>${p.boundary.threshold}</b>.</span></div>` : ""}
    ${(p.table || []).length ? `<div class="pb-row"><span class="pb-lbl">Per leg</span><span class="mono" style="font-size:11px">${p.table.map((r) => `${esc(r.to || `leg ${r.leg + 1}`)} · ${esc(r.side)} ${r.off_nm}nm: <b>${esc(r.verdict)}</b> (continue ${r.continue_min}m / rejoin ${r.rejoin_min}m)`).join("<br>")}</span></div>` : ""}
  </details>`;
}

function pbVariantCard(v, recommended) {
  const conf = v.route_confidence, cc = conf == null ? "" : conf >= 0.6 ? "ok" : conf >= 0.4 ? "warn" : "bad";
  const share = v.share == null ? "—" : Math.round(v.share * 100) + "%";
  const rng = v.hours_range ? ` · ${v.hours_range[0]}–${v.hours_range[1]} h` : "";
  return `<div class="pb-var${recommended ? " rec" : ""}">
    <div class="pb-var-top">
      <b>${esc(v.name)}</b>${recommended ? '<span class="pill ok">recommended</span>' : ""}
      <span class="pill">${share}</span>
      <span class="muted" style="font-size:12px">${v.total_hours ?? "—"} h${rng} · <span class="conf ${cc}">confidence ${conf ?? "—"}</span> · ${esc((v.supported_by || []).map((m) => m.toUpperCase()).join(", "))}</span>
    </div>
    <div class="pb-var-sum">${esc(v.summary || "")}</div>
    ${pbSailPlanRow(v)}
    ${v.rationale ? `<div class="pb-row"><span class="pb-lbl">Why</span><span>${esc(v.rationale)}</span></div>` : ""}
    ${v.tradeoffs ? `<div class="pb-row"><span class="pb-lbl">Cost</span><span>${esc(v.tradeoffs)}</span></div>` : ""}
    ${v.what_flips_it ? `<div class="pb-row flips"><span class="pb-lbl">What changes it</span><span>${esc(v.what_flips_it)}</span></div>` : ""}
  </div>`;
}

function pbBoatModelNote(b) {
  const m = b.boat_model; if (!m || !m.sail_inventory) return "";
  const inv = (m.sail_inventory || []).map((s) => `<span class="sail sail-${esc(s)}">${esc(s)}</span>`).join(" ");
  return `<div class="pb-boatmodel muted" style="font-size:12px;margin-top:6px">⛵ Sails in this plan: ${inv} · draft ${m.draft_ft ?? "?"} ft.</div>`;
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
    <span class="pill ok">🔒 Frozen</span>
    <code title="sha256">${esc((sig.value || "").slice(0, 16))}…</code>
    <button class="mini" onclick="downloadPlaybook('${esc(pid)}')">Download playbook</button>
    <button class="mini" onclick="downloadPlaybookGpx('${esc(pid)}')" title="Marks, route and options for the chartplotter. Load it with ActiveCaptain or a memory card.">⤓ GPX for chartplotter</button>
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
  } catch (e) { alert("Couldn't download the playbook: " + e.message); }
}

// The gameplan as GPX for the boat's chartplotter (the GPSMAP imports GPX via ActiveCaptain /
// a memory card and draws it on the chart). Same authed-fetch dance as the bundle download.
async function downloadPlaybookGpx(pid) {
  try {
    const res = await apiGet("/api/playbooks/" + encodeURIComponent(pid) + "/gpx?variants=all");
    if (!res.ok) throw new Error("download failed (" + res.status + ")");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = pid + ".gpx";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch (e) { alert("Couldn't download the GPX: " + e.message); }
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
const Dep = { races: null, raceId: null, ready: null, sel: null, busy: false, boat: null, boatBusy: false };

async function renderDeploy() {
  const view = document.getElementById("view");
  if (!Dep.races) {
    view.innerHTML = '<div class="loading">Loading…</div>';
    try { Dep.races = (await (await apiGet("/api/races")).json()).races || []; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  if (!Dep.raceId) Dep.raceId = Lab.sel || (Dep.races[0] && Dep.races[0].race_id) || null;
  if (!Dep.raceId) { view.innerHTML = '<div class="placeholder">No race yet. Add one in the Races tab.</div>'; return; }
  try { Dep.ready = await (await apiGet("/api/deploy?race_id=" + encodeURIComponent(Dep.raceId))).json(); }
  catch (e) { view.innerHTML = '<div class="placeholder">Couldn\'t load the deploy page.</div>'; return; }
  if (stale("deploy")) return;
  paintDeploy();
  depCheckBoat();
  depLoadPlume();
}

// ---- Forecast plume: every router model along the planned route, run by run (app/plume.py) —
// the same payload the boat caches; drawn with the console's own plume.js.
const Plume = { data: null, field: "tws", only: null, busy: false, note: "" };
async function depLoadPlume() {
  try { Plume.data = await (await apiGet("/api/plume")).json(); } catch (e) { Plume.data = { error: e.message }; }
  const el = document.getElementById("depPlume");
  if (el && !stale("deploy")) el.innerHTML = plumePanelInner();
}
function plumeSet(k, v) {
  if (k !== "field" && k !== "only") return;
  Plume[k] = (k === "only" && (!v || Plume.only === v)) ? null : v; depLoadPlume();
}
document.addEventListener("click", (e) => {
  const b = e.target.closest && e.target.closest("#depPlume button[data-pl-k]");
  if (b) plumeSet(b.dataset.plK, b.dataset.plV);
});
async function plumeRefresh() {
  Plume.busy = true; Plume.note = "Checking every model for a new run…"; depLoadPlume();
  try {
    await apiPost("/api/plume/refresh", {});
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => setTimeout(r, 4000));
      const st = await (await apiGet("/api/plume/refresh/status")).json();
      Plume.note = (st.progress || []).slice(-1)[0] || Plume.note;
      if (st.state !== "running") { Plume.note = st.state === "error" ? "Failed: " + st.error : ""; break; }
      depLoadPlume();
    }
  } catch (e) {
    Plume.note = "Check failed: " + e.message;
  } finally {
    Plume.busy = false; depLoadPlume();
  }
}
async function plumeFollow(on) {
  await apiPost("/api/plume/target", on ? { race_id: Dep.raceId, playbook_id: (Dep.ready.lock_in || {}).playbook_id } : {});
  depLoadPlume();
}
function plumePanelInner() {
  const P = window.C4Plume, d = Plume.data;
  const head = `<h3>Forecast plume <span class="muted" style="font-weight:400">— every router model along the planned route, run by run (what the boat's Forecast tile shows)</span></h3>`;
  if (!d) return head + `<div class="muted">Loading…</div>`;
  if (d.error) return head + `<div class="banner warn">${esc(d.error)}</div>`;
  const tgt = d.target, lp = d.last_push, pl = d.plume;
  const frozenBar = d.paused ? `<div class="banner warn" style="margin:6px 0">Paused: race started ${esc(depStamp(d.race_start))}.</div>` : "";
  const follows = tgt ? `pinned to <b>${esc(tgt.race_id)}</b>${tgt.playbook_id ? ` · <code>${esc(tgt.playbook_id)}</code>` : ""}` : "following the race last pushed to the boat";
  const ctl = `<div class="opt-controls">
      <button onclick="plumeRefresh()" ${Plume.busy ? "disabled" : ""}>${Plume.busy ? "Checking…" : "Check for new runs now"}</button>
      ${tgt && tgt.race_id === Dep.raceId ? `<button class="ghost" onclick="plumeFollow(false)">Follow the boat's race</button>`
        : `<button class="ghost" onclick="plumeFollow(true)">Pin to this race${(Dep.ready.lock_in || {}).playbook_id ? " + locked playbook" : ""}</button>`}
      <span class="muted">${follows}${d.recorder ? " · recorder on (hourly)" : " · recorder off (PLUME_RECORDER)"}</span>
    </div>${Plume.note ? `<div class="muted" style="font-size:12px">${esc(Plume.note)}</div>` : ""}`;
  const push = lp ? `<div class="muted" style="font-size:12px;margin-top:6px">Last push to the boat: ${esc(depStamp(lp.at))} (${esc(depAgo(lp.at))}) · ${lp.ok ? "✓ loaded" : "✗ " + esc(lp.error || "failed")}</div>` : `<div class="muted" style="font-size:12px;margin-top:6px">Not pushed to the boat yet.</div>`;
  if (!pl || !P || !P.available(pl)) return head + frozenBar + ctl + `<div class="muted">No runs recorded yet for this target.</div>` + push;
  const now = Date.now() / 1000;
  const b = (k, v, lab, on) => `<button class="${on ? "" : "ghost"}" data-pl-k="${esc(k)}" data-pl-v="${esc(v)}">${lab}</button>`;
  const picks = b("only", "", "All models", !Plume.only) + P.models(pl).map((m) => b("only", m.model, P.label(m.model), Plume.only === m.model)).join("");
  return head + frozenBar + ctl +
    `<div class="opt-controls">${b("field", "tws", "Speed", Plume.field === "tws")}${b("field", "twd", "Direction", Plume.field === "twd")} ${picks}</div>
     <div class="muted" style="font-size:12px">${esc(pl.label || "")} · newest run recorded ${esc(depAgo(pl.built_at))}</div>
     <div class="lab-plume" style="max-width:900px">${P.chartSvg(pl, { w: 860, h: 300, now: now, field: Plume.field, only: Plume.only, allRuns: true, back_h: 6, ahead_h: 24 })}
     ${P.legendHtml(pl, { only: Plume.only, cycles: true, now: now })}${P.runTimesHtml(pl, now)}</div>` + push;
}

// Live read of what the boat has loaded (read-only GETs over the Lab's Tailscale line). Slow when
// the boat is off the line (a few seconds of timeout), so it paints after the rest of the tab.
let depBoatSeq = 0;
async function depCheckBoat() {
  const seq = ++depBoatSeq;       // a slow older check must not overwrite a newer one (e.g. post-push)
  Dep.boatBusy = true; depPaintBoat();
  let boat;
  try {
    const res = await apiGet("/api/deploy/boat");
    boat = res.ok ? await res.json() : { error: "check failed (" + res.status + ")" };
  } catch (e) { boat = { error: e.message }; }
  if (seq !== depBoatSeq) return;
  Dep.boat = boat; Dep.boatBusy = false;
  if (!stale("deploy")) depPaintBoat();
}

function depPaintBoat() {
  const el = document.getElementById("depBoat");
  if (el && Dep.ready) el.innerHTML = boatPanelInner();
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
    esc((b.signed ? "🔒 " : "draft ") + (b.headline || b.id).slice(0, 60))} · ${b.n_variants} options · ${esc(fmtDate(b.generated_at))}</option>`;

  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Lock-in &amp; Deploy <span class="muted" style="font-weight:400">— send the race prep to the boat</span></h3>
      <div class="opt-controls">
        <label>Race <select id="depRace" onchange="depPickRace(this.value)">
          ${Dep.races.map((x) => `<option value="${esc(x.race_id)}" ${x.race_id === Dep.raceId ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
      </div>

      <div class="dep-grid">
        <div class="dep-row">${depPill(r.reviewed, "Race reviewed")}<span class="muted">${r.reviewed ? "approved" : "approve it in the Races tab"}</span></div>
        <div class="dep-row">${depPill(c.ready, "Course")}<span class="muted">${esc(c.course_id || "—")} · ${c.marks} marks${c.skipped && c.skipped.length ? " · ⚠ " + c.skipped.length + " without a position (" + c.skipped.map(esc).join(", ") + ")" : ""}</span></div>
        <div class="dep-row">${depPill(fl.ready, "Fleet")}<span class="muted">${fl.roster} boats${fl.scoring ? " · " + esc(fl.scoring) : ""}${fl.tracker_permitted ? " · tracker permitted" : ""}</span></div>
        <div class="dep-row">${depPill(ck.ready, "Checklists")}<span class="muted">${ck.total} items · ${ck.ipad} go to the iPad as reminders</span></div>
        <div class="dep-row">${depPill((r.watch || {}).ready, "Watch plan")}<span class="muted">${(r.watch || {}).ready ? (r.watch.blocks + " watches. The iPad can still change them.") : "optional. Set it up in the Races tab."}</span></div>
        <div class="dep-row">${depPill(pbReady, "Playbook")}<span class="muted">${pbReady ? pbs.length + " frozen for this race" : "Build and freeze one in Gameplan."}</span></div>
      </div>
    </div>

    <div class="card">
      <h3>Lock in the playbook</h3>
      ${pbReady ? `
        <div class="opt-controls">
          <label>Playbook <select id="depPb" onchange="Dep.sel=this.value" style="min-width:340px">${pbs.map(pbOpt).join("")}</select></label>
          <button id="depLock" onclick="depLockIn()" ${Dep.busy ? "disabled" : ""}>${lock ? "Re-lock selected" : "🔒 Lock in selected playbook"}</button>
        </div>` : `<div class="muted">No frozen playbook yet. Build one in <a href="#gameplan">Gameplan</a> and freeze it. Then lock it in here.</div>`}
      ${lock ? `<div class="banner ok" style="margin-top:10px">🔒 Locked in: <code>${esc(lock.playbook_id)}</code>${lock.signed ? ` · signature <code>${esc(lock.signature)}…</code>` : " · <b>unsigned</b>"} · ${esc(fmtDate(lock.locked_at))}</div>` : ""}
    </div>

    <div class="card" id="depBoat">${boatPanelInner()}</div>

    <div class="card" id="depPlume">${plumePanelInner()}</div>

    ${lock ? deployPanel(lock, t) : ""}
  </div>`;
}

const depStamp = (e) => e ? new Date(e * 1000).toISOString().slice(0, 16).replace("T", " ") + "Z" : "—";
function depAgo(e) {
  if (!e) return "";
  const m = Math.max(0, Math.round((Date.now() / 1000 - e) / 60));
  if (m < 1) return "just now";
  if (m < 60) return m + " min ago";
  if (m < 48 * 60) return Math.round(m / 60) + " h ago";
  return Math.round(m / 1440) + " days ago";
}

// "What's on the boat": the live read when the boat answers, the last recorded push always, and
// plain warnings when the boat's load is not the homework locked in here.
function boatPanelInner() {
  const lp = (Dep.boat && Dep.boat.last_push) || (Dep.ready && Dep.ready.last_push) || {};
  const last = lp.last, lastOk = lp.last_ok;
  const b = Dep.boat && Dep.boat.boat;
  const lock = Dep.ready && Dep.ready.lock_in;
  const rid = Dep.raceId;
  const eng = (b && b.engine) || {}, cop = (b && b.copilot) || {};
  const epb = eng.playbook, cpb = cop.playbook;
  const warns = [];
  let liveRace = null;            // the race the boat itself says it holds (live read only)

  // live line
  let live;
  if (Dep.boatBusy && !b) live = `<div class="banner">Checking the boat…</div>`;
  else if (Dep.boat && Dep.boat.error) live = `<div class="banner warn">Couldn't check the boat (${esc(Dep.boat.error)}). Showing the last push only.</div>`;
  else if (b && !b.reachable) live = `<div class="banner warn"><b>Can't reach the boat.</b> Nothing below is live. ${last ? `This is the last push (${esc(depAgo(last.at))}). The boat may have changed since.` : "No push on record either."}<div class="muted" style="font-size:12px;margin-top:4px">${esc(eng.error || cop.error || "")}</div></div>`;
  else if (b) {
    const rows = [];
    const pbText = (p) => !p ? "no answer" : p.loaded === false ? "no playbook loaded" : p.loaded == null ? (p.note || "unknown") :
      `${esc(p.race_id || "?")}${p.id ? ` · <code>${esc(p.id)}</code>` : ""}${p.variants != null ? ` · ${p.variants} variants` : ""}${p.plays != null ? ` · ${p.plays} plays` : ""}${p.signed != null ? (p.signed ? (p.signature_ok === false ? " · ⚠ signature doesn't match" : ` · signed${p.signature ? ` <code>${esc(p.signature)}…</code>` : ""}`) : " · unsigned") : ""}${p.partial ? ` · <span class="muted">older boat software: race only</span>` : ""}`;
    const okPill = (ok, label) => `<span class="pill ${ok ? "ok" : "warn"}">${ok ? "●" : "○"} ${esc(label)}</span>`;
    rows.push(`<div class="dep-row">${okPill(eng.reachable, "Pi engine")}<span>${eng.reachable ? "playbook: " + pbText(epb) : "unreachable" + (eng.error ? " — " + esc(eng.error) : "")}</span></div>`);
    if (eng.course) rows.push(`<div class="dep-row"><span class="pill" style="visibility:hidden">● Pi engine</span><span>course: ${eng.course.available ? `${esc(eng.course.route || "?")} · ${eng.course.marks} marks${eng.course.mark_names && eng.course.mark_names.length ? ` <span class="muted">(${eng.course.mark_names.map(esc).join(" → ")})</span>` : ""}` : "none loaded"}</span></div>`);
    const oa = eng.orin_assets;
    if (oa) rows.push(`<div class="dep-row"><span class="pill" style="visibility:hidden">● Pi engine</span><span>re-route: ${!oa.available ? "no chart or boat profile" : `${oa.chart ? `chart for ${esc(oa.course_id || "?")}` : "no chart"} · ${oa.boat_id ? `boat ${esc(oa.boat_id)} <code>${esc(oa.boat_version || "")}</code>` : "no boat profile"}`}</span></div>`);
    rows.push(`<div class="dep-row">${okPill(cop.reachable, "Orin copilot")}<span>${cop.reachable ? "playbook: " + pbText(cpb) : "unreachable" + (cop.error ? " — " + esc(cop.error) : "")}</span></div>`);
    live = `<div class="muted" style="font-size:12px">Checked · ${esc(depStamp(b.checked_at))}</div><div class="dep-grid">${rows.join("")}</div>`;

    // live mismatches
    liveRace = (epb && epb.race_id) || (cpb && cpb.loaded && cpb.race_id) || null;
    if (epb && cpb && epb.race_id && cpb.loaded && cpb.race_id && epb.race_id !== cpb.race_id)
      warns.push(`The Pi engine has <b>${esc(epb.race_id)}</b> but the Orin copilot has <b>${esc(cpb.race_id)}</b>. Push again so both match.`);
    if (liveRace && liveRace !== rid) {
      warns.push(`The boat is loaded for <b>${esc(liveRace)}</b>, not this race.`);
    } else if (lock && epb && epb.id && (epb.id !== lock.playbook_id ||
               (epb.signature || "") !== (lock.signature || "")))
      warns.push(epb.id === lock.playbook_id
        ? `The boat has a different version of <code>${esc(epb.id)}</code> than the one locked in. Re-lock and push.`
        : `The boat's playbook (<code>${esc(epb.id)}</code>) is not the one locked in here (<code>${esc(lock.playbook_id)}</code>). Push to replace it.`);
    if ((epb && epb.loaded === false) || (cop.reachable && cpb && !cpb.loaded))
      warns.push(`${epb && epb.loaded === false ? "The Pi engine" : "The Orin copilot"} has no playbook loaded.`);
  }

  // last push
  const stepPills = (r) => (r.steps || []).map((x) => `<span class="pill ${x.ok ? "ok" : "warn"}" title="${esc(x.ok ? "" : ((x.detail || {}).error || (x.detail || {}).detail || ""))}">${x.ok ? "✓" : "✗"} ${esc(x.target)} · ${esc(x.step)}</span>`).join(" ");
  const pushLine = (r, label) => `<div style="margin-top:6px"><b>${label}</b> ${esc(depStamp(r.at))} <span class="muted">(${esc(depAgo(r.at))})</span> · ${esc(r.race_name || r.race_id || "?")}${r.playbook_id ? ` · playbook <code>${esc(r.playbook_id)}</code>` : " · <b>no playbook in this push</b>"} · ${r.ok ? `<span style="color:var(--accent2)">✓ all ${(r.steps || []).length} steps loaded</span>` : `<span style="color:var(--warn)">⚠ ${(r.steps || []).filter((x) => x.ok).length} of ${(r.steps || []).length} steps loaded</span>`}<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">${stepPills(r)}</div></div>`;
  let pushes;
  if (!last) pushes = `<div class="muted" style="margin-top:6px">No push on record. Anything on the boat was loaded by hand or long ago.</div>`;
  else {
    pushes = pushLine(last, "Last push:");
    if (!last.ok && lastOk) pushes += pushLine(lastOk, "Last complete push:");
  }

  // a failed latest push always warns: the copilot's /health carries no bundle signature, so a
  // matching Pi playbook can't prove the Orin took the new one too
  if (last && !last.ok) {
    const bad = [...new Set((last.steps || []).filter((x) => !x.ok).map((x) => x.target))];
    warns.push(`The last push didn't finish${bad.length ? ` (failed on ${bad.map(esc).join(", ")})` : ""}. The boat may have a mix of old and new. Push again.`);
  }

  // push-record mismatches: the only signal when the boat is unreachable, and the fallback when an
  // older engine can't say exactly which playbook it holds (no bundle id in its answer), or the
  // copilot can't be read at all
  if (!(b && b.reachable && epb && epb.id && cop.reachable)) {
    const ref = (last && last.ok) ? last : lastOk;
    if (ref && ref.race_id !== rid) { if (!liveRace) warns.push(`The last complete push was for <b>${esc(ref.race_name || ref.race_id)}</b>, not this race. This race's prep hasn't gone to the boat.`); }
    else if (lock && ref && ref.playbook_id === lock.playbook_id && (ref.signature || "") !== (lock.signature || ""))
      warns.push(`The last push sent a different version of <code>${esc(lock.playbook_id)}</code> than the one locked in. Re-lock and push.`);
    else if (lock && ref && ref.playbook_id !== lock.playbook_id) warns.push(`The locked playbook <code>${esc(lock.playbook_id)}</code> hasn't reached the boat. The last full push (${esc(depAgo(ref.at))}) ${ref.playbook_id ? `sent <code>${esc(ref.playbook_id)}</code>` : "sent no playbook"}.`);
    else if (lock && ref && ref.at < lock.locked_at) warns.push("You re-locked after the last push. Push again to be sure the boat matches.");
  }

  return `<h3>On the boat <span class="muted" style="font-weight:400">— what's loaded now</span>
      <button style="float:right;font-size:12px" onclick="depCheckBoat()" ${Dep.boatBusy ? "disabled" : ""}>${Dep.boatBusy ? "Checking…" : "↻ Check boat"}</button></h3>
    ${live || ""}
    ${warns.map((w) => `<div class="banner warn" style="margin:8px 0 0">⚠ ${w}</div>`).join("")}
    ${pushes}`;
}

function deployPanel(lock, t) {
  const rid = Dep.raceId, pid = lock.playbook_id;
  const hw = `homework_${rid}.json`, pb = `${pid}.json`;
  const cmds = [
    `# 1. Course + fleet → the Pi engine (Tailscale host: ${t.pi_host})`,
    `jq .course_load ${hw} | ssh ${t.pi_host} 'curl -sX POST ${t.pi_engine}/course/load -H "Content-Type: application/json" -d @-'`,
    `jq .fleet_load  ${hw} | ssh ${t.pi_host} 'curl -sX POST ${t.pi_engine}/fleet/load  -H "Content-Type: application/json" -d @-'`,
    ...(((Dep.ready || {}).checklists || {}).ipad ? [
      `jq .checklist_load ${hw} | ssh ${t.pi_host} 'curl -sX POST ${t.pi_engine}/checklist/load -H "Content-Type: application/json" -d @-'`,
    ] : []),
    ...(((Dep.ready || {}).watch || {}).ready ? [
      `jq .watch_load  ${hw} | ssh ${t.pi_host} 'curl -sX POST ${t.pi_engine}/watch       -H "Content-Type: application/json" -d @-'`,
    ] : []),
    ``,
    `# 2. Signed playbook → the Pi engine. The Pi passes it to the Orin copilot and approves it.`,
    `#    Check the answer: "copilot": {"loaded": true}. Never copy the file to the Orin by hand:`,
    `#    the copilot only uses a playbook the Pi approved.`,
    `ssh ${t.pi_host} 'curl -sX POST ${t.pi_engine}/playbook/load -H "Content-Type: application/json" -d @-' < ${pb}`,
  ].join("\n");
  const res = Dep.pushRes;
  const stepRow = (s) => `<div class="dep-row"><span class="pill ${s.ok ? "ok" : "warn"}">${s.ok ? "✓" : "✗"} ${esc(s.target)} · ${esc(s.step)}</span><span class="muted">${esc(s.ok ? pushDetail(s) : (s.detail && (s.detail.error || s.detail.detail)) || JSON.stringify(s.detail).slice(0, 120))}</span></div>`;
  const resHtml = !res ? "" : res.pushing ? `<div class="banner" style="margin-top:10px">Pushing to the boat…</div>`
    : res.refused ? `<div class="banner warn" style="margin-top:10px">⚠ ${esc(res.detail || "Race started. Not sent.")}</div>`
    : `<div class="banner ${res.ok ? "ok" : "warn"}" style="margin-top:10px">${res.ok ? "✓ Boat loaded. The iPad updates by itself." : "⚠ Push didn't finish. Fix it and push again."}</div>
       <div class="dep-grid" style="margin-top:8px">${(res.steps || []).map(stepRow).join("")}</div>`;
  return `<div class="card">
    <h3>Send to the boat</h3>
    <div class="muted" style="font-size:12px;margin-bottom:8px">Sends course, fleet, checklist, watch plan and playbook to the boat. Replaces what's there. If the boat is offline, use the downloads below.</div>
    <div class="dep-actions">
      <button id="depPush" onclick="depPush()" ${Dep.busy ? "disabled" : ""}>🚀 Push to boat</button>
      <button onclick="downloadPlaybook('${esc(pid)}')">⬇ Playbook file</button>
      <button onclick="downloadHomework('${esc(rid)}')">⬇ Race prep file (Pi)</button>
    </div>
    ${resHtml}
    <pre class="dep-cmds">${esc(cmds)}</pre>
  </div>`;
}

function pushDetail(s) {
  const d = s.detail || {};
  if (s.step === "playbook") return `${d.race_id || ""} · ${d.variants ?? "?"} variants · ${d.plays ?? 0} plays${d.signed ? " · signed" + (d.signature_ok === false ? " ⚠ signature doesn't match" : " ✓") : ""}`;
  return JSON.stringify(d).slice(0, 120);
}

async function depPush() {
  Dep.busy = true; Dep.pushRes = { pushing: true }; paintDeploy();
  try {
    const res = await apiPost("/api/deploy/push", { race_id: Dep.raceId });
    Dep.pushRes = await jsonOrFriendly(res);
    if (!res.ok) Dep.pushRes = { ok: false, steps: [], ...Dep.pushRes };
  } catch (e) { Dep.pushRes = { ok: false, steps: [{ target: "lab", step: "push", ok: false, detail: { error: e.message } }] }; }
  Dep.busy = false; paintDeploy();
  depCheckBoat();                 // the push is now on record — re-read what the boat holds
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
  } catch (e) { alert("Couldn't download the race prep file: " + e.message); }
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
  if (!Lab.sel) { view.innerHTML = '<div class="placeholder">No race yet. Add one in the Races tab.</div>'; return; }
  let fetched = false;
  if (!Lab.editDef || Lab.editDef.race_id !== Lab.sel) {
    view.innerHTML = '<div class="loading">Loading fleet…</div>';
    try { Lab.editDef = await (await apiGet("/api/races/" + encodeURIComponent(Lab.sel))).json(); Lab.fleetMsg = ""; fetched = true; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  if (stale("fleet")) return;
  const keepDirty = !fetched && raceDirty();   // re-entered with unsaved edits → don't bless them
  if (!Array.isArray(Lab.editDef.fleet)) Lab.editDef.fleet = [];
  if (!Lab.editDef.own || typeof Lab.editDef.own !== "object") Lab.editDef.own = {};
  Lab.fleetEdit = !Lab.editDef.fleet.length;   // empty roster → open the editor; populated → show the read-only view
  if (!keepDirty) markClean();            // freshly loaded from disk = in sync
  paintFleet();
}

async function fleetPickRace(id) {
  if (!(await confirmLeave())) return paintFleet();   // Stay → repaint puts the picker back
  Lab.sel = id; Lab.editDef = null; Lab.fleetMsg = ""; renderFleet(); }
function fleetSet(i, f, v) { if (Lab.editDef && Lab.editDef.fleet[i]) { Lab.editDef.fleet[i][f] = v; } }
function fleetOwnSet(f, v) { if (!Lab.editDef) return; if (!Lab.editDef.own) Lab.editDef.own = {}; Lab.editDef.own[f] = v; }
function fleetAddRow() { Lab.editDef.fleet.push({ boat: "", division: "", cls: "", owner: "", sail: "", orc_gph: null, rating: null, mmsi: "", source: "manual" }); Lab.fleetEdit = true; paintFleet(); }
function fleetRemove(i) { Lab.editDef.fleet.splice(i, 1); paintFleet(); }

// Auto-import the roster from public data. source: 'yb' = the tracker entry list, 'both' = YB → ORC,
// 'website' = a regatta-site URL/pasted text → ORC, 'orc' = enrich the current roster. extra = {url|text}
// for the website source. Replaces the draft roster for review (nothing is saved until Save).
function _fleetCC() { return (document.getElementById("fleetOrcCC") || {}).value || "USA"; }
function _fleetApplyResult(r, ed) {
  if (Lab.editDef !== ed) return;   // switched race / discarded while the import ran — don't land on it
  if (!r.ok) { Lab.fleetMsg = r.note || r.detail || "import failed. No boats found."; Lab.fleetMsgErr = true; return; }
  Lab.editDef.fleet = r.entries || [];
  Lab.fleetMsgErr = false;
  const bits = [`${r.count} boats`];
  if (r.matched != null) bits.push(`${r.matched}/${r.total} matched to ORC`);
  if (r.unmatched && r.unmatched.length) bits.push(`${r.unmatched.length} need a handicap by hand`);
  if (r.seeded_mmsi) bits.push(`${r.seeded_mmsi} kept their MMSI`);
  if (r.orc_error) bits.push("ORC lookup failed: " + r.orc_error);
  Lab.fleetMsg = "Imported " + bits.join(" · ") + ". Check below, then Save.";
}
// An import REPLACES the draft roster. With boats already in it, ask first and say how many go.
async function fleetConfirmReplace() {
  const n = ((Lab.editDef && Lab.editDef.fleet) || []).length;
  if (!n) return true;
  const boats = `${n} boat${n === 1 ? "" : "s"}`;
  return (await askModal("Replace the roster?",
    `This replaces all <b>${boats}</b> with the imported list. Handicaps you typed in are lost. MMSIs are kept. Nothing is saved until you Save.`,
    [{ c: "cancel", label: "Cancel", cls: "ghost" }, { c: "replace", label: `Replace ${boats}`, cls: "ghost danger" }],
    "cancel", "cancel")) === "replace";
}
async function fleetAutoImport(source, extra) {
  if (source !== "orc" && !(await fleetConfirmReplace())) return;
  const ed = Lab.editDef;
  Lab.fleetBusy = true;
  // a big fleet from a slow entry-list site (e.g. Bayview Mackinac ~200 boats via bycmack's embedded
  // entries page) + ORC enrichment can take a minute or two — set expectations so the wait isn't a mystery.
  Lab.fleetMsg = "Importing… A big fleet can take 2 min.";
  paintFleet();
  try {
    const r = await (await apiPost("/api/fleet/import", Object.assign(
      // fleet = the roster as it stands here (unsaved edits too): 'orc' enriches it, and seeded
      // MMSIs are carried over from it rather than from the last save
      { race_id: Lab.sel, source, country: _fleetCC(), fleet: Lab.editDef.fleet }, extra || {}))).json();
    _fleetApplyResult(r, ed);
  } catch (e) { Lab.fleetMsg = "import failed: " + e.message; }
  Lab.fleetBusy = false; if (activeSec === "fleet" && Lab.editDef) paintFleet();
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
  if (!(await fleetConfirmReplace())) return;
  const ed = Lab.editDef;
  Lab.fleetBusy = true; Lab.fleetMsg = "Reading the PDF…"; paintFleet();
  try {
    const fd = new FormData(); fd.append("file", f); fd.append("fleet", JSON.stringify(Lab.editDef.fleet));
    const q = "?race_id=" + encodeURIComponent(Lab.sel) + "&country=" + encodeURIComponent(_fleetCC());
    const r = await (await api("/api/fleet/import/upload" + q, { method: "POST", body: fd })).json();
    _fleetApplyResult(r, ed);
  } catch (e) { Lab.fleetMsg = "upload failed: " + e.message; }
  Lab.fleetBusy = false; if (activeSec === "fleet" && Lab.editDef) paintFleet();
}

/* Division GUN TIMES (from the SIs, race morning) — the elapsed-time term that makes the
   onboard corrected standings a FULL-race estimate. Rides fleet_blob → POST /fleet/load. */
function epochToWall(e, tz) {
  if (e == null) return "";
  try {
    // an SI extraction stores ISO local strings; the card writes epochs — render both
    const d = typeof e === "string" ? new Date(e) : new Date(e * 1000);
    const p = {};
    new Intl.DateTimeFormat("en-CA", { timeZone: tz || undefined, hourCycle: "h23",
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
      .formatToParts(d).forEach((x) => { p[x.type] = x.value; });
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
  } catch (err) { return ""; }
}
function fleetGunSet(div, val) {
  const m = (val || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  const ds = (Lab.editDef.division_starts = Lab.editDef.division_starts || {});
  if (!m) { delete ds[div]; }
  else {
    const [, y, mo, d, h, mi] = m.map(Number);
    const tz = Lab.editDef.timezone || "";
    ds[div] = tz ? zonedWallToEpoch(y, mo, d, h, mi, tz)
                 : new Date(y, mo - 1, d, h, mi).getTime() / 1000;
  }
 
  paintFleet();
}
function fleetGunsCard(roster, own) {
  const divs = [...new Set([(own.division || ""), ...roster.map((e) => e.division || "")]
    .map((d) => d.trim()).filter(Boolean))].sort();
  if (!divs.length) return "";
  const ds = Lab.editDef.division_starts || {};
  const tz = Lab.editDef.timezone || "";
  const set = divs.filter((d) => ds[d] != null).length;
  const rows = divs.map((d) => `<label style="margin-right:14px">${esc(d)}${(own.division || "").trim() === d ? " ◆" : ""}
    <input type="datetime-local" value="${epochToWall(ds[d], tz)}" onchange="fleetGunSet('${esc(d)}', this.value)"></label>`).join("");
  return `<div class="card">
    <h3>Start times by division <span class="muted" style="font-weight:400">— ${set}/${divs.length} set</span></h3>
    <div class="muted" style="font-size:12px;margin-bottom:8px">Needed for full-race corrected standings on the boat. Times are ${tz ? "race local (" + esc(tz) + ")" : "this browser's time"}. Save in the bar below.
      ${qhelp("From the sailing instructions, on race morning. Without these the boat uses the ⏺ LOG start, which is rough when divisions start at different times.")}</div>
    <div class="opt-controls">${rows}</div>
  </div>`;
}

async function fleetFindOrc(i) {
  const e = (Lab.editDef.fleet || [])[i];
  if (!e) return;
  Lab.fleetMsg = `Searching ORC for "${e.boat}"…`; Lab.fleetMsgErr = false; paintFleet();
  try {
    const cc = (document.getElementById("fleetOrcCC") || {}).value || "USA,CAN";
    const r = await (await apiPost("/api/fleet/orc-candidates",
      { race_id: Lab.sel, boat: e.boat, sail: e.sail, country: cc })).json();
    Lab.fleetCand = { i, list: r.candidates || [] };
    Lab.fleetMsg = (r.candidates || []).length
      ? `${r.candidates.length} ORC match(es) for "${e.boat}". Pick one below the row.`
      : `No ORC match for "${e.boat}". Enter the handicap by hand.`;
  } catch (err) { Lab.fleetMsg = "ORC search failed: " + (err.message || err); Lab.fleetMsgErr = true; }
  paintFleet();
}
function fleetApplyCand(i, ci) {
  const e = (Lab.editDef.fleet || [])[i];
  const c = ((Lab.fleetCand || {}).list || [])[ci];
  if (!e || !c) return;
  if (c.orc_gph != null) e.orc_gph = c.orc_gph;
  if (c.rating != null) e.rating = c.rating;
  if (!e.cls && c.cls) e.cls = c.cls;
  if (!e.sail && c.sail) e.sail = c.sail;
  e.source = (e.source || "") ? e.source + "+orc" : "orc";
  Lab.fleetCand = null;
 
  Lab.fleetMsg = `Applied ${c.yacht} (${c.country}) to "${e.boat}". Check it, then Save.`;
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
  let ok = false;
  const sent = JSON.stringify(Lab.editDef), ed = Lab.editDef;
  try {
    const r = await postDef("/api/races", { definition: JSON.parse(sent) });
    if (r.saved) { markSaved(sent, ed); ok = true; Lab.fleetMsgErr = false;
      Lab.fleetMsg = `Saved. ${Lab.editDef.fleet.length} boat${Lab.editDef.fleet.length === 1 ? "" : "s"} in the roster.`; }
    else { Lab.fleetMsg = "Save failed."; Lab.fleetMsgErr = true; }
  } catch (e) { Lab.fleetMsg = "Save failed: " + e.message; Lab.fleetMsgErr = true; }
  paintFleet();
  return ok;
}

function fleetSrcBadge(e) {
  const s = e.source || "";
  if (!s || s === "manual") return "";
  const orc = s.includes("orc");
  return ` <span class="pill ${orc ? "ok" : "warn"}" title="${esc(orc ? "Handicap from ORC" : "No ORC handicap. Enter it by hand.")}" style="font-size:9px;padding:1px 5px">${orc ? "ORC" : "entry"}</span>`;
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
  const li = (e) => {
    const i = roster.indexOf(e);
    const unrated = e.rating == null && e.orc_gph == null;
    const findBtn = unrated
      ? ` <button class="mini" onclick="fleetFindOrc(${i})" title="Search ORC by name and pick a match.">find</button>`
      : "";
    const cand = (Lab.fleetCand && Lab.fleetCand.i === i) ? `<tr><td colspan="6" style="padding:4px 8px 8px">${
      Lab.fleetCand.list.length ? Lab.fleetCand.list.map((c, ci) =>
        `<button class="mini" style="margin:2px" onclick="fleetApplyCand(${i},${ci})" title="match ${c.score}">` +
        `${esc(c.yacht || "?")} · ${esc(c.sail || "")} · ${esc(c.cls || "")} · GPH ${c.orc_gph ?? "—"}${c.rating != null ? " · " + c.rating : ""} (${esc(c.country)})</button>`).join("")
      : '<span class="muted">no ORC match. Enter the handicap by hand.</span>'
    }</td></tr>` : "";
    return `<tr>
    <td><b>${esc(e.boat || "—")}</b>${fleetSrcBadge(e)}</td>
    <td class="muted">${esc(e.sail || "")}</td>
    <td class="muted">${esc(e.cls || "")}</td>
    <td>${esc(e.division || "")}</td>
    <td>${e.rating != null ? esc(e.rating) : '<span class="conf bad" title="No ORC handicap. Enter it.">—</span>' + findBtn}</td>
    <td class="muted">${e.orc_gph != null ? esc(e.orc_gph) : ""}</td></tr>` + cand;
  };
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
      <h3>Fleet <span class="muted" style="font-weight:400">— boats and handicaps</span></h3>
      <div class="opt-controls">
        <label>Race <select id="fleetRace" onchange="fleetPickRace(this.value)">
          ${Lab.races.map((x) => `<option value="${esc(x.race_id)}" ${x.race_id === Lab.sel ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
        <span class="muted" style="font-size:12px">The boat uses this to show who's ahead on corrected time.</span>
      </div>
    </div>

    <div class="card">
      <h3>Our boat</h3>
      <div class="opt-controls">
        <label>Boat ${inp(own.boat, "SR33 \"C4\"", "fleetOwnSet('boat',this.value)", 150)}</label>
        <label>Division ${inp(own.division, "div", "fleetOwnSet('division',this.value)", 70)}</label>
        <label>ORC GPH ${inp(own.orc_gph, "GPH", "fleetOwnSet('orc_gph',this.value)", 70)}</label>
        <label>Rating ${inp(own.rating, "rating", "fleetOwnSet('rating',this.value)", 80)}</label>
        <label>MMSI ${inp(own.mmsi, "MMSI", "fleetOwnSet('mmsi',this.value)", 100)}</label>
      </div>
    </div>

    ${fleetGunsCard(roster, own)}

    <div class="card">
      <h3>Import the fleet</h3>
      ${Lab.fleetMsg ? `<div class="banner ${Lab.fleetMsgErr ? "warn" : "ok"}" style="margin-bottom:8px">${esc(Lab.fleetMsg)}</div>` : ""}
      <div class="muted" style="font-size:12px;margin-bottom:8px">Gets boats from the YB tracker and handicaps from ORC. Check the list, then Save. ${ybCfg ? "" : '<b>No YB tracker set for this race.</b> Use the website import below, or add ORC handicaps to the current list.'}
        ${qhelp("YB gives boat, sail number, owner and class. ORC matches each boat by sail number or name and adds GPH and rating. Both are public.")}</div>
      <div class="opt-controls">
        <span class="muted" style="font-size:12px">From the <b>YB tracker</b>:</span>
        <button class="${roster.length ? "mini" : ""}" onclick="fleetAutoImport('both')" ${Lab.fleetBusy || !ybCfg ? "disabled" : ""} title="${ybCfg ? "" : "No YB tracker set for this race"}">${Lab.fleetBusy ? "Importing…" : "Boats + handicaps"}</button>
        <button class="mini" onclick="fleetAutoImport('yb')" ${Lab.fleetBusy || !ybCfg ? "disabled" : ""}>Boats only</button>
        <label>ORC countries <input id="fleetOrcCC" class="ein" style="width:84px" value="USA,CAN" title="Countries to search, comma separated. Great Lakes = USA,CAN."></label>
        <button class="mini" onclick="fleetAutoImport('orc')" ${Lab.fleetBusy ? "disabled" : ""}>Add ORC handicaps to list</button>
      </div>
      <div style="margin-top:10px;border-top:1px solid var(--line);padding-top:10px">
        <div class="muted" style="font-size:12px;margin-bottom:6px">Or from the <b>regatta website</b>. YachtScoring and Regatta Network links work directly. For other sites, copy the entry list and paste it below, or upload the PDF.
          ${qhelp("Paste a <code>yachtscoring.com/emenu/…</code> or <code>regattanetwork.com/…?regatta_id=…</code> link. Most other sites build the list in the browser, so the Lab can't read the link. ORC then fills the handicaps.")}</div>
        <div class="opt-controls">
          <input id="fleetWebUrl" class="ein" style="width:340px" placeholder="YachtScoring or Regatta Network link">
          <button class="${roster.length ? "mini" : ""}" onclick="fleetWebImport()" ${Lab.fleetBusy ? "disabled" : ""}>Import from website</button>
          <label>PDF <input type="file" id="fleetEntryPdf" accept=".pdf,application/pdf"></label>
          <button class="mini" onclick="fleetUploadEntry()" ${Lab.fleetBusy ? "disabled" : ""}>Upload PDF</button>
        </div>
        <textarea id="fleetWebText" rows="3" style="width:100%;box-sizing:border-box;margin-top:6px" placeholder="…or paste the entry list here, then tap Import from website"></textarea>
      </div>
      <div class="muted" style="font-size:11px;margin-top:6px">Import replaces the list (it asks first). Boats with no ORC match keep their name. Enter their handicap by hand.
        ${qhelp("ORC matches by sail number, so check it on unmatched boats. MMSIs you typed in are kept on a re-import. The MMSI is how a boat on AIS is matched to this list.")}</div>
    </div>

    <div class="card">
      <h3>Roster
        ${roster.length ? '<span class="pill warn if-dirty" style="margin-left:8px">not saved</span><span class="pill ok if-clean" style="margin-left:8px">saved</span>' : ""}</h3>
      ${roster.length ? fleetGlance(roster) : `<div class="banner warn">No boats yet. Import above or add boats by hand. Then <b>Save</b>.</div>`}
      <details ${Lab.fleetEdit ? "open" : ""} style="margin-top:10px" ontoggle="Lab.fleetEdit=this.open">
        <summary style="cursor:pointer;font-size:13px">Edit roster ${roster.length ? `(${roster.length})` : "(add boats)"}</summary>
        ${roster.length ? `<table class="fleet-tbl" style="margin-top:8px"><thead><tr>
          <th>Boat</th><th>Sail #</th><th>Div</th><th>Class</th><th>Owner</th><th>ORC GPH</th><th>Rating</th><th>MMSI</th><th></th>
          </tr></thead><tbody>${roster.map(row).join("")}</tbody></table>` : ""}
        <div style="margin-top:8px"><button class="mini" onclick="fleetAddRow()">+ Add boat</button></div>
      </details>
    </div>

    <div class="dep-row" style="margin-top:4px"><a href="#rules">Rules →</a>
      <span class="muted">scoring ${esc(sc.method || "not set")} · tracker ${tr.provider ? esc(tr.provider) + (permitted ? " (allowed on board)" : " (not allowed on board)") : "not set"}</span></div>
  </div>`;
  syncDirty();
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
  if (!Lab.sel) { view.innerHTML = '<div class="placeholder">No race yet. Add one in the Races tab.</div>'; return; }
  let fetched = false;
  if (!Lab.editDef || Lab.editDef.race_id !== Lab.sel) {
    view.innerHTML = '<div class="loading">Loading checklist…</div>';
    try { Lab.editDef = await (await apiGet("/api/races/" + encodeURIComponent(Lab.sel))).json(); fetched = true; }
    catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  try { Lab.checked = (await (await apiGet("/api/checklist?race_id=" + encodeURIComponent(Lab.sel))).json()).checked || {}; }
  catch (e) { Lab.checked = {}; }
  if (stale("rules")) return;
  const keepDirty = !fetched && raceDirty();   // re-entered with unsaved edits → don't bless them
  if (!Lab.editDef.rules_profile || typeof Lab.editDef.rules_profile !== "object") Lab.editDef.rules_profile = {};
  if (!Array.isArray(Lab.editDef.rules_profile.modifications)) Lab.editDef.rules_profile.modifications = [];
  if (!Lab.editDef.rules_profile.scoring) Lab.editDef.rules_profile.scoring = {};
  if (!Array.isArray(Lab.editDef.requirements)) Lab.editDef.requirements = [];
  if (!keepDirty) markClean();   // after the shape fill-ins above, so they don't read as edits
  paintRules();
}

async function rulesPickRace(id) {
  if (!(await confirmLeave())) return paintRules();   // Stay → repaint puts the picker back
  Lab.sel = id; Lab.editDef = null; renderRules();
}
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
  const sent = JSON.stringify(Lab.editDef), ed = Lab.editDef;
  try {
    const r = await postDef("/api/races", { definition: JSON.parse(sent) });
    if (r.saved) markSaved(sent, ed);
    paintRules();
    const m2 = document.getElementById("rulesMsg");
    if (m2) m2.textContent = r.saved ? `Saved · ${(r.errors || []).length} error(s), ${(r.warnings || []).length} warning(s).` : "Save failed.";
    return !!r.saved;
  } catch (e) { const m2 = document.getElementById("rulesMsg"); if (m2) m2.textContent = "Save failed: " + e.message; return false; }
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
      ${ein(`requirements.${i}.text`, r.text, "item")}
      <button class="mini" onclick="reqRemove(${i})" title="remove">✕</button>
    </div>
    <div class="req-l2">
      ${esel(`requirements.${i}.category`, r.category, REQ_CATEGORIES)}
      ${esel(`requirements.${i}.phase`, r.phase, REQ_PHASES)}
      ${esel(`requirements.${i}.trigger_type`, r.trigger_type || "none", TRIGGER_TYPES)}
      ${ein(`requirements.${i}.trigger_detail`, r.trigger_detail, "when (detail)")}
      ${ein(`requirements.${i}.source`, r.source, "source §")}
      ${echk(`requirements.${i}.deliver_to_ipad`, r.deliver_to_ipad, "to iPad")}
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
      <h3>Rules &amp; Checklist</h3>
      <div class="opt-controls">
        <label>Race <select onchange="rulesPickRace(this.value)">
          ${Lab.races.map((x) => `<option value="${attr(x.race_id)}" ${x.race_id === Lab.sel ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
      </div>
    </div>

    <div class="card">
      <h3>Race rules <span class="muted" style="font-weight:400">— outside help, scoring, rule changes</span></h3>
      <div class="opt-controls">
        <label>Rules edition ${ein("rules_profile.rrs_edition", rp.rrs_edition, "2025-2028")}</label>
        <label>Tracker allowed on board? ${etri("rules_profile.tracker_permitted", rp.tracker_permitted)}</label>
      </div>
      <div class="dep-grid" style="margin-top:8px">
        ${echk("rules_profile.info_available_to_all_permitted", rp.info_available_to_all_permitted, "Public info allowed on board (rule 41)")}
        ${echk("rules_profile.customized_advice_while_underway_prohibited", rp.customized_advice_while_underway_prohibited, "No outside advice while racing")}
        ${echk("rules_profile.appendix_wp", rp.appendix_wp, "Waypoint racing rules apply (Appendix WP)")}
      </div>
      <div class="opt-controls" style="margin-top:8px">
        <label>Scoring system ${ein("rules_profile.scoring.system", sc.system, "ORC")}</label>
        <label>Method ${ein("rules_profile.scoring.method", sc.method, "Single-Number ToT")}</label>
        <label>Ref ${ein("rules_profile.scoring.ref", sc.ref, "NOR §13")}</label>
      </div>
      <div style="margin-top:10px"><b style="font-size:12px">Rule changes</b>
        <div class="muted" style="font-size:11px;margin:2px 0 6px">Reference and rule on top, full text below.</div>
        ${mods.length ? mods.map((m, i) => `<div class="req-block">
            <div class="req-l1">${ein(`rules_profile.modifications.${i}.ref`, m.ref, "ref (e.g. NOR §2.1)")}
              ${ein(`rules_profile.modifications.${i}.rule`, m.rule, "rule (e.g. RRS 41)")}
              <button class="mini" onclick="modRemove(${i})" title="remove">✕</button></div>
            <div style="margin-top:5px">${etxt(`rules_profile.modifications.${i}.summary`, m.summary, "what the change says")}</div>
          </div>`).join("")
          : `<div class="muted" style="font-size:12px">No rule changes.</div>`}
        <div style="margin-top:6px"><button class="mini" onclick="modAdd()">+ Add rule change</button></div>
      </div>
    </div>

    <div class="card">
      <h3>Checklist <span class="muted" style="font-weight:400">— ${reqs.length} items · ${ipadN} to iPad · ${critN} critical · ${doneN}/${reqs.length} checked</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:8px">The team's prep list. <b>To iPad</b> items pop up on the boat at the right time.</div>
      ${checklistHtml || '<div class="muted">No items yet.</div>'}
      <div style="margin-top:10px"><button class="mini" onclick="reqAdd()">+ Add item</button></div>
    </div>

    <div class="dactions"><button onclick="rulesSave()">Save rules &amp; checklist</button>
      <span id="rulesMsg" class="muted" style="font-size:12px"></span>
      <span class="muted" style="font-size:11px">(ticks save by themselves)</span></div>
  </div>`;
  syncDirty();
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
  if (!Mon.raceId) { view.innerHTML = '<div class="placeholder">No race yet. Add one in the Races tab.</div>'; return; }
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
  // Pill colour and age come from the NEWEST fix's measured age (backend `freshness`), not the
  // tracker's configured delay — a dead feed still "promises" ~15 min.
  const fmtAge = (s) => s < 3600 ? Math.round(s / 60) + " min" : s < 172800 ? (s / 3600).toFixed(s < 36000 ? 1 : 0) + " h" : Math.round(s / 86400) + " days";
  const flCls = { fresh: "ok", stale: "warn", very_stale: "bad" }[fl.freshness] || "warn";
  const flAge = fl.newest_age_s != null
    ? ` · latest position ${fmtAge(fl.newest_age_s)} old${fl.freshness === "fresh" ? "" : fl.freshness === "stale" ? " <span class=\"pill warn\">stale</span>" : " <span class=\"pill bad\">not live</span>"}`
    : " · <span class=\"pill warn\">position times unknown</span>";
  const flLine = fixes.length
    ? `<span class="pill ${flCls}">${fixes.length} boats</span> via ${esc(fl.provider || "tracker")}${flAge}${fl.delay_min ? ` <span class="muted">(tracker runs ~${fl.delay_min} min behind)</span>` : ""}${fl.demo || fl.provider === "sample" ? " <span class=\"pill warn\">demo</span>" : ""}`
    : `<span class="pill warn">no fleet</span> <span class="muted">${esc(fl.reason || "unavailable")}</span>`;
  const rivals = monRivals(fixes, own);
  const permNote = fl.onboard_permitted ? "allowed on board" : "shore only (not allowed on board)";

  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Monitor <span class="muted" style="font-weight:400">— live view from shore</span></h3>
      <div class="opt-controls">
        <label>Race <select onchange="monPickRace(this.value)">
          ${Lab.races.map((x) => `<option value="${attr(x.race_id)}" ${x.race_id === Mon.raceId ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
        <button class="mini" onclick="monRefresh()">↻ Refresh</button>
        <label class="req-flag"><input type="checkbox" ${Mon.auto ? "checked" : ""} onchange="monToggleAuto(this.checked)"> auto refresh (30 s)</label>
        <label class="req-flag"><input type="checkbox" ${Mon.demo ? "checked" : ""} onchange="monToggleDemo(this.checked)"> demo fleet</label>
        <span class="muted" style="font-size:11px">${Mon.lastAt ? "updated " + new Date(Mon.lastAt).toLocaleTimeString() : ""}</span>
      </div>
      <div class="dep-grid" style="margin-top:8px">
        <div class="dep-row"><b style="min-width:78px;display:inline-block">Our boat</b> ${ownLine}</div>
        <div class="dep-row"><b style="min-width:78px;display:inline-block">Fleet</b> ${flLine} <span class="muted">· ${esc(permNote)}</span></div>
      </div>
    </div>
    <section class="cockpit-map"><div id="monMap" class="routemap routemap-hero"></div></section>
    ${fixes.length ? `<div class="card"><h3>Fleet positions <span class="muted" style="font-weight:400">— from the public tracker</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:4px">Tap a row to find that boat on the map. ★ us · ● nearest rivals.</div>
      <table class="fleet-tbl mon-tbl"><thead><tr><th>Boat</th><th>SOG</th><th>COG</th><th>DTF</th><th>Age</th></tr></thead><tbody>
      ${fixes.map((f, i) => ({ f, i })).sort((a, b) => (b.f.own ? 1 : 0) - (a.f.own ? 1 : 0) || (a.f.dtf_nm ?? 1e9) - (b.f.dtf_nm ?? 1e9)).map(({ f, i }) => `<tr class="mon-row${f.own ? " own" : rivals.idx.has(i) ? " rival" : ""}" onclick="monFocus(${i})" title="Show on the map">
        <td>${f.own ? "★ " : rivals.idx.has(i) ? "● " : ""}${esc(f.name || "?")}${f.own ? ' <span class="pill warn">us</span>' : ""}</td><td>${f.sog != null ? f.sog.toFixed(1) + " kts" : ""}</td>
        <td>${f.cog != null ? Math.round(f.cog) + "°" : ""}</td><td>${f.dtf_nm != null ? f.dtf_nm.toFixed(1) + " nm" : ""}</td>
        <td>${ageMin(f.time) != null ? ageMin(f.time) + " min" : ""}</td></tr>`).join("")}
      </tbody></table></div>` : ""}
  </div>`;
  initMonitorMap();
}

// The map labels only what a shore watcher needs to find at a glance: our boat (live telemetry
// and its own tracker fix) and the MON_RIVALS boats nearest to it. Everyone else is a plain dot
// with a hover/tap tooltip until the view is zoomed in far enough that few boats remain; labels
// that would overlap one already placed are skipped (higher priority wins), so a finish-line
// cluster never turns into a pile of names.
const MON_RIVALS = 5;
const MON_LABEL_ALL_MAX = 15;     // label every boat in view once this few are in view
const MON_GOLD = "#f5c451", MON_BLUE = "#36b3ff";
const MON_TIP = { direction: "top", offset: [0, -8] };   // above the dot, clear of the side labels

function monDistNm(a, b) {
  const k = Math.cos(((a.lat + b.lat) / 2) * Math.PI / 180);
  return Math.hypot((a.lat - b.lat) * 60, (a.lon - b.lon) * 60 * k);
}

// Which boats are "nearest rivals", measured from our tracker fix when the feed has one (same
// delay as theirs — like with like), else from the live position.
function monRivals(fixes, own) {
  const ownFix = fixes.find((f) => f.own);
  const ref = ownFix || (own.available ? own : null);
  if (!ref) return { ref: null, idx: new Set() };
  const near = fixes.map((f, i) => ({ i, f })).filter((x) => !x.f.own)
    .map((x) => ({ i: x.i, d: monDistNm(ref, x.f) })).sort((a, b) => a.d - b.d).slice(0, MON_RIVALS);
  return { ref: ownFix ? "tracker" : "live", idx: new Set(near.map((x) => x.i)) };
}

function initMonitorMap() {
  if (Mon.map) { try { Mon.map.remove(); } catch (e) {} Mon.map = null; }
  const el = document.getElementById("monMap");
  if (!el || !window.L) return;
  const map = L.map(el, { preferCanvas: true }); Mon.map = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
  const data = Mon.data || {}, fl = data.fleet || {}, fixes = fl.fixes || [], own = data.own || {};
  const ownName = fl.own_name || "Our boat";
  const riv = Mon.rivals = monRivals(fixes, own);
  const pts = [];
  Mon.markers = [];
  // plain fleet first, rivals over them, our boat on top (canvas draws in insertion order)
  const order = fixes.map((f, i) => i).sort((a, b) =>
    (fixes[a].own ? 2 : riv.idx.has(a) ? 1 : 0) - (fixes[b].own ? 2 : riv.idx.has(b) ? 1 : 0));
  order.forEach((i) => {
    const f = fixes[i], rival = riv.idx.has(i);
    const style = f.own
      ? { radius: 7, color: MON_GOLD, weight: 3, fillColor: "#0b0b0e", fillOpacity: 0.9 }
      : rival ? { radius: 6, color: "#ffffff", weight: 1.5, fillColor: MON_BLUE, fillOpacity: 0.95 }
        : { radius: 4, color: MON_BLUE, weight: 1, fillColor: MON_BLUE, fillOpacity: 0.55 };
    Mon.markers[i] = L.circleMarker([f.lat, f.lon], style)
      .bindTooltip(`${esc(f.name || "?")}${f.own ? " (us, tracker)" : ""}${f.sog != null ? " · " + f.sog.toFixed(1) + " kts" : ""}`, MON_TIP).addTo(map);
    pts.push([f.lat, f.lon]);
  });
  if (own.available) {
    L.circleMarker([own.lat, own.lon], { radius: 16, color: MON_GOLD, weight: 1, fillColor: MON_GOLD, fillOpacity: 0.18, interactive: false }).addTo(map);
    Mon.ownMarker = L.circleMarker([own.lat, own.lon], { radius: 8, color: "#0b0b0e", fillColor: MON_GOLD, fillOpacity: 1, weight: 2 })
      .bindTooltip(`${esc(ownName)} (us, live)${own.sog != null ? " · " + own.sog.toFixed(1) + " kts" : ""}`, MON_TIP).addTo(map);
    pts.push([own.lat, own.lon]);
  } else Mon.ownMarker = null;
  Mon.labelLayer = L.layerGroup().addTo(map);
  monLegend(map, fixes, own, riv, ownName);
  map.on("zoomend moveend", monPlaceLabels);
  if (pts.length) map.fitBounds(pts, { padding: [30, 30], maxZoom: 11 });
  else map.setView([44.2, -82.5], 7);   // Lake Huron default
  monPlaceLabels();
}

function monLegend(map, fixes, own, riv, ownName) {
  const ctl = L.control({ position: "bottomleft" });
  const sw = (css) => `<span class="mon-sw" style="${css}"></span>`;
  const rows = [];
  if (own.available) rows.push(`${sw(`background:${MON_GOLD};border:2px solid #0b0b0e;box-shadow:0 0 0 4px rgba(245,196,81,.3)`)} ${esc(ownName)} — live`);
  if (fixes.some((f) => f.own)) rows.push(`${sw(`background:#0b0b0e;border:3px solid ${MON_GOLD}`)} ${esc(ownName)} — tracker`);
  if (riv.idx.size) rows.push(`${sw(`background:${MON_BLUE};border:1.5px solid #fff`)} ${riv.idx.size} nearest rivals${riv.ref === "live" ? " (to our live position)" : ""}`);
  if (fixes.filter((f) => !f.own).length > riv.idx.size) rows.push(`${sw(`background:${MON_BLUE};opacity:.6;width:8px;height:8px`)} rest of the fleet — zoom in for names`);
  if (!rows.length) return;
  ctl.onAdd = () => {
    const d = L.DomUtil.create("div", "mon-legend");
    d.innerHTML = rows.map((r) => `<div>${r}</div>`).join("");
    L.DomEvent.disableClickPropagation(d);
    return d;
  };
  ctl.addTo(map);
}

// Greedy label placement in screen space: own boat first, then rivals by distance, then (only
// when few boats are in view) everyone else. A label goes right of its dot, else left, else
// is skipped — the dot keeps its tooltip.
function monPlaceLabels() {
  const map = Mon.map;
  if (!map || !Mon.labelLayer) return;
  Mon.labelLayer.clearLayers();
  const data = Mon.data || {}, fl = data.fleet || {}, fixes = fl.fixes || [], own = data.own || {};
  const ownName = fl.own_name || "Our boat";
  const riv = Mon.rivals || { idx: new Set() };
  const size = map.getSize(), bounds = map.getBounds();
  const cands = [];
  if (own.available) cands.push({ lat: own.lat, lon: own.lon, text: ownName + " · live", cls: "own" });
  fixes.forEach((f, i) => { if (f.own) cands.push({ lat: f.lat, lon: f.lon, text: (f.name || ownName) + " · tracker", cls: "own" }); });
  riv.idx.forEach((i) => { const f = fixes[i]; cands.push({ lat: f.lat, lon: f.lon, text: f.name || "?", cls: "rival" }); });   // nearest first
  const inView = fixes.filter((f, i) => !f.own && !riv.idx.has(i) && bounds.contains([f.lat, f.lon]));
  const keyInView = cands.filter((c) => bounds.contains([c.lat, c.lon])).length;
  if (inView.length + keyInView <= MON_LABEL_ALL_MAX) inView.forEach((f) => cands.push({ lat: f.lat, lon: f.lon, text: f.name || "?", cls: "" }));
  const boxes = [];
  const hits = (b) => b.x0 < 0 || b.x1 > size.x || b.y0 < 0 || b.y1 > size.y
    || boxes.some((o) => b.x0 < o.x1 && b.x1 > o.x0 && b.y0 < o.y1 && b.y1 > o.y0);
  cands.forEach((c) => {
    const p = map.latLngToContainerPoint([c.lat, c.lon]);
    const w = c.text.length * (c.cls === "own" ? 7.2 : 6.4) + 12, h = 18, gap = c.cls === "own" ? 12 : 8;
    const right = { x0: p.x + gap, x1: p.x + gap + w, y0: p.y - h / 2, y1: p.y + h / 2 };
    const left = { x0: p.x - gap - w, x1: p.x - gap, y0: right.y0, y1: right.y1 };
    const side = !hits(right) ? "r" : !hits(left) ? "l" : (c.cls === "own" ? "r" : null);   // ours always shows
    if (!side) return;
    boxes.push(side === "r" ? right : left);
    L.marker([c.lat, c.lon], { interactive: false, keyboard: false,
      icon: L.divIcon({ className: "mon-lbl-wrap", iconSize: null, iconAnchor: [0, 0],
        html: `<span class="mon-lbl ${c.cls} ${side === "l" ? "left" : ""}" style="--gap:${gap}px">${esc(c.text)}</span>` }) })
      .addTo(Mon.labelLayer);
  });
}

// Table row → pan to that boat and ring it for a moment.
function monFocus(i) {
  const f = ((((Mon.data || {}).fleet) || {}).fixes || [])[i], map = Mon.map;
  if (!f || !map) return;
  const el = document.getElementById("monMap");
  if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
  map.setView([f.lat, f.lon], Math.max(map.getZoom(), 12));
  if (Mon.flash) { try { Mon.flash.remove(); } catch (e) {} }
  Mon.flash = L.circleMarker([f.lat, f.lon], { radius: 15, color: "#ffffff", weight: 3, fill: false, interactive: false }).addTo(map);
  const m = Mon.markers && Mon.markers[i];
  if (m) m.openTooltip();
  const ring = Mon.flash;
  setTimeout(() => { if (Mon.flash === ring) { try { ring.remove(); } catch (e) {} Mon.flash = null; } }, 3000);
}

/* ---------- Debrief (Lab-4 post-race judge loop) ---------- */
const Deb = { raceId: null, playbookId: null, playbooks: null, report: null, track: null,
  trackBusy: false, trackMsg: "", recordings: null, agent: null, job: null, poll: null, msg: "",
  viewing: null };
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
  if (!Deb.raceId) { view.innerHTML = '<div class="placeholder">No race yet. Add one in the Races tab.</div>'; return; }
  try { Deb.playbooks = ((await (await apiGet("/api/playbooks")).json()).playbooks || []).filter((b) => b.race_id === Deb.raceId); }
  catch (e) { Deb.playbooks = []; }
  if (stale("debrief")) return;
  if (!Deb.playbookId || !Deb.playbooks.some((b) => b.id === Deb.playbookId)) Deb.playbookId = (Deb.playbooks[0] || {}).id || null;
  try { Deb.track = await (await apiGet("/api/debrief/track?race_id=" + encodeURIComponent(Deb.raceId))).json(); }
  catch (e) { Deb.track = null; }
  await debLoadRecordings();
  if (stale("debrief")) return;
  try { Deb.job = await (await apiGet("/api/debrief/run/status")).json(); }
  catch (e) { Deb.job = null; }
  if (Deb.job && Deb.job.state === "running" && !Deb.poll) Deb.poll = setInterval(debPoll, 4000);
  await retroRefresh();
  try {
    Retro.job = await (await apiGet("/api/retro/run/status")).json();
    if (Retro.job.state === "running" && !Retro.poll) Retro.poll = setInterval(retroPoll, 8000);
  } catch (e) { /* card renders without job state */ }
  if (stale("debrief")) return;
  paintDebrief();
}

// Boat-track card: upload a GPX or fetch our YB track, then "Run debrief" scores helm vs optimal.
// Tracks that DON'T come off the boat's own log — a GPX export or the public YB feed. The boat's
// own recordings live in the recordings card above, which is the normal path.
function debTrackCard() {
  const t = Deb.track || {};
  const status = t.available
    ? `<span class="pill ok">track loaded</span> <span class="muted">${esc(t.source === "yb" ? "YB" : t.source === "boatlog" ? "boat log" : "GPX")}${t.boat ? " · " + esc(t.boat) : ""} · ${t.n} fixes${t.matched_by ? " · " + esc(t.matched_by) : ""}</span>`
    : `<span class="muted">No track yet. Upload a GPX or get it from YB.</span>`;
  return `<details class="card"><summary><b>Other track sources</b> <span class="muted" style="font-weight:400">— GPX file or YB tracker</span></summary>
    <div style="margin:4px 0 8px">${status} ${t.available ? '<button class="mini" onclick="debClearTrack()">Remove</button>' : ""}</div>
    <div class="opt-controls">
      <label>GPX <input type="file" id="debGpx" accept=".gpx,application/gpx+xml,text/xml"></label>
      <button onclick="debUploadGpx()" ${Deb.trackBusy ? "disabled" : ""}>Upload GPX</button>
      <span class="muted">or</span>
      <label>Our boat <input id="debBoat" placeholder="boat name on YB" style="width:200px"></label>
      <button onclick="debFetchYb()" ${Deb.trackBusy ? "disabled" : ""}>${Deb.trackBusy ? "Fetching…" : "Get from YB"}</button>
    </div>
    ${t.available && t.window ? `<div class="muted" style="font-size:12px;margin-top:4px">Window: <b>${esc(debWinRange(t.window))}</b> · ${t.window.kind === "derived" ? "from the recording" : "from the ⏺ LOG button"}${t.window.motion_device ? " · from the " + esc(t.window.motion_device) : ""}${(t.window.provenance || []).map((p) => "<br>· " + esc(p)).join("")}</div>` : ""}
    ${t.available && t.trust && t.trust.available && trustDanger((t.trust.summary || {}).danger_s, (t.trust.summary || {}).line)
      ? `<div style="margin-top:6px">${trustBanner("Instrument fault in this track", (t.trust.summary || {}).line, "Bad stretches are left out of the polar. Fix the calibration before the next race.")}</div>`
      : t.available && t.trust ? `<div class="muted" style="font-size:12px;margin-top:2px">Instrument check: ${t.trust.available ? `<b>${esc(((t.trust.summary || {}).line) || "done")}</b>. Bad stretches are left out.` : `<b>not run</b>. ${esc(t.trust.note || "")}`}</div>` : ""}
    <div class="muted" style="font-size:12px;margin-top:4px">GPX: export from Expedition, Vakaros, the instruments or a phone. YB: our track from the public tracker. ${Deb.trackMsg ? '<b>' + esc(Deb.trackMsg) + '</b>' : ""}</div>
  </div>`;
}

// The window a session will actually be loaded over: the record's, with the button's alongside
// whenever the two disagree (Jul 18: 7.36 h derived vs 1.81 h pressed).
function debWinRange(w) {
  const iso = (t) => new Date(t * 1000).toISOString().slice(11, 16) + "Z";
  return `${iso(w.start_ts)} → ${iso(w.end_ts)}` + (w.hours != null ? ` · ${w.hours} h` : "");
}

// ---- the recordings surface: what the boat sailed, and what has been debriefed ----------------
async function debLoadRecordings() {
  try {
    const r = await (await apiGet("/api/debrief/recordings?race_id=" + encodeURIComponent(Deb.raceId))).json();
    Deb.recordings = r.recordings || []; Deb.agent = r.agent || null;
  } catch (e) { Deb.recordings = []; Deb.agent = { available: false, note: "the Lab could not list recordings" }; }
  try { Deb.track = await (await apiGet("/api/debrief/track?race_id=" + encodeURIComponent(Deb.raceId))).json(); }
  catch (e) { Deb.track = null; }
}

// One button per recording: the job fetches the boat log for that session (if it isn't loaded
// already) and judges it. No ordering for the crew to get right.
async function debRunRecording(i) {
  const x = (Deb.recordings || [])[i];
  if (!x) return;
  Deb.msg = ""; Deb.report = null; Deb.viewing = null;
  const body = { race_id: Deb.raceId, playbook_id: Deb.playbookId || undefined,
                 recording: x.recording };
  if (x.session_id != null || x.start_ts != null) {
    body.session_id = x.session_id; body.start_ts = x.start_ts; body.name = x.name;
  }
  try {
    const r = await (await apiPost("/api/debrief/run", body)).json();
    if (!r.ok) { Deb.msg = r.note || r.detail || "could not start the debrief"; return paintDebrief(); }
    Deb.job = { state: "running", progress: [], race_id: Deb.raceId, recording: x.recording };
    if (Deb.poll) clearInterval(Deb.poll);
    Deb.poll = setInterval(debPoll, 4000);
  } catch (e) { Deb.msg = "could not start the debrief: " + (e.message || e); }
  paintDebrief();
}

async function debPoll() {
  try { Deb.job = await (await apiGet("/api/debrief/run/status")).json(); } catch (e) { return; }
  if (Deb.job.state !== "running") {
    if (Deb.poll) { clearInterval(Deb.poll); Deb.poll = null; }
    if (Deb.job.state === "done") { Deb.report = Deb.job.report; Deb.viewing = null; }
    await debLoadRecordings();
  }
  if (location.hash.replace("#", "") === "debrief") paintDebrief();
}

// A debrief already archived reopens from the record — re-running one is a choice, not the only
// way to see it again.
async function debOpenDebrief(id) {
  Deb.msg = ""; Deb.viewing = id; Deb.report = null; paintDebrief();
  try {
    const d = await (await apiGet("/api/learning/debriefs/" + id)).json();
    const rep = d.report || {};
    Deb.report = Object.assign({ available: true, race_name: d.race_name, archived_id: d.id,
                                 archived_at: d.created_at }, rep);
    if (Deb.report.actual_track && d.perf_bins) Deb.report.actual_track.n_bins = d.perf_bins.length;
  } catch (e) { Deb.msg = "could not open that debrief"; Deb.viewing = null; }
  paintDebrief();
}

function debRecordingsCard() {
  const rs = Deb.recordings;
  const j = Deb.job || {};
  const running = j.state === "running";
  const agentNote = Deb.agent && Deb.agent.available === false
    ? `<div class="banner warn" style="font-size:12px;margin-bottom:8px">${esc(Deb.agent.note || "the boat agent is unreachable")}</div>` : "";
  if (!rs) return `<div class="card"><h3>Recordings</h3><div class="loading">Loading…</div></div>`;
  const rows = rs.map((x, i) => {
    const d = x.debrief;
    const when = x.start_ts ? new Date(x.start_ts * 1000).toISOString().slice(0, 16).replace("T", " ") + "Z" : "—";
    const grew = x.marker_hours != null && x.hours != null && x.hours - x.marker_hours > 0.05
      ? ` <span class="muted" title="The ⏺ LOG button stopped early. The recording runs longer.">(button said ${x.marker_hours} h)</span>` : "";
    const state = d
      ? `<span class="pill ok">debriefed</span> <span class="muted">${new Date(d.created_at * 1000).toLocaleDateString()}${d.polar_pct != null ? " · " + d.polar_pct + "% of polar" : ""}${d.n_bins ? " · " + d.n_bins + " measured cells" : ""}${x.n_debriefs > 1 ? " · " + x.n_debriefs + " runs" : ""}</span>`
      : (x.track ? `<span class="pill">track loaded</span> <span class="muted">${x.track.n} fixes</span>`
                 : `<span class="muted">not debriefed yet</span>`);
    const isThis = running && j.recording != null && x.recording != null
      && Math.abs(j.recording - x.recording) < 1;
    const btns = running
      ? (isThis ? '<span class="muted">running…</span>' : "")
      : `<button class="mini" onclick="debRunRecording(${i})">${d ? "Re-run" : "Debrief"}</button>`
        + (d ? ` <button class="mini" onclick="debOpenDebrief(${d.id})">Open</button>` : "");
    return `<tr${isThis ? ' class="winrow"' : ""}>
      <td><b>${esc(x.name || "recording")}</b><div class="muted" style="font-size:11px">${when}${x.kind ? " · " + esc(x.kind) : ""}</div></td>
      <td>${x.hours != null ? x.hours + " h" : "—"}${grew}</td>
      <td>${state}</td>
      <td style="white-space:nowrap">${btns}</td></tr>`;
  }).join("");
  const prog = running
    ? `<div class="muted" style="font-size:12px;margin-top:6px">Debrief running… <span class="loading-dot"></span><br>${esc((j.progress || []).slice(-3).join(" · "))}</div>`
    : (j.state === "error" ? `<div class="banner warn" style="font-size:12px;margin-top:6px">Debrief failed: ${esc(j.error || "")}</div>` : "");
  return `<div class="card">
    <h3>Recordings <span class="muted" style="font-weight:400">— races the boat logged</span></h3>
    ${agentNote}
    ${rows ? `<table class="fleet-tbl"><thead><tr><th>Recording</th><th>Length</th><th>State</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
           : '<div class="placeholder">No recordings for this race yet.</div>'}
    ${prog}
    ${Deb.msg ? `<div class="muted" style="font-size:12px;margin-top:6px"><b>${esc(Deb.msg)}</b></div>` : ""}
    <div class="muted" style="font-size:11px;margin-top:6px">A debrief scores boat speed against the polar. With a frozen playbook it also checks the tactics.
      ${qhelp("Speed needs only the recording, and it teaches the boat model. Tactics says which side paid and how much time the plan lost.")}</div>
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
  await debLoadRecordings();
  paintDebrief();
}

async function debFetchYb() {
  const boat = document.getElementById("debBoat").value.trim();
  Deb.trackBusy = true; Deb.trackMsg = ""; paintDebrief();
  try {
    const r = await (await apiPost("/api/debrief/track/fetch", { race_id: Deb.raceId, boat })).json();
    Deb.trackMsg = r.ok ? `Fetched ${r.n} fixes for ${esc(r.boat || boat)} (${r.matched_by || "matched"}).`
      : (r.note || r.detail || "fetch failed") + (r.boats ? ". Boats: " + r.boats.slice(0, 20).join(", ") : "");
  } catch (e) { Deb.trackMsg = "fetch failed"; }
  Deb.trackBusy = false;
  await debLoadRecordings();
  paintDebrief();
}

async function debClearTrack() {
  await apiPost("/api/debrief/track/clear", { race_id: Deb.raceId });
  Deb.trackMsg = "Track removed.";
  await debLoadRecordings();
  paintDebrief();
}

async function debPickRace(id) {
  Deb.raceId = id; Lab.sel = id; Deb.report = null; Deb.viewing = null; Deb.playbookId = null;
  Deb.recordings = null; renderDebrief();
}

async function debApply() {
  const ta = document.getElementById("debLearn"); if (!ta) return;
  const msg = document.getElementById("debApplyMsg"); if (msg) msg.textContent = "Saving…";
  try {
    const r = await (await apiPost("/api/debrief/apply", { race_id: Deb.raceId, learnings: ta.value })).json();
    if (msg) msg.textContent = r.saved ? "Added to Learnings ✓" : ("Failed: " + (r.note || ""));
  } catch (e) { if (msg) msg.textContent = "Failed: " + e.message; }
}

function paintDebrief() {
  const r = Deb.report;
  const running = (Deb.job || {}).state === "running";
  const pbOpts = (Deb.playbooks || []).map((b) => `<option value="${attr(b.id)}" ${b.id === Deb.playbookId ? "selected" : ""}>${esc((b.signed ? "🔒 " : "") + (b.headline || b.id).slice(0, 56))} · ${b.n_variants} var</option>`).join("");
  let body = "";
  if (r && r.available) body = debReport(r);
  else if (r && !r.available) body = `<div class="card"><div class="placeholder">${esc(r.note || "debrief unavailable")}</div></div>`;

  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Debrief <span class="muted" style="font-weight:400">— how the race went</span></h3>
      <div class="opt-controls">
        <label>Race <select onchange="debPickRace(this.value)">
          ${Lab.races.map((x) => `<option value="${attr(x.race_id)}" ${x.race_id === Deb.raceId ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
        ${(Deb.playbooks || []).length
          ? `<label title="The plan to check tactics against. Optional.">Compare to plan <select onchange="Deb.playbookId=this.value" style="min-width:300px">${pbOpts}</select></label>`
          : `<span class="muted">No frozen playbook for this race. The debrief checks speed only.</span>`}
      </div>
    </div>
    ${debRecordingsCard()}
    ${body}
    ${debTrackCard()}
    ${retroCard()}
  </div>`;
  if (running && !Deb.poll) Deb.poll = setInterval(debPoll, 4000);
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
    ? `<div class="muted" style="font-size:12px;margin-top:6px">Scoring the fleet… <span class="loading-dot"></span><br>${esc((j.progress || []).slice(-3).join(" · "))}</div>`
    : (j.state === "done" && j.result ? `<div class="muted" style="font-size:12px;margin-top:6px"><b>Done:</b> ${j.result.ran} boats scored${(j.result.failed || []).length ? ", " + j.result.failed.length + " failed" : ""}.</div>`
       : (j.state === "error" ? `<div class="muted" style="font-size:12px;margin-top:6px"><b>Error:</b> ${esc(j.error || "")}</div>` : ""));
  return `<div class="card">
    <h3>Fleet retro <span class="muted" style="font-weight:400">— how every boat sailed a past race</span></h3>
    <div style="margin:4px 0 8px">${archived}</div>
    <div class="opt-controls">
      <label>YB race id <input id="retroYb" value="${attr(Retro.ybId)}" style="width:170px"></label>
      <button class="mini" onclick="retroIngest()" ${Retro.busy ? "disabled" : ""}>1 · Get race</button>
      <button class="mini" onclick="retroPolars()" ${Retro.busy ? "disabled" : ""}>2 · Find ORC polars</button>
      <button class="mini" onclick="retroRun()" ${Retro.busy || running ? "disabled" : ""}>${running ? "Scoring…" : "3 · Score the fleet"}</button>
      <button class="mini" onclick="retroReport()" ${Retro.busy ? "disabled" : ""}>4 · Report</button>
      ${Retro.msg ? `<span class="muted" style="font-size:12px">${esc(Retro.msg)}</span>` : ""}
    </div>
    ${jobLine}
    ${retroReportHtml()}
    <div class="muted" style="font-size:12px;margin-top:6px">Compares each boat's line and speed with its best route. Shows patterns, not proof.
      ${qhelp("Each boat gets its own ORC polar and the forecast at its own start. A good crew both routes well and sails fast. Comparisons run within each division to reduce rating luck. Everything is kept in the retro archive.")}</div>
  </div>`;
}

function retroReportHtml() {
  const r = Retro.report;
  if (!r) return "";
  if (!r.ok) return `<div class="placeholder" style="margin-top:8px">${esc(r.note || "no report")}</div>`;
  const rho = (v) => v === null || v === undefined ? "—" : `<b style="color:${v > 0.25 ? "var(--ok, #2e7d32)" : (v < -0.25 ? "var(--warn, #b26a00)" : "inherit")}">${v}</b>`;
  const pooled = r.pooled ? `<div style="margin:6px 0"><b>All boats (${r.pooled.n}):</b>
      time behind best route ρ=${rho(r.pooled.rho_behind_min)} ·
      distance off best route ρ=${rho(r.pooled.rho_xte)} ·
      % of polar ρ=${rho(r.pooled.rho_polar_pct)}
      <span class="muted" style="font-size:12px">(ρ above 0 for the first two: boats closer to the best route finished higher. ρ below 0 for polar: faster boats finished higher.)</span></div>` : "";
  const divs = (r.divisions || []).map((d) => `
    <details style="margin-top:6px"><summary><b>${esc(d.division)}</b> — n=${d.n} ·
      behind ρ=${rho(d.rho_behind_min)} · off route ρ=${rho(d.rho_xte)} · extra distance ρ=${rho(d.rho_extra_distance)} · polar ρ=${rho(d.rho_polar_pct)}
      · top third went: ${esc(Object.entries(d.top_third_sides || {}).map(([s, n]) => s + "×" + n).join(", ") || "—")}</summary>
      <table class="tbl" style="margin-top:4px"><tr><th>#</th><th>boat</th><th>behind best route</th><th>off route (avg)</th><th>extra distance</th><th>polar %</th><th>side</th></tr>
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
  Retro.busy = true; Retro.msg = "getting tracks and results…"; paintDebrief();
  try {
    const r = await (await api("/api/retro/ingest", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ race_id: Retro.ybId }) })).json();
    Retro.msg = r.ok ? `got ${r.tracks} tracks, ${r.results} results, ${r.divisions} divisions` : (r.note || r.detail || "couldn't get the race");
  } catch (e) { Retro.msg = "couldn't get the race"; }
  Retro.busy = false; await retroRefresh(); paintDebrief();
}
async function retroPolars() {
  Retro.ybId = document.getElementById("retroYb").value.trim();
  Retro.busy = true; Retro.msg = "finding ORC polars (USA + CAN)…"; paintDebrief();
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
  } catch (e) { Retro.msg = "couldn't start scoring"; }
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
    return `<div class="card"><h3>Speed vs polar</h3><div class="placeholder">${esc(at.note || "No track scored for this recording.")}</div></div>`;
  }
  const tb = at.time_behind_optimal_min;
  const tbTxt = tb == null ? "—" : (tb >= 0 ? tb + " min behind" : Math.abs(tb) + " min faster");
  const row = (label, val) => `<div class="dep-row"><b style="min-width:150px;display:inline-block">${label}</b> ${val}</div>`;
  // >100% can be real (soft rating / sailing above the cert) once the current is removed — colour it
  // ok when current-corrected, warn when not (likely a fair tide), and a runaway >120 always warns.
  const over = at.polar_pct > 108;
  const polCls = at.polar_pct == null ? "" : (at.polar_pct > 120 ? "warn" : over ? (at.current_corrected ? "ok" : "warn") : (at.polar_pct >= 90 ? "ok" : "bad"));
  const overNote = over ? (at.current_corrected ? " · over 100%: faster than the rating" : " · over 100%: probably current") : "";
  const pol = at.polar_pct == null ? null
    : `<span class="pill ${polCls}">${at.polar_pct}% of polar</span> <span class="muted">(${at.polar_samples} samples${overNote})</span>`;
  const ccNote = at.current_corrected
    ? `<span class="muted"> · current removed (avg ${at.current_mean_kn ?? "?"} kts)</span>`
    : (at.polar_pct != null ? '<span class="muted"> · current not removed</span>' : "");
  // helm_pct = flat-water-equivalent (sea-state loss removed) — the number the learning loop refines
  // helm_factor from. Show it when the seaway materially depressed the raw polar%.
  const helmGap = (at.wave_corrected && at.helm_pct != null && at.polar_pct != null) ? (at.helm_pct - at.polar_pct) : 0;
  const helmRow = helmGap >= 3
    ? row("Helm (waves removed)", `<span class="pill ok">${at.helm_pct}%</span> <span class="muted">waves ~${at.sea_state_hs_mean ?? "?"} m cost ~${helmGap} pts. This is the crew's number.</span>`)
    : "";
  const cav = (at.caveats || []).length
    ? `<div class="banner warn" style="margin-top:8px;font-size:12px">${at.caveats.map(esc).join("<br>")}</div>` : "";
  return `<div class="card">
    <h3>Speed vs polar <span class="muted" style="font-weight:400">— ${esc(at.source === "yb" ? "YB track" : at.source === "boatlog" ? "the boat's own log" : "GPX track")}${at.boat ? " · " + esc(at.boat) : ""} · ${at.fixes_scored}/${at.fixes_total} fixes used${(at.perf_bins || at.n_bins) ? " · " + ((at.perf_bins || []).length || at.n_bins) + " measured cells saved" : ""}${(at.trust || {}).refused_samples ? " · " + at.trust.refused_samples + " left out (" + esc((at.trust.refused_channels || []).join(", ")) + ")" : ""}</span></h3>
    <div class="dep-grid">
      ${at.oracle_hours != null ? row("Vs best route", `${tbTxt} <span class="muted">(sailed ${at.elapsed_hours != null ? at.elapsed_hours.toFixed(1) + " h" : "—"}, best route ${at.oracle_hours.toFixed(1)} h)</span>`)
        : row("Sailed", `${at.elapsed_hours != null ? at.elapsed_hours.toFixed(1) + " h" : "—"} <span class="muted">(no playbook, so no best route to compare)</span>`)}
      ${row("Distance sailed", `${at.sailed_nm} nm` + (at.extra_distance_pct != null ? ` · <b>${at.extra_distance_pct}% more</b> than the best route${at.optimal_nm != null ? " (" + at.optimal_nm + " nm)" : ""}` : "") + (at.rhumb_nm ? ` <span class="muted">(direct ${at.rhumb_nm} nm)</span>` : ""))}
      ${at.xte_mean_nm != null ? row("Off the best route", `avg ${at.xte_mean_nm} nm · mostly within ${at.xte_p90_nm} nm · max ${at.xte_max_nm} nm`) : ""}
      ${at.side_worked ? row("First beat side", `<b>${esc(at.side_worked)}</b>`) : ""}
      ${pol ? row("% of polar", pol + ccNote) : ""}
      ${helmRow}
    </div>
    ${cav}
    <div class="muted" style="font-size:11px;margin-top:6px">Nobody sails the best route exactly. Use these for coaching.
      ${qhelp('Extra distance and distance off route = steering and tactics. % of polar = helm and trim. The measured cells feed <a href="#learnings">Learnings → Refine the boat model</a>.')}</div>
  </div>`;
}

function debReport(r) {
  const c = r.critique || {};
  const w = r.recording || (r.actual_track || {}).window || null;
  const head = `<div class="card">
    <h3>${esc(r.race_name || "Debrief")} <span class="muted" style="font-weight:400">— ${w && w.start_ts ? esc(new Date(w.start_ts * 1000).toISOString().slice(0, 16).replace("T", " ") + "Z") + (w.hours != null ? " · " + w.hours + " h" : "") : "loaded track"}</span></h3>
    ${r.archived_id ? `<div class="muted" style="font-size:12px">Saved debrief #${r.archived_id}${r.archived_at ? " · " + new Date(r.archived_at * 1000).toLocaleString() : ""}.</div>` : ""}
    ${r.tactics_available ? "" : `<div class="banner" style="font-size:12px;margin-top:6px"><b>Speed only.</b> ${esc(r.tactics_note || "No frozen playbook, so tactics aren't checked.")}</div>`}
  </div>`;
  return head + (r.tactics_available ? debTacticsCard(r) : "") + `
    <div class="card">
      <h3>Coach's critique <span class="muted" style="font-weight:400">— ${esc(c.model || "built-in")}</span></h3>
      <p style="margin:4px 0">${esc(c.assessment || "")}</p>
      ${c.key_lesson ? `<p style="margin:4px 0"><b>Lesson:</b> ${esc(c.key_lesson)}</p>` : ""}
      ${c.brain_edit ? `<p style="margin:4px 0"><b>Change for the boat's advisor:</b> ${esc(c.brain_edit)}</p>` : ""}
      ${c.boat_model_note ? `<p style="margin:4px 0"><b>Boat model:</b> ${esc(c.boat_model_note)}</p>` : ""}
    </div>
    ${debTrackScore(r.actual_track || {})}
    <div class="card">
      <h3>Save to Learnings <span class="muted" style="font-weight:400">— check it, then add it</span></h3>
      <textarea id="debLearn" rows="3" style="width:100%;box-sizing:border-box">${esc(c.proposed_learnings || "")}</textarea>
      <div style="margin-top:8px"><button onclick="debApply()">Add to Learnings</button> <span id="debApplyMsg" class="muted" style="font-size:12px"></span></div>
    </div>`;
}

// The tactics half — only when a frozen playbook gave the oracle something to judge against.
function debTacticsCard(r) {
  const reg = r.regret, o = r.oracle, pb = r.playbook;
  const pill = reg.side_matched ? '<span class="pill ok">side matched</span>' : '<span class="pill warn">side missed</span>';
  const regMin = reg.minutes != null ? (reg.minutes >= 0 ? reg.minutes + " min slower than the best route" : Math.abs(reg.minutes) + " min faster (plan beat the best route)") : "—";
  const vrows = (pb.variants || []).map((v) => `<tr class="${v.side === reg.side_paid ? "winrow" : ""}">
      <td>${esc(v.side || "?")}${v.side === pb.recommended ? " ★" : ""}</td>
      <td>${v.total_hours != null ? v.total_hours.toFixed(1) + " h" : ""}</td>
      <td>${v.share != null ? Math.round(v.share * 100) + "%" : ""}</td>
      <td>${v.side === reg.side_paid ? "✓ paid" : ""}</td></tr>`).join("");
  return `<div class="card">
      <h3>Tactics ${pill}</h3>
      <div class="dep-grid">
        <div class="dep-row"><b style="min-width:130px;display:inline-block">Side that paid</b> <b>${esc(reg.side_paid)}</b> · recommended <b>${esc(reg.recommended_side || "—")}</b> ${reg.side_matched ? "(matched)" : "(missed)"}</div>
        <div class="dep-row"><b style="min-width:130px;display:inline-block">Best route</b> ${o.total_hours != null ? o.total_hours.toFixed(1) + " h" : "—"} · plan predicted ${pb.predicted_hours != null ? pb.predicted_hours.toFixed(1) + " h" : "—"}</div>
        <div class="dep-row"><b style="min-width:130px;display:inline-block">Time lost</b> ${esc(regMin)}</div>
      </div>
      <table class="fleet-tbl" style="margin-top:8px"><thead><tr><th>Option</th><th>Predicted</th><th>Forecasts</th><th>Outcome</th></tr></thead><tbody>${vrows}</tbody></table>
      <div class="muted" style="font-size:11px;margin-top:6px">★ = recommended · highlighted = the side that paid. ${esc(r.caveat || "")}</div>
    </div>`;
}

/* ---------- Learnings (boat-level library + per-regatta notes) ---------- */
async function renderLearnings() {
  const view = document.getElementById("view");
  view.innerHTML = '<div class="loading">Loading learnings…</div>';
  try {
    await reloadBoats();
    bmLoaded(await (await apiGet("/api/crossovers")).json());
    Opt.polarGrid = await (await apiGet("/api/polars")).json();
    if (!Lab.races) Lab.races = (await (await apiGet("/api/races")).json()).races || [];
  } catch (e) { view.innerHTML = '<div class="placeholder">Failed to load learnings.</div>'; return; }
  if (!Lab.sel && Lab.races.length) Lab.sel = Lab.races[0].race_id;
  let fetched = false;
  if (Lab.sel && (!Lab.editDef || Lab.editDef.race_id !== Lab.sel)) {
    try { Lab.editDef = await (await apiGet("/api/races/" + encodeURIComponent(Lab.sel))).json(); fetched = true; } catch (e) {}
  }
  await learnReload();
  if (stale("learnings")) return;
  // a boat-model save repaints via renderLearnings() — only a fresh load is a clean baseline
  if (fetched || !raceDirty()) markClean();
  paintLearnings();
}

async function learnPickRace(id) {
  if (!(await confirmLeave())) return paintLearnings();   // Stay → repaint puts the picker back
  Lab.sel = id; Lab.editDef = null; renderLearnings();
}
async function learnSaveNotes() {
  if (!Lab.editDef) return false;
  const ta = document.getElementById("learnNotes");
  if (ta) Lab.editDef.learnings_notes = ta.value;
  const msg = document.getElementById("learnMsg"); if (msg) msg.textContent = "Saving…";
  const sent = JSON.stringify(Lab.editDef), ed = Lab.editDef;
  try {
    const r = await postDef("/api/races", { definition: JSON.parse(sent) });
    if (r.saved) markSaved(sent, ed);
    if (msg) msg.textContent = r.saved ? "Saved." : "Save failed.";
    return !!r.saved;
  } catch (e) { if (msg) msg.textContent = "Save failed: " + e.message; return false; }
}

function paintLearnings() {
  const ab = (Opt.boats || []).find((b) => b.boat_id === Opt.activeBoat) || {};
  const m = Opt.boatModel;
  const card = (m && m.crossovers) ? boatModelCard(m) : `<div class="card"><div class="placeholder">Boat model unavailable.</div></div>`;
  const d = Lab.editDef || {};
  document.getElementById("view").innerHTML = `<div class="opt">
    <div class="card">
      <h3>Learnings <span class="muted" style="font-weight:400">— what we know about the boat and venue</span></h3>
      <div class="muted" style="font-size:12px">Boat: <b>${esc(ab.name || Opt.activeBoat || "—")}</b>${ab.draft_ft != null ? " · draft " + ab.draft_ft + " ft" : ""} · change it in <a href="#gameplan">Gameplan</a>. The route uses the speeds and sails below.</div>
    </div>
    ${card}
    <div class="card">
      <h3>Notes for this race</h3>
      <div class="opt-controls">
        <label>Race <select onchange="learnPickRace(this.value)">
          ${(Lab.races || []).map((x) => `<option value="${attr(x.race_id)}" ${x.race_id === Lab.sel ? "selected" : ""}>${esc(x.name || x.race_id)}</option>`).join("")}
        </select></label>
      </div>
      <div class="muted" style="font-size:12px;margin:8px 0 4px">Venue tips, calibration reminders, anything for this race.</div>
      <textarea id="learnNotes" rows="6" oninput="if (Lab.editDef) Lab.editDef.learnings_notes = this.value" style="width:100%;box-sizing:border-box" placeholder="e.g. Cove Island current sets NE on the ebb; J2 crossover felt early last year — try 15 kts; main calibration +2° at the masthead…">${esc(d.learnings_notes || "")}</textarea>
      <div style="margin-top:8px"><button onclick="learnSaveNotes()">Save notes</button> <span id="learnMsg" class="muted" style="font-size:12px"></span></div>
    </div>
    ${learnRefineCard(ab)}
    ${learnWaveCard(ab)}
    ${learnTrendCard() + learnConfigCard()}
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
    ? `<div class="muted" style="font-size:12px">In use: helm <b>${ab.helm_factor != null ? ab.helm_factor : 1}</b>${ab.helm_factor > 1 ? " (faster than the rating)" : ""}${adjN ? ` · <b>${adjN}</b> polar tweaks` : " · no polar tweaks"}.</div>` : "";
  let body;
  if (L.busy) body = '<div class="loading">Working…</div>';
  else if (pending) body = learnProposalReview(pending);
  else body = `<div class="muted" style="font-size:12px">Suggest changes to <b>${esc(ab.name || "this boat")}</b>'s boat model from past debriefs. <b>Nothing changes until you approve.</b></div>
    <div style="margin-top:8px"><button onclick="learnPropose()">Suggest changes</button> ${L.msg ? `<span class="muted" style="font-size:12px">${esc(L.msg)}</span>` : ""}</div>`;
  return `<div class="card">
    <h3>Refine the boat model <span class="muted" style="font-weight:400">— you approve every change</span></h3>
    ${applied}
    ${body}
    <div class="muted" style="font-size:11px;margin-top:8px">The ORC polar stays the base. Approved changes sit on top.
      ${qhelp("Helm sets the overall speed level. Polar tweaks show where the boat is fast or slow at a given wind speed and angle.")}</div>
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
      <div class="dep-row"><b style="min-width:150px;display:inline-block">Overall</b> ${s.overall_pct}% of polar over ${s.n_samples} samples · ${s.recordings ? s.recordings.length + " recording(s) of " : ""}${(s.races || []).length} race(s)${s.excluded_forecast_bins ? ` · <span class="muted">${s.excluded_forecast_bins} from forecasts left out</span>` : ""}${s.thin_cells_skipped ? ` · <span class="muted">${s.thin_cells_skipped} cell(s) under ${s.min_cell_samples} samples skipped</span>` : ""}</div>
      <div class="dep-row"><b style="min-width:150px;display:inline-block">Helm</b> ${p.helm_current} → <b>${p.helm_proposed}</b>${p.helm_proposed > 1 ? ' <span class="muted">(faster than the rating)</span>' : ""} <input id="learnHelmEdit" type="number" step="0.01" min="0.5" max="1.2" value="${p.helm_proposed}" style="width:70px;margin-left:8px"> <span class="muted">(you can edit)</span></div>
      ${Object.keys(pos).length ? `<div class="dep-row"><b style="min-width:150px;display:inline-block">By point of sail</b> ${Object.entries(pos).map(([k, v]) => `${k} ${v}%`).join(" · ")}</div>` : ""}
    </div>
    ${rows ? `<div class="muted" style="font-size:12px;margin-bottom:4px">Suggested polar tweaks. Untick any you don't want:</div>
    <table class="fleet-tbl" id="learnAdjTbl"><thead><tr><th></th><th>TWS</th><th>TWA</th><th>Point of sail</th><th>Change</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table>`
      : '<div class="muted" style="font-size:12px">No polar tweaks. The boat matches the polar shape. Helm only.</div>'}
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
    ? `<div class="muted" style="font-size:12px">In use, speed loss per metre of wave: upwind <b>${wc.k_up}</b> · reach <b>${wc.k_reach}</b> · run <b>${wc.k_down}</b> (counts above ${wc.hs_deadband} m, floor ${wc.floor}).</div>`
    : `<div class="muted" style="font-size:12px">Using default wave settings. Fit them to this boat below.</div>`;
  let body;
  if (L.waveBusy) body = '<div class="loading">Fitting…</div>';
  else if (pending) body = learnWaveReview(pending);
  else body = `<div class="muted" style="font-size:12px">Works out how much waves slow the boat, from past debriefs. Needs races in different wave heights. <b>Nothing changes until you approve.</b></div>
    <div style="margin-top:8px"><button onclick="learnCalibrateWaves()">Fit from past races</button> ${L.waveMsg ? `<span class="muted" style="font-size:12px">${esc(L.waveMsg)}</span>` : ""}</div>`;
  return `<div class="card">
    <h3>Wave model <span class="muted" style="font-weight:400">— you approve every change</span></h3>
    ${appliedTxt}
    ${body}
    <div class="muted" style="font-size:11px;margin-top:8px">Keeps wave losses out of the helm number.
      ${qhelp("Waves are counted here, so helm stays a flat-water number. Each point of sail is fit on its own and kept within safe limits. One with no range of wave heights keeps its default.")}</div>
  </div>`;
}

function learnWaveReview(p) {
  const s = p.summary || {};
  const pos = s.by_point_of_sail || {};
  const prop = p.wave || {};
  const label = { upwind: "Upwind", reaching: "Reaching", downwind: "Running" };
  const rows = Object.entries(pos).map(([k, d]) => `<tr>
    <td>${esc(label[k] || k)}</td>
    <td>${d.k_current}</td>
    <td><b class="conf ${d.confidence > 0 ? "ok" : ""}">${d.k_proposed}</b>${d.confidence > 0 ? "" : ' <span class="muted">(kept)</span>'}</td>
    <td>${d.n_cells} / ${d.hs_spread} m</td>
    <td>${d.confidence != null ? Math.round(d.confidence * 100) + "%" : "—"}${d.r2 != null ? ` · r²${d.r2}` : ""}</td>
    <td class="muted" style="font-size:11px">${esc(d.note || (d.helm_level != null ? "helm level " + d.helm_level : ""))}</td></tr>`).join("");
  const db = s.deadband || {};
  const dbRow = db.proposed != null
    ? `<div class="dep-row" style="margin:2px 0"><b style="min-width:150px;display:inline-block">Waves count above</b> ${db.current} → <b>${db.proposed}</b> m <span class="muted">(${esc(db.source === "fit" ? "from the data" : "default")}${db.floor != null ? ` · floor ${db.floor}` : ""})</span></div>`
    : "";
  return `<div class="muted" style="font-size:12px;margin:4px 0">From ${p.n_bins} cells in ${(s.races || []).length} race(s). Suggested speed loss per metre of wave:</div>
    ${dbRow}
    <table class="fleet-tbl"><thead><tr><th>Point of sail</th><th>Now</th><th>Proposed</th><th>Cells / wave range</th><th>Confidence</th><th></th></tr></thead><tbody>${rows}</tbody></table>
    <div style="margin-top:10px">
      <button onclick="learnApplyWave(${p.id})">✓ Approve &amp; apply</button>
      <button class="mini" onclick="learnReject(${p.id})">Reject</button>
      <span id="learnWaveMsg" class="muted" style="font-size:12px"></span>
    </div>`;
}

// Lab-4 condition attribution: multi-race trend — see the model improving over a season.
/* Observed polars BY SAIL CONFIGURATION — the innovation record: the crew's sails-bar log
   attributes every fix of a boat-log debrief to its configuration, so combinations the
   crossover chart doesn't rate (C0+J2, kite+staysail…) accumulate their own observed curves. */
function learnConfigCard() {
  const cp = (Lab.learning || {}).configPolars;
  const inner = !cp ? '<div class="muted">Loading…</div>'
    : !(cp.configs || []).length
      ? '<div class="muted" style="font-size:12px">Log sail changes, then run a boat-log debrief.</div>'
      : cp.configs.map((g) => `<details style="margin-bottom:6px"><summary style="cursor:pointer">
          <b>${esc(g.config)}</b> <span class="muted">· ${g.n_cells} cell${g.n_cells === 1 ? "" : "s"} · ${g.samples} samples · ${g.races} race${g.races === 1 ? "" : "s"}</span></summary>
          <table style="margin-top:6px"><thead><tr><th>TWS</th><th>TWA</th><th>best speed</th><th>vs rating</th><th>n</th></tr></thead>
          <tbody>${g.cells.map((c) => `<tr><td>${c.tws}</td><td>${c.twa}°</td><td><b>${c.best_stw}</b> kts</td><td class="muted">${c.target_stw} (${c.pct}%)</td><td class="muted">${c.samples}</td></tr>`).join("")}</tbody></table>
        </details>`).join("");
  return `<div class="card">
    <h3>Speed by sail combination <span class="muted" style="font-weight:400">— from the CURRENT SAILS log</span></h3>
    ${inner}
    ${cp && (cp.configs || []).length ? `<div class="muted" style="font-size:11px;margin-top:6px">${esc(cp.note || "")}</div>` : ""}
  </div>`;
}

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
    const what = a.wave_coeffs ? "wave model" : (a.helm_factor != null ? `helm ${a.helm_factor}` + ((a.polar_adjustments || []).length ? ` + ${a.polar_adjustments.length} polar cells` : "") : "refinement");
    const when = m.decided_at ? new Date(m.decided_at * 1000).toLocaleDateString() : "";
    return `<li>${when} — approved <b>${esc(what)}</b></li>`;
  }).join("");
  return `<div class="card">
    <h3>Performance trend <span class="muted" style="font-weight:400">— ${s.length} race(s), oldest → newest</span></h3>
    <table class="fleet-tbl"><thead><tr><th>Date</th><th>Race</th><th>Helm (no waves)</th><th>% of polar</th><th>Behind best route</th><th>Time lost</th><th>Waves</th></tr></thead><tbody>${rows}</tbody></table>
    ${ms ? `<div class="muted" style="font-size:12px;margin-top:8px">Applied refinements:</div><ul class="muted" style="font-size:12px;margin:4px 0">${ms}</ul>` : ""}
    <div class="muted" style="font-size:11px;margin-top:6px">Helm should go up and time lost should go down as changes land.
      ${qhelp("Helm has the wave loss removed. % of polar still includes waves. The gap between them is the conditions.")}</div>
  </div>`;
}

function learnArchiveCard() {
  const dbs = (Lab.learning || {}).debriefs || [];
  if (!dbs.length) return `<div class="card"><h3>Performance archive</h3><div class="muted" style="font-size:12px">No debriefs yet. Run one in the <a href="#debrief">Debrief</a> tab.</div></div>`;
  const rows = dbs.map((d) => `<tr>
    <td>${d.created_at ? new Date(d.created_at * 1000).toLocaleDateString() : ""}</td>
    <td>${esc(d.race_name || d.race_id || "")}${d.window_start ? `<div class="muted" style="font-size:11px">recording ${new Date(d.window_start * 1000).toISOString().slice(0, 16).replace("T", " ")}Z${d.elapsed_hours != null ? " · " + d.elapsed_hours + " h" : ""}</div>` : ""}</td>
    <td>${d.regret_min != null ? d.regret_min + " min" : "—"}</td>
    <td>${d.time_behind_min != null ? d.time_behind_min + " min" : "—"}</td>
    <td>${d.oversail_pct != null ? d.oversail_pct + "%" : "—"}</td>
    <td>${d.polar_pct != null ? d.polar_pct + "%" : "—"}</td>
    <td>${d.helm_pct != null ? d.helm_pct + "%" : "—"}</td>
    <td>${esc(d.side_worked || "—")}${d.side_matched != null ? (d.side_matched ? " ✓" : " ✗") : ""}</td>
    <td>${esc(d.track_source || "—")}</td></tr>`).join("");
  return `<div class="card">
    <h3>Performance archive <span class="muted" style="font-weight:400">— ${dbs.length} debrief(s)</span></h3>
    <table class="fleet-tbl"><thead><tr><th>Date</th><th>Race</th><th>Time lost</th><th>Behind best route</th><th>Extra distance</th><th>% of polar</th><th>Helm %</th><th>Side</th><th>Track</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="muted" style="font-size:11px;margin-top:6px">Every debrief is kept here for the season.</div>
  </div>`;
}

async function learnPropose() {
  Lab.learning = Object.assign({}, Lab.learning, { busy: true, msg: "" }); paintLearnings();
  try {
    const r = await (await apiPost("/api/learning/propose", {})).json();
    if (!r.ok) Lab.learning = Object.assign({}, Lab.learning, { busy: false, msg: r.note || "no proposal" });
    else await learnReload();
  } catch (e) { Lab.learning = Object.assign({}, Lab.learning, { busy: false, msg: "couldn't suggest changes" }); }
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
  } catch (e) { Lab.learning = Object.assign({}, Lab.learning, { waveBusy: false, waveMsg: "fit failed" }); }
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
    const [props, dbs, trend, cfg] = await Promise.all([
      (await apiGet("/api/learning/proposals")).json(),
      (await apiGet("/api/learning/debriefs")).json(),
      (await apiGet("/api/learning/trend")).json(),
      (await apiGet("/api/learning/config-polars")).json(),
    ]);
    Lab.learning = { busy: false, msg: "", waveBusy: false, waveMsg: "",
      proposals: props.proposals || [], debriefs: dbs.debriefs || [], trend,
      configPolars: cfg };
  } catch (e) { Lab.learning = Object.assign({ busy: false }, Lab.learning); }
}

function optDegradedBanner(r) {
  const w = r.warnings || [];
  if (!w.length) return "";
  const cls = r.degraded ? "bad" : "warn";
  const head = r.degraded ? "⚠ Weak forecast. Read this before using the route." : "⚠ Notes";
  // when degraded, surface the common-error checklist inline so the cause is actionable (2.5)
  const checklist = r.degraded ? `<div style="margin-top:6px;font-size:12px;font-weight:400">
    <b>What usually fixes it:</b>
    <ul style="margin:3px 0 0 16px;padding:0">
      <li>The newest forecast may not be out yet. Try again in 30 min, or add GFS/NAM.</li>
      <li>Uncheck ECMWF if it's slow.</li>
      <li>HRRR only covers 48 h. For a long race, use GFS/NAM.</li>
      <li>If a Fine run ran out of time, set Detail to <b>Auto</b> or <b>Fast</b>.</li>
    </ul></div>` : "";
  return `<div class="pill ${cls}" style="display:block;margin-bottom:8px;text-align:left">
    <b>${head}</b><ul style="margin:4px 0 0 16px;padding:0">${w.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>${checklist}</div>`;
}

function optObstacleNote(r) {
  const ob = r.obstacles || {};
  if (!ob.active) return '<div class="muted" style="font-size:12px;margin-top:6px">Land avoidance is off. The route may cross land.</div>';
  const L = ob.layers || {};
  const steps = r.obstacle_steps_avoided || 0;
  const boat = r.boat ? esc(r.boat.name) : "boat";
  if (ob.source === "enc") {
    return `<div class="muted" style="font-size:12px;margin-top:6px">⚓ <b>NOAA charts</b>: the route avoids land, shoals and obstructions.
      It keeps out of water under <b>${ob.safety_depth_m ?? "?"} m</b> (${boat} draft plus a margin). Not for navigation. Check the official chart.
      ${qhelp(`${steps} route steps avoided. Chart cells: land ${L.coastline || 0}, shoal ${L.shoal || 0}, rocks ${L.obstruction || 0}, zones ${L.zones || 0}.`)}</div>`;
  }
  return `<div class="muted" style="font-size:12px;margin-top:6px">⛰ Basic coastline: the route avoids land, islands and race zones.
    No depths. Pick <b>NOAA charts</b> above to avoid shoals.
    ${qhelp(`${steps} route steps avoided. Map cells: coastline ${L.coastline || 0}, islands ${L.islands || 0}, zones ${L.zones || 0}. Data: Natural Earth ${esc(ob.data_version || "")}.`)}</div>`;
}


/* ---------- Polar (the observed polar — instruments only) ----------
   The debrief interface Cole asked for 2026-09-09: every past race's measured performance as a
   polar diagram + tables, and the decision hook for what future optimizer runs sail on. Data is
   /api/polar/observed (app/obspolar.py): measured TWS/TWA/STW only, trust-gated, kite-gated —
   never GRIB, never the cert (the cert appears only as a dashed reference curve). */
const Pol = { data: null, tws: null, config: "all", race: "all", split: "config",
  busy: false, proposals: null, sources: null, use: null, msg: "", _maxV: null };

// Fixed hue order — color follows the sail, never the filter (dataviz rule). Validated for CVD +
// contrast on this app's panel surface (#141418), 7 slots, 2026-09-09. Unattributed = muted.
const POL_ORDER = ["J1", "J1+J3", "A3", "A3+J1", "A3+SS", "S2", "S2+SS"];
const POL_HUES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9"];
// Race instances are ORDERED (they happened in sequence), so they get a sequential ramp rather
// than a categorical palette: oldest lightest, newest darkest. It encodes which race AND when,
// and it can never be mistaken for the sail colors above — the two modes mean different things
// and must not share a vocabulary.
const POL_RACE_RAMP = ["#a5e0ef", "#6cc6e0", "#38a8cc", "#2a86ab", "#1f6485"];

function polColor(cfg) {
  if (!cfg) return "#9a9aa6";
  const known = POL_ORDER.indexOf(cfg);
  if (known >= 0) return POL_HUES[known];
  // a config outside the known order gets a stable slot from the artifact's sorted config list
  const extras = ((Pol.data || {}).configs || []).filter((c) => !POL_ORDER.includes(c));
  return POL_HUES[(POL_ORDER.length + extras.indexOf(cfg)) % POL_HUES.length];
}

// the race instances that carry measured cells, oldest first (the ramp's order)
function polRaces() {
  return ((Pol.data || {}).races || [])
    .filter((r) => !r.skipped && r.recording != null)
    .slice().sort((x, y) => x.recording - y.recording);
}
function polRaceColor(rec) {
  const i = polRaces().findIndex((r) => polSameRec(r.recording, rec));
  return i < 0 ? "#9a9aa6" : POL_RACE_RAMP[i % POL_RACE_RAMP.length];
}
function polRaceName(rec) {
  const r = polRaces().find((x) => polSameRec(x.recording, rec));
  return r ? (r.name || "recording") : "recording";
}
function polSameRec(a, b) {
  return a != null && b != null && Math.abs(Number(a) - Number(b)) < 1;
}

async function renderPolar() {
  const view = document.getElementById("view");
  if (!Pol.data) {
    view.innerHTML = '<div class="loading">Loading the observed polar…</div>';
    try {
      Pol.data = await (await apiGet("/api/polar/observed")).json();
      Pol._maxV = null;
      Opt.polarGrid = Opt.polarGrid || (await (await apiGet("/api/polars")).json());
    } catch (e) { view.innerHTML = '<div class="placeholder">Failed to load.</div>'; return; }
  }
  try { Pol.proposals = ((await (await apiGet("/api/learning/proposals")).json()).proposals || []); }
  catch (e) { Pol.proposals = []; }
  try { Pol.sources = ((await (await apiGet("/api/learning/bin-sources")).json()).sources || []); }
  catch (e) { Pol.sources = []; }
  if (stale("polar")) return;
  // every race teaches the model until a human says otherwise
  if (!Pol.use) {
    Pol.use = {};
    (Pol.sources || []).forEach((x) => { Pol.use[polUseKey(x.recording)] = true; });
  }
  const buckets = Pol.data.tws_buckets || [];
  if (Pol.tws == null || !buckets.includes(Pol.tws)) {
    // default to the bucket with the most evidence, not the first
    const n = {}; (Pol.data.cells || []).forEach((c) => { n[c.tws] = (n[c.tws] || 0) + c.samples; });
    Pol.tws = buckets.slice().sort((a, b) => (n[b] || 0) - (n[a] || 0))[0] || null;
  }
  paintPolar();
}

async function polRefresh() {
  Pol.busy = true; paintPolar();
  try { Pol.data = await (await apiGet("/api/polar/observed?refresh=1")).json(); Pol._maxV = null; }
  catch (e) { /* keep the cached build */ }
  Pol.busy = false; Pol.tws = null; renderPolar();
}

// The cells behind the table and the config curves, for the current race filter. One race means
// that race's OWN p80 (re-pooled from its samples server-side), not a slice of the pooled build.
function polCells(tws, config) {
  const d = Pol.data || {};
  const src = Pol.race === "all" ? (d.cells || [])
    : (d.race_cells || []).filter((c) => polSameRec(c.recording, Pol.race));
  return src.filter((c) => (tws == null || c.tws === tws)
    && (config === "all" || (c.config || "—") === config));
}

// The cells the evidence gate REFUSED, under the same filters as polCells — so the card can say
// what it is not showing at the TWS and race on screen, not just a grand total. A gate nobody can
// see is a lie about where the numbers came from.
function polThin(tws, config) {
  const d = Pol.data || {};
  const src = Pol.race === "all" ? (d.refused_thin || [])
    : (d.refused_thin_race || []).filter((c) => polSameRec(c.recording, Pol.race));
  return src.filter((c) => (tws == null || c.tws === tws)
    && (config === "all" || (c.config || "—") === config));
}

// ONE radial scale for the whole tab. Cole, 2026-09-16: the chart must not rescale between the
// TWS chips — it did, and the jump he caught between 22 and 24 kn was the CERT reference, not the
// record: the cert's fastest cell steps 11.12 -> 14.26 kn across those buckets while the boat's
// own best moved 8.73 -> 9.23. A curve that looked longer was only drawn bigger. The scale is
// taken over everything this tab can ever draw — every TWS bucket, every race, every config, and
// every cert bucket that can appear as the reference — so it is immune to the race and sail
// filters too, and two screenshots are legitimately comparable.
function polMaxV() {
  if (Pol._maxV) return Pol._maxV;
  const d = Pol.data || {};
  let m = 4;
  for (const set of [d.cells, d.race_cells, d.race_curves, d.curve])
    (set || []).forEach((c) => { if (c.stw > m) m = c.stw; });
  const g = Opt.polarGrid;
  if (!g || !g.grid) return Math.max(6, Math.ceil((m + 0.4) / 2) * 2);   // don't memoise a
  Object.values(g.grid).forEach((row) =>                                 // scale missing the cert
    Object.values(row).forEach((v) => { if (v != null && v > m) m = v; }));
  // round up to a whole ring, so the outermost 2 kn ring is the chart's edge
  Pol._maxV = Math.max(6, Math.ceil((m + 0.4) / 2) * 2);
  return Pol._maxV;
}

// What the chart draws: one series per sail configuration, or one per race instance.
function polSeries() {
  const d = Pol.data || {};
  if (Pol.split === "race") {
    const src = (d.race_curves || []).filter((c) => c.tws === Pol.tws
      && (Pol.race === "all" || polSameRec(c.recording, Pol.race)));
    const by = {};
    src.forEach((c) => (by[c.recording] = by[c.recording] || []).push(c));
    return Object.keys(by)
      .sort((x, y) => Number(x) - Number(y))
      .map((rec) => ({ key: rec, label: polRaceName(Number(rec)), color: polRaceColor(Number(rec)),
                       pts: by[rec].slice().sort((m, n) => m.twa - n.twa) }));
  }
  const cells = polCells(Pol.tws, Pol.config);
  const by = {};
  cells.forEach((c) => (by[c.config || "—"] = by[c.config || "—"] || []).push(c));
  return Object.keys(by)
    .sort((x, y) => (POL_ORDER.indexOf(x) + 1 || 99) - (POL_ORDER.indexOf(y) + 1 || 99) || x.localeCompare(y))
    .map((name) => ({ key: name, label: name, color: polColor(name === "—" ? null : name),
                      pts: by[name].slice().sort((m, n) => m.twa - n.twa) }));
}

function polCertCurve(tws) {
  // nearest ORC-rated TWS bucket, reference only — dashed, muted, labelled
  const g = Opt.polarGrid; if (!g || !g.grid) return null;
  const buckets = (g.tws_buckets || []).map(Number);
  if (!buckets.length) return null;
  const b = buckets.reduce((x, y) => Math.abs(y - tws) < Math.abs(x - tws) ? y : x);
  const pts = (g.twa_buckets || []).map((a) => ({ twa: Number(a), stw: g.grid[String(b)][String(a)] }))
    .filter((p) => p.stw != null).sort((x, y) => x.twa - y.twa);
  return pts.length ? { tws: b, pts } : null;
}

function polSvg() {
  const series = polSeries();
  const cert = polCertCurve(Pol.tws);
  const maxV = polMaxV();     // fixed across every TWS chip and every filter — see polMaxV()
  const CX = 70, CY = 320, R = 268;
  const rr = (v) => R * v / maxV;
  const XY = (twa, v) => [CX + rr(v) * Math.sin(twa * Math.PI / 180), CY - rr(v) * Math.cos(twa * Math.PI / 180)];
  let out = [];
  Pol._pts = [];
  // rings every 2 kn + spokes every 30° — recessive grid
  for (let v = 2; v <= maxV + 1e-9; v += 2) {
    const [x0, y0] = XY(0, v), [x1, y1] = XY(180, v);
    out.push(`<path d="M ${x0} ${y0} A ${rr(v)} ${rr(v)} 0 0 1 ${x1} ${y1}" fill="none" stroke="var(--line)" stroke-width="1"/>`);
    out.push(`<text x="${CX + rr(v) + 3}" y="${CY - 4}" fill="var(--muted)" font-size="10">${v}</text>`);
  }
  for (let a = 0; a <= 180; a += 30) {
    const [x, y] = XY(a, maxV);
    out.push(`<line x1="${CX}" y1="${CY}" x2="${x}" y2="${y}" stroke="var(--line)" stroke-width="1"/>`);
    const [lx, ly] = XY(a, maxV + 0.35);
    out.push(`<text x="${lx}" y="${ly + 3}" fill="var(--muted)" font-size="11" text-anchor="middle">${a}°</text>`);
  }
  out.push(`<text x="${CX + R * 0.62}" y="${CY - R - 14}" fill="var(--muted)" font-size="11" text-anchor="middle">wind angle · rings = boat speed kn</text>`);
  if (cert) {
    const d = cert.pts.map((p, i) => (i ? "L" : "M") + XY(p.twa, p.stw).map((n) => n.toFixed(1)).join(" ")).join(" ");
    out.push(`<path d="${d}" fill="none" stroke="var(--muted)" stroke-width="2" stroke-dasharray="6 5" opacity="0.75"/>`);
    // named in the legend below the chart, not at the curve end — the deep sector is where the
    // observed labels live, and the reference must never crowd the record
  }
  for (const sr of series) {
    if (sr.pts.length > 1) {
      const d = sr.pts.map((p, i) => (i ? "L" : "M") + XY(p.twa, p.stw).map((n) => n.toFixed(1)).join(" ")).join(" ");
      out.push(`<path d="${d}" fill="none" stroke="${sr.color}" stroke-width="2"/>`);
    }
    for (const p of sr.pts) {
      const [x, y] = XY(p.twa, p.stw);
      const i = Pol._pts.push({ ...p, label: sr.label, mode: Pol.split }) - 1;
      out.push(`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4" fill="${sr.color}" stroke="var(--panel)" stroke-width="2"/>`);
      out.push(`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="11" fill="transparent" style="cursor:pointer"
        onmousemove="polTip(event,${i})" onmouseleave="polTipHide()"/>`);
    }
    // direct label at the curve's FASTEST point, pushed radially outward — curve ends converge
    // in the deep sector and labels there collide (seen on the first render)
    const top = sr.pts.reduce((a, b) => (b.stw > a.stw ? b : a));
    const [ex, ey] = XY(top.twa, top.stw + 0.55);
    out.push(`<text x="${ex}" y="${ey + 4}" fill="var(--ink)" font-size="11" font-weight="600" text-anchor="middle">${esc(sr.label)}</text>`);
  }
  return `<svg viewBox="0 0 640 660" style="width:100%;max-width:620px" role="img"
    aria-label="Observed polar, TWS ${Pol.tws} kn, by ${Pol.split === "race" ? "race instance" : "sail configuration"}">${out.join("")}</svg>`;
}

function polTip(ev, i) {
  const c = (Pol._pts || [])[i];
  if (!c) return;
  let tip = document.getElementById("polTipEl");
  if (!tip) { tip = document.createElement("div"); tip.id = "polTipEl"; document.body.appendChild(tip); }
  tip.setAttribute("style", "position:fixed;z-index:99;background:var(--panel2);border:1px solid var(--line);" +
    "border-radius:8px;padding:8px 10px;font-size:12px;pointer-events:none;max-width:260px;" +
    `left:${Math.min(ev.clientX + 14, innerWidth - 280)}px;top:${ev.clientY + 12}px`);
  const src = c.mode === "race" ? "all sails this race flew at this angle"
    : (c.races ? `${c.races.length} race(s): ${esc(c.races.join(", "))}`
               : esc(polRaceName(c.recording)));
  tip.innerHTML = `<b>${esc(c.label)}</b> · TWS ${c.tws} · TWA ${c.twa}°<br>` +
    `Speed <b>${c.stw}</b> kn (best held) · typical ${c.median_stw}<br>` +
    `${c.seconds != null ? c.seconds : c.samples} s of data (${c.samples} fixes) · ${src}`;
}
function polTipHide() { const t = document.getElementById("polTipEl"); if (t) t.remove(); }

function polUseKey(rec) { return rec == null ? "none" : String(Math.round(Number(rec))); }
function polToggleUse(key, on) { Pol.use[key] = on; paintPolar(); }
function polSetAllUse(on) {
  (Pol.sources || []).forEach((x) => { Pol.use[polUseKey(x.recording)] = on; });
  paintPolar();
}
function polSelectedRecordings() {
  return (Pol.sources || []).filter((x) => Pol.use[polUseKey(x.recording)]).map((x) => x.recording);
}

// Which race instances feed the boat model. The count of what is being LEFT OUT is on screen
// next to the button, because a filter nobody can see is a lie about where the numbers came from.
function polDecisionsCard() {
  const pending = (Pol.proposals || []).find((p) => p.status === "proposed");
  const src = Pol.sources || [];
  const chosen = polSelectedRecordings();
  const rows = src.map((x) => {
    const k = polUseKey(x.recording);
    const on = !!Pol.use[k];
    const dot = x.recording != null
      ? `<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${polRaceColor(x.recording)};margin-right:5px"></span>` : "";
    return `<tr>
      <td><label style="cursor:pointer"><input type="checkbox" ${on ? "checked" : ""}
        onchange="polToggleUse('${k}', this.checked)"> ${dot}<b>${esc(x.race_name || x.race_id || "recording")}</b></label>
        <div class="muted" style="font-size:11px">${x.recording ? esc(new Date(x.recording * 1000).toISOString().slice(0, 16).replace("T", " ") + "Z") : "uploaded track (no recording)"}${x.hours != null ? " · " + x.hours + " h" : ""}${x.track_source ? " · " + esc(x.track_source) : ""}</div></td>
      <td>${x.measured_bins} measured cell(s)<div class="muted" style="font-size:11px">${x.samples} samples${x.n_bins > x.measured_bins ? ` · ${x.n_bins - x.measured_bins} from forecasts, not used` : ""}</div></td>
      <td>${x.polar_pct != null ? x.polar_pct + "% of polar" : "—"}<div class="muted" style="font-size:11px">${x.debrief_id ? "debrief #" + x.debrief_id : ""}</div></td></tr>`;
  }).join("");
  const undebriefed = polRaces().filter((r) => !src.some((x) => polSameRec(x.recording, r.recording)));
  return `<div class="card">
    <h3>Teach the boat model <span class="muted" style="font-weight:400">— pick which races count</span></h3>
    <div class="muted" style="font-size:12px;margin-bottom:6px">Only measured wind counts. Nothing changes until you approve it.
      ${qhelp("The route uses the ORC polar plus the changes you approved. Suggestions use measured wind only. Data from forecasts is left out and counted.")}</div>
    ${pending ? `<div class="dep-row"><span class="pill warn">suggestion #${pending.id} waiting</span> helm ${pending.helm_current} → <b>${pending.helm_proposed}</b> · ${(pending.adjustments || []).length} polar tweak(s) · from ${((pending.summary || {}).recordings || []).length || "?"} recording(s)${(pending.summary || {}).excluded_by_choice ? `, ${(pending.summary || {}).excluded_by_choice} left out by you` : ""} · <a href="#learnings">review it</a></div>` : ""}
    ${src.length ? `<div class="muted" style="font-size:12px;margin:8px 0 4px">Which races teach the boat model:</div>
      <table class="fleet-tbl"><thead><tr><th>Race</th><th>Gives</th><th>Debrief</th></tr></thead><tbody>${rows}</tbody></table>
      <div style="margin-top:4px"><button class="mini" onclick="polSetAllUse(true)">All</button> <button class="mini" onclick="polSetAllUse(false)">None</button></div>`
      : '<div class="placeholder">Nothing yet. Debrief a recording in the <a href="#debrief">Debrief</a> tab first.</div>'}
    ${undebriefed.length ? `<div class="muted" style="font-size:12px;margin-top:6px">Not debriefed yet: ${undebriefed.map((r) => esc(r.name)).join(", ")}. <a href="#debrief">Debrief them</a> to use them.</div>` : ""}
    <div style="margin-top:8px">
      <button class="mini" onclick="polPropose()" ${src.length && chosen.length ? "" : "disabled"}>Suggest changes from ${chosen.length} of ${src.length} race(s)</button>
      <span class="muted" id="polMsg" style="font-size:12px">${esc(Pol.msg || "")}</span>
    </div>
  </div>`;
}

// A trust sweep that found DANGER windows is an instrument fault (Jul 18: compass 98° off GPS
// course for 43 min) — not a footnote. `danger_s` is the structured signal; artifacts built
// before it existed only carry the human line, whose channel entries read "<ch> DANGER for …".
function trustDanger(dangerS, line) {
  if (dangerS != null) return dangerS > 0;
  return /\bDANGER\b/.test(line || "");
}

function trustBanner(title, line, tail) {
  const parts = String(line || "").split("; ").filter(Boolean);
  return `<div class="banner bad" role="alert"><b>⚠ ${esc(title)}</b>
    ${parts.map((p) => `<div class="banner-line">${esc(p)}</div>`).join("")}
    ${tail ? `<div class="banner-tail">${tail}</div>` : ""}</div>`;
}

function polDangerBanner(races) {
  const bad = races.filter((r) => !r.skipped && trustDanger(r.trust_danger_s, r.trust_line));
  return bad.map((r) => trustBanner(`Instrument fault — ${r.name}`, r.trust_line,
    `${r.refused_by_trust ? r.refused_by_trust + " fixes left out of the polar." : "Those stretches are left out of the polar."} Fix the calibration before the next race. The same sensor feeds the boat's live numbers.`)).join("");
}

function paintPolar() {
  const d = Pol.data || {};
  const cells = polCells(Pol.tws, Pol.config);
  const minS = Math.round((d.grid || {}).min_seconds || 60);
  const thin = polThin(Pol.tws, Pol.config);
  const allTws = d.tws_buckets || [];
  const cfgs = ["all", ...((d.configs || [])), "—"];
  const races = polRaces();
  const byRace = Pol.split === "race";
  const chips = allTws.map((t) =>
    `<button class="mini" style="${t === Pol.tws ? "background:var(--accent);color:#fff" : ""}"
       onclick="Pol.tws=${t};paintPolar()">${t} kn</button>`).join(" ");
  const cfgSel = `<select ${byRace ? "disabled" : ""} onchange="Pol.config=this.value;paintPolar()">${cfgs.map((c) =>
    `<option value="${esc(c)}" ${c === Pol.config ? "selected" : ""}>${c === "all" ? "all sails" : c === "—" ? "unknown sails" : esc(c)}</option>`).join("")}</select>`;
  const raceSel = `<select onchange="Pol.race=this.value==='all'?'all':Number(this.value);paintPolar()">
    <option value="all" ${Pol.race === "all" ? "selected" : ""}>all races</option>
    ${races.slice().reverse().map((r) => `<option value="${r.recording}" ${polSameRec(Pol.race, r.recording) ? "selected" : ""}>${esc(r.name)}${r.hours != null ? " · " + r.hours + " h" : ""}</option>`).join("")}
  </select>`;
  const splitRadio = ["config", "race"].map((k) =>
    `<label style="cursor:pointer;margin-right:10px"><input type="radio" name="polsplit" value="${k}" ${Pol.split === k ? "checked" : ""}
      onchange="Pol.split='${k}';paintPolar()"> ${k === "config" ? "sails" : "race"}</label>`).join("");
  const legend = byRace
    ? races.map((r) => `<span style="white-space:nowrap"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${polRaceColor(r.recording)};margin-right:4px"></span>${esc(r.name)}</span>`).join(" &nbsp; ")
    : (d.configs || []).map((c) =>
        `<span style="white-space:nowrap"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${polColor(c)};margin-right:4px"></span>${esc(c)}</span>`
      ).join(" &nbsp; ") + ` &nbsp; <span style="white-space:nowrap"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:#9a9aa6;margin-right:4px"></span>unknown sails</span>`;
  const legendTail = ` &nbsp; <span style="white-space:nowrap;color:var(--muted)"><svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="#9a9aa6" stroke-width="2" stroke-dasharray="5 4"/></svg> ORC rating (for reference)</span>`;
  const tbl = cells.length ? `<div class="pg-wrap"><table class="pg">
    <tr><th>TWA</th><th>sails</th><th>best held</th><th>typical</th><th>data (s)</th><th>${Pol.race === "all" ? "races" : "race"}</th></tr>
    ${cells.slice().sort((a, b) => a.twa - b.twa || String(a.config).localeCompare(String(b.config))).map((c) =>
      `<tr><td>${c.twa}°</td><td><span style="color:${polColor(c.config)}">●</span> ${esc(c.config || "unknown")}</td>
       <td><b>${c.stw}</b></td><td>${c.median_stw}</td><td>${c.seconds != null ? c.seconds : c.samples}</td><td>${esc(c.races ? c.races.join(", ") : polRaceName(c.recording))}</td></tr>`).join("")}
  </table></div>` : '<div class="placeholder">No measured cells at this TWS for this race.</div>';
  document.getElementById("view").innerHTML = `<div class="opt">
    ${polDangerBanner(d.races || [])}
    <div class="card">
      <h3>Observed polar <span class="muted" style="font-weight:400">— what the boat really sailed, from the instruments</span>
        ${qhelp("Measured wind and boat speed only. The dashed line is the ORC rating, shown for reference.")}</h3>
      ${(d.races || []).map((r) => r.skipped
        ? `<div class="muted" style="font-size:12px">⊘ ${esc(r.name)} — ${esc(r.skipped)}</div>`
        : `<div class="muted" style="font-size:12px"><b style="color:var(--ink)">${esc(r.name)}</b> · ${r.hours} h · ${r.fixes} fixes (${r.refused_by_trust} failed the instrument check) · ${r.measured_samples} measured${r.refused_below_min_twa ? ", " + r.refused_below_min_twa + " below " + ((d.grid || {}).min_twa_deg || 30) + "° TWA (not sailing)" : ""}${r.cells != null ? " → " + r.cells + " cells" : ""}${r.thin_cells ? " (+" + r.thin_cells + " under " + minS + " s, left out)" : ""} · instruments: ${trustDanger(r.trust_danger_s, r.trust_line) ? '<span class="pill bad">⚠ fault, see above</span>' : esc(r.trust_line || "—")}</div>`).join("")}
      <div style="margin-top:6px"><button class="mini" onclick="polRefresh()" ${Pol.busy ? "disabled" : ""}>${Pol.busy ? "Rebuilding…" : "Rebuild"}</button>
      <span class="muted" style="font-size:11px"> built ${d.generated_at ? new Date(d.generated_at * 1000).toISOString().slice(0, 16) + "Z" : "—"}${d.refresh_error ? " · last refresh failed: " + esc(d.refresh_error) : ""}</span></div>
    </div>
    <div class="card">
      <div class="opt-controls" style="margin-bottom:6px">TWS: ${chips}</div>
      <div class="opt-controls" style="margin-bottom:6px">race: ${raceSel} &nbsp; sails: ${cfgSel} &nbsp;
        <span class="muted">lines by:</span> ${splitRadio}</div>
      ${byRace ? `<div class="muted" style="font-size:12px;margin-bottom:4px">One line per race, all sails together. The sail filter is off.</div>` : ""}
      ${polSvg()}
      <div class="muted" style="font-size:12px;margin-top:4px">${legend}${legendTail}</div>
      <div class="muted" style="font-size:11px;margin-top:4px">Rings every 2 kn, up to ${polMaxV()} kn. The scale never changes, so a longer line is a faster boat.</div>
    </div>
    <div class="card"><h3>Cells at ${Pol.tws} kn${Pol.race === "all" ? "" : " · " + esc(polRaceName(Pol.race))} <span class="muted" style="font-weight:400">— best speed held, at least ${minS} s of data each</span></h3>${tbl}
      ${thin.length ? `<div class="muted" style="font-size:11px;margin-top:6px">Not shown: <b>${thin.length}</b> cell(s) with under ${minS} s of data (${thin.slice().sort((x, y) => y.seconds - x.seconds).slice(0, 6).map((c) => `${c.twa}°${c.config ? " " + esc(c.config) : ""} ${c.seconds}s`).join(", ")}${thin.length > 6 ? `, +${thin.length - 6} more` : ""}). Too short to trust.</div>` : ""}</div>
    ${polDecisionsCard()}
  </div>`;
}

async function polPropose() {
  Pol.msg = "Proposing…"; paintPolar();
  try {
    const r = await (await apiPost("/api/learning/propose", { recordings: polSelectedRecordings() })).json();
    Pol.msg = r.ok ? `Suggestion #${r.id} made from ${(r.summary || {}).n_samples || "?"} samples. Review it in Learnings.`
                   : (r.note || "couldn't suggest changes");
  } catch (e) { Pol.msg = "couldn't suggest changes: " + (e.message || e); }
  renderPolar();
}
