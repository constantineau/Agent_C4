/* criticality.js — the "time lost if you're here" band on the Gameplan map (#379).

   The optimizer (app/criticality.py, opt-in: "Time-loss band") sends, per leg, a grid of minutes
   lost against the best route if the boat sails through each point. Here it is drawn as a shaded
   band: dark within 5 min of the best route, then 10, then 20. A narrow band = the route matters;
   a wide one = the side hardly matters. Pure helpers (no Leaflet); mapview.js calls draw() from its
   own canvas layer. When the wind changes fast the band is only approximate (a detour meets
   different wind than the best route); legs where it changes a lot say "rough". Each leg's band ends at the mark approach (the inbound
   standoff); the rounding isn't in it.
   vps/lab/test_criticality.js requires this file directly. */
"use strict";

const Criticality = (function () {
  // one hue, darker = closer to the best route; [max minutes, colour, alpha]
  const APPROX = "The wind changes a lot on this leg. The band is a rough guide there.";
  const SHADES = [[5, "#5b2a86", 0.42], [10, "#8e5cc4", 0.30], [20, "#c9a8ee", 0.22]];

  function has(result) {
    const c = result && result.criticality;
    return !!(c && c.legs && c.legs.length);
  }

  // which shade a loss (minutes) falls in: 0, 1, 2, or -1 for none (null / past the last band)
  function shadeOf(min) {
    if (min == null || !isFinite(min)) return -1;
    for (let k = 0; k < SHADES.length; k++) if (min <= SHADES[k][0]) return k;
    return -1;
  }

  // every shaded cell of a leg: [{lat, lon, shade}], node at the cell centre
  function cells(leg) {
    const out = [];
    if (!leg || !leg.loss_min) return out;
    for (let j = 0; j < leg.ny; j++) {
      for (let i = 0; i < leg.nx; i++) {
        const k = shadeOf(leg.loss_min[j * leg.nx + i]);
        if (k >= 0) out.push({ lat: leg.lat0 + j * leg.dlat, lon: leg.lon0 + i * leg.dlon, shade: k });
      }
    }
    return out;
  }

  // draw every leg's band; project(lat, lon) -> {x, y} container pixels (mapview's CanvasOverlay)
  function draw(ctx, project, result) {
    if (!has(result)) return;
    for (const leg of result.criticality.legs) {
      const cs = cells(leg);
      if (!cs.length) continue;
      // one cell's size in pixels at this leg's latitude (cells are small: a flat square is fine)
      const a = project(leg.lat0, leg.lon0), b = project(leg.lat0 + leg.dlat, leg.lon0 + leg.dlon);
      const w = Math.abs(b.x - a.x) + 0.6, h = Math.abs(b.y - a.y) + 0.6;   // +0.6: no seams
      // light shades first, so the 5-min band sits on top where they meet
      for (let k = SHADES.length - 1; k >= 0; k--) {
        ctx.fillStyle = SHADES[k][1];
        ctx.globalAlpha = SHADES[k][2];
        ctx.beginPath();
        for (const c of cs) {
          if (c.shade !== k) continue;
          const p = project(c.lat, c.lon);
          ctx.rect(p.x - w / 2, p.y - h / 2, w, h);
        }
        ctx.fill();
      }
    }
    ctx.globalAlpha = 1;
  }

  // how wide each leg's 10-min band is on average: "Top 2.1 nm" (narrow = the route matters)
  function widths(result) {
    if (!has(result)) return [];
    return result.criticality.legs
      .filter((l) => l.width_nm && l.width_nm["10"] != null)
      .map((l) => ({ to: l.to || "mark", nm: l.width_nm["10"], rough: !!l.rough }));
  }

  function esc(t) {
    return String(t).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  function legendHtml(result) {
    if (!has(result)) return "";
    const w = widths(result);
    return `<span class="cc-sep"></span><span class="cc-lbl">Time lost</span>` +
      SHADES.map(([m, col, al]) =>
        `<span class="mv-sw"><i style="background:${col};opacity:${Math.min(1, al * 2)}"></i>&lt;${m} min</span>`).join("") +
      `<span class="mv-legnote" title="${esc(APPROX)}">narrow = route matters. Approximate when the wind changes fast. Each leg up to the mark, not the rounding.</span>` +
      (w.length ? `<span class="mv-legnote">10 min wide: ${w.map((x) =>
        `${esc(x.to)} ${x.nm} nm${x.rough ? ` <b title="${esc(APPROX)}">rough</b>` : ""}`).join(", ")}</span>` : "");
  }

  return { SHADES, has, shadeOf, cells, draw, widths, legendHtml };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Criticality;
