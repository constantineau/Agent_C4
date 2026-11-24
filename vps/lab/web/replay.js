/* replay.js — Debrief replay map + strip chart (#373, #374).

   One time cursor drives both: the map (our boat, the fleet, the course, the day's wind) and the
   strip chart (any logged channel vs time). Scrub either one and the other follows. Up to 4
   markers: markers 1–2 are window A, 3–4 window B; mean and SD for each, and B − A. Window A can
   be saved as a named 2–5 min "test", kept next to the race's track.

   Server: app/replay.py (/api/debrief/replay*). Uses app.js helpers (apiGet, apiPost, esc, attr,
   debList, Deb) and the vendored Leaflet. Opened from a recording row in the Debrief tab
   ("Replay"); the card's DOM node is kept across Debrief repaints and re-attached, so the map
   survives the job poll. */

// ---- pure helpers (no DOM; tools/lab-web/replay.test.mjs) -----------------------------------
const ReplayCore = (function () {
  const LABELS = { bsp: "BSP", sog: "SOG", tws: "TWS", twa: "TWA", twd: "TWD", vmg: "VMG",
    heel: "Heel", pct_polar: "% polar", cog: "COG", hdg: "HDG", aws: "AWS", awa: "AWA" };
  const UNITS = { bsp: "kn", sog: "kn", tws: "kn", vmg: "kn", aws: "kn", heel: "°", twa: "°",
    twd: "°", cog: "°", hdg: "°", awa: "°", pct_polar: "%" };
  const ANGLE = new Set(["twa", "awa", "twd", "cog", "hdg"]);
  const SIGNED = new Set(["twa", "awa"]);

  // index of the last row with row[0] <= t (rows sorted by time), -1 before the first
  function idxAt(rows, t, key) {
    const k = key || ((r) => r[0]);
    let lo = 0, hi = rows.length - 1, ans = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (k(rows[mid]) <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  // position on a [[t, lat, lon, ...]] track at time t, linearly between fixes; null outside it
  function posAt(fixes, t) {
    if (!fixes || !fixes.length || t < fixes[0][0] || t > fixes[fixes.length - 1][0]) return null;
    const i = idxAt(fixes, t);
    const a = fixes[i], b = fixes[Math.min(i + 1, fixes.length - 1)];
    if (b[0] === a[0]) return { lat: a[1], lon: a[2], i };
    const f = (t - a[0]) / (b[0] - a[0]);
    return { lat: a[1] + (b[1] - a[1]) * f, lon: a[2] + (b[2] - a[2]) * f, i };
  }

  // bearing a→b in degrees (for the boat arrow when the track carries no COG)
  function bearing(a, b) {
    const r = Math.PI / 180, y = Math.sin((b[2] - a[2]) * r) * Math.cos(b[1] * r);
    const x = Math.cos(a[1] * r) * Math.sin(b[1] * r) - Math.sin(a[1] * r) * Math.cos(b[1] * r) * Math.cos((b[2] - a[2]) * r);
    return (Math.atan2(y, x) / r + 360) % 360;
  }

  function frameAt(times, t) {
    if (!times || !times.length) return -1;
    return Math.max(0, idxAt(times, t, (x) => x));
  }

  // a boat-log sample averages [t, t + step): it is faulty when any of that touches a danger
  // interval (same rule as app/replay._in). No step = a point sample.
  function inDanger(t, danger, step) {
    const span = step || 0;
    for (const iv of danger || []) {
      if (span ? (iv[0] < t + span && t <= iv[1]) : (t >= iv[0] && t <= iv[1])) return true;
    }
    return false;
  }

  // Mean and SD per channel over [a, b], leaving out samples in a danger interval. Same rule as
  // app/replay.window_stats: angles are circular (TWA/AWA signed, directions 0..360).
  function windowStats(samples, a, b, danger, chans, step) {
    const lo = Math.min(a, b), hi = Math.max(a, b);
    const inside = (samples || []).filter((s) => s.t >= lo && s.t <= hi);
    const used = inside.filter((s) => !inDanger(s.t, danger, step));
    const stats = {};
    for (const ch of chans || Object.keys(LABELS)) {
      const v = used.map((s) => s[ch]).filter((x) => x != null);
      if (!v.length) continue;
      let mean, sd;
      if (ANGLE.has(ch)) {
        const r = Math.PI / 180;
        const sx = v.reduce((p, x) => p + Math.sin(x * r), 0) / v.length;
        const cx = v.reduce((p, x) => p + Math.cos(x * r), 0) / v.length;
        const R = Math.min(1, Math.hypot(sx, cx));   // float noise can push it past 1
        mean = Math.atan2(sx, cx) / r;
        if (!SIGNED.has(ch)) mean = (mean + 360) % 360;
        sd = R > 1e-9 ? Math.sqrt(-2 * Math.log(R)) / r : 180;
      } else {
        mean = v.reduce((p, x) => p + x, 0) / v.length;
        sd = v.length > 1 ? Math.sqrt(v.reduce((p, x) => p + (x - mean) ** 2, 0) / v.length) : 0;
      }
      stats[ch] = { mean: Math.round(mean * 100) / 100, sd: Math.round(sd * 100) / 100, n: v.length };
    }
    return { start: lo, end: hi, seconds: hi - lo, n: inside.length, left_out: inside.length - used.length, stats };
  }

  // B − A for a channel; angles take the short way round
  function diff(ch, a, b) {
    if (a == null || b == null) return null;
    let d = b - a;
    if (ANGLE.has(ch)) d = ((d + 540) % 360) - 180;
    return Math.round(d * 100) / 100;
  }

  // markers sorted in time → windows A (1–2) and B (3–4), each null until both ends exist
  function windows(markers) {
    const m = (markers || []).slice().sort((x, y) => x - y);
    return { A: m.length >= 2 ? [m[0], m[1]] : null, B: m.length >= 4 ? [m[2], m[3]] : null, sorted: m };
  }

  function addMarker(markers, t, max) {
    const m = (markers || []).slice();
    if (m.length < (max || 4)) { m.push(t); return m.sort((x, y) => x - y); }
    let bi = 0;                                  // full: the nearest marker moves here
    for (let i = 1; i < m.length; i++) if (Math.abs(m[i] - t) < Math.abs(m[bi] - t)) bi = i;
    m[bi] = t;
    return m.sort((x, y) => x - y);
  }

  function fmtClock(t) {
    if (t == null || !isFinite(t)) return "—";
    return new Date(t * 1000).toISOString().slice(11, 19) + "Z";
  }
  function fmtDur(s) {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return h ? `${h}h${String(m).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
  }
  function twsColor(tws) {
    return tws < 6 ? "#4aa3ff" : tws < 12 ? "#43c463" : tws < 18 ? "#f5c542" : tws < 24 ? "#ef8a3a" : "#e0524a";
  }

  return { LABELS, UNITS, ANGLE, idxAt, posAt, bearing, frameAt, inDanger, windowStats, diff,
           windows, addMarker, fmtClock, fmtDur, twsColor };
})();

// ---- the card ---------------------------------------------------------------------------------
const Replay = (function () {
  const C = ReplayCore;
  const SPEEDS = [1, 2, 5, 10, 30, 60, 120, 300];
  const DEFAULT_CH = ["bsp", "tws", "twa", "heel"];
  const LANE_H = 64, AXIS_H = 18;
  const TEST_MIN = 120, TEST_MAX = 300;
  const FLEET_TAIL_S = 1800;                      // fleet trails: the last 30 min
  const S = { open: false, raceId: null, recording: null, boatId: null, name: "", data: null,
    ch: null, chBusy: false, chSeq: 0, t: 0, playing: false, speed: 60, last: 0, follow: true, showFleet: true,
    showWind: true, chans: null, markers: [], view: null, msg: "", windBusy: false, windMsg: "",
    saveMsg: "", dragging: null };
  let node = null, map = null, overlay = null, ownLine = null, markGroup = null, raf = 0, drawQ = 0;

  function api(path) { return apiGet(path).then((r) => r.json()); }
  function q(o) { return Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&"); }
  function key() { return { race_id: S.raceId || "", recording: S.recording == null ? "" : S.recording }; }

  // ---- open / close / attach --------------------------------------------------------------
  async function openRow(i, src) {
    const x = debList(src)[i];
    if (!x) return;
    await open(src === "p" ? null : Deb.raceId, x.recording, x.boat_id || null, x.name || "");
  }

  async function open(raceId, recording, boatId, name) {
    stop();
    Object.assign(S, { open: true, raceId, recording, boatId, name, data: null, ch: null,
      markers: [], view: null, msg: "Loading…", saveMsg: "", windMsg: "" });
    ensureNode(); attach(); paint();
    try {
      const d = await api("/api/debrief/replay?" + q(key()));
      if (!S.open || S.recording !== recording || S.raceId !== raceId) return;
      S.data = d; S.msg = d.available ? "" : (d.note || "Can't replay this recording.");
      if (d.available) S.t = d.own.t0;
    } catch (e) { S.msg = "Couldn't load the replay."; }
    paint();
    if (S.data && S.data.available) { buildMap(); loadChannels(null, null); }
    if (node) node.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function close() {
    stop(); S.open = false; S.data = null; S.ch = null;
    if (map) { map.remove(); map = null; }
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  // Debrief repaints replace #view; the card's node is moved back into the fresh host so the map
  // survives. A different race closes it (a practice sail stays open across races).
  function attach() {
    if (!S.open || !node) return;
    if (S.raceId && typeof Deb !== "undefined" && Deb.raceId && Deb.raceId !== S.raceId) { close(); return; }
    const host = document.getElementById("replayHost");
    if (!host) return;
    if (node.parentNode !== host) { host.appendChild(node); if (map) setTimeout(() => map.invalidateSize(), 30); }
  }

  function ensureNode() {
    if (node) return;
    injectStyle();
    node = document.createElement("div");
    node.className = "card rp-card";
    node.innerHTML = `<div class="rp-head"></div>
      <div class="rp-mapwrap"><div id="rpMap" class="rp-map"></div></div>
      <div class="rp-bar"></div>
      <div class="rp-chans"></div>
      <div class="rp-chartwrap"><canvas class="rp-chart"></canvas></div>
      <div class="rp-tools"></div>
      <div class="rp-stats"></div>
      <div class="rp-tests"></div>`;
    const cv = node.querySelector(".rp-chart");
    cv.addEventListener("pointerdown", chartDown);
    cv.addEventListener("pointermove", chartMove);
    window.addEventListener("pointerup", () => { S.dragging = null; });
    window.addEventListener("resize", () => queueDraw());
  }

  // ---- paint (the HTML parts; the map + chart draw themselves) --------------------------------
  function paint() {
    if (!node) return;
    const d = S.data;
    const title = `<h3>Replay <span class="muted" style="font-weight:400">— ${esc(S.name || "recording")}${d && d.own ? " · " + esc(new Date(d.own.t0 * 1000).toISOString().slice(0, 10)) : ""}</span>
      <button class="mini ghost" style="float:right" onclick="Replay.close()">Close</button></h3>`;
    node.querySelector(".rp-head").innerHTML = title + (S.msg ? `<div class="placeholder" style="padding:16px">${esc(S.msg)}</div>` : "");
    const ok = d && d.available;
    node.querySelector(".rp-mapwrap").style.display = ok ? "" : "none";
    node.querySelector(".rp-chartwrap").style.display = ok ? "" : "none";
    if (!ok) { ["rp-bar", "rp-chans", "rp-tools", "rp-stats", "rp-tests"].forEach((c) => { node.querySelector("." + c).innerHTML = ""; }); return; }
    paintBar(); paintChans(); paintTools(); paintStats(); paintTests();
  }

  function paintBar() {
    if (!node) return;
    const d = S.data;
    const fleetN = (d.fleet.boats || []).length;
    const wind = d.wind_grid ? `<label class="mv-chk"><input type="checkbox" ${S.showWind ? "checked" : ""} onchange="Replay.set('showWind', this.checked)"> Wind</label>`
      : `<button class="mini" ${S.windBusy ? "disabled" : ""} onclick="Replay.loadWind()">${S.windBusy ? "Loading wind…" : "Load wind"}</button>`;
    node.querySelector(".rp-bar").innerHTML = `
      <button class="mini rp-play" onclick="Replay.toggle()">${S.playing ? "⏸" : "▶"}</button>
      <select onchange="Replay.set('speed', +this.value)" title="Speed">${SPEEDS.map((s) => `<option value="${s}" ${s === S.speed ? "selected" : ""}>${s}×</option>`).join("")}</select>
      <input type="range" class="rp-range" min="${d.own.t0}" max="${d.own.t1}" step="1" value="${S.t}" oninput="Replay.seek(+this.value)">
      <span class="rp-time mono">${C.fmtClock(S.t)}</span>
      <span class="rp-now mono muted"></span>
      <label class="mv-chk"><input type="checkbox" ${S.follow ? "checked" : ""} onchange="Replay.set('follow', this.checked)"> Follow</label>
      ${fleetN ? `<label class="mv-chk"><input type="checkbox" ${S.showFleet ? "checked" : ""} onchange="Replay.set('showFleet', this.checked)"> Fleet (${fleetN})</label>` : `<span class="muted" style="font-size:12px">${esc(d.fleet.note || "")}</span>`}
      ${wind}
      ${S.windMsg ? `<span class="muted" style="font-size:12px">${esc(S.windMsg)}</span>` : ""}`;
    updateNow();
  }

  function paintChans() {
    if (!node) return;
    const ch = S.ch;
    const have = (ch && ch.channels) || [];
    if (ch && ch.available) {
      S.chans = (S.chans || DEFAULT_CH).filter((c) => have.includes(c));
      if (!S.chans.length) S.chans = DEFAULT_CH.filter((c) => have.includes(c));
      if (!S.chans.length) S.chans = have.slice(0, 3);
    } else if (!S.chans) S.chans = [];
    const note = ch && ch.note ? `<span class="muted" style="font-size:12px">${esc(ch.note)}</span>` : "";
    node.querySelector(".rp-chans").innerHTML = S.chBusy && !ch ? '<span class="muted">Loading channels…</span>'
      : (ch && !ch.available ? `<span class="muted">${esc(ch.note || "No channels.")}</span>`
      : Object.keys(C.LABELS).filter((c) => have.includes(c)).map((c) =>
          `<label class="mv-chk"><input type="checkbox" ${S.chans.includes(c) ? "checked" : ""} onchange="Replay.chan('${c}', this.checked)"> ${C.LABELS[c]}</label>`).join("") + " " + note);
    sizeChart();
  }

  function paintTools() {
    if (!node) return;
    const d = S.data;
    const w = C.windows(S.markers);
    const zoomed = S.view && (S.view[0] > d.own.t0 + 1 || S.view[1] < d.own.t1 - 1);
    const left = (d.danger || []).length ? ` <span class="rp-swatch rp-bad"></span> faulty, left out` : (d.trust_ran ? "" : " · instrument check not run");
    node.querySelector(".rp-tools").innerHTML = `
      <button class="mini" onclick="Replay.mark()">Marker here</button>
      <button class="mini ghost" ${S.markers.length ? "" : "disabled"} onclick="Replay.clearMarks()">Clear markers</button>
      <button class="mini ghost" ${w.A ? "" : "disabled"} onclick="Replay.zoom()">Zoom to markers</button>
      ${zoomed ? '<button class="mini ghost" onclick="Replay.unzoom()">Whole race</button>' : ""}
      <span class="muted" style="font-size:12px">Tap the chart to move. Shift-tap or "Marker here" adds a marker. Drag a marker to move it.
      <span class="rp-swatch rp-a"></span> A = 1–2 <span class="rp-swatch rp-b"></span> B = 3–4${left}</span>`;
  }

  function fmtVal(ch, v) {
    if (v == null) return "—";
    if (ch === "twa" || ch === "awa") return (v < 0 ? "P " : "S ") + Math.abs(Math.round(v));
    return C.ANGLE.has(ch) || ch === "pct_polar" ? String(Math.round(v)) : v.toFixed(2);
  }

  function paintStats() {
    if (!node) return;
    const el = node.querySelector(".rp-stats");
    const w = C.windows(S.markers);
    if (!w.A || !S.ch || !S.ch.available) { el.innerHTML = ""; return; }
    const chans = (S.ch.channels || []);
    const a = C.windowStats(S.ch.samples, w.A[0], w.A[1], S.data.danger, chans, S.ch.step_s);
    const b = w.B ? C.windowStats(S.ch.samples, w.B[0], w.B[1], S.data.danger, chans, S.ch.step_s) : null;
    const cell = (st, ch) => st && st.stats[ch] ? `${fmtVal(ch, st.stats[ch].mean)} <span class="muted">± ${st.stats[ch].sd.toFixed(ch === "pct_polar" || C.ANGLE.has(ch) ? 0 : 2)}</span>` : "—";
    const rows = Object.keys(C.LABELS).filter((c) => chans.includes(c)).map((c) => {
      const dd = b ? C.diff(c, (a.stats[c] || {}).mean, (b.stats[c] || {}).mean) : null;
      return `<tr><td>${C.LABELS[c]} <span class="muted">${C.UNITS[c] || ""}</span></td><td>${cell(a, c)}</td>${b ? `<td>${cell(b, c)}</td><td>${dd == null ? "—" : (dd > 0 ? "+" : "") + (C.ANGLE.has(c) || c === "pct_polar" ? Math.round(dd) : dd.toFixed(2))}</td>` : ""}</tr>`;
    }).join("");
    const head = (lbl, st) => `${lbl} <span class="muted" style="font-weight:400">${C.fmtClock(st.start)} · ${C.fmtDur(st.seconds)}${st.left_out ? ` · ${st.left_out} left out` : ""}</span>`;
    const okLen = a.seconds >= TEST_MIN && a.seconds <= TEST_MAX;
    el.innerHTML = `<table class="fleet-tbl rp-stbl"><thead><tr><th></th><th>${head("A", a)}</th>${b ? `<th>${head("B", b)}</th><th>B − A</th>` : ""}</tr></thead><tbody>${rows}</tbody></table>
      <div class="opt-controls" style="margin-top:6px">
        <input id="rpTestName" placeholder="Test name, e.g. port, new jib" style="width:240px" maxlength="80">
        <button class="mini" ${okLen ? "" : "disabled"} onclick="Replay.saveTest()">Save A as test</button>
        <span class="muted" style="font-size:12px">${okLen ? "" : "A test is 2 to 5 minutes. A is " + C.fmtDur(a.seconds) + "."} ${esc(S.saveMsg)}</span>
      </div>`;
  }

  function paintTests() {
    if (!node) return;
    const el = node.querySelector(".rp-tests");
    const ts = (S.data && S.data.tests) || [];
    if (!ts.length) { el.innerHTML = ""; return; }
    const v = (x, c) => x.stats && x.stats[c] ? fmtVal(c, x.stats[c].mean) : "—";
    el.innerHTML = `<h4>Saved tests</h4><table class="fleet-tbl"><thead><tr><th>Name</th><th>Start</th><th>Length</th><th>BSP</th><th>TWS</th><th>TWA</th><th>% polar</th><th></th></tr></thead><tbody>
      ${ts.map((x) => `<tr><td><b>${esc(x.name)}</b>${x.left_out ? ` <span class="muted" style="font-size:11px">${x.left_out} left out</span>` : ""}</td>
        <td class="mono">${C.fmtClock(x.start)}</td><td>${C.fmtDur(x.seconds)}</td>
        <td>${v(x, "bsp")}</td><td>${v(x, "tws")}</td><td>${v(x, "twa")}</td><td>${v(x, "pct_polar")}</td>
        <td style="white-space:nowrap"><button class="mini" onclick="Replay.showTest('${attr(x.id)}')">Show</button>
          <button class="mini ghost" onclick="Replay.delTest('${attr(x.id)}')">Delete</button></td></tr>`).join("")}</tbody></table>`;
  }

  // ---- channels --------------------------------------------------------------------------------
  async function loadChannels(a, b) {
    S.chBusy = true; paintChans();
    // only the newest request for the recording still open may land (a slow zoom must not
    // overwrite "Whole race", nor one race's answer another race's with the same timestamp)
    const seq = ++S.chSeq, race = S.raceId, rec = S.recording;
    const current = () => S.open && seq === S.chSeq && S.raceId === race && S.recording === rec;
    try {
      const r = await api("/api/debrief/replay/channels?" + q({ ...key(), start: a, end: b, max_points: 4000, boat_id: S.boatId }));
      if (!current()) return;
      S.ch = r;
      S.view = r.available ? [a != null ? a : S.data.own.t0, b != null ? b : S.data.own.t1] : null;
    } catch (e) {
      if (!current()) return;
      S.ch = { available: false, note: "Couldn't load the channels." };
    }
    S.chBusy = false;
    paintChans(); paintTools(); paintStats(); queueDraw();
  }

  // ---- the time cursor (the one thing both views follow) ---------------------------------------
  function seek(t, fromPlay) {
    const d = S.data;
    if (!d || !d.available) return;
    S.t = Math.max(d.own.t0, Math.min(d.own.t1, t));
    if (!fromPlay && S.playing) { /* keep playing from the new spot */ S.last = performance.now(); }
    const rng = node.querySelector(".rp-range");
    if (rng && +rng.value !== Math.round(S.t)) rng.value = Math.round(S.t);
    const tl = node.querySelector(".rp-time");
    if (tl) tl.textContent = C.fmtClock(S.t);
    updateNow();
    queueDraw();
  }

  function updateNow() {
    const el = node && node.querySelector(".rp-now");
    if (!el || !S.ch || !S.ch.samples) return;
    const i = C.idxAt(S.ch.samples, S.t, (s) => s.t);
    const s = S.ch.samples[Math.max(0, i)];
    if (!s) { el.textContent = ""; return; }
    el.textContent = ["bsp", "sog", "tws", "twa"].filter((c) => s[c] != null).map((c) => C.LABELS[c] + " " + fmtVal(c, s[c])).join(" · ");
  }

  function toggle() {
    if (S.playing) { stop(); paintBar(); return; }
    const d = S.data;
    if (!d || !d.available) return;
    if (S.t >= d.own.t1) S.t = d.own.t0;
    S.playing = true; S.last = performance.now();
    raf = requestAnimationFrame(tick);
    paintBar();
  }
  function tick(now) {
    if (!S.playing) return;
    const dt = (now - S.last) / 1000; S.last = now;
    seek(S.t + dt * S.speed, true);
    if (S.t >= S.data.own.t1) { stop(); paintBar(); return; }
    raf = requestAnimationFrame(tick);
  }
  function stop() { S.playing = false; if (raf) cancelAnimationFrame(raf); raf = 0; }

  function queueDraw() {
    if (drawQ) return;
    drawQ = requestAnimationFrame(() => { drawQ = 0; drawChart(); drawMap(); });
  }

  // ---- map -------------------------------------------------------------------------------------
  const Overlay = (typeof L !== "undefined") ? L.Layer.extend({
    onAdd: function (m) {
      this._map = m;
      this._c = L.DomUtil.create("canvas", "rp-canvas");
      Object.assign(this._c.style, { position: "absolute", pointerEvents: "none" });
      m.getPanes().overlayPane.appendChild(this._c);
      m.on("moveend zoomend resize viewreset", this.redraw, this);
      this.redraw();
    },
    onRemove: function (m) { m.off("moveend zoomend resize viewreset", this.redraw, this); L.DomUtil.remove(this._c); },
    redraw: function () {
      const m = this._map; if (!m) return this;
      const size = m.getSize();
      L.DomUtil.setPosition(this._c, m.containerPointToLayerPoint([0, 0]));
      this._c.width = size.x; this._c.height = size.y;
      const ctx = this._c.getContext("2d");
      ctx.clearRect(0, 0, size.x, size.y);
      paintMap(ctx, (lat, lon) => m.latLngToContainerPoint([lat, lon]), size);
      return this;
    },
  }) : null;

  function buildMap() {
    if (map) { map.remove(); map = null; }
    const d = S.data;
    map = L.map("rpMap", { preferCanvas: true, zoomControl: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
    const pts = d.own.fixes.map((f) => [f[1], f[2]]);
    const b = L.latLngBounds(pts);
    map.setView(b.getCenter(), 10);
    ownLine = L.polyline(pts, { color: "#8fa3b8", weight: 2, opacity: 0.45 }).addTo(map);
    ownLine.on("click", (e) => {                 // tap our track: jump the cursor to that fix
      let bi = 0, bd = Infinity;
      d.own.fixes.forEach((f, i) => { const dd = (f[1] - e.latlng.lat) ** 2 + (f[2] - e.latlng.lng) ** 2; if (dd < bd) { bd = dd; bi = i; } });
      seek(d.own.fixes[bi][0]);
    });
    markGroup = L.layerGroup().addTo(map);
    (d.marks || []).forEach((m) => L.circleMarker([m.lat, m.lon], { radius: 5, color: "#fff", weight: 1.5, fillColor: "#ed1c24", fillOpacity: 1 })
      .bindTooltip(esc(m.name || "mark")).addTo(markGroup));
    overlay = new Overlay().addTo(map);
    setTimeout(() => { if (map) { map.invalidateSize(); map.fitBounds(b, { padding: [24, 24] }); } }, 60);
  }

  function drawMap() { if (overlay) overlay.redraw(); followBoat(); }

  function followBoat() {
    if (!map || !S.follow || !S.playing) return;
    const p = C.posAt(S.data.own.fixes, S.t);
    if (!p) return;
    const pt = map.latLngToContainerPoint([p.lat, p.lon]), sz = map.getSize();
    if (pt.x < sz.x * 0.15 || pt.x > sz.x * 0.85 || pt.y < sz.y * 0.15 || pt.y > sz.y * 0.85) map.panTo([p.lat, p.lon], { animate: false });
  }

  function paintMap(ctx, proj, size) {
    const d = S.data;
    if (!d || !d.available) return;
    if (S.showWind && d.wind_grid) paintWind(ctx, proj, size, d.wind_grid);
    if (S.showFleet) {
      ctx.font = "11px sans-serif";
      for (const bt of d.fleet.boats || []) {
        if (bt.own) continue;                    // our own boat is drawn from our track
        paintBoat(ctx, proj, bt.fixes, "#f5b13d", 1.4, 4, bt.name, FLEET_TAIL_S);
      }
    }
    paintBoat(ctx, proj, d.own.fixes, "#ed1c24", 3, 7, null, null);
  }

  function paintBoat(ctx, proj, fx, color, w, r, label, tail) {
    const p = C.posAt(fx, S.t);
    if (!p) return;
    const from = tail ? C.idxAt(fx, S.t - tail) : 0;
    ctx.strokeStyle = color; ctx.lineWidth = w; ctx.globalAlpha = tail ? 0.55 : 0.95;
    ctx.beginPath();
    let first = true;
    for (let i = Math.max(0, from); i <= p.i; i++) {
      const q2 = proj(fx[i][1], fx[i][2]);
      if (first) { ctx.moveTo(q2.x, q2.y); first = false; } else ctx.lineTo(q2.x, q2.y);
    }
    const here = proj(p.lat, p.lon);
    ctx.lineTo(here.x, here.y); ctx.stroke(); ctx.globalAlpha = 1;
    // a pointed marker along the course (COG from the fix, else from the last leg)
    const a = fx[p.i], b = fx[Math.min(p.i + 1, fx.length - 1)];
    const cog = a[4] != null ? a[4] : (b !== a ? C.bearing(a, b) : 0);
    const th = cog * Math.PI / 180, dx = Math.sin(th), dy = -Math.cos(th);
    ctx.fillStyle = color; ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(here.x + dx * r * 1.6, here.y + dy * r * 1.6);
    ctx.lineTo(here.x - dx * r + dy * r * 0.8, here.y - dy * r - dx * r * 0.8);
    ctx.lineTo(here.x - dx * r - dy * r * 0.8, here.y - dy * r + dx * r * 0.8);
    ctx.closePath(); ctx.fill(); ctx.stroke();
    if (label) { ctx.fillStyle = "#16202b"; ctx.fillText(label, here.x + 6, here.y - 6); }
  }

  function paintWind(ctx, proj, size, g) {
    const fi = C.frameAt(g.times, S.t);
    const frame = (g.frames || [])[fi] || [];
    const drawn = [];
    for (const c of frame) {
      if (c.tws == null || c.twd == null) continue;
      const pt = proj(c.lat, c.lon);
      if (pt.x < -20 || pt.y < -20 || pt.x > size.x + 20 || pt.y > size.y + 20) continue;
      if (drawn.some((o) => Math.abs(o.x - pt.x) < 28 && Math.abs(o.y - pt.y) < 28)) continue;
      drawn.push(pt);
      const th = (c.twd + 180) * Math.PI / 180, dx = Math.sin(th), dy = -Math.cos(th);
      const len = 9 + Math.min(20, c.tws * 0.9), hx = pt.x + dx * len, hy = pt.y + dy * len;
      ctx.globalAlpha = 0.3 + 0.7 * (c.confidence == null ? 0.6 : c.confidence);
      ctx.strokeStyle = C.twsColor(c.tws); ctx.fillStyle = ctx.strokeStyle; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(pt.x, pt.y); ctx.lineTo(hx, hy); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(hx, hy);
      ctx.lineTo(hx - 5 * (dx * 0.88 - dy * 0.48), hy - 5 * (dy * 0.88 + dx * 0.48));
      ctx.lineTo(hx - 5 * (dx * 0.88 + dy * 0.48), hy - 5 * (dy * 0.88 - dx * 0.48));
      ctx.closePath(); ctx.fill();
    }
    ctx.globalAlpha = 1;
    if (g.times && g.times[fi] != null) {
      ctx.fillStyle = "rgba(255,255,255,.85)"; ctx.fillRect(size.x - 150, size.y - 22, 146, 18);
      ctx.fillStyle = "#16202b"; ctx.font = "11px sans-serif";
      ctx.fillText("Wind " + C.fmtClock(g.times[fi]), size.x - 144, size.y - 9);
    }
  }

  // ---- strip chart -----------------------------------------------------------------------------
  function chartEl() { return node && node.querySelector(".rp-chart"); }

  function sizeChart() {
    const cv = chartEl();
    if (!cv) return;
    const lanes = Math.max(1, (S.chans || []).length);
    cv.style.height = (lanes * LANE_H + AXIS_H) + "px";
    queueDraw();
  }

  function xOf(t, w) { const v = S.view; return v ? ((t - v[0]) / Math.max(1, v[1] - v[0])) * w : 0; }
  function tOf(x, w) { const v = S.view; return v[0] + (x / w) * (v[1] - v[0]); }

  function drawChart() {
    const cv = chartEl();
    if (!cv || !S.ch || !S.ch.available || !S.view) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    cv.width = w * dpr; cv.height = h * dpr;
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const v = S.view;
    const samples = S.ch.samples.filter((s) => s.t >= v[0] && s.t <= v[1]);
    const lanes = S.chans || [];
    const win = C.windows(S.markers);
    // windows A and B, then faulty stretches
    const band = (iv, col) => { if (!iv) return; const x0 = xOf(iv[0], w), x1 = xOf(iv[1], w); ctx.fillStyle = col; ctx.fillRect(x0, 0, x1 - x0, h - AXIS_H); };
    band(win.A, "rgba(74,163,255,.14)"); band(win.B, "rgba(245,177,61,.16)");
    for (const iv of S.data.danger || []) band(iv, "rgba(150,150,160,.35)");
    ctx.font = "11px sans-serif";
    lanes.forEach((ch, li) => {
      const y0 = li * LANE_H, y1 = y0 + LANE_H - 6;
      const vals = samples.map((s) => s[ch]).filter((x) => x != null);
      ctx.strokeStyle = "rgba(255,255,255,.08)"; ctx.beginPath(); ctx.moveTo(0, y0 + LANE_H - 3); ctx.lineTo(w, y0 + LANE_H - 3); ctx.stroke();
      if (!vals.length) { ctx.fillStyle = "#9a9aa6"; ctx.fillText(C.LABELS[ch] + ": no data", 6, y0 + 14); return; }
      let lo = Math.min(...vals), hi = Math.max(...vals);
      if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
      const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
      const yOf = (x) => y1 - ((x - lo) / (hi - lo)) * (y1 - y0 - 4);
      ctx.lineWidth = 1.4;
      let prev = null;
      for (const s of samples) {
        const val = s[ch];
        if (val == null) { prev = null; continue; }
        const bad = C.inDanger(s.t, S.data.danger, S.ch.step_s);
        const x = xOf(s.t, w), y = yOf(val);
        // angles: don't draw a line across a wrap (359 → 1)
        const wrap = prev && C.ANGLE.has(ch) && Math.abs(val - prev.v) > 180;
        if (prev && !wrap) {
          ctx.strokeStyle = bad ? "#6b6b75" : "#cdd2da";
          ctx.beginPath(); ctx.moveTo(prev.x, prev.y); ctx.lineTo(x, y); ctx.stroke();
        }
        prev = { x, y, v: val };
      }
      ctx.fillStyle = "#f3f4f6";
      ctx.fillText(`${C.LABELS[ch]} ${C.UNITS[ch] || ""}`, 6, y0 + 13);
      ctx.fillStyle = "#9a9aa6";
      ctx.fillText(fmtVal(ch, hi - pad), w - 44, y0 + 13);
      ctx.fillText(fmtVal(ch, lo + pad), w - 44, y1);
    });
    // time axis
    ctx.fillStyle = "#9a9aa6";
    const n = Math.max(2, Math.floor(w / 110));
    for (let i = 0; i <= n; i++) {
      const t = v[0] + (i / n) * (v[1] - v[0]);
      const x = (i / n) * w;
      ctx.fillText(C.fmtClock(t).slice(0, 5), Math.min(w - 32, Math.max(0, x - 14)), h - 4);
    }
    // markers
    win.sorted.forEach((t, i) => {
      const x = xOf(t, w);
      ctx.strokeStyle = i < 2 ? "#4aa3ff" : "#f5b13d"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h - AXIS_H); ctx.stroke();
      ctx.fillStyle = ctx.strokeStyle; ctx.fillText(String(i + 1), x + 3, 11);
    });
    // cursor
    const cx = xOf(S.t, w);
    ctx.strokeStyle = "#ed1c24"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, h - AXIS_H); ctx.stroke();
  }

  function chartX(e) { const r = chartEl().getBoundingClientRect(); return { x: e.clientX - r.left, w: r.width }; }

  function chartDown(e) {
    if (!S.view) return;
    const { x, w } = chartX(e);
    const sorted = C.windows(S.markers).sorted;
    const near = sorted.findIndex((t) => Math.abs(xOf(t, w) - x) < 7);
    if (near >= 0) { S.dragging = { from: sorted[near] }; return; }
    const t = tOf(x, w);
    if (e.shiftKey) { S.markers = C.addMarker(S.markers, t); markersChanged(); return; }
    S.dragging = { cursor: true };
    seek(t);
  }

  function chartMove(e) {
    if (!S.dragging || !S.view) return;
    const { x, w } = chartX(e);
    const t = Math.max(S.view[0], Math.min(S.view[1], tOf(x, w)));
    if (S.dragging.cursor) { seek(t); return; }
    const i = S.markers.findIndex((m) => m === S.dragging.from);
    if (i < 0) return;
    S.markers[i] = t; S.dragging.from = t;
    S.markers.sort((a, b) => a - b);
    markersChanged(true);
  }

  function markersChanged(light) {
    S.saveMsg = "";
    if (!light) paintTools();
    paintStats(); queueDraw();
  }

  // ---- actions ---------------------------------------------------------------------------------
  function set(k, v) {
    S[k] = v;
    if (k === "speed" || k === "follow") return;
    queueDraw();
  }
  function chan(c, on) {
    S.chans = on ? Object.keys(C.LABELS).filter((x) => x === c || S.chans.includes(x)) : S.chans.filter((x) => x !== c);
    sizeChart();
  }
  function mark() { S.markers = C.addMarker(S.markers, S.t); markersChanged(); }
  function clearMarks() { S.markers = []; markersChanged(); }
  function zoom() {
    const w = C.windows(S.markers);
    const iv = w.B ? [w.A[0], w.B[1]] : w.A;
    if (!iv) return;
    const pad = Math.max(30, (iv[1] - iv[0]) * 0.15);
    loadChannels(Math.max(S.data.own.t0, iv[0] - pad), Math.min(S.data.own.t1, iv[1] + pad));
  }
  function unzoom() { loadChannels(null, null); }

  async function saveTest() {
    const w = C.windows(S.markers);
    const nm = (document.getElementById("rpTestName") || {}).value || "";
    if (!w.A) return;
    S.saveMsg = "Saving…"; paintStats();
    try {
      const r = await (await apiPost("/api/debrief/replay/tests", { ...key(), recording: S.recording, name: nm, start: w.A[0], end: w.A[1], boat_id: S.boatId })).json();
      if (r.ok) { S.data.tests = (S.data.tests || []).concat([r.test]); S.saveMsg = "Saved."; paintTests(); }
      else S.saveMsg = r.note || r.detail || "Couldn't save.";
    } catch (e) { S.saveMsg = "Couldn't save."; }
    paintStats();
  }
  function showTest(id) {
    const x = (S.data.tests || []).find((y) => y.id === id);
    if (!x) return;
    S.markers = [x.start, x.end]; seek(x.start); markersChanged();
  }
  async function delTest(id) {
    try {
      const r = await (await apiPost("/api/debrief/replay/tests/delete", { ...key(), recording: S.recording, id })).json();
      if (r.ok) S.data.tests = (S.data.tests || []).filter((y) => y.id !== id);
    } catch (e) { /* the list stays as it was */ }
    paintTests();
  }

  async function loadWind() {
    S.windBusy = true; S.windMsg = ""; paintBar();
    const rec = S.recording, race = S.raceId;
    let jobId = "";
    try {
      const r = await (await apiPost("/api/debrief/replay/wind", { ...key(), recording: S.recording })).json();
      if (r.ok === false) { S.windBusy = false; S.windMsg = r.note || "Couldn't start."; paintBar(); return; }
      jobId = r.job_id || "";
    } catch (e) { S.windBusy = false; S.windMsg = "Couldn't start."; paintBar(); return; }
    const poll = async () => {
      if (!S.open || S.recording !== rec || S.raceId !== race) return;
      let j;
      try { j = await api("/api/debrief/replay/wind/status?" + q({ job_id: jobId })); } catch (e) { j = null; }
      if (!S.open || S.recording !== rec || S.raceId !== race) return;
      if (j && j.state === "running") { S.windMsg = (j.progress || []).slice(-1)[0] || ""; paintBar(); setTimeout(poll, 4000); return; }
      S.windBusy = false;
      const res = (j && j.result) || {};
      if (j && j.state === "done" && res.ok) {
        S.windMsg = "";
        try {
          const d = await api("/api/debrief/replay?" + q(key()));
          if (d.available && S.data && S.recording === rec && S.raceId === race) S.data.wind_grid = d.wind_grid;
        } catch (e) { /* keep going without */ }
      } else S.windMsg = res.note || (j && j.error) || "No wind.";
      paintBar(); queueDraw();
    };
    setTimeout(poll, 2000);
  }

  function injectStyle() {
    if (document.getElementById("rpStyle")) return;
    const st = document.createElement("style");
    st.id = "rpStyle";
    st.textContent = `
      .rp-map { height: 440px; border-radius: 10px; overflow: hidden; margin: 6px 0; }
      .rp-bar, .rp-chans, .rp-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 6px 0; }
      .rp-range { flex: 1 1 220px; min-width: 160px; }
      .rp-play { min-width: 40px; font-size: 16px; }
      .rp-time { min-width: 72px; }
      .rp-now { font-size: 12px; }
      .rp-chart { width: 100%; display: block; background: var(--panel2); border-radius: 8px; touch-action: none; cursor: crosshair; }
      .rp-swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: middle; margin-left: 6px; }
      .rp-a { background: rgba(74,163,255,.6); } .rp-b { background: rgba(245,177,61,.7); } .rp-bad { background: rgba(150,150,160,.8); }
      .rp-stbl td, .rp-stbl th { white-space: nowrap; }
      .rp-stats, .rp-tests { margin-top: 8px; overflow-x: auto; }
      @media (max-width: 700px) { .rp-map { height: 320px; } }`;
    document.head.appendChild(st);
  }

  return { open, openRow, close, attach, toggle, seek, set, chan, mark, clearMarks, zoom, unzoom,
           saveTest, showTest, delTest, loadWind, _state: S, _loadChannels: loadChannels };
})();
