/* mapview.js — Leaflet route map for the Gameplan optimizer ([C], GRIB-on-ENC overlay).

   Replaces the hand-drawn schematic canvas with a real slippy map = a LAYER STACK:
     1. OSM basemap (geographic context) + optional OpenSeaMap seamark overlay;
     2. ENC obstacle overlay — OUR OWN draft-aware extraction (shoals < safety depth, rocks /
        obstructions, real land), drawn on a canvas so thousands of polygons stay fast;
     3. GRIB wind overlay — arrows rotated by TWD, coloured by TWS, FADED BY CONFIDENCE
        (low model agreement = faint = the fuzzy-adherence signal);
     4. the optimized route + marks.
   A forecast TIME SLIDER scrubs the embedded multi-time wind frames; a marker shows where the boat
   is on the route at the selected time (time-synced wind = what the boat will actually meet).

   The map auto-aligns lat/lon (GRIB) onto Web-Mercator (tiles) — the reason to drop the canvas. */
const MapView = (function () {
  let map = null;
  let chartLayer = null, windLayer = null, exploreLayer = null, routeGroup = null, seamarks = null;
  let boatMarker = null, legHighlight = null;
  let R = null;                 // current optimize result
  let frameIdx = 0;
  let playTimer = null;         // forecast animation (▶/⏸)
  let windMode = "arrows";      // wind overlay style: arrows | barbs | shaded (2.4)
  let followScrub = true;       // Tier 3.3: pan the map to the projected boat position while scrubbing
  const show = { wind: true, shoals: true, rocks: true, land: false, sea: false,
                 iso: false, laylines: true, models: true };

  // a stable colour per weather model for the per-model candidate-route fan (PR-4)
  const MODEL_COLORS = { gfs: "#1f77b4", nam: "#2ca02c", hrrr: "#d62728", gefs: "#9467bd",
    ecmwf: "#ff7f0e", "ecmwf-ens": "#8c564b" };
  function modelColor(m) { return MODEL_COLORS[m] || "#7a7a7a"; }

  // ---- a generic canvas overlay: positions a full-map canvas + calls draw(ctx) on move/zoom ----
  const CanvasOverlay = L.Layer.extend({
    initialize: function (drawFn) { this._draw = drawFn; },
    onAdd: function (m) {
      this._map = m;
      this._c = L.DomUtil.create("canvas", "mv-canvas");
      this._c.style.position = "absolute";
      this._c.style.pointerEvents = "none";
      m.getPanes().overlayPane.appendChild(this._c);
      m.on("moveend zoomend resize viewreset zoomanim", this._reset, this);
      this._reset();
    },
    onRemove: function (m) {
      m.off("moveend zoomend resize viewreset zoomanim", this._reset, this);
      L.DomUtil.remove(this._c);
    },
    redraw: function () { if (this._map) this._reset(); return this; },
    _reset: function () {
      const m = this._map, size = m.getSize();
      const tl = m.containerPointToLayerPoint([0, 0]);
      L.DomUtil.setPosition(this._c, tl);
      this._c.width = size.x; this._c.height = size.y;
      const ctx = this._c.getContext("2d");
      ctx.clearRect(0, 0, size.x, size.y);
      this._draw(ctx, function (lat, lon) { return m.latLngToContainerPoint([lat, lon]); }, m.getZoom(), size);
    },
  });

  function twsColor(tws) {
    return tws < 6 ? "#4aa3ff" : tws < 12 ? "#43c463" : tws < 18 ? "#f5c542"
      : tws < 24 ? "#ef8a3a" : "#e0524a";
  }

  function drawArrow(ctx, x, y, tws, twd, conf) {
    // wind FROM twd → blows toward twd+180. screen bearing: dx=sin, dy=-cos.
    const th = (twd + 180) * Math.PI / 180;
    const dx = Math.sin(th), dy = -Math.cos(th);
    const len = 9 + Math.min(20, tws * 0.9);
    const hx = x + dx * len, hy = y + dy * len;
    ctx.globalAlpha = 0.30 + 0.70 * (conf == null ? 0.6 : conf);
    ctx.strokeStyle = twsColor(tws); ctx.fillStyle = ctx.strokeStyle;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(hx, hy); ctx.stroke();
    // arrowhead
    const a = 0.5, hs = 5;
    ctx.beginPath();
    ctx.moveTo(hx, hy);
    ctx.lineTo(hx - hs * (dx * Math.cos(a) - dy * Math.sin(a)), hy - hs * (dy * Math.cos(a) + dx * Math.sin(a)));
    ctx.lineTo(hx - hs * (dx * Math.cos(-a) - dy * Math.sin(-a)), hy - hs * (dy * Math.cos(-a) + dx * Math.sin(-a)));
    ctx.closePath(); ctx.fill();
    ctx.globalAlpha = 1;
  }

  // standard meteorological wind barb (the offshore convention): a shaft pointing toward the wind's
  // SOURCE, with half-barbs (5 kn), full barbs (10 kn) and pennants (50 kn) at the upwind end. Calm = ○.
  function drawBarb(ctx, x, y, tws, twd, conf) {
    ctx.globalAlpha = 0.30 + 0.70 * (conf == null ? 0.6 : conf);
    ctx.strokeStyle = twsColor(tws); ctx.fillStyle = ctx.strokeStyle; ctx.lineWidth = 1.6;
    if (tws < 2.5) { ctx.beginPath(); ctx.arc(x, y, 3, 0, 2 * Math.PI); ctx.stroke(); ctx.globalAlpha = 1; return; }
    const th = twd * Math.PI / 180;            // shaft points toward where the wind comes FROM
    const ux = Math.sin(th), uy = -Math.cos(th);
    const px = -uy, py = ux;                    // perpendicular (barbs to one side)
    const L = 24, step = 4.5, full = 9, half = 5;
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + ux * L, y + uy * L); ctx.stroke();
    let kt = Math.round(tws / 5) * 5, d = L;
    while (kt >= 50) {                          // pennants (filled triangles)
      const b1x = x + ux * d, b1y = y + uy * d, b2x = x + ux * (d - step * 1.4), b2y = y + uy * (d - step * 1.4);
      ctx.beginPath(); ctx.moveTo(b1x, b1y); ctx.lineTo(b1x + px * full, b1y + py * full); ctx.lineTo(b2x, b2y);
      ctx.closePath(); ctx.fill(); kt -= 50; d -= step * 1.6;
    }
    while (kt >= 10) {                          // full barbs
      ctx.beginPath(); ctx.moveTo(x + ux * d, y + uy * d); ctx.lineTo(x + ux * d + px * full, y + uy * d + py * full); ctx.stroke();
      kt -= 10; d -= step;
    }
    if (kt >= 5) {                              // a lone half-barb sits in from the tip (convention)
      if (d === L) d -= step;
      ctx.beginPath(); ctx.moveTo(x + ux * d, y + uy * d); ctx.lineTo(x + ux * d + px * half, y + uy * d + py * half); ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  // shaded TWS field — fill each grid cell by wind speed (Orca-style heatmap; the "contour" option).
  function drawShaded(ctx, project, size) {
    const frame = (R.wind_grid.frames || [])[frameIdx] || [];
    const b = R.wind_grid.bbox;                 // [n, s, w, e]
    if (!frame.length || !b) return;
    const aspect = (b[3] - b[2]) / Math.max(1e-6, (b[0] - b[1]));
    const cols = Math.max(2, Math.round(Math.sqrt(frame.length * aspect)));
    const dlon = (b[3] - b[2]) / cols, dlat = dlon;   // ~square cells
    for (const p of frame) {
      const a = project(p.lat + dlat / 2, p.lon - dlon / 2);
      const c = project(p.lat - dlat / 2, p.lon + dlon / 2);
      if (Math.max(a.x, c.x) < -20 || Math.max(a.y, c.y) < -20 || Math.min(a.x, c.x) > size.x + 20 || Math.min(a.y, c.y) > size.y + 20) continue;
      ctx.globalAlpha = 0.30 * (p.confidence == null ? 0.7 : 0.4 + 0.6 * p.confidence);
      ctx.fillStyle = twsColor(p.tws);
      ctx.fillRect(Math.min(a.x, c.x), Math.min(a.y, c.y), Math.abs(c.x - a.x) + 1, Math.abs(c.y - a.y) + 1);
    }
    ctx.globalAlpha = 1;
  }

  function drawWind(ctx, project, zoom, size) {
    if (!show.wind || !R || !R.wind_grid) return;
    if (windMode === "shaded") { drawShaded(ctx, project, size); return; }
    const frame = (R.wind_grid.frames || [])[frameIdx] || [];
    const draw = windMode === "barbs" ? drawBarb : drawArrow;
    const minSpace = windMode === "barbs" ? 32 : 26;   // px; barbs are wider → thin a bit more
    const drawn = [];
    for (const p of frame) {
      const pt = project(p.lat, p.lon);
      if (pt.x < -20 || pt.y < -20 || pt.x > size.x + 20 || pt.y > size.y + 20) continue;
      let ok = true;
      for (const d of drawn) { if (Math.abs(d.x - pt.x) < minSpace && Math.abs(d.y - pt.y) < minSpace) { ok = false; break; } }
      if (!ok) continue;
      drawn.push(pt);
      draw(ctx, pt.x, pt.y, p.tws, p.twd, p.confidence);
    }
  }

  function fillRings(ctx, project, rings, fill, stroke, alpha) {
    if (!rings || !rings.length) return;
    ctx.globalAlpha = alpha;
    ctx.fillStyle = fill; ctx.strokeStyle = stroke; ctx.lineWidth = 1;
    for (const ring of rings) {
      if (!ring || ring.length < 2) continue;
      ctx.beginPath();
      for (let i = 0; i < ring.length; i++) {
        const pt = project(ring[i][0], ring[i][1]);   // ring pts are [lat,lon]
        if (i === 0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y);
      }
      ctx.closePath();
      if (fill) ctx.fill();
      if (stroke) ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  function drawChart(ctx, project) {
    if (!R || !R.obstacles || !R.obstacles.geometry) return;
    const g = R.obstacles.geometry;
    if (show.land) fillRings(ctx, project, g.land_rings, "#cfc6ad", "#9a8f73", 0.35);
    if (show.shoals) fillRings(ctx, project, g.shoal_rings, "#d9534f", "#c9302c", 0.20);
    if (show.rocks) fillRings(ctx, project, g.obstruction_rings, "#a01b1b", "#a01b1b", 0.55);
    // race zones (exclusion/hazard/tss) always shown
    for (const z of g.zones || []) fillRings(ctx, project, [z.ring], "#7a5cff", "#5a3fd6", 0.18);
  }

  // route-EXPLORATION overlay: the isochrone frontier (equal-time-from-start arcs the optimizer
  // explored — "here's WHY the line") + laylines into each beat/run mark (the VMG approach corridor).
  function drawExplore(ctx, project, zoom, size) {
    if (!R) return;
    // per-model candidate routes (PR-4) — the fan the blended route's confidence summarizes; tight =
    // models agree (high confidence), spread = they disagree. Drawn under the isochrones + chosen route.
    if (show.models && R.candidate_paths && R.candidate_paths.length) {
      ctx.lineWidth = 2; ctx.globalAlpha = 0.55;
      for (const cp of R.candidate_paths) {
        if (!cp.path || cp.path.length < 2) continue;
        ctx.strokeStyle = modelColor(cp.model);
        ctx.beginPath();
        for (let i = 0; i < cp.path.length; i++) {
          const pt = project(cp.path[i][0], cp.path[i][1]);
          if (i === 0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y);
        }
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    if (show.iso && R.isochrones && R.isochrones.length) {
      ctx.globalAlpha = 0.45; ctx.strokeStyle = "#36b3ff"; ctx.lineWidth = 1;
      for (const poly of R.isochrones) {
        if (!poly || poly.length < 2) continue;
        ctx.beginPath();
        for (let i = 0; i < poly.length; i++) {
          const pt = project(poly[i][0], poly[i][1]);
          if (i === 0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y);
        }
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    }
    if (show.laylines && R.laylines && R.laylines.length) {
      ctx.save();
      ctx.setLineDash([6, 5]); ctx.lineWidth = 1.5; ctx.globalAlpha = 0.75; ctx.strokeStyle = "#0b5bd3";
      for (const ll of R.laylines) {
        if (!ll.pts || ll.pts.length < 2) continue;
        const a = project(ll.pts[0][0], ll.pts[0][1]), b = project(ll.pts[1][0], ll.pts[1][1]);
        ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      }
      ctx.restore(); ctx.globalAlpha = 1;
    }
  }

  function buildRoute() {
    if (routeGroup) { map.removeLayer(routeGroup); routeGroup = null; }
    if (!R || !R.path || R.path.length < 2) return;
    const items = [];

    // rhumb (direct) lines between the real marks — the geometric course, dashed, under the route
    const marks = (R.marks && R.marks.length) ? R.marks : null;
    if (marks) {
      items.push(L.polyline(marks.map((m) => [m.lat, m.lon]),
        { color: "#444", weight: 1.5, opacity: 0.8, dashArray: "7,7" })
        .bindTooltip("Rhumb line (direct course)", { sticky: true }));
    }

    // the optimized (sailed) route on top
    items.push(L.polyline(R.path.map((p) => [p.lat, p.lon]),
      { color: "#111", weight: 3, opacity: 0.9 }).bindTooltip("Optimized route", { sticky: true }));

    // mark markers at their REAL positions (start green, finish gold, marks blue)
    const list = marks || [{ name: "Start", lat: R.path[0].lat, lon: R.path[0].lon }];
    list.forEach((m, i) => {
      const isStart = i === 0, isFinish = i === list.length - 1 && list.length > 1;
      const color = isStart ? "#1a7f37" : isFinish ? "#d9a400" : "#0b5bd3";
      items.push(L.circleMarker([m.lat, m.lon],
        { radius: 6, color: "#fff", weight: 1.5, fillColor: color, fillOpacity: 1 })
        .bindTooltip(m.name || (isStart ? "Start" : isFinish ? "Finish" : "Mark"),
          { permanent: false }));
    });
    routeGroup = L.layerGroup(items).addTo(map);
  }

  function nearestPathByT(t) {
    if (!R || !R.path) return null;
    let best = null, bd = Infinity;
    for (const p of R.path) { const d = Math.abs((p.t || 0) - t); if (d < bd) { bd = d; best = p; } }
    return best;
  }

  function updateBoatMarker() {
    if (boatMarker) { map.removeLayer(boatMarker); boatMarker = null; }
    if (!R || !R.wind_grid) return;
    const t = (R.wind_grid.times || [])[frameIdx];
    const p = nearestPathByT(t);
    if (!p) return;
    boatMarker = L.circleMarker([p.lat, p.lon],
      { radius: 7, color: "#fff", weight: 2, fillColor: "#111", fillOpacity: 1 })
      .bindTooltip("boat @ " + fmtTime(t), { permanent: false }).addTo(map);
  }

  function fmtTime(epoch) {
    try { return new Date(epoch * 1000).toUTCString().replace(":00 GMT", " UTC").replace("GMT", "UTC"); }
    catch (e) { return String(epoch); }
  }
  function frameLabel(i) {
    const times = (R && R.wind_grid && R.wind_grid.times) || [];
    if (!times.length) return "";
    const h = Math.round((times[i] - times[0]) / 3600);
    return fmtTime(times[i]) + "  (T+" + h + "h)";
  }

  // ---- ONE consolidated Control Center (Tier 3.1) — Orca-style docked panel: forecast scrubber +
  //      layer toggles + wind-mode + follow + the legend, all in a single collapsible bottom bar. ----
  function addControls() {
    const hasIso = R && R.isochrones && R.isochrones.length;
    const hasLay = R && R.laylines && R.laylines.length;
    const hasModels = R && R.candidate_paths && R.candidate_paths.length;
    const hasFrames = R && R.wind_grid && (R.wind_grid.times || []).length > 1;
    const hasWind = R && R.wind_grid && (R.wind_grid.frames || []).length;

    const scrub = hasFrames ? `<div class="cc-grp cc-scrub">
        <button id="mvPlay" class="mv-play" title="Play / pause the forecast animation">▶</button>
        <input type="range" id="mvRange" min="0" max="${R.wind_grid.times.length - 1}" value="${frameIdx}" step="1">
        <span id="mvTime">${frameLabel(frameIdx)}</span></div>` : "";

    const windSel = hasWind ? `<label class="mv-windmode">Wind <select data-windmode>
        <option value="arrows"${windMode === "arrows" ? " selected" : ""}>arrows</option>
        <option value="barbs"${windMode === "barbs" ? " selected" : ""}>barbs</option>
        <option value="shaded"${windMode === "shaded" ? " selected" : ""}>shaded</option></select></label>` : "";
    const followChk = hasFrames ? `<label class="mv-chk" title="Pan the map to the projected boat position while scrubbing/playing">
        <input type="checkbox" data-follow ${followScrub ? "checked" : ""}> Follow</label>` : "";
    const layers = `<div class="cc-grp cc-layers"><span class="cc-lbl">Layers</span>
        ${chk("wind", "Wind")}${chk("shoals", "Shoals")}${chk("rocks", "Rocks")}${chk("land", "ENC land")}${chk("sea", "Seamarks")}` +
        (hasModels ? chk("models", "Model routes") : "") +
        (hasIso ? chk("iso", "Isochrones") : "") + (hasLay ? chk("laylines", "Laylines") : "") +
        windSel + followChk + `</div>`;

    const stops = [[3, "<6"], [9, "6–12"], [15, "12–18"], [21, "18–24"], [27, "24+"]];
    const sw = stops.map(([v, lbl]) => `<span class="mv-sw"><i style="background:${twsColor(v)}"></i>${lbl}</span>`).join("");
    const modelLeg = hasModels ? `<span class="cc-sep"></span><span class="cc-lbl">Model routes</span>` +
        R.candidate_paths.map((cp) => `<span class="mv-sw"><i style="background:${modelColor(cp.model)}"></i>${cp.model.toUpperCase()}` +
          `${cp.total_hours != null ? " " + cp.total_hours + "h" : ""} <small>${cp.favored_side || ""}</small></span>`).join("") : "";
    const legend = `<div class="cc-grp cc-legend"><span class="cc-lbl">Wind kn</span>${sw}
        <span class="mv-legnote">opacity = confidence (faint = models split)</span>${modelLeg}</div>`;

    const CC = L.Control.extend({
      options: { position: "bottomleft" },
      onAdd: function () {
        const d = L.DomUtil.create("div", "mv-ctl mv-cc");
        d.innerHTML = `<div class="cc-head"><b>Map controls</b>
            <button class="cc-collapse" title="Collapse / expand">▾</button></div>
          <div class="cc-body">${scrub}${layers}${legend}</div>`;
        L.DomEvent.disableClickPropagation(d);
        L.DomEvent.disableScrollPropagation(d);
        d.querySelectorAll('input[type="checkbox"][data-k]').forEach((el) =>
          el.addEventListener("change", () => toggle(el.dataset.k, el.checked)));
        const ws = d.querySelector("[data-windmode]");
        if (ws) ws.addEventListener("change", () => { windMode = ws.value; windLayer.redraw(); });
        const fl = d.querySelector("[data-follow]");
        if (fl) fl.addEventListener("change", () => { followScrub = fl.checked; if (followScrub) setFrame(frameIdx); });
        const rng = d.querySelector("#mvRange");
        if (rng) rng.addEventListener("input", (e) => { stopPlay(); const b = d.querySelector("#mvPlay"); if (b) b.textContent = "▶"; setFrame(parseInt(e.target.value, 10)); });
        const pl = d.querySelector("#mvPlay");
        if (pl) pl.addEventListener("click", togglePlay);
        d.querySelector(".cc-collapse").addEventListener("click", (e) => {
          d.classList.toggle("cc-collapsed");
          e.currentTarget.textContent = d.classList.contains("cc-collapsed") ? "▸" : "▾";
        });
        return d;
      },
    });
    map.addControl(new CC());
  }
  function chk(k, label) {
    return `<label class="mv-chk"><input type="checkbox" data-k="${k}" ${show[k] ? "checked" : ""}> ${label}</label>`;
  }
  function toggle(k, on) {
    show[k] = on;
    if (k === "sea") { if (on) seamarks.addTo(map); else map.removeLayer(seamarks); return; }
    if (k === "iso" || k === "laylines" || k === "models") { exploreLayer.redraw(); return; }
    chartLayer.redraw(); windLayer.redraw();
  }
  function setFrame(i) {
    frameIdx = i;
    const lab = document.getElementById("mvTime");
    if (lab && R.wind_grid) lab.textContent = frameLabel(i);
    const rng = document.getElementById("mvRange");      // keep the thumb in sync during auto-play
    if (rng && +rng.value !== i) rng.value = i;
    windLayer.redraw(); updateBoatMarker();
    // Tier 3.3: ride along — keep the projected boat position in view as the timeline scrubs/plays
    if (followScrub && boatMarker && map) map.panTo(boatMarker.getLatLng(), { animate: false });
  }
  function frameCount() {
    return ((R && R.wind_grid && R.wind_grid.times) || []).length;
  }
  function togglePlay() {
    const btn = document.getElementById("mvPlay");
    if (playTimer) {
      clearInterval(playTimer); playTimer = null;
      if (btn) btn.textContent = "▶";
      return;
    }
    const n = frameCount();
    if (n <= 1) return;
    if (btn) btn.textContent = "⏸";
    playTimer = setInterval(() => setFrame((frameIdx + 1) % n), 700);
  }
  function stopPlay() {
    if (playTimer) { clearInterval(playTimer); playTimer = null; }
  }

  // snap the forecast time slider to the frame nearest an epoch (used by leg-row linking)
  function snapToTime(epoch) {
    const times = (R && R.wind_grid && R.wind_grid.times) || [];
    if (!times.length) return;
    let bi = 0, bd = Infinity;
    for (let i = 0; i < times.length; i++) { const d = Math.abs(times[i] - epoch); if (d < bd) { bd = d; bi = i; } }
    stopPlay(); const b = document.getElementById("mvPlay"); if (b) b.textContent = "▶";
    setFrame(bi);
  }

  // leg-row ↔ map ↔ time linking: highlight a leg's path segment, fit to it, snap the slider to its ETA.
  function focusLeg(i) {
    if (!map || !R || !R.legs || !R.legs[i] || !R.path) return;
    const end = R.legs[i].eta_epoch;
    const startT = i > 0 ? R.legs[i - 1].eta_epoch : (R.start_epoch != null ? R.start_epoch : R.path[0].t);
    const seg = R.path.filter((p) => p.t >= startT - 1 && p.t <= end + 1).map((p) => [p.lat, p.lon]);
    if (legHighlight) { map.removeLayer(legHighlight); legHighlight = null; }
    if (seg.length >= 2) {
      // explicit SVG renderer so the highlight stays crisp + on top of the canvas route/wind layers
      // (the map runs preferCanvas) and survives their redraws.
      legHighlight = L.polyline(seg, { color: "#ff8c1a", weight: 6, opacity: 0.85, renderer: L.svg() })
        .bindTooltip("Leg to " + (R.legs[i].to || "mark"), { sticky: true }).addTo(map);
      map.fitBounds(legHighlight.getBounds(), { padding: [40, 40], maxZoom: 11 });
    }
    snapToTime(end);
  }

  function resultBounds() {
    if (R && R.path && R.path.length) return L.latLngBounds(R.path.map((p) => [p.lat, p.lon]));
    if (R && R.wind_grid && R.wind_grid.bbox) {
      const b = R.wind_grid.bbox; return L.latLngBounds([[b[1], b[2]], [b[0], b[3]]]);
    }
    return null;
  }

  function render(id, result) {
    stopPlay();
    R = result; frameIdx = 0;
    const el = document.getElementById(id);
    if (!el) return;
    if (map) { map.remove(); map = null; }
    el.innerHTML = "";
    map = L.map(id, { zoomControl: true, attributionControl: true, preferCanvas: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
    seamarks = L.tileLayer("https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png",
      { maxZoom: 18, opacity: 0.9, attribution: "© OpenSeaMap" });
    if (show.sea) seamarks.addTo(map);

    // set a view FIRST (center+zoom, size-independent) so vector/overlay layers have a valid
    // pixel origin when added — adding a polyline before any view set throws in Leaflet.
    const b = resultBounds();
    if (b) map.setView(b.getCenter(), 7); else map.setView([45, -83], 6);

    chartLayer = new CanvasOverlay(drawChart).addTo(map);
    exploreLayer = new CanvasOverlay(drawExplore).addTo(map);
    windLayer = new CanvasOverlay(drawWind).addTo(map);
    legHighlight = null;
    buildRoute();
    updateBoatMarker();
    addControls();

    setTimeout(() => {                              // container just appeared → recalc size + fit
      map.invalidateSize();
      if (b) map.fitBounds(b, { padding: [30, 30] });
    }, 80);
  }

  return { render, setFrame, focusLeg };
})();
