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
  let chartLayer = null, windLayer = null, routeGroup = null, seamarks = null, boatMarker = null;
  let R = null;                 // current optimize result
  let frameIdx = 0;
  let playTimer = null;         // forecast animation (▶/⏸)
  const show = { wind: true, shoals: true, rocks: true, land: false, sea: false };

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

  function drawWind(ctx, project, zoom, size) {
    if (!show.wind || !R || !R.wind_grid) return;
    const frame = (R.wind_grid.frames || [])[frameIdx] || [];
    const minSpace = 26;                 // px; thins density adaptively with zoom
    const drawn = [];
    for (const p of frame) {
      const pt = project(p.lat, p.lon);
      if (pt.x < -20 || pt.y < -20 || pt.x > size.x + 20 || pt.y > size.y + 20) continue;
      let ok = true;
      for (const d of drawn) { if (Math.abs(d.x - pt.x) < minSpace && Math.abs(d.y - pt.y) < minSpace) { ok = false; break; } }
      if (!ok) continue;
      drawn.push(pt);
      drawArrow(ctx, pt.x, pt.y, p.tws, p.twd, p.confidence);
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

  // ---- in-map controls: layer toggles + the forecast time slider ----
  function addControls() {
    const Layers = L.Control.extend({
      options: { position: "topright" },
      onAdd: function () {
        const d = L.DomUtil.create("div", "mv-ctl");
        d.innerHTML = `<b>Layers</b>
          ${chk("wind", "Wind")}${chk("shoals", "Shoals")}${chk("rocks", "Rocks")}${chk("land", "ENC land")}${chk("sea", "Seamarks")}`;
        L.DomEvent.disableClickPropagation(d);
        d.querySelectorAll("input").forEach((el) => el.addEventListener("change", () => toggle(el.dataset.k, el.checked)));
        return d;
      },
    });
    map.addControl(new Layers());

    if (R && R.wind_grid && (R.wind_grid.times || []).length > 1) {
      const Slider = L.Control.extend({
        options: { position: "bottomleft" },
        onAdd: function () {
          const d = L.DomUtil.create("div", "mv-ctl mv-slider");
          const n = R.wind_grid.times.length;
          d.innerHTML = `<b>Forecast — drag to scrub (hourly)</b> <span id="mvTime">${frameLabel(frameIdx)}</span><br>
            <button id="mvPlay" class="mv-play" title="Play / pause the forecast animation">▶</button>
            <input type="range" id="mvRange" min="0" max="${n - 1}" value="${frameIdx}" step="1" style="width:250px;vertical-align:middle">`;
          L.DomEvent.disableClickPropagation(d);
          // a manual scrub stops auto-play so the two don't fight
          d.querySelector("#mvRange").addEventListener("input", (e) => { stopPlay(); const b = document.getElementById("mvPlay"); if (b) b.textContent = "▶"; setFrame(parseInt(e.target.value, 10)); });
          d.querySelector("#mvPlay").addEventListener("click", togglePlay);
          return d;
        },
      });
      map.addControl(new Slider());
    }

    // wind color-scale legend — makes the TWS ramp + the confidence-fade encoding legible
    const Legend = L.Control.extend({
      options: { position: "bottomright" },
      onAdd: function () {
        const d = L.DomUtil.create("div", "mv-ctl mv-legend");
        const stops = [[3, "<6"], [9, "6–12"], [15, "12–18"], [21, "18–24"], [27, "24+"]];
        const sw = stops.map(([v, lbl]) =>
          `<span class="mv-sw"><i style="background:${twsColor(v)}"></i>${lbl}</span>`).join("");
        d.innerHTML = `<b>Wind (kn)</b><div class="mv-legrow">${sw}</div>
          <div class="mv-legnote">arrow opacity = model confidence (faint = models split)</div>`;
        L.DomEvent.disableClickPropagation(d);
        return d;
      },
    });
    map.addControl(new Legend());
  }
  function chk(k, label) {
    return `<label class="mv-chk"><input type="checkbox" data-k="${k}" ${show[k] ? "checked" : ""}> ${label}</label>`;
  }
  function toggle(k, on) {
    show[k] = on;
    if (k === "sea") { if (on) seamarks.addTo(map); else map.removeLayer(seamarks); return; }
    chartLayer.redraw(); windLayer.redraw();
  }
  function setFrame(i) {
    frameIdx = i;
    const lab = document.getElementById("mvTime");
    if (lab && R.wind_grid) lab.textContent = frameLabel(i);
    const rng = document.getElementById("mvRange");      // keep the thumb in sync during auto-play
    if (rng && +rng.value !== i) rng.value = i;
    windLayer.redraw(); updateBoatMarker();
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
    windLayer = new CanvasOverlay(drawWind).addTo(map);
    buildRoute();
    updateBoatMarker();
    addControls();

    setTimeout(() => {                              // container just appeared → recalc size + fit
      map.invalidateSize();
      if (b) map.fitBounds(b, { padding: [30, 30] });
    }, 80);
  }

  return { render, setFrame };
})();
