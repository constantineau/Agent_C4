/* Sail-range dial (5.1). A point-of-sail arc (0° head-to-wind at top → 180° dead-downwind
   at bottom) split into the boat's sail zones for the current TWS; a live needle marks where
   the true wind angle sits, crossover boundaries are ticked, and the crew's hoisted sail is
   highlighted so a wrong sail / imminent peel is obvious at a glance. Data from /api/sail. */
"use strict";
(function () {
  // Colours by sail FAMILY (#305): the boat's own sail names come with /api/sail's `sails`.
  // Day palette per family; night uses a red monochrome ramp for night vision.
  const DAY = { jib: "#3f7bd6", genoa: "#3f7bd6", code0: "#2fa6a0", staysail: "#7f8c99",
                asym: "#e0902f", sym: "#b06bd8" };
  const NIGHT = { jib: "#3a0c0c", genoa: "#3a0c0c", code0: "#5a1515", staysail: "#4a1010",
                  asym: "#7e1d1d", sym: "#a83030" };
  // the usual shorthand, for a sail the server didn't list (same rules as shared/sail_vocab.py)
  const SHORTHAND = [[/^(C0|CODE ?0)/, "code0"], [/^(SS|STAY)/, "staysail"], [/^J\d/, "jib"],
                     [/^G\d/, "genoa"], [/^A\d/, "asym"], [/^S\d/, "sym"]];
  function family(name) {
    const n = String(name || "").toUpperCase();
    const e = ((last && last.sails) || []).find((x) => x.name === n);
    if (e) return e.family;
    const m = SHORTHAND.find(([rx]) => rx.test(n));
    return m ? m[1] : "other";
  }
  function colour(pal, name) { return pal[family(name)] || "#888"; }

  let hoisted = localStorage.getItem("sr33.hoisted") || "";
  let last = null;   // last /api/sail response, for redraw on resize/theme

  function isNight() { return document.documentElement.getAttribute("data-theme") === "night"; }
  function palette() { return isNight() ? NIGHT : DAY; }
  function ink() { return getComputedStyle(document.body).getPropertyValue("--text").trim(); }
  function muted() { return getComputedStyle(document.body).getPropertyValue("--muted").trim(); }

  // Top-semicircle gauge: TWA 0° at left (head to wind), 90° at top (beam), 180° at right (run).
  const twaToAngle = (twa) => Math.PI + (Math.max(0, Math.min(180, twa)) / 180) * Math.PI;

  // A small diagonal two-colour tile — used to hatch the toss-up bands (two sails within ~1.5% of
  // target, where the winner-take-all zones can't show a tie) in both sails' colours.
  function hatch(g, c1, c2) {
    const t = document.createElement("canvas"); t.width = t.height = 10;
    const tg = t.getContext("2d"); tg.lineWidth = 3;
    tg.strokeStyle = c1; tg.beginPath(); tg.moveTo(-2, 8); tg.lineTo(8, -2); tg.stroke();
    tg.strokeStyle = c2; tg.beginPath(); tg.moveTo(3, 13); tg.lineTo(13, 3); tg.stroke();
    return g.createPattern(t, "repeat");
  }

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
      g.fillStyle = pal[family(z.sail)] || "#444";
      g.globalAlpha = (z.sail === last.optimal_sail) ? 1 : 0.55;
      g.fill(); g.globalAlpha = 1;
      // zone label at mid-angle (keep it red in night mode for night vision)
      const am = (a1 + a2) / 2, rl = (R + rin) / 2;
      g.fillStyle = isNight() ? "#ffc0c0" : "#fff"; g.font = "600 12px system-ui";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(z.sail, cx + Math.cos(am) * rl, cy + Math.sin(am) * rl);
    });

    // toss-up overlays: a two-colour hatch over the zone ring where two sails tie (~1.5%), so a sail the
    // winner-take-all zones erased on a near-tie reappears in its own colour.
    (last.overlaps || []).forEach((o) => {
      if (!o.sails || o.sails.length < 2) return;
      const a1 = twaToAngle(o.twa_min), a2 = twaToAngle(o.twa_max);
      g.save();
      g.beginPath();
      g.arc(cx, cy, R, a1, a2); g.arc(cx, cy, rin, a2, a1, true); g.closePath();
      g.globalAlpha = 0.6;
      g.fillStyle = hatch(g, colour(pal, o.sails[0]), colour(pal, o.sails[1]));
      g.fill();
      g.restore();
      // small ≈ marker at mid-angle, outer edge
      const am = (a1 + a2) / 2, rl = R * 0.9;
      g.fillStyle = isNight() ? "#ffd0d0" : "#0b1622"; g.font = "700 11px system-ui";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText("≈", cx + Math.cos(am) * rl, cy + Math.sin(am) * rl);
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
    g.fillText("TWA " + last.tack + " · " + Math.round(last.tws_used) + " kts", cx, cy - R * 0.42 + 16);
  }

  function render() {
    const rec = document.getElementById("sailRec");
    if (last && last.available) {
      rec.textContent = last.recommendation +
        (last.targets && last.targets.btv ? `  ·  target ${last.targets.btv} kts, heel ${last.targets.heel}°` : "");
      rec.className = "sailrec" + (last.wrong_sail ? " bad"
        : (last.next_crossover && last.next_crossover.deg_away <= 8 ? " warn" : ""));
    } else {
      rec.textContent = (last && last.note) || "Waiting for true wind…";
      rec.className = "sailrec";
    }
    buttons();
    document.querySelectorAll("#sailSel button").forEach((b) =>
      b.classList.toggle("on", (b.dataset.sail || "") === hoisted));
    draw();
  }

  // The "Up:" buttons are the boat's own sails (#305), rebuilt when the list changes.
  // An explicit `sails: []` (a boat that lists none) clears them, never leaving another boat's
  // buttons live; a missing field (a gated or older answer) keeps the buttons shown.
  let shownSails = null;
  function buttons() {
    if (!last || !Array.isArray(last.sails)) return;
    const list = last.sails.map((x) => x && x.name).filter(Boolean);
    const key = list.join(",");
    const sel = document.getElementById("sailSel");
    if (!sel || key === shownSails) return;
    shownSails = key;
    if (hoisted && !list.includes(hoisted)) {          // the last boat's sail: not this boat's
      hoisted = "";
      try { localStorage.setItem("sr33.hoisted", ""); } catch (e) { /* private mode */ }
    }
    sel.querySelectorAll("button").forEach((b) => b.remove());
    list.concat([""]).forEach((name) => {
      const b = document.createElement("button");
      b.dataset.sail = name; b.textContent = name || "—";
      b.addEventListener("click", () => pick(name));
      sel.appendChild(b);
    });
  }

  function pick(name) {
    hoisted = name || "";
    try { localStorage.setItem("sr33.hoisted", hoisted); } catch (e) { /* private mode */ }
    if (App.lastConditions) update(App.lastConditions); else render();
  }

  async function update(c) {
    // Sail-selection coaching is gated by race mode (RRS 41) — withheld server-side in a race.
    // Onboard (Pi engine) it's always available: the boat's own computer is legal in-race.
    if (typeof tacticsAllowed === "function" && !tacticsAllowed()) {
      last = { available: false, note: "Sail advice withheld — RACE mode (RRS 41)." }; render(); return;
    }
    if (!c || c.tws == null || c.twa == null) { last = { available: false, note: "No true wind yet." }; render(); return; }
    try {
      const q = `tws=${c.tws}&twa=${c.twa}` + (hoisted ? `&hoisted=${encodeURIComponent(hoisted)}` : "");
      last = await (await apiFetch("/api/sail?" + q)).json();
    } catch (e) { last = { available: false, note: "Sail advice unavailable." }; }
    render();
  }

  // wire selector + live updates
  window.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("#sailSel button").forEach((b) =>
      b.addEventListener("click", () => pick(b.dataset.sail || "")));
    render();
  });
  window.addEventListener("sr33:conditions", (e) => { App.lastConditions = e.detail; update(e.detail); });
  window.addEventListener("resize", draw);
  // redraw on theme change (palette swap)
  new MutationObserver(draw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
})();
