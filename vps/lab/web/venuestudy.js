/* Venue study (prep): which weather model is right at a place, and what the wind is usually like
   there in the race window. Any place in the world, before the race is even in the library.
   Server: app/venuestudy.py + app/obsstations.py, /api/venue-study/*. Uses app.js helpers
   (api, apiGet, apiPost, esc, attr, stale, jsonOrFriendly). */
"use strict";

const Vs = { list: null, open: null, form: null, found: null, running: false, jobId: null, map: null,
  climIdx: 0, msg: "" };
const VS_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function vsNewForm() {
  return { name: "", lat: "", lon: "", radius_nm: 40, mode: "dates", start: "", end: "", month: "",
    deep: false };
}

async function renderVenueStudy() {
  const view = document.getElementById("view");
  if (!Vs.list) view.innerHTML = '<div class="loading">Loading venue studies…</div>';
  try { Vs.list = (await (await apiGet("/api/venue-study")).json()).studies || []; }
  catch (e) { Vs.list = Vs.list || []; }
  if (stale("venue")) return;
  if (!Vs.form) Vs.form = vsNewForm();
  paintVenueStudy();
}

function paintVenueStudy() {
  const view = document.getElementById("view");
  const saved = (Vs.list || []).map((s) => `<button class="ghost vs-saved${Vs.open && Vs.open.key === s.key ? " active" : ""}"
      onclick="vsOpen('${attr(s.key)}')"><b>${esc(s.name)}</b>
      <span class="muted">${esc(s.window_label || "")}</span>
      <span class="muted vs-verdict">${esc(s.verdict || "")}</span></button>`).join("");
  view.innerHTML = `<div class="vs">
    <div class="card"><h3>Venue study</h3>
      <div class="muted" style="font-size:13px">Which weather model is right at a place, and what the wind is usually like there.
        ${qhelp("We compare each model's past forecasts with the measured wind at nearby stations, in your race window, every year back to 2021. A race near a studied place uses the result for its route automatically.")}</div>
      ${saved ? `<div class="vs-list">${saved}</div>` : ""}
      <div style="margin-top:8px"><button class="ghost" onclick="vsStartNew()">New study</button></div>
    </div>
    ${Vs.open ? vsResults(Vs.open) : vsFormHtml()}
  </div>`;
  if (!Vs.open) vsDrawMap();
}

/* ---------- new study form ---------- */
function vsFormHtml() {
  const f = Vs.form;
  const monthOpts = ['<option value="">Month…</option>'].concat(VS_MONTHS.map((m, i) =>
    `<option value="${i + 1}" ${String(f.month) === String(i + 1) ? "selected" : ""}>${m}</option>`)).join("");
  return `<div class="card"><h3>New study</h3>
    <div class="vs-grid">
      <label>Name <input class="ein" value="${attr(f.name)}" placeholder="e.g. Fort Lauderdale"
        oninput="Vs.form.name=this.value"></label>
      <label>Lat <input class="ein num" id="vsLat" inputmode="decimal" value="${attr(f.lat)}" placeholder="26.10"
        oninput="Vs.form.lat=this.value; vsMoved()"></label>
      <label>Lon <input class="ein num" id="vsLon" inputmode="decimal" value="${attr(f.lon)}" placeholder="-80.05"
        oninput="Vs.form.lon=this.value; vsMoved()"></label>
      <label>Radius nm <input class="ein num" inputmode="numeric" value="${attr(f.radius_nm)}"
        oninput="Vs.form.radius_nm=this.value; vsMoved()"></label>
    </div>
    <div id="vsMap" class="vs-map"></div>
    <div class="muted" style="font-size:12px">Tap the map to set the place.</div>
    <div class="vs-grid" style="margin-top:10px">
      <label><input type="radio" name="vsMode" ${f.mode === "dates" ? "checked" : ""} onchange="Vs.form.mode='dates'; paintVenueStudy()"> Race dates</label>
      <label><input type="radio" name="vsMode" ${f.mode === "month" ? "checked" : ""} onchange="Vs.form.mode='month'; paintVenueStudy()"> Month</label>
      ${f.mode === "dates"
        ? `<label>From <input class="ein" type="date" value="${attr(f.start)}" onchange="Vs.form.start=this.value"></label>
           <label>To <input class="ein" type="date" value="${attr(f.end)}" onchange="Vs.form.end=this.value"></label>`
        : `<label>Month <select class="esel" onchange="Vs.form.month=this.value">${monthOpts}</select></label>`}
    </div>
    <div class="muted" style="font-size:12px">${f.mode === "dates" ? "Each past year, ±3 weeks around these dates." : "The whole month, each past year."}</div>
    <div style="margin-top:10px"><button class="ghost" onclick="vsFind()">Find stations</button>
      <span id="vsFindMsg" class="muted" style="font-size:12px"></span></div>
    <div id="vsStations">${vsStationsHtml()}</div>
  </div>`;
}

function vsBody() {
  const f = Vs.form;
  // a blank lat/lon stays blank (the server says "set the place"), never 0
  const num = (v) => (String(v).trim() === "" ? null : Number(v));
  const b = { name: f.name, lat: num(f.lat), lon: num(f.lon), radius_nm: Number(f.radius_nm) || 40 };
  if (f.mode === "month") b.month = Number(f.month) || null; else { b.start = f.start; b.end = f.end || f.start; }
  return b;
}

async function vsFind() {
  const m = document.getElementById("vsFindMsg");
  if (m) m.textContent = "Looking…";
  const gen = Vs.gen = (Vs.gen || 0) + 1;     // a newer search or a moved pin outdates this one
  try {
    const r = await jsonOrFriendly(await apiPost("/api/venue-study/stations", vsBody()));
    if (gen !== Vs.gen) return;
    if (r.detail) throw new Error(r.detail);
    Vs.found = r;
    Vs.found.stations.forEach((s) => { s.use = !!s.ticked; });
  } catch (e) { if (m) m.textContent = String(e.message || e); return; }
  if (stale("venue")) return;          // left the tab meanwhile
  paintVenueStudy();
}

function vsStationsHtml() {
  const r = Vs.found;
  if (!r) return "";
  const kind = (s) => (s.kind === "ndbc" ? "Buoy / coast" : "Airport");
  const rows = r.stations.map((s, i) => `<tr>
      <td><input type="checkbox" ${s.use ? "checked" : ""} onchange="Vs.found.stations[${i}].use=this.checked"
        aria-label="Use ${attr(s.id)}"></td>
      <td>${esc(s.id)}${s.curated ? ' <span class="pill ok" title="On our own list">ours</span>' : ""}</td>
      <td>${esc(s.name || "")}</td><td>${kind(s)}</td><td>${s.dist_nm} nm</td>
      <td>${s.obs == null ? '<span class="muted">not checked</span>' : s.obs ? s.obs.toLocaleString() : '<span class="muted">none</span>'}</td></tr>`).join("");
  const notes = (r.notes || []).map((n) => `<div class="muted" style="font-size:12px">${esc(n)}</div>`).join("");
  return `<h4 style="margin-top:14px">Stations</h4>
    ${rows ? `<div class="vs-scroll"><table class="legs"><thead><tr><th></th><th>ID</th><th>Name</th><th>Type</th><th>Away</th>
      <th title="Measured wind readings in ${esc(r.checked_year || "")}">Data ${esc(r.checked_year || "")}</th></tr></thead>
      <tbody>${rows}</tbody></table></div>` : ""}
    ${notes}
    <div class="muted" style="font-size:12px;margin-top:6px">A race within 30 nm uses this study. Venue key ${esc(r.key)}.</div>
    <label class="echk" style="margin-top:8px"><input type="checkbox" ${Vs.form.deep ? "checked" : ""}
      onchange="Vs.form.deep=this.checked"> Add older years (before 2021). Slow: up to hours.</label>
    <div style="margin-top:10px"><button class="primary" id="vsRun" onclick="vsRun()" ${Vs.running ? "disabled" : ""}>
      ${Vs.running ? "Running…" : "Run study"}</button>
      <span id="vsRunMsg" class="muted" style="font-size:12px">${esc(Vs.msg)}</span></div>`;
}

async function vsRun() {
  const sts = ((Vs.found || {}).stations || []).filter((s) => s.use);
  const setMsg = (t) => { Vs.msg = t; const m = document.getElementById("vsRunMsg"); if (m) m.textContent = t; };
  if (!sts.length) return setMsg("Tick at least one station.");
  const body = Object.assign(vsBody(), { stations: sts, deep: !!Vs.form.deep });
  try {
    const r = await jsonOrFriendly(await apiPost("/api/venue-study/run", body));
    if (r.detail || r.ok === false) throw new Error(r.detail || "A study is already running. Try again when it's done.");
    Vs.running = true; Vs.jobId = r.job_id;
    const b = document.getElementById("vsRun"); if (b) { b.disabled = true; b.textContent = "Running…"; }
    setMsg("Starting… You can leave this page.");
    vsPoll();
  } catch (e) { setMsg(String(e.message || e)); }
}

function vsPoll() {
  const setMsg = (t) => { Vs.msg = t; const m = document.getElementById("vsRunMsg"); if (m) m.textContent = t; };
  const tick = async () => {
    let st;
    try { st = await (await api("/api/venue-study/status?job_id=" + encodeURIComponent(Vs.jobId))).json(); }
    catch (e) { return setTimeout(tick, 5000); }       // a dropped poll: keep going
    if (st.state === "running") {
      setMsg("Running… " + ((st.progress || []).slice(-1)[0] || ""));
      return setTimeout(tick, 4000);
    }
    Vs.running = false;
    if (st.state === "done" && st.result) {
      Vs.msg = ""; Vs.open = st.result; Vs.found = null; Vs.form = vsNewForm(); Vs.list = null;
      if (activeSec === "venue") renderVenueStudy();
    } else {
      setMsg("Failed: " + String(st.error || st.state));
      const b = document.getElementById("vsRun"); if (b) { b.disabled = false; b.textContent = "Run study"; }
    }
  };
  setTimeout(tick, 3000);
}

/* ---------- map: tap to set the place, circle = radius ---------- */
function vsDrawMap() {
  const el = document.getElementById("vsMap");
  if (!el || !window.L) return;
  if (Vs.map) { try { Vs.map.remove(); } catch (e) {} Vs.map = null; }
  const map = L.map(el, { worldCopyJump: true }); Vs.map = map;
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
  const f = Vs.form;
  if (f.lat !== "" && f.lon !== "" && isFinite(+f.lat) && isFinite(+f.lon)) map.setView([+f.lat, +f.lon], 8);
  else map.setView([30, -40], 2);
  map.on("click", (e) => {
    const lon = ((e.latlng.lng + 540) % 360) - 180;
    f.lat = e.latlng.lat.toFixed(3); f.lon = lon.toFixed(3);
    const a = document.getElementById("vsLat"), b = document.getElementById("vsLon");
    if (a) a.value = f.lat; if (b) b.value = f.lon;
    vsMoved();
  });
  vsPin();
}

// The place or radius changed: stations found for the old one no longer apply. Find them again.
function vsMoved() {
  Vs.gen = (Vs.gen || 0) + 1;
  if (Vs.found) {
    Vs.found = null;
    const box = document.getElementById("vsStations"); if (box) box.innerHTML = "";
  }
  vsPin();
}

function vsPin() {
  const map = Vs.map, f = Vs.form;
  if (!map) return;
  if (Vs._pin) { Vs._pin.forEach((l) => map.removeLayer(l)); }
  Vs._pin = [];
  const la = +f.lat, lo = +f.lon;
  if (f.lat === "" || f.lon === "" || !isFinite(la) || !isFinite(lo)) return;
  Vs._pin.push(L.circleMarker([la, lo], { radius: 6, color: "#0b0b0e", weight: 1, fillColor: "#ed1c24", fillOpacity: 1 }).addTo(map));
  Vs._pin.push(L.circle([la, lo], { radius: (Number(f.radius_nm) || 40) * 1852, color: "#cdd2da", weight: 1, fillOpacity: 0.05 }).addTo(map));
  ((Vs.found || {}).stations || []).forEach((s) => Vs._pin.push(L.circleMarker([s.lat, s.lon],
    { radius: 4, color: "#cdd2da", weight: 1, fillColor: s.kind === "ndbc" ? "#3ecf8e" : "#f5b13d", fillOpacity: 0.9 })
    .bindTooltip(s.id).addTo(map)));
}

function vsStartNew() { Vs.open = null; Vs.found = null; Vs.form = vsNewForm(); paintVenueStudy(); }

async function vsOpen(key) {
  try {
    const r = await api("/api/venue-study/" + encodeURIComponent(key));
    if (!r.ok) throw new Error();
    Vs.open = await r.json(); Vs.climIdx = 0;
  } catch (e) { Vs.open = null; }
  if (stale("venue")) return;
  paintVenueStudy();
}

async function vsDelete(key) {
  if (!confirm("Delete this study? Races there go back to default model weights.")) return;
  try {
    const r = await api("/api/venue-study/" + encodeURIComponent(key), { method: "DELETE" });
    if (!r.ok) { alert((await r.json()).detail || "Couldn't delete."); return; }
  } catch (e) { return; }
  Vs.open = null; Vs.list = null; renderVenueStudy();
}

function vsRerun() {
  const s = Vs.open;
  const f = vsNewForm();
  f.name = s.name; f.lat = String(s.lat); f.lon = String(s.lon); f.radius_nm = s.radius_nm;
  const sp = s.spec || {};
  if (sp.pad_days === 0 && sp.start_md && sp.start_md[1] === 1) { f.mode = "month"; f.month = sp.start_md[0]; }
  else if (sp.start_md) {
    const y = new Date().getFullYear(), p = (n) => String(n).padStart(2, "0");
    // a race over New Year (Dec 26 – Jan 2) ends in the next year
    const wraps = sp.end_md[0] * 100 + sp.end_md[1] < sp.start_md[0] * 100 + sp.start_md[1];
    f.start = `${y}-${p(sp.start_md[0])}-${p(sp.start_md[1])}`;
    f.end = `${y + (wraps ? 1 : 0)}-${p(sp.end_md[0])}-${p(sp.end_md[1])}`;
  }
  Vs.form = f; Vs.open = null; Vs.found = null; paintVenueStudy();
}

/* ---------- results ---------- */
function vsDir(d) {
  if (d == null || Math.abs(d) < 3) return "—";
  return `${Math.abs(Math.round(d))}° ${d > 0 ? "right" : "left"}`;
}
function vsSpeed(b) {
  if (b == null || Math.abs(b) < 0.3) return "about right";
  return `${Math.abs(b).toFixed(1)} kn ${b < 0 ? "light" : "windy"}`;
}
function vsYears(ys) {
  if (!ys || !ys.length) return "—";
  return ys.length === 1 ? String(ys[0]) : `${ys[0]}–${ys[ys.length - 1]} (${ys.length})`;
}

function vsResults(s) {
  const r = s.result || {};
  const rows = r.table || [];
  const maxE = Math.max(1, ...rows.map((t) => t.vector_rmse_kn || 0));
  const trs = rows.map((t) => {
    const w = t.reference ? '<span class="muted" title="Shown for reference. Not used in the route.">ref</span>'
      : t.weighted ? `<b>×${Number(t.weight).toFixed(2)}</b>` : `<span class="muted" title="Not enough data to change it">×1</span>`;
    const pct = Math.round(100 * (t.vector_rmse_kn || 0) / maxE);
    return `<tr><td>${esc(t.model.toUpperCase())}</td>
      <td title="Average wind error, kn (speed and direction together). Lower is better.">
        <span class="vs-bar"><span style="width:${pct}%"></span></span> ${t.vector_rmse_kn}</td>
      <td>${vsSpeed(t.speed_bias_kn)}</td><td>${vsDir(t.dir_bias_deg)}</td>
      <td>${(t.n || 0).toLocaleString()}</td><td>${vsYears(t.years)}</td><td>${w}</td></tr>`;
  }).join("");
  const notes = (r.notes || []).map((n) => `<div class="muted" style="font-size:12px">${esc(n)}</div>`).join("");
  const sts = (s.stations || []).map((x) => `${esc(x.id)} ${esc(x.name || "")}`).join(" · ");
  const when = s.updated_at ? new Date(s.updated_at * 1000).toLocaleDateString() : "";
  return `<div class="card"><h3>${esc(s.name)}</h3>
    <div class="vs-verdict-big">${esc(r.verdict || "")}</div>
    <div class="muted" style="font-size:12px">${esc(r.window_label || "")} · ${vsYears(r.years)} · ${sts}${r.deep ? " · older years" : ""} · ${esc(when)}</div>
    ${trs ? `<div class="vs-scroll"><table class="legs" style="margin-top:10px"><thead><tr><th>Model</th>
      <th title="Average wind error, kn. Lower is better.">Error kn</th><th>Speed</th>
      <th title="Forecast wind direction compared with what was measured">Direction</th>
      <th title="Forecast hours checked">Hours</th><th>Years</th>
      <th title="How much the route uses this model here">Weight</th></tr></thead><tbody>${trs}</tbody></table></div>` : ""}
    ${notes}
    <div class="muted" style="font-size:12px;margin-top:6px">A race within 30 nm uses these weights. Venue key ${esc(s.key)}.</div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="ghost" onclick="vsRerun()">Run again</button>
      <button class="ghost" onclick="vsDelete('${attr(s.key)}')">Delete</button></div>
  </div>
  ${vsClimate(r.climate || [])}`;
}

/* What the wind is usually like: a rose (share of hours by direction, stacked by speed — one hue,
   dim = light air, bright = strong, on the dark Lab canvas) beside the same numbers as a table,
   then time of day. */
const VS_BIN_COLORS = ["#2b3f5c", "#36609a", "#3f86d4", "#79b4ee", "#cfe6ff"];
function vsClimate(list) {
  if (!list.length) return "";
  const i = Math.min(Vs.climIdx || 0, list.length - 1);
  const c = list[i];
  const pick = list.length > 1 ? `<select class="esel" onchange="Vs.climIdx=+this.value; paintVenueStudy()">${list.map((x, k) =>
    `<option value="${k}" ${k === i ? "selected" : ""}>${esc(x.station)} ${esc(x.name)}</option>`).join("")}</select>` : "";
  const legend = c.bins.map((b, k) => `<span class="vs-key"><i style="background:${VS_BIN_COLORS[k]}"></i>${esc(b)} kn</span>`).join("");
  const head = c.bins.map((b) => `<th>${esc(b)}</th>`).join("");
  const body = c.rose.map((r) => `<tr><td>${esc(r.dir)}</td>${r.bins.map((p) => `<td>${!p ? "" : p < 0.5 ? "<1" : p.toFixed(0)}</td>`).join("")}<td><b>${r.pct.toFixed(0)}</b></td></tr>`).join("");
  const tod = c.tod.map((t) => `<tr><td>${esc(t.label)} <span class="muted">${esc(t.hours)}</span></td>
    <td>${t.mean_kn == null ? "—" : t.mean_kn + " kn"}</td><td>${esc(t.top_dir || "—")}</td></tr>`).join("");
  return `<div class="card"><h3>Usual wind ${pick}</h3>
    <div class="vs-verdict-big">${esc(c.line)}</div>
    <div class="muted" style="font-size:12px">${c.hours.toLocaleString()} hours measured, ${vsYears(c.years)}.
      Calm ${c.calm_pct}% · under 6 kn ${c.light_pct}% · 18 kn+ ${c.strong_pct}%</div>
    <div class="vs-clim">
      <div>${vsRoseSvg(c)}<div class="vs-legend">${legend}</div></div>
      <div><table class="legs vs-rose-t"><thead><tr><th>From</th>${head}<th>All</th></tr></thead><tbody>${body}</tbody></table>
        <div class="muted" style="font-size:11px">% of hours. Calm not shown.</div>
        <table class="legs" style="margin-top:10px"><thead><tr><th>Time (${esc(c.time_label)})</th><th>Average</th><th>Mostly from</th></tr></thead>
        <tbody>${tod}</tbody></table></div>
    </div></div>`;
}

function vsRoseSvg(c) {
  const R = 120, cx = 140, cy = 140, max = Math.max(5, ...c.rose.map((r) => r.pct));
  const rad = (p) => 18 + (R - 18) * Math.sqrt(p / max);      // area-true: radius ~ sqrt(share)
  const wedge = (a0, a1, r0, r1) => {
    const p = (a, r) => [cx + r * Math.sin(a), cy - r * Math.cos(a)];
    const [x0, y0] = p(a0, r0), [x1, y1] = p(a0, r1), [x2, y2] = p(a1, r1), [x3, y3] = p(a1, r0);
    return `M${x0},${y0} L${x1},${y1} A${r1},${r1} 0 0 1 ${x2},${y2} L${x3},${y3} A${r0},${r0} 0 0 0 ${x0},${y0}Z`;
  };
  let paths = "";
  c.rose.forEach((r, k) => {
    const mid = (k * 45) * Math.PI / 180, half = 19 * Math.PI / 180;
    let cum = 0;
    r.bins.forEach((p, b) => {
      if (!p) return;
      const r0 = rad(cum), r1 = rad(cum + p); cum += p;
      paths += `<path d="${wedge(mid - half, mid + half, r0, r1)}" fill="${VS_BIN_COLORS[b]}" stroke="var(--panel)" stroke-width="1">
        <title>From ${r.dir}, ${c.bins[b]} kn: ${p.toFixed(1)}% of hours</title></path>`;
    });
  });
  const rings = [0.25, 0.5, 1].map((f) => `<circle cx="${cx}" cy="${cy}" r="${rad(max * f)}" fill="none" stroke="var(--line)"/>`).join("");
  const lab = c.rose.map((r, k) => {
    const a = k * 45 * Math.PI / 180;
    return `<text x="${cx + (R + 12) * Math.sin(a)}" y="${cy - (R + 12) * Math.cos(a) + 4}" text-anchor="middle" fill="var(--muted)" font-size="11">${r.dir}</text>`;
  }).join("");
  return `<svg class="vs-rose" viewBox="0 0 280 280" role="img" aria-label="Wind rose: share of hours by direction and speed">
    ${rings}${paths}${lab}<text x="${cx + 4}" y="${cy - rad(max) + 12}" fill="var(--muted)" font-size="10">${max.toFixed(0)}%</text></svg>`;
}
