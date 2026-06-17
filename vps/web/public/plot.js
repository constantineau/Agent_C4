/* Schematic course plot + navigator panel (5.2). Draws the boat, marks, course legs, the
   active leg's laylines, a wind arrow and the boat's track on a local equirectangular
   projection (no chart tiles — offline-safe). North-up / course-up toggle. Data from
   /api/course and /api/navigator; boat state from the live conditions event. */
"use strict";
const Plot = (function () {
  const R2D = 180 / Math.PI, D2R = Math.PI / 180;
  let route = localStorage.getItem("sr33.route") || "default";
  let orient = localStorage.getItem("sr33.orient") || "north";   // north | course
  let course = null, nav = null, boat = null, tactics = null, routeData = null;
  let routeOn = false;                             // boat = {lat,lon,hdg}
  const track = [];                                // recent {lat,lon}
  let lastNav = 0;

  // Onboard (Pi engine) is never "racing" for gating purposes — the boat's own computer is
  // legal in-race, so navigator/tactics/route stay available.
  function racing() { return (typeof App !== "undefined" && !App.onboard && App.mode === "race"); }

  function setRoute(r) { route = r; localStorage.setItem("sr33.route", r);
    document.getElementById("routeName").textContent = r; loadCourse(); }

  async function loadCourse() {
    try { course = await (await apiFetch("/api/course?route=" + route)).json(); }
    catch (e) { course = null; }
    document.getElementById("routeName").textContent = route;
    draw();
  }
  async function loadNav() {
    // Navigator (next mark/ETA/laylines) + tactics are gated by race mode (RRS 41) — withheld
    // server-side in a race, so don't fetch them. The course marks (/api/course) stay: they're
    // the published course, available to all.
    if (racing()) { nav = null; tactics = null; renderPanel(); draw(); return; }
    try { nav = await (await apiFetch("/api/navigator?route=" + route)).json(); }
    catch (e) { nav = null; }
    try { tactics = await (await apiFetch("/api/tactics?route=" + route)).json(); }
    catch (e) { tactics = null; }
    renderPanel(); draw();
  }
  async function fetchRoute() {
    if (!routeOn || racing()) { routeData = null; return; }
    try { routeData = await (await apiFetch("/api/route?target=next&route=" + route)).json(); }
    catch (e) { routeData = null; }
    renderPanel(); draw();
  }
  function toggleRoute() {
    routeOn = !routeOn;
    document.getElementById("routeBtn").classList.toggle("on", routeOn);
    if (routeOn && !racing()) { routeData = { available: false }; renderPanel(); draw(); fetchRoute(); }
    else { routeData = null; renderPanel(); draw(); }
  }
  async function dropPractice() {
    try {
      await apiFetch("/api/course/practice?leg_nm=1.0", { method: "POST" });
      setRoute("practice"); await loadNav();
    } catch (e) {}
  }
  function toggleOrient() {
    orient = orient === "north" ? "course" : "north";
    localStorage.setItem("sr33.orient", orient);
    document.getElementById("orientBtn").textContent = orient === "north" ? "N↑" : "Crs↑";
    document.getElementById("orientBtn").classList.toggle("on", orient === "course");
    draw();
  }

  // ---- projection: lat/lon -> nm (east,north) relative to a reference, then rotate+fit ----
  function toScreen(lat, lon, P) {
    const x = (lon - P.lon0) * 60 * P.cl, y = (lat - P.lat0) * 60;
    const rx = x * P.cs - y * P.sn, ry = x * P.sn + y * P.cs;
    return { x: P.ox + rx * P.scale, y: P.oy - ry * P.scale };
  }

  function buildProjection(w, h) {
    const pts = [];
    if (course && course.marks) course.marks.forEach((m) => pts.push({ lat: m.lat, lon: m.lon }));
    if (boat) pts.push({ lat: boat.lat, lon: boat.lon });
    track.forEach((t) => pts.push(t));
    if (routeOn && routeData && routeData.path) routeData.path.forEach((p) => pts.push(p));
    if (!pts.length) return null;
    const lat0 = pts.reduce((s, p) => s + p.lat, 0) / pts.length;
    const lon0 = pts.reduce((s, p) => s + p.lon, 0) / pts.length;
    const cl = Math.cos(lat0 * D2R);
    const upBrg = (orient === "course" && nav && nav.available)
      ? nav.next_mark.bearing_deg * D2R : 0;
    const cs = Math.cos(upBrg), sn = Math.sin(upBrg);
    let minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;
    pts.forEach((p) => {
      const x = (p.lon - lon0) * 60 * cl, y = (p.lat - lat0) * 60;
      const rx = x * cs - y * sn, ry = x * sn + y * cs;
      minX = Math.min(minX, rx); maxX = Math.max(maxX, rx);
      minY = Math.min(minY, ry); maxY = Math.max(maxY, ry);
    });
    const pad = 36, spanX = Math.max(0.05, maxX - minX), spanY = Math.max(0.05, maxY - minY);
    const scale = Math.min((w - 2 * pad) / spanX, (h - 2 * pad) / spanY);
    const ox = pad - minX * scale + (w - 2 * pad - spanX * scale) / 2;
    const oy = h - pad + minY * scale - (h - 2 * pad - spanY * scale) / 2;
    return { lat0, lon0, cl, cs, sn, scale, ox, oy };
  }

  function rayFromMark(mark, brgDeg, P, lenNm) {
    const b = brgDeg * D2R;
    const lat2 = mark.lat + (lenNm / 60) * Math.cos(b);
    const lon2 = mark.lon + (lenNm / 60) * Math.sin(b) / Math.max(0.1, P.cl);
    return toScreen(lat2, lon2, P);
  }

  function css(v) { return getComputedStyle(document.body).getPropertyValue(v).trim(); }

  function draw() {
    const cv = document.getElementById("coursePlot");
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    cv.width = w * dpr; cv.height = h * dpr;
    const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const ink = css("--text"), mut = css("--muted"), acc = css("--accent"),
      ok = css("--ok"), bad = css("--bad"), line = css("--line");

    if (!course || !course.marks || !course.marks.length) {
      g.fillStyle = mut; g.font = "13px system-ui"; g.textAlign = "center";
      g.fillText("No course — tap “Drop practice course”.", w / 2, h / 2); return;
    }
    const P = buildProjection(w, h);
    if (!P) return;
    const M = course.marks.map((m) => ({ m, s: toScreen(m.lat, m.lon, P) }));

    // course legs
    g.strokeStyle = line; g.lineWidth = 2; g.setLineDash([]);
    g.beginPath();
    M.forEach((p, i) => i ? g.lineTo(p.s.x, p.s.y) : g.moveTo(p.s.x, p.s.y));
    g.stroke();

    // laylines for the active leg (windward/leeward mark = the next mark)
    if (nav && nav.available && nav.leg.laylines) {
      const nm = M.find((p) => p.m.name === nav.next_mark.name) || M[0];
      const L = nav.leg.laylines, len = 1.2 * (nav.next_mark.distance_nm || 1) + 0.3;
      g.strokeStyle = mut; g.lineWidth = 1.4; g.setLineDash([6, 5]);
      [L.starboard_from_mark, L.port_from_mark].forEach((brg) => {
        const e = rayFromMark(nm.m, brg, P, len);
        g.beginPath(); g.moveTo(nm.s.x, nm.s.y); g.lineTo(e.x, e.y); g.stroke();
      });
      g.setLineDash([]);
    }

    // optimal route overlay (practice only)
    if (routeOn && !racing() && routeData && routeData.available && routeData.path && routeData.path.length > 1) {
      g.strokeStyle = acc; g.lineWidth = 2.5; g.setLineDash([]);
      g.beginPath();
      routeData.path.forEach((p, i) => { const s = toScreen(p.lat, p.lon, P); i ? g.lineTo(s.x, s.y) : g.moveTo(s.x, s.y); });
      g.stroke();
      routeData.path.forEach((p) => { const s = toScreen(p.lat, p.lon, P); g.fillStyle = acc; g.beginPath(); g.arc(s.x, s.y, 2.6, 0, 2 * Math.PI); g.fill(); });
    }

    // marks
    M.forEach((p, i) => {
      const isNext = nav && nav.available && p.m.name === nav.next_mark.name;
      g.fillStyle = isNext ? acc : ink;
      g.beginPath(); g.arc(p.s.x, p.s.y, isNext ? 7 : 5, 0, 2 * Math.PI); g.fill();
      g.fillStyle = mut; g.font = "11px system-ui"; g.textAlign = "left";
      g.fillText(p.m.name, p.s.x + 10, p.s.y + 4);
    });

    // track
    if (track.length > 1) {
      g.strokeStyle = acc; g.globalAlpha = 0.5; g.lineWidth = 1.5; g.beginPath();
      track.forEach((t, i) => { const s = toScreen(t.lat, t.lon, P); i ? g.lineTo(s.x, s.y) : g.moveTo(s.x, s.y); });
      g.stroke(); g.globalAlpha = 1;
    }

    // boat (triangle pointing to heading/cog), rotated for course-up
    if (boat) {
      const s = toScreen(boat.lat, boat.lon, P);
      const upBrg = (orient === "course" && nav && nav.available) ? nav.next_mark.bearing_deg : 0;
      const hdg = ((boat.hdg ?? 0) - upBrg) * D2R;
      g.save(); g.translate(s.x, s.y); g.rotate(hdg);
      g.fillStyle = ok; g.beginPath();
      g.moveTo(0, -11); g.lineTo(7, 9); g.lineTo(0, 5); g.lineTo(-7, 9); g.closePath(); g.fill();
      g.restore();
      // lifted/headed badge by the boat (practice only)
      if (tactics && tactics.available && tactics.phase !== "even") {
        const col = tactics.phase === "lifted" ? ok : bad;
        g.fillStyle = col; g.font = "700 11px system-ui"; g.textAlign = "left";
        g.fillText(tactics.phase.toUpperCase(), s.x + 12, s.y - 8);
      }
    }

    // wind arrow (points the way the wind blows TO) + TWD label, top-left
    if (nav && nav.available && nav.wind.twd != null) {
      const upBrg = (orient === "course" && nav.available) ? nav.next_mark.bearing_deg : 0;
      const a = (nav.wind.twd + 180 - upBrg) * D2R, cxw = 30, cyw = 30, len = 16;
      g.strokeStyle = bad; g.fillStyle = bad; g.lineWidth = 2;
      g.beginPath();
      g.moveTo(cxw - Math.sin(a) * len, cyw + Math.cos(a) * len);
      g.lineTo(cxw + Math.sin(a) * len, cyw - Math.cos(a) * len); g.stroke();
      g.beginPath();
      g.arc(cxw + Math.sin(a) * len, cyw - Math.cos(a) * len, 3, 0, 2 * Math.PI); g.fill();
      g.fillStyle = mut; g.font = "11px system-ui"; g.textAlign = "left";
      g.fillText(Math.round(nav.wind.twd) + "°", cxw + 22, cyw + 4);
    }
  }

  function renderPanel() {
    const el = document.getElementById("navBody");
    if (!nav || !nav.available) {
      el.innerHTML = `<div class="placeholder" style="min-height:80px;">${(nav && nav.note) || "No course loaded — drop a practice course."}</div>`;
      return;
    }
    const n = nav.next_mark, eta = n.eta_min != null ? `${n.eta_min} min` : "—";
    const callCls = (nav.layline_call && nav.layline_call.startsWith("On the")) ? "navcall lay" : "navcall";
    el.innerHTML = `
      <div class="nrow"><span class="lbl">Next mark</span><span class="legtag">${nav.leg.type}</span></div>
      <div class="nrow"><span class="big">${n.name}</span><span class="big">${n.distance_nm} nm</span></div>
      <div class="nrow"><span class="sub">brg ${n.bearing_deg}°</span><span class="sub">ETA ${eta}</span></div>
      <div class="nrow"><span class="sub">wind ${nav.wind.twd ?? "–"}° @ ${nav.wind.tws ?? "–"} kn</span><span class="sub">${nav.remaining_nm} nm to finish</span></div>
      ${nav.layline_call ? `<div class="${callCls}">${nav.layline_call}</div>` : ""}
      ${tacticsHtml()}
      ${routeHtml()}`;
  }

  function routeHtml() {
    if (!routeOn || racing()) return "";
    if (!routeData || !routeData.available) return `<div class="tacgate">Computing route…</div>`;
    const r = routeData;
    return `<div class="tacblock">
      <div class="nrow"><span class="lbl">Route → ${r.target}</span>
        <span class="phasetag">${r.tacks} tack${r.tacks === 1 ? "" : "s"}</span></div>
      <div class="taccall">ETA ~${r.eta_min} min · ${r.sailed_nm} nm (${r.direct_nm} direct).
        Start ${r.first_tack} @ ${r.recommended_heading}°. <span class="sub">${r.wind_source}</span></div>
    </div>`;
  }

  function tacticsHtml() {
    if (racing()) return `<div class="tacgate">Tactics hidden — RACE mode (RRS 41). Switch to Practice to show.</div>`;
    if (!tactics || !tactics.available) return "";
    const ph = tactics.phase, badge = ph === "lifted" ? "ok" : (ph === "headed" ? "bad" : "");
    return `<div class="tacblock">
      <div class="nrow"><span class="lbl">Tactics</span>
        <span class="phasetag ${badge}">${ph.toUpperCase()} · ${tactics.tack}</span></div>
      <div class="taccall">${tactics.recommendation}</div>
    </div>`;
  }

  // ---- wire ----
  window.addEventListener("sr33:conditions", (e) => {
    const c = e.detail;
    if (typeof c.lat === "number" && typeof c.lon === "number") {
      boat = { lat: c.lat, lon: c.lon, hdg: (c.heading ?? c.cog ?? 0) };
      const last = track[track.length - 1];
      if (!last || Math.abs(last.lat - c.lat) > 1e-5 || Math.abs(last.lon - c.lon) > 1e-5) {
        track.push({ lat: c.lat, lon: c.lon }); if (track.length > 400) track.shift();
      }
    }
    if (Date.now() - lastNav > 3000) { lastNav = Date.now(); loadNav(); } else { draw(); }
  });
  window.addEventListener("sr33:mode", () => { fetchRoute(); loadNav(); });  // show/hide tactics+route
  setInterval(() => { if (routeOn && !racing()) fetchRoute(); }, 20000);     // refresh route
  window.addEventListener("resize", draw);
  new MutationObserver(draw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  window.addEventListener("DOMContentLoaded", () => {
    document.getElementById("routeName").textContent = route;
    document.getElementById("orientBtn").textContent = orient === "north" ? "N↑" : "Crs↑";
    loadCourse(); loadNav();
  });

  return { toggleOrient, dropPractice, setRoute, toggleRoute };
})();
