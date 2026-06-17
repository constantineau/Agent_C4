/* Sail-range dial (5.1). A point-of-sail arc (0° head-to-wind at top → 180° dead-downwind
   at bottom) split into the SR33's sail zones for the current TWS; a live needle marks where
   the true wind angle sits, crossover boundaries are ticked, and the crew's hoisted sail is
   highlighted so a wrong sail / imminent peel is obvious at a glance. Data from /api/sail. */
"use strict";
(function () {
  const FAMILY = ["J1", "A2", "A3", "S2"];
  // day palette per sail; night uses a red monochrome ramp for night vision.
  const DAY = { J1: "#3f7bd6", A2: "#36c08a", A3: "#e0902f", S2: "#b06bd8" };
  const NIGHT = { J1: "#3a0c0c", A2: "#5a1414", A3: "#7e1d1d", S2: "#a83030" };

  let hoisted = localStorage.getItem("sr33.hoisted") || "";
  let last = null;   // last /api/sail response, for redraw on resize/theme

  function isNight() { return document.documentElement.getAttribute("data-theme") === "night"; }
  function palette() { return isNight() ? NIGHT : DAY; }
  function ink() { return getComputedStyle(document.body).getPropertyValue("--text").trim(); }
  function muted() { return getComputedStyle(document.body).getPropertyValue("--muted").trim(); }

  // Top-semicircle gauge: TWA 0° at left (head to wind), 90° at top (beam), 180° at right (run).
  const twaToAngle = (twa) => Math.PI + (Math.max(0, Math.min(180, twa)) / 180) * Math.PI;

  function draw() {
    const cv = document.getElementById("sailDial");
    if (!cv || !last || !last.available) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    cv.width = w * dpr; cv.height = h * dpr;
    const g = cv.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    const R = Math.min(w / 2 - 28, h - 30), cx = w / 2, cy = h - 16, rin = R * 0.52;
    const pal = palette();

    // sail zones as filled annulus segments
    last.zones.forEach((z) => {
      const a1 = twaToAngle(z.twa_min), a2 = twaToAngle(z.twa_max);
      g.beginPath();
      g.arc(cx, cy, R, a1, a2); g.arc(cx, cy, rin, a2, a1, true); g.closePath();
      g.fillStyle = pal[z.sail] || "#444";
      g.globalAlpha = (z.sail === last.optimal_sail) ? 1 : 0.55;
      g.fill(); g.globalAlpha = 1;
      // zone label at mid-angle (keep it red in night mode for night vision)
      const am = (a1 + a2) / 2, rl = (R + rin) / 2;
      g.fillStyle = isNight() ? "#ffc0c0" : "#fff"; g.font = "600 12px system-ui";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(z.sail, cx + Math.cos(am) * rl, cy + Math.sin(am) * rl);
    });

    // crossover boundary ticks + TWA labels
    g.strokeStyle = ink(); g.fillStyle = muted(); g.font = "11px system-ui";
    last.zones.slice(1).forEach((z) => {
      const a = twaToAngle(z.twa_min);
      g.lineWidth = 1.5; g.beginPath();
      g.moveTo(cx + Math.cos(a) * rin, cy + Math.sin(a) * rin);
      g.lineTo(cx + Math.cos(a) * (R + 5), cy + Math.sin(a) * (R + 5)); g.stroke();
      g.textAlign = "center";
      g.fillText(Math.round(z.twa_min) + "°", cx + Math.cos(a) * (R + 13), cy + Math.sin(a) * (R + 13));
    });

    // live TWA needle
    const an = twaToAngle(last.twa_abs);
    g.strokeStyle = last.wrong_sail ? "#ff5d5d" : ink();
    g.lineWidth = 3; g.beginPath();
    g.moveTo(cx, cy); g.lineTo(cx + Math.cos(an) * R, cy + Math.sin(an) * R); g.stroke();
    g.fillStyle = g.strokeStyle;
    g.beginPath(); g.arc(cx, cy, 4.5, 0, 2 * Math.PI); g.fill();

    // center readout (inside the arc, above the hub)
    g.fillStyle = ink(); g.textAlign = "center";
    g.font = "700 24px system-ui";
    g.fillText(Math.round(last.twa_abs) + "°", cx, cy - R * 0.42);
    g.font = "11px system-ui"; g.fillStyle = muted();
    g.fillText("TWA " + last.tack + " · " + Math.round(last.tws_used) + " kn", cx, cy - R * 0.42 + 16);
  }

  function render() {
    const rec = document.getElementById("sailRec");
    if (last && last.available) {
      rec.textContent = last.recommendation +
        (last.targets && last.targets.btv ? `  ·  target ${last.targets.btv} kn, heel ${last.targets.heel}°` : "");
      rec.className = "sailrec" + (last.wrong_sail ? " bad"
        : (last.next_crossover && last.next_crossover.deg_away <= 8 ? " warn" : ""));
    } else {
      rec.textContent = (last && last.note) || "Waiting for true wind…";
      rec.className = "sailrec";
    }
    document.querySelectorAll("#sailSel button").forEach((b) =>
      b.classList.toggle("on", (b.dataset.sail || "") === hoisted));
    draw();
  }

  async function update(c) {
    if (!c || c.tws == null || c.twa == null) { last = { available: false, note: "No true wind yet." }; render(); return; }
    try {
      const q = `tws=${c.tws}&twa=${c.twa}` + (hoisted ? `&hoisted=${hoisted}` : "");
      last = await (await apiFetch("/api/sail?" + q)).json();
    } catch (e) { last = { available: false, note: "Sail advice unavailable." }; }
    render();
  }

  // wire selector + live updates
  window.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("#sailSel button").forEach((b) =>
      b.addEventListener("click", () => {
        hoisted = b.dataset.sail || "";
        localStorage.setItem("sr33.hoisted", hoisted);
        if (App.lastConditions) update(App.lastConditions); else render();
      }));
    render();
  });
  window.addEventListener("sr33:conditions", (e) => { App.lastConditions = e.detail; update(e.detail); });
  window.addEventListener("resize", draw);
  // redraw on theme change (palette swap)
  new MutationObserver(draw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
})();
