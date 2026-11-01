/* FORECAST PLUME — every router weather model's wind along the planned route, run by run.
   Pure functions (no DOM, no fetch): loaded by index.html ahead of dashboard.js
   (window.C4Plume) and required by pi/console/test_plume.js under node.

   The plume comes from the Lab (vps/lab/app/plume.py) via the Pi engine's cached GET /plume:
     { models: [{ model: "hrrr", runs: [{ cycle, at, pts: [[t, tws_kn, twd_deg], ...], est? }] }],
       basis: "route"|"point", label, built_at, age_s, ... }
   runs are newest first. The chart draws the latest run of each model bold and the older runs
   of the same model fading, with the boat's own measured wind pinned at "now" so you can see
   which model is tracking. Colour is never the only channel: every latest line carries its
   model name at its end, and the night theme adds dash patterns (dashboard.css). */
(function (root) {
  "use strict";

  const ORDER = ["gfs", "nam", "hrrr", "ecmwf", "icon", "gem"];
  const LABEL = { gfs: "GFS", nam: "NAM", hrrr: "HRRR", ecmwf: "ECMWF", icon: "ICON", gem: "GEM" };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const known = (m) => Object.prototype.hasOwnProperty.call(LABEL, m);
  const label = (m) => esc(known(m) ? LABEL[m] : String(m || "?").toUpperCase());
  const cls = (m) => "pl-" + (known(m) ? m : "other");
  const angDiff = (a, b) => ((a - b + 540) % 360) - 180;     // a−b in [−180, 180]

  function models(plume) {
    // only the supported models, and only runs whose numbers are numbers
    const num = (x) => typeof x === "number" && isFinite(x);
    const ms = ((plume && plume.models) || []).filter((m) => m && known(m.model) && Array.isArray(m.runs))
      .map((m) => ({ model: m.model, runs: m.runs.filter((r) => r && num(r.cycle) && Array.isArray(r.pts) &&
        r.pts.every((p) => Array.isArray(p) && p.length === 3 && p.every(num))) }))
      .filter((m) => m.runs.length);
    return ms.slice().sort((a, b) => {
      const ia = ORDER.indexOf(a.model), ib = ORDER.indexOf(b.model);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
  }
  function available(plume) { return !!(plume && plume.available !== false && models(plume).length); }

  /* a run's value at time t (linear; direction the short way round), or null outside its span */
  function valueAt(pts, t, field) {
    if (!pts || !pts.length) return null;
    const k = field === "twd" ? 2 : 1;
    if (t < pts[0][0] || t > pts[pts.length - 1][0]) return null;
    for (let i = 0; i < pts.length; i++) {
      if (pts[i][0] === t) return pts[i][k];
      if (i && pts[i - 1][0] < t && t < pts[i][0]) {
        const a = pts[i - 1], b = pts[i], f = (t - a[0]) / ((b[0] - a[0]) || 1);
        return k === 2 ? (a[2] + angDiff(b[2], a[2]) * f + 360) % 360 : a[1] + (b[1] - a[1]) * f;
      }
    }
    return null;
  }

  /* the latest run of every model at time t: [{model, tws, twd}] + speed range + direction spread */
  function spreadAt(plume, t) {
    const out = [];
    for (const m of models(plume)) {
      const r = m.runs[0], tws = valueAt(r.pts, t, "tws"), twd = valueAt(r.pts, t, "twd");
      if (tws != null && twd != null) out.push({ model: m.model, tws: tws, twd: twd });
    }
    if (!out.length) return null;
    const tws = out.map((o) => o.tws);
    // direction spread = widest gap from the circular mean
    let sx = 0, sy = 0;
    out.forEach((o) => { sx += Math.sin(o.twd * Math.PI / 180); sy += Math.cos(o.twd * Math.PI / 180); });
    const mean = (Math.atan2(sx, sy) * 180 / Math.PI + 360) % 360;
    const dev = out.map((o) => angDiff(o.twd, mean));
    return { n: out.length, models: out, lo: Math.min.apply(null, tws), hi: Math.max.apply(null, tws),
      mean_tws: tws.reduce((a, b) => a + b, 0) / tws.length, mean_twd: mean,
      twd_spread: Math.max.apply(null, dev) - Math.min.apply(null, dev) };
  }

  function ageText(s) {
    if (s == null || !isFinite(s)) return "?";
    s = Math.max(0, s);
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min";
    if (s < 48 * 3600) return Math.round(s / 3600) + " h";
    return Math.round(s / 86400) + " d";
  }
  /* the data counts as OLD (say so on the face) once it's past this — HRRR alone runs hourly,
     so a healthy feed is never more than ~2 h behind */
  const OLD_S = 3 * 3600;
  const isOld = (plume) => !!(plume && plume.age_s != null && plume.age_s > OLD_S);

  /* frozen at the gun: from the race start until the race is over the boat keeps the pre-start
     plume (RRS 41) — say so instead of looking live */
  function isFrozen(plume, now) {
    if (!plume) return false;
    if (plume.frozen === true) return true;
    const rs = plume.race_start, re = plume.race_end;
    return typeof rs === "number" && now >= rs && (typeof re !== "number" || now < re);
  }
  const hhmm = (epoch) => { const d = new Date(epoch * 1000); return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2); };
  const frozenText = (plume) => "Frozen at the start · " + hhmm(plume.race_start);

  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function cycleText(epoch, now) {
    const d = new Date(epoch * 1000), hh = ("0" + d.getUTCHours()).slice(-2) + "Z";
    const today = new Date((now || epoch) * 1000);
    return d.getUTCDate() === today.getUTCDate() ? hh : DAYS[d.getUTCDay()] + " " + hh;
  }

  /* unwrap directions to one continuous band around `ref` so a line crossing north doesn't jump */
  const unwrap = (v, ref) => ref + angDiff(v, ref);

  /* The plume as an SVG string. o: { w, h, now, field: "tws"|"twd", only: model|null,
     allRuns: bool, back_h, ahead_h, obs: [{t, tws, twd}], nowObs: {tws, twd}|null, compact } */
  function chartSvg(plume, o) {
    o = o || {};
    const w = o.w || 512, h = o.h || 200, field = o.field === "twd" ? "twd" : "tws";
    const now = o.now || Date.now() / 1000;
    const back = (o.back_h != null ? o.back_h : 6) * 3600, ahead = (o.ahead_h != null ? o.ahead_h : 24) * 3600;
    const t0 = now - back, t1 = now + ahead;
    const compact = !!o.compact;
    const padL = compact ? 22 : 30, padR = compact ? 6 : 46, padT = 6, padB = compact ? 13 : 16;
    const ms = models(plume).filter((m) => !o.only || m.model === o.only);
    if (!ms.length) return "";
    const k = field === "twd" ? 2 : 1;
    // the runs to draw: [model, run, rank, nRuns]
    const draw = [];
    for (const m of ms) {
      const runs = o.allRuns ? m.runs : m.runs.slice(0, 1);
      runs.forEach((r, i) => draw.push([m.model, r, i, runs.length]));
    }
    const obs = (o.obs || []).filter((p) => p.t >= t0 && p.t <= now && p[field] != null);
    const nowObs = o.nowObs && o.nowObs[field] != null ? o.nowObs : null;
    // the direction reference: the circular mean of the latest runs (+ the boat's reading)
    let ref = 0;
    if (field === "twd") {
      let sx = 0, sy = 0;
      const add = (d) => { sx += Math.sin(d * Math.PI / 180); sy += Math.cos(d * Math.PI / 180); };
      draw.filter((d) => d[2] === 0).forEach((d) => d[1].pts.forEach((p) => add(p[2])));
      if (nowObs) add(nowObs.twd);
      ref = (Math.atan2(sx, sy) * 180 / Math.PI + 360) % 360;
    }
    const val = (v) => (field === "twd" ? unwrap(v, ref) : v);
    const vals = [];
    draw.forEach((d) => d[1].pts.forEach((p) => { if (p[0] >= t0 && p[0] <= t1) vals.push(val(p[k])); }));
    obs.forEach((p) => vals.push(val(p[field])));
    if (nowObs) vals.push(val(nowObs[field]));
    if (!vals.length) return "";
    let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    let step;
    if (field === "tws") {
      if (hi - lo < 4) { const mid = (hi + lo) / 2; lo = mid - 2; hi = mid + 2; }
      step = hi - lo > 16 ? 5 : hi - lo > 8 ? 4 : 2;
      lo = Math.max(0, Math.floor(lo / step) * step); hi = Math.ceil(hi / step) * step;
    } else {
      if (hi - lo < 30) { const mid = (hi + lo) / 2; lo = mid - 15; hi = mid + 15; }
      step = hi - lo > 120 ? 45 : hi - lo > 60 ? 30 : 15;
      lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
    }
    const X = (t) => padL + ((t - t0) / (t1 - t0)) * (w - padL - padR);
    const Y = (v) => padT + (1 - (v - lo) / ((hi - lo) || 1)) * (h - padT - padB);
    const f1 = (x) => x.toFixed(1);
    let s = '<svg class="rchart plume' + (compact ? " compact" : "") + '" viewBox="0 0 ' + w + " " + h +
      '" width="100%" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Forecast plume, ' +
      (field === "tws" ? "wind speed" : "wind direction") + ' by model">';
    // y grid + labels
    const ystep = compact ? step * Math.max(1, Math.round((hi - lo) / step / 2)) : step;
    for (let v = lo; v <= hi + 1e-9; v += ystep) {
      const y = f1(Y(v));
      s += '<line class="ax-grid" x1="' + padL + '" y1="' + y + '" x2="' + (w - padR) + '" y2="' + y + '"/>';
      const lab = field === "twd" ? (((Math.round(v) % 360) + 360) % 360) + "°" : String(Math.round(v));
      s += '<text class="ax-lab" x="' + (padL - 3) + '" y="' + f1(Y(v) + 3.5) + '" text-anchor="end">' + lab + "</text>";
    }
    // x ticks every 6 h relative to now
    for (let hh = -Math.floor(back / 3600 / 6) * 6; hh * 3600 <= ahead; hh += 6) {
      const x = f1(X(now + hh * 3600));
      if (hh !== 0) s += '<line class="ax-grid" x1="' + x + '" y1="' + padT + '" x2="' + x + '" y2="' + (h - padB) + '"/>';
      const anchor = (hh + 6) * 3600 > ahead ? "end" : "middle";       // the last tick stays inside
      s += '<text class="ax-lab" x="' + x + '" y="' + (h - 3) + '" text-anchor="' + anchor + '">' +
        (hh === 0 ? "now" : (hh > 0 ? "+" : "−") + Math.abs(hh) + "h") + "</text>";
    }
    s += '<line class="pl-nowline" x1="' + f1(X(now)) + '" y1="' + padT + '" x2="' + f1(X(now)) + '" y2="' + (h - padB) + '"/>';
    // older runs first (fading with age), then every model's latest on top
    const ordered = draw.slice().sort((a, b) => b[2] - a[2]);
    for (const [m, r, rank, n] of ordered) {
      let pts = r.pts.filter((p) => p[0] >= t0 && p[0] <= t1);
      if (pts.length < 2) continue;
      const poly = pts.map((p) => f1(X(p[0])) + "," + f1(Y(val(p[k])))).join(" ");
      const op = rank === 0 ? 1 : Math.max(0.12, 0.6 * (1 - rank / Math.max(2, n)));
      s += '<polyline class="pl-run ' + cls(m) + (rank === 0 ? " pl-latest" : " pl-old") + '" points="' + poly +
        '"' + (rank ? ' style="opacity:' + op.toFixed(2) + '"' : "") + "/>";
    }
    // the boat's own measured wind: the recent history + a dot pinned at now
    if (obs.length > 1)
      s += '<polyline class="pl-obs" points="' + obs.map((p) => f1(X(p.t)) + "," + f1(Y(val(p[field])))).join(" ") + '"/>';
    if (nowObs)
      s += '<circle class="pl-nowdot" cx="' + f1(X(now)) + '" cy="' + f1(Y(val(nowObs[field]))) + '" r="' + (compact ? 3.5 : 4.5) + '"/>';
    // end-of-line model labels (detail only), nudged apart so they never overlap
    if (!compact) {
      const ends = [];
      for (const [m, r, rank] of draw) {
        if (rank !== 0) continue;
        const pts = r.pts.filter((p) => p[0] >= t0 && p[0] <= t1);
        if (pts.length < 2) continue;
        const p = pts[pts.length - 1];
        ends.push({ m: m, x: X(p[0]), y: Y(val(p[k])) });
      }
      ends.sort((a, b) => a.y - b.y);
      for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 11) ends[i].y = ends[i - 1].y + 11;
      ends.forEach((e) => {
        s += '<text class="pl-end ' + cls(e.m) + '" x="' + f1(Math.min(e.x + 3, w - padR + 3)) + '" y="' + f1(e.y + 3.5) + '">' + label(e.m) + "</text>";
      });
    }
    return s + "</svg>";
  }

  /* the colour key: one chip per model (+ its latest cycle when `cycles`) */
  function legendHtml(plume, o) {
    o = o || {};
    return '<div class="pl-legend">' + models(plume).filter((m) => !o.only || m.model === o.only).map((m) =>
      '<span class="pl-key ' + cls(m.model) + '"><i></i>' + label(m.model) +
      (o.cycles ? " " + (m.runs[0].est ? "~" : "") + cycleText(m.runs[0].cycle, o.now) : "") + "</span>").join("") +
      (o.boat ? '<span class="pl-key pl-boat"><i></i>boat</span>' : "") + "</div>";
  }

  /* per-model run times for the detail: latest first */
  function runTimesHtml(plume, now) {
    const rows = models(plume).map((m) => {
      const r = m.runs;
      return '<div class="t-row"><span class="rl"><span class="pl-key ' + cls(m.model) + '"><i></i>' + label(m.model) + "</span></span>" +
        '<span class="rc"><b>' + (r[0].est ? "~" : "") + cycleText(r[0].cycle, now) + "</b>" +
        (r.length > 1 ? " · then " + r.slice(1, 6).map((x) => cycleText(x.cycle, now)).join(", ") + (r.length > 6 ? " +" + (r.length - 6) : "") : "") +
        "</span></div>";
    }).join("");
    return '<div class="t-rows pl-runs"><div class="t-row hdr"><span class="rl">model</span><span class="rc">latest run · older runs (UTC)</span></div>' + rows + "</div>";
  }

  /* one-line summary for the tile face: speed range across the models at +h hours */
  function summary(plume, now, hAhead) {
    const sp = spreadAt(plume, now + (hAhead || 6) * 3600);
    if (!sp) return "";
    const lo = Math.round(sp.lo), hi = Math.round(sp.hi);
    return "+" + (hAhead || 6) + " h: " + (lo === hi ? lo : lo + "–" + hi) + " kts · " + sp.n + " model" + (sp.n === 1 ? "" : "s");
  }

  const api = { ORDER, LABEL, label, known, models, available, valueAt, spreadAt, ageText, isOld, OLD_S,
    isFrozen, frozenText, cycleText, chartSvg, legendHtml, runTimesHtml, summary, esc };
  root.C4Plume = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
