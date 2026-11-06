/* monitor-geo.js — pure helpers for the Lab Monitor tab (no DOM, no Leaflet), so node can test them.
   Loaded by index.html before app.js; vps/lab/test_monitor_geo.js requires it directly. */
"use strict";

const MonGeo = (function () {
  // A heading or COG older than this is not drawn as a direction. Matches the agent strip's
  // `stale` line (30 s), the same freshness the onboard instruments use.
  const FRESH_S = 30;
  // Below this SOG the COG is GPS noise: still drawn, but dimmed.
  const SLOW_KN = 0.5;

  // "12 min", "3.4 h", "41 h", "102 days". Tracker ages run from seconds to months.
  function fmtAge(s) {
    if (s == null || !isFinite(s)) return "";
    s = Math.max(0, s);
    return s < 3600 ? Math.round(s / 60) + " min"
      : s < 172800 ? (s / 3600).toFixed(s < 36000 ? 1 : 0) + " h"
        : Math.round(s / 86400) + " days";
  }

  // How to draw our live boat (#240): the hull points along heading only when the heading is
  // fresh AND its reference is known. The map is true north and the Lab has no magnetic
  // variation, so a magnetic heading can't be drawn honestly: it is named in text only.
  // COG is its own vector, dimmed when SOG is near zero. No usable heading → neutral symbol.
  function ownSymbol(own) {
    own = own || {};
    const num = (v) => typeof v === "number" && isFinite(v);
    const age = (a) => (num(a) ? a : num(own.age_s) ? own.age_s : null);
    const hAge = age(own.heading_age_s), cAge = age(own.cog_age_s);
    const headingFresh = num(own.heading) && hAge != null && hAge <= FRESH_S;
    const hull = headingFresh && own.heading_ref === "true" ? own.heading : null;
    const cogFresh = num(own.cog) && cAge != null && cAge <= FRESH_S;
    const cog = cogFresh ? own.cog : null;
    const cogDim = cog != null && !(num(own.sog) && own.sog >= SLOW_KN);
    return { hull, cog, cogDim, headingFresh, headingRef: own.heading_ref || null };
  }

  // Short text for heading: "HDG 339°T", "HDG 339°M", "HDG 339° (old)", "HDG ?".
  function headingText(own) {
    own = own || {};
    if (typeof own.heading !== "number") return "HDG ?";
    const s = ownSymbol(own);
    const ref = own.heading_ref === "true" ? "T" : own.heading_ref === "magnetic" ? "M" : "";
    return `HDG ${Math.round(own.heading)}°${ref}${s.headingFresh ? "" : " (old)"}`;
  }

  function distNm(a, b) {
    const k = Math.cos(((a.lat + b.lat) / 2) * Math.PI / 180);
    return Math.hypot((a.lat - b.lat) * 60, (a.lon - b.lon) * 60 * k);
  }

  // Distance (nm) from p to the segment a-b, on a local flat projection (fine at course scale).
  function segDistNm(p, a, b) {
    const k = Math.cos(p.lat * Math.PI / 180);
    const ax = (a.lon - p.lon) * 60 * k, ay = (a.lat - p.lat) * 60;
    const bx = (b.lon - p.lon) * 60 * k, by = (b.lat - p.lat) * 60;
    const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
    const t = L2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / L2)) : 0;
    return Math.hypot(ax + t * dx, ay + t * dy);
  }

  // What the Monitor map fits to (#102 G86). With course marks: the marks, our live boat, and every
  // tracker fix near the course. Without marks: fixes near the middle of the fleet. A fix far off
  // (a trailer fix inland, a boat that went home) is returned in `far`: drawn faint, not fitted.
  // "Near the course" = within max(10 nm, a sixth of the course span) of any leg (marks in order,
  // per `course` index). With no marks: within max(20 nm, 3 × the median distance) of the fleet's
  // median position.
  function fitView(marks, fixes, own) {
    marks = (marks || []).filter((m) => m && isFinite(m.lat) && isFinite(m.lon));
    fixes = fixes || [];
    own = own || {};
    const pts = [], far = new Set();
    let near;
    if (marks.length) {
      let span = 0;
      for (let i = 0; i < marks.length; i++) for (let j = i + 1; j < marks.length; j++) span = Math.max(span, distNm(marks[i], marks[j]));
      const lim = Math.max(10, span / 6);
      const legs = [];
      marks.forEach((m, i) => {
        const prev = marks[i - 1];
        legs.push(prev && prev.course === m.course ? [prev, m] : [m, m]);
      });
      near = (p) => legs.some(([a, b]) => segDistNm(p, a, b) <= lim);
      marks.forEach((m) => pts.push([m.lat, m.lon]));
    } else if (fixes.length) {
      const med = (a) => { const s = a.slice().sort((x, y) => x - y), n = s.length; return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; };
      const c = { lat: med(fixes.map((f) => f.lat)), lon: med(fixes.map((f) => f.lon)) };
      const lim = Math.max(20, 3 * med(fixes.map((f) => distNm(c, f))));
      near = (p) => distNm(c, p) <= lim;
    } else near = () => true;
    fixes.forEach((f, i) => { if (near(f)) pts.push([f.lat, f.lon]); else far.add(i); });
    if (own.available && isFinite(own.lat) && isFinite(own.lon)) pts.push([own.lat, own.lon]);
    return { pts, far };
  }

  return { FRESH_S, SLOW_KN, fmtAge, ownSymbol, headingText, distNm, segDistNm, fitView };
})();

if (typeof module !== "undefined" && module.exports) module.exports = MonGeo;
