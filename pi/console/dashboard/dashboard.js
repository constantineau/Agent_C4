/* SR33 Crew Dashboard — higher-order tiles (live onboard engine + deterministic status).
   Design: docs/COPILOT_DASHBOARD.md + the crew's example slide. The grid surfaces the
   higher-order reads the sensors alone don't show:
     VMG · TWS Trend · Tactics · Forecast · Sail · Time to Mark · Crew Energy · Data
   TWS Trend and Forecast use VECTOR ARROWS for true wind direction (the arrow points the way
   the wind blows TO — a north wind points down, an east wind points left), with the speed in
   kts beside it, at fixed time points (now / −10 / −30 / −60 for the trend; now / +30 / +60
   for the forecast, plus a "forecast vs. actual" verification at −30 / −60). Both are computed
   client-side from the live poll stream. Engine owns the truth (deterministic, LLM-off).
   LLM refinement + commentary arrive in phase 3. */
(function () {
  "use strict";

  const STATUS = {
    ok:    { icon: "●", word: "OK"    },
    watch: { icon: "▲", word: "WATCH" },
    act:   { icon: "■", word: "ACT"   },
    na:    { icon: "◌", word: "—"     },
  };
  const SEV = { ok: 0, watch: 1, act: 2, na: 2 };

  const TILES = ["vmg", "wind", "tactics", "forecast", "sail", "eta", "charge", "data"];
  const NAME = {
    vmg: "VMG", wind: "TWS Trend", tactics: "Tactics", forecast: "Forecast",
    sail: "Sail", eta: "Time to Mark", charge: "Crew Energy", data: "Data",
  };

  const D2R = Math.PI / 180, R2D = 180 / Math.PI;
  const WIND_WIN_S = 12 * 3600;   // keep the whole race (~12 h) — feeds both the lookback rows and the race chart
  const FCST_WIN_S = 140 * 60;    // keep ~140 min of forecast snapshots (for the −120 min verification)
  const SERIES_MIN = 720;         // ask the engine for ~12 h of archived wind for the chart
  const COPILOT = "/copilot";     // proxied to the Orin copilot (:8300); writes the commentary
  const BRIEF_EVERY = 90000;      // ask the LLM for a fresh brief ~every 90 s (it's slow, ~45 s)
  const BRIEF_TTL = 300;          // an LLM brief stays "fresh" 5 min before reverting to engine-read

  /* ---- tiny helpers needed early (used in the demo scenarios) ---- */
  const r0 = (x) => (x == null ? "?" : Math.round(x));
  const r1 = (x) => (x == null ? "?" : Math.round(x * 10) / 10);
  const angDiff = (a, b) => ((a - b + 540) % 360) - 180;   // a−b in [−180,180]; + = a right of b
  /* a wind-direction vector arrow. Points where the wind blows TO = (TWD+180): N wind→down,
     E wind→left. SVG arrow drawn pointing up, rotated about its centre. */
  function windArrow(twd) {
    const rot = Math.round(((twd || 0) + 180) % 360);
    return '<svg class="warr" width="16" height="16" viewBox="0 0 24 24">' +
      '<g transform="rotate(' + rot + ' 12 12)"><path d="M12 20 L12 5 M12 4 L7.5 10 M12 4 L16.5 10"/></g></svg>';
  }
  const arrowKts = (twd, kts) => windArrow(twd) + r0(kts) + " kts";

  /* ============================ DEMO scenarios (canned) ============================ */
  const SCENARIOS = {
    calm: {
      mode: "llm-live", focus: "Solid groove — the left side is paying.", confidence: "high",
      notes: [
        { tile: "tactics", status: "ok", text: "Left has paid the last two oscillations — stay set up to tack on the next header.", conf: "high" },
        { tile: "vmg",     status: "ok", text: "VMG at 96% of target and pointing well. Hold the groove.", conf: "high" },
      ],
      tiles: {
        vmg:     { status: "ok", value: "5.4 kts", sub: "upwind · 96% of target", why: "VMG 5.4 kts to windward vs a 5.6 kts polar target — 96%.", consider: "Good VMG — hold the groove.", clears: "—", based: ["computed VMG = STW·cos(TWA)", "get_sail: target VMG 5.6 kts"], conf: "high" },
        wind:    { status: "ok", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(250, 12)] }, { label: "−60 min", cols: [arrowKts(246, 14)] }, { label: "−120 min", cols: [arrowKts(242, 16)] }],
                   why: "True wind speed + direction now and looking back. Arrows point the way the wind blows (north wind points down, east points left). Eased a touch and edged right over the last couple of hours.", consider: "Oscillating breeze — work the shifts.", clears: "—", based: ["engine archive + live buffer"], conf: "high" },
        tactics: { status: "ok", value: "◀ Left", sub: "oscillating, favor left", why: "Oscillating; the left has paid. Lifted now.", consider: "Tack on the next header.", clears: "—", based: ["get_tactics: favored left, lifted"], conf: "high" },
        forecast:{ status: "ok", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(250, 10)] }, { label: "+60 min", cols: [arrowKts(256, 13)] }, { label: "+120 min", cols: [arrowKts(262, 15)] }, { sep: true, label: "FORECAST VS. ACTUAL" }, { hdr: true, cols: ["forecast", "actual"] }, { label: "−60 min", cols: [arrowKts(244, 13), arrowKts(243, 14)] }, { label: "−120 min", cols: [arrowKts(240, 16), arrowKts(238, 17)] }],
                   why: "Forecast wind speed + direction (arrows show direction; north wind points down). Building to ~15 kts and veering right. \"Forecast vs. actual\" compares the earlier forecast for −60/−120 min ago against what actually happened — within ~1 kt, verifying well.", consider: "Plan the gear for the build.", clears: "—", based: ["fetch_forecast + engine archive"], conf: "high" },
        sail:    { status: "ok", value: "J1", sub: "in range", why: "J1 is right for 12 kts upwind.", consider: "No change.", clears: "TWS > 16 kts", based: ["get_sail: optimal J1"], conf: "high" },
        eta:     { status: "ok", value: "16 min", sub: "Cove Island", why: "~16 min to Cove Island at the current made-good.", consider: "On schedule for the mark.", clears: "—", based: ["get_navigator: ETA 16 min"], conf: "high" },
        charge:  { status: "ok", value: "72", sub: "fresh", why: "Crew energy ~72% (inverse of the fatigue index; lower = more depleted).", consider: "Driver fresh — no rotation needed.", clears: "—", based: ["get_fatigue: index 28 → energy 72%"], conf: "high" },
        data:    { status: "ok", value: "5", sub: "sources live", why: "All five sensor groups fresh.", consider: "Instruments healthy.", clears: "—", based: ["get_sources: 5 live"], conf: "high" },
      },
    },
    escalated: {
      mode: "llm-live", focus: "Bear-away coming up, and the crew tank is getting low.", confidence: "med",
      notes: [
        { tile: "sail",     status: "act",   text: "Peel J1 → A3 before the bear-away at the gate — start staging now (~4 min out).", conf: "high" },
        { tile: "charge",   status: "act",   text: "Crew energy down to 28% (rotate soon) — plan a driver change in the next few minutes.", conf: "med" },
        { tile: "forecast", status: "watch", text: "Forecast has been under-calling the breeze by ~2-3 kts — expect a bit more than it says.", conf: "med" },
      ],
      tiles: {
        vmg:     { status: "watch", value: "4.6 kts", sub: "upwind · 82% of target", why: "VMG 4.6 kts vs a 5.6 kts target — 82%. Pinching in the chop.", consider: "Down on VMG — ease the angle to rebuild made-good.", clears: "back over 90% of the VMG target", based: ["computed VMG = STW·cos(TWA)"], conf: "med" },
        wind:    { status: "watch", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(262, 16)] }, { label: "−60 min", cols: [arrowKts(252, 12)] }, { label: "−120 min", cols: [arrowKts(246, 9)] }],
                   why: "True wind speed + direction now and looking back. Arrows point the way the wind blows (north wind points down). Built ~7 kts and veered right ~16° over the last two hours — a persistent right trend.", consider: "Persistent right shift — favor the right side of the course.", clears: "the trend settles", based: ["engine archive + live buffer"], conf: "med" },
        tactics: { status: "watch", value: "Right ▶", sub: "persistent, favor right", why: "The breeze has shifted right and is holding — persistent, not oscillating.", consider: "Favor the right.", clears: "the shift reverses", based: ["get_tactics: favored right, persistent"], conf: "med" },
        forecast:{ status: "watch", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(262, 16)] }, { label: "+60 min", cols: [arrowKts(268, 18)] }, { label: "+120 min", cols: [arrowKts(274, 19)] }, { sep: true, label: "FORECAST VS. ACTUAL" }, { hdr: true, cols: ["forecast", "actual"] }, { label: "−60 min", cols: [arrowKts(252, 12), arrowKts(256, 14)] }, { label: "−120 min", cols: [arrowKts(248, 10), arrowKts(252, 12)] }],
                   why: "Forecast wind speed + direction (arrows show direction; north wind points down). Building to ~19 kts and veering right. \"Forecast vs. actual\" shows it has under-called the wind by ~2 kts and the right shift — trust the trend over the model.", consider: "Forecast running light — plan for more than it says.", clears: "forecast comes back in line", based: ["fetch_forecast + engine archive"], conf: "med" },
        sail:    { status: "act",   value: "J1 → A3", sub: "peel before bear-away", why: "The leg after the gate bears away to ~135° TWA — an A3 leg. Peel before the rounding.", consider: "Stage the A3 and peel in ~4 min.", clears: "A3 hoisted", based: ["get_sail: A3 for TWA 135°"], conf: "high" },
        eta:     { status: "watch", value: "4 min", sub: "Cove Island", why: "~4 min to Cove Island at the current made-good.", consider: "Mark in ~4 min — start the rounding prep.", clears: "past the rounding", based: ["get_navigator: ETA 4 min"], conf: "high" },
        charge:  { status: "act",   value: "28", sub: "rotate soon", why: "Crew energy ~28% (rotate soon). Heading instability and steering reversals up, speed deficit creeping.", consider: "Tank getting low — plan a helm rotation.", clears: "energy back above 65%", based: ["get_fatigue: index 72 → energy 28%"], conf: "med", components: { heading: 0.7, reversals: 0.8, heel: 0.4, "spd-def": 0.5 } },
        data:    { status: "watch", value: "4", sub: "1 stale", why: "Masthead wind stale ~50 s ago; running on the Orca backup.", consider: "Running on backup wind — watch for it to return.", clears: "all sources fresh", based: ["get_sources: 4 live, 1 stale"], conf: "med" },
      },
    },
  };

  /* ============================ helpers ============================ */
  const API = "/api";
  const NA = (note) => ({ status: "na", value: "—", sub: note || "no data", why: note || "No data from the engine.",
    consider: "—", clears: "—", based: [], conf: "engine" });
  const stripTags = (s) => String(s).replace(/<[^>]+>/g, "");

  function fetchJSON(path, ms) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms || 5000);
    return fetch(API + path, { signal: ctl.signal, headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null)).catch(() => null).finally(() => clearTimeout(t));
  }
  function circMeanDeg(degs) {
    const s = degs.reduce((a, d) => a + Math.sin(d * D2R), 0) / degs.length;
    const c = degs.reduce((a, d) => a + Math.cos(d * D2R), 0) / degs.length;
    return (Math.atan2(s, c) * R2D + 360) % 360;
  }

  /* ---- rolling buffers fed by the poll loop ---- */
  function pushWind(c) {
    if (!c || !c.available || c.tws == null || c.twd == null) return;
    const now = Date.now() / 1000;
    App.windHist.push({ t: now, tws: c.tws, twd: c.twd });
    const cut = now - WIND_WIN_S;
    while (App.windHist.length && App.windHist[0].t < cut) App.windHist.shift();
    if (now - App.lastPersist > 30) { App.lastPersist = now; persistRace(); }   // survive a reload
  }
  /* persist a downsampled copy of the race wind + forecast history so a reload doesn't lose it */
  function persistRace() {
    try {
      const ds = dsample(App.windHist, 1800);
      localStorage.setItem("sr33.dash.windhist", JSON.stringify(ds.map((p) => [Math.round(p.t), Math.round(p.tws * 10) / 10, Math.round(p.twd)])));
      localStorage.setItem("sr33.dash.fcsthist", JSON.stringify(App.fcstHist));
    } catch (e) { /* quota / disabled — best effort */ }
  }
  function loadRace() {
    try {
      const now = Date.now() / 1000;
      const w = localStorage.getItem("sr33.dash.windhist");
      if (w) App.windHist = JSON.parse(w).map((a) => ({ t: a[0], tws: a[1], twd: a[2] })).filter((p) => p.t > now - WIND_WIN_S);
      const f = localStorage.getItem("sr33.dash.fcsthist");
      if (f) App.fcstHist = JSON.parse(f).filter((e) => e.t > now - FCST_WIN_S);
    } catch (e) { /* ignore */ }
  }
  /* pull the whole-race wind series from the onboard engine archive (authoritative + reload-proof) */
  async function fetchSeries() {
    const r = await fetchJSON("/series?minutes=" + SERIES_MIN, 8000);
    App.seriesHist = (r && r.available && Array.isArray(r.points)) ? r.points.filter((p) => p.tws != null) : [];
  }
  /* best available race history: the engine archive (authoritative) + the live tail not yet archived;
     falls back to the client buffer when the archive is empty (e.g. on the bench). */
  function raceData() {
    if (App.seriesHist && App.seriesHist.length) {
      const lastT = App.seriesHist[App.seriesHist.length - 1].t;
      const tail = App.windHist.filter((p) => p.t > lastT + 1);
      return App.seriesHist.concat(tail);
    }
    return App.windHist;
  }
  function pushForecast(fc) {
    if (!fc || !fc.available || !fc.hours || !fc.hours.length) return;
    const now = Date.now() / 1000;
    const last = App.fcstHist[App.fcstHist.length - 1];
    if (last && now - last.t < 55) return;
    App.fcstHist.push({ t: now, hours: fc.hours.map((h) => ({ in_h: h.in_h, tws: h.tws, twd: h.twd })) });
    const cut = now - FCST_WIN_S;
    while (App.fcstHist.length && App.fcstHist[0].t < cut) App.fcstHist.shift();
  }
  /* observed wind nearest a point `agoSec` in the past (0 = latest); null if history too short */
  function observedAt(agoSec) {
    const h = raceData(); if (!h.length) return null;
    const now = Date.now() / 1000, target = now - agoSec;
    if (agoSec > 0 && h[0].t > target + 60) return null;
    let best = null, bd = 1e9;
    for (const p of h) { const d = Math.abs(p.t - target); if (d < bd) { bd = d; best = p; } }
    return best ? { tws: best.tws, twd: best.twd } : null;
  }
  /* interpolate a forecast series (ascending in_h) to a given hours-ahead value */
  function fcstAt(hours, h) {
    if (!hours || !hours.length) return null;
    if (h <= hours[0].in_h) return { tws: hours[0].tws, twd: hours[0].twd };
    for (let i = 0; i < hours.length - 1; i++) {
      const a = hours[i], b = hours[i + 1];
      if (h >= a.in_h && h <= b.in_h) {
        const f = (h - a.in_h) / ((b.in_h - a.in_h) || 1);
        return { tws: a.tws + (b.tws - a.tws) * f, twd: (a.twd + angDiff(b.twd, a.twd) * f + 360) % 360 };
      }
    }
    const last = hours[hours.length - 1];
    return { tws: last.tws, twd: last.twd };
  }
  /* what an earlier forecast predicted for time T (=agoSec back) vs what was actually observed then */
  function fcstVsActual(agoSec) {
    const actual = observedAt(agoSec);
    const now = Date.now() / 1000, target = now - agoSec;
    let best = null, bd = 1e9;
    for (const e of App.fcstHist) { if (e.t <= target) { const d = target - e.t; if (d < bd) { bd = d; best = e; } } }
    if (!best || !actual) return null;
    const fcst = fcstAt(best.hours, (target - best.t) / 3600);
    return fcst ? { fcst: fcst, actual: actual } : null;
  }
  /* SVG polyline of TWD deviation from the window mean (right = up, left = down) — detail only */
  function makeTwdSpark(hist, w, h) {
    if (!hist || hist.length < 3) return "";
    const mean = circMeanDeg(hist.map((p) => p.twd));
    const devs = hist.map((p) => angDiff(p.twd, mean));
    const maxAbs = Math.max(6, Math.max.apply(null, devs.map(Math.abs)));
    const t0 = hist[0].t, span = (hist[hist.length - 1].t - t0) || 1, pad = 3;
    const pts = hist.map((p, i) => {
      const x = pad + ((p.t - t0) / span) * (w - 2 * pad);
      const y = h / 2 - (devs[i] / maxAbs) * (h / 2 - pad);
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    return '<svg class="spark" viewBox="0 0 ' + w + ' ' + h + '" width="100%" height="' + h + '" preserveAspectRatio="none">' +
      '<line class="spark-mid" x1="0" y1="' + (h / 2) + '" x2="' + w + '" y2="' + (h / 2) + '"/>' +
      '<polyline class="spark-line" points="' + pts + '"/></svg>';
  }
  /* ---- race-length TWS chart (dynamic; re-rendered each poll while the detail is open) ---- */
  function fmtDur(s) { s = Math.round(s); if (s < 90) return s + "s"; const m = Math.round(s / 60); if (m < 90) return m + "m"; return Math.floor(m / 60) + "h " + (m % 60) + "m"; }
  function dsample(hist, max) {
    if (!hist || hist.length <= max) return hist || [];
    const stride = Math.ceil(hist.length / max), out = [];
    for (let i = 0; i < hist.length; i += stride) out.push(hist[i]);
    if (out[out.length - 1] !== hist[hist.length - 1]) out.push(hist[hist.length - 1]);
    return out;
  }
  function raceChart(hist, w, h) {
    if (!hist || hist.length < 2) return '<span class="dc-empty">collecting race data…</span>';
    const padL = 30, padR = 6, padT = 6, padB = 16;
    const t0 = hist[0].t, t1 = hist[hist.length - 1].t, span = (t1 - t0) || 1;
    const tws = hist.map((p) => p.tws);
    let lo = Math.min.apply(null, tws), hi = Math.max.apply(null, tws);
    if (hi - lo < 3) { const m = (hi + lo) / 2; lo = m - 1.5; hi = m + 1.5; }
    lo = Math.max(0, Math.floor(lo - 1)); hi = Math.ceil(hi + 1);
    const X = (t) => padL + ((t - t0) / span) * (w - padL - padR);
    const Y = (v) => padT + (1 - (v - lo) / ((hi - lo) || 1)) * (h - padT - padB);
    const step = Math.max(2, Math.round((hi - lo) / 4));
    let grid = "", ylab = "";
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      const y = Y(v).toFixed(1);
      grid += '<line class="ax-grid" x1="' + padL + '" y1="' + y + '" x2="' + (w - padR) + '" y2="' + y + '"/>';
      ylab += '<text class="ax-lab" x="' + (padL - 3) + '" y="' + (Y(v) + 3).toFixed(1) + '" text-anchor="end">' + v + '</text>';
    }
    let xlab = "";
    [[t0, "−" + fmtDur(span)], [t0 + span / 2, "−" + fmtDur(span / 2)], [t1, "now"]].forEach(([t, lab]) => {
      xlab += '<text class="ax-lab" x="' + X(t).toFixed(1) + '" y="' + (h - 3) + '" text-anchor="middle">' + lab + '</text>';
    });
    const pts = hist.map((p) => X(p.t).toFixed(1) + "," + Y(p.tws).toFixed(1)).join(" ");
    return '<svg class="rchart" viewBox="0 0 ' + w + ' ' + h + '" width="100%" preserveAspectRatio="xMidYMid meet">' +
      grid + '<polyline class="rc-line" points="' + pts + '"/>' +
      '<circle class="rc-dot" cx="' + X(t1).toFixed(1) + '" cy="' + Y(hist[hist.length - 1].tws).toFixed(1) + '" r="3"/>' + ylab + xlab + '</svg>';
  }
  /* deterministic demo race series so the chart shows in DEMO mode */
  function genDemoRace(esc) {
    const n = 90, now = Date.now() / 1000, span = 2 * 3600, arr = [];
    for (let i = 0; i < n; i++) {
      const f = i / (n - 1), t = now - span * (1 - f);
      const base = esc ? 10 + 6 * f : 12 + Math.sin(f * 6) * 1.5;
      const tws = Math.max(2, base + Math.sin(i * 0.7) * 1.2 + (esc ? Math.sin(i * 0.3) : 0));
      const twd = (esc ? 248 + 14 * f : 245 + Math.sin(f * 5) * 5) + Math.sin(i * 0.9) * 3;
      arr.push({ t: t, tws: tws, twd: twd });
    }
    return arr;
  }

  function rowsHtml(rows) {
    if (!rows) return "";
    return '<div class="t-rows">' + rows.map((r) => {
      if (r.sep) return '<div class="t-row rsub"><span class="rl">' + r.label + '</span></div>';
      const cls = r.hdr ? "t-row hdr" : r.emph ? "t-row emph" : "t-row";
      const lbl = '<span class="rl">' + (r.label != null ? r.label : "") + '</span>';
      return '<div class="' + cls + '">' + lbl + (r.cols || []).map((c) => '<span class="rc">' + c + '</span>').join("") + '</div>';
    }).join("") + "</div>";
  }

  /* ============================ tile builders (live) ============================ */
  const BUILD = {
    vmg(p) {
      const c = p.conditions, s = p.sail;
      if (!c || !c.available || c.stw == null || c.twa == null) return NA("no speed / angle");
      const twa = Math.abs(c.twa), vmg = c.stw * Math.cos(twa * D2R), absv = Math.abs(vmg);
      if (!(twa < 70 || twa > 110)) {
        return { status: "ok", value: r1(absv) + " kts", sub: "reaching",
          why: "VMG " + r1(absv) + " kts — on a reach, VMG-to-wind isn't the target (sail for the mark).",
          consider: "Reaching — sail fast, not for VMG.", clears: "—", based: ["computed VMG = STW·cos(TWA " + r0(twa) + "°)"], conf: "engine" };
      }
      const tgt = s && s.available && s.targets ? Math.abs(s.targets.vmg) : null;
      const pct = tgt ? Math.round((absv / tgt) * 100) : null;
      const st = pct == null ? "ok" : pct >= 90 ? "ok" : pct >= 78 ? "watch" : "act";
      return { status: st, value: r1(absv) + " kts",
        sub: (vmg >= 0 ? "upwind" : "downwind") + (pct != null ? " · " + pct + "% of target" : ""),
        why: "VMG " + r1(absv) + " kts " + (vmg >= 0 ? "to windward" : "downwind") + (tgt ? " vs a " + r1(tgt) + " kts polar target (" + pct + "%)." : "."),
        consider: st === "ok" ? "Good VMG — hold the groove." : "Down on VMG — adjust angle/trim for better made-good.",
        clears: st !== "ok" ? "back over 90% of the VMG target" : "—",
        based: ["computed VMG = STW·cos(TWA)"].concat(tgt ? ["get_sail: target VMG " + r1(tgt) + " kts"] : []), conf: "engine" };
    },
    wind(p) {
      const pts = [["Now", 0], ["−60 min", 3600], ["−120 min", 7200]];
      const samples = pts.map(([lbl, ago]) => [lbl, observedAt(ago)]);
      const nowS = samples[0][1] || (p.conditions && p.conditions.available ? { tws: p.conditions.tws, twd: p.conditions.twd } : null);
      if (!nowS) return NA("building TWS history…");
      if (!samples[0][1]) samples[0][1] = nowS;
      const rows = samples.map(([lbl, s], i) => ({ label: lbl, emph: i === 0, cols: [s ? arrowKts(s.twd, s.tws) : "—"] }));
      let oldest = null;
      for (let i = samples.length - 1; i >= 1; i--) { if (samples[i][1]) { oldest = samples[i][1]; break; } }
      let st = "ok";
      if (oldest) { const dT = nowS.tws - oldest.tws, dD = angDiff(nowS.twd, oldest.twd); if (Math.abs(dT) >= 6 || Math.abs(dD) >= 12) st = "watch"; }
      return { status: st, value: null, rows: rows,
        why: "True wind speed + direction, now and looking back. Arrows point the way the wind is blowing (a north wind points down, an east wind points left)." +
          (oldest ? " Over the window TWS " + (nowS.tws >= oldest.tws ? "built" : "eased") + " from " + r0(oldest.tws) + " to " + r0(nowS.tws) + " kts." : ""),
        consider: st === "ok" ? "Steady — work the oscillations." : "Building/shifting — favor the developing side and watch the gear.",
        clears: st === "ok" ? "—" : "the trend settles",
        based: [App.seriesHist && App.seriesHist.length ? "engine archive (" + App.seriesHist.length + " pts) + live buffer" : "live wind buffer (" + App.windHist.length + " samples)"], conf: "engine" };
    },
    tactics(p) {
      const t = p.tactics;
      if (!t || !t.available) return NA(t && t.note ? t.note : "no tactics");
      const side = t.favored_side;
      const value = side === "left" ? "◀ Left" : side === "right" ? "Right ▶" : "Even";
      const persistent = t.shift && t.shift.oscillation_deg != null && t.shift.shift_deg != null && Math.abs(t.shift.shift_deg) > t.shift.oscillation_deg;
      const st = side && side !== "either" && persistent ? "watch" : "ok";
      return { status: st, value: value, sub: (t.phase || "") + (persistent ? ", persistent" : ", oscillating"),
        why: t.recommendation || (t.phase + ", favored " + side), consider: t.favored_reason || "Sail your phase.",
        clears: st === "ok" ? "—" : "the shift reverses", based: ["get_tactics: " + (t.phase || "?") + ", favored " + (side || "?")], conf: "engine" };
    },
    forecast(p) {
      const fc = p.forecast;
      if (!fc || !fc.available || !fc.hours || !fc.hours.length) return NA("no forecast");
      const obs = observedAt(0) || (p.conditions && p.conditions.available ? { tws: p.conditions.tws, twd: p.conditions.twd } : null);
      const aug = obs ? [{ in_h: 0, tws: obs.tws, twd: obs.twd }].concat(fc.hours) : fc.hours.slice();
      const f60 = fcstAt(aug, 1.0), f120 = fcstAt(aug, 2.0);
      const v60 = fcstVsActual(3600), v120 = fcstVsActual(7200);
      const fwd = (s) => (s ? arrowKts(s.twd, s.tws) : "—");
      const rows = [
        { label: "Now", emph: true, cols: [obs ? arrowKts(obs.twd, obs.tws) : "—"] },
        { label: "+60 min", cols: [fwd(f60)] },
        { label: "+120 min", cols: [fwd(f120)] },
        { sep: true, label: "FORECAST VS. ACTUAL" },
        { hdr: true, cols: ["forecast", "actual"] },
        { label: "−60 min", cols: v60 ? [fwd(v60.fcst), fwd(v60.actual)] : ["—", "accumulating"] },
        { label: "−120 min", cols: v120 ? [fwd(v120.fcst), fwd(v120.actual)] : ["—", "accumulating"] },
      ];
      const bigChange = obs && (Math.abs(f120.tws - obs.tws) >= 6 || Math.abs(angDiff(f120.twd, obs.twd)) >= 20);
      const badSkill = (v60 && (Math.abs(v60.actual.tws - v60.fcst.tws) >= 4 || Math.abs(angDiff(v60.actual.twd, v60.fcst.twd)) >= 15)) ||
        (v120 && (Math.abs(v120.actual.tws - v120.fcst.tws) >= 5 || Math.abs(angDiff(v120.actual.twd, v120.fcst.twd)) >= 20));
      const st = bigChange || badSkill ? "watch" : "ok";
      return { status: st, value: null, rows: rows,
        why: "Forecast wind speed + direction (arrows show direction — a north wind points down). +60 min " + r0(f60.tws) + " kts, +120 min " + r0(f120.tws) + " kts. " +
          "\"Forecast vs. actual\" compares the earlier forecast for −60 / −120 min ago against what actually happened — a read on how well the model is verifying" +
          (v60 || v120 ? "." : " (fills in after the dashboard has run ~1-2 h)."),
        consider: badSkill ? "Forecast off lately — trust the live trend more." : bigChange ? "A notable change is forecast — plan the gear." : "Forecast steady and verifying.",
        clears: st === "ok" ? "—" : "forecast comes back in line", based: ["fetch_forecast + live wind buffer"], conf: "engine" };
    },
    sail(p) {
      const s = p.sail;
      if (!s || !s.available) return NA(s && s.note ? s.note : "no sail data");
      const xo = s.next_crossover;
      let st = "ok", value = s.optimal_sail || "—";
      if (s.wrong_sail) { st = "act"; value = (s.hoisted_sail || "?") + " → " + s.optimal_sail; }
      else if (xo && xo.deg_away <= 8) { st = "watch"; value = s.optimal_sail + " → " + xo.to_sail; }
      return { status: st, value: value,
        sub: s.wrong_sail ? "wrong sail up" : s.in_range ? "in range" : (xo ? xo.direction + " " + r0(xo.deg_away) + "° → " + xo.to_sail : ""),
        why: s.recommendation || "",
        consider: s.wrong_sail ? "Change to " + s.optimal_sail + "." : (xo && xo.deg_away <= 8 ? "Stage the " + xo.to_sail + "; crossover " + r0(xo.deg_away) + "° away." : "No change."),
        clears: st === "ok" ? "—" : "cross to " + (xo ? xo.to_sail : s.optimal_sail),
        based: ["get_sail: optimal " + s.optimal_sail + ", TWA " + r0(s.twa) + "°, TWS " + r0(s.tws_used) + " kts" + (xo ? ", crossover " + xo.to_sail + " " + r0(xo.deg_away) + "°" : "")], conf: "engine" };
    },
    eta(p) {
      const n = p.navigator;
      if (!n || !n.available || !n.next_mark) return NA(n && n.note ? n.note : "no active course");
      const m = n.next_mark, e = m.eta_min;
      if (e == null) return NA("ETA needs made-good to the mark");
      const st = e < 2 ? "act" : e < 5 ? "watch" : "ok";
      const txt = e >= 60 ? Math.floor(e / 60) + "h " + r0(e % 60) + " min" : r0(e) + " min";
      return { status: st, value: txt, sub: m.name,
        why: "~" + r0(e) + " min to " + m.name + " at the current made-good.",
        consider: e < 5 ? "Mark in ~" + r0(e) + " min — start the rounding prep." : "On schedule for the mark.",
        clears: st !== "ok" ? "past the rounding" : "—", based: ["get_navigator: ETA " + r0(e) + " min to " + m.name], conf: "engine" };
    },
    charge(p) {
      const f = p.fatigue;
      if (!f || !f.available || f.index == null) return NA(f && f.note ? f.note : "no helm data");
      const chg = Math.round(100 - f.index), lvl = (f.level || "").replace(/_/g, " ");
      const st = f.level === "fresh" ? "ok" : f.level === "watch" ? "watch" : "act";
      const o = { status: st, value: String(chg), sub: lvl,
        why: "Crew energy ~" + chg + "% (inverse of the fatigue index; lower = more depleted). Level: " + lvl + ".",
        consider: st === "ok" ? "Driver fresh — no rotation needed." : "Tank getting low — plan a helm rotation.",
        clears: st === "ok" ? "—" : "energy back above 65%",
        based: ["get_fatigue: index " + r0(f.index) + " → energy " + chg + "%, level " + (f.level || "?")], conf: "engine" };
      if (f.components) o.components = f.components;
      return o;
    },
    data(p) {
      const c = p.conditions, src = p.sources;
      const count = src && src.count != null ? src.count : 0;
      const anyStale = src && src.sources && src.sources.some((s) => s.last_seen_s > 45);
      const st = !c || !c.available ? "act" : (c.stale || anyStale) ? "watch" : "ok";
      return { status: st, value: String(count), sub: "source" + (count === 1 ? "" : "s") + " live",
        why: !c || !c.available ? "No live conditions from the engine." : count + " sensor groups reporting" + (anyStale ? ", one stale." : "."),
        consider: st === "ok" ? "Instruments healthy." : "Cross-check before trusting a lone reading.",
        clears: st === "ok" ? "—" : "all sources fresh", based: ["get_sources: " + count + " sources"], conf: "engine" };
    },
  };

  function buildLive(p) {
    const tiles = {};
    for (const k of TILES) {
      const raw = (BUILD[k] || (() => NA("—")))(p);
      raw.status = commitStatus(k, raw.status);
      tiles[k] = raw;
    }
    const flagged = TILES.filter((k) => tiles[k].status === "watch" || tiles[k].status === "act")
      .sort((a, b) => SEV[tiles[b].status] - SEV[tiles[a].status]);
    const reachable = p.conditions || p.navigator || p.sources;
    const notes = flagged.slice(0, 3).map((k) => ({ tile: k, status: tiles[k].status, text: tiles[k].consider || tiles[k].why, conf: "engine" }));
    const focus = !reachable ? "Engine unreachable — no live data." :
      flagged.length === 0 ? "All systems nominal (engine read)." :
      flagged.length === 1 ? "1 item needs attention (engine read)." :
      flagged.length + " items need attention (engine read).";
    return applyBrief({ tiles, focus, notes, confidence: "engine", mode: "engine read" });
  }
  /* overlay the LLM brief (commentary + grounded status nudges) when it's fresh; otherwise the
     deterministic engine-read commentary stands. The engine values are never touched. */
  function applyBrief(d) {
    const b = App.brief;
    if (!b || b.mode !== "llm" || Date.now() / 1000 - b.t > BRIEF_TTL) return d;
    for (const a of b.adjust || []) {
      const t = d.tiles[a.tile];
      if (t && t.status !== "na") { t.status = a.status; t.llmNote = a.reason; }
    }
    const notes = (b.notes || []).map((n) => ({ tile: n.tile, status: (d.tiles[n.tile] || {}).status || "ok", text: n.text, conf: n.conf || "med" }));
    return { tiles: d.tiles, focus: b.focus || d.focus, notes: notes.length ? notes : d.notes, confidence: "llm", mode: "llm-live" };
  }
  /* a compact text value for each tile to send to the copilot (rows flattened, arrows stripped) */
  function tileText(t) {
    if (t.value != null && t.value !== "") return stripTags(t.value);
    if (t.rows) return t.rows.filter((r) => !r.sep && !r.hdr).map((r) => r.label + " " + (r.cols || []).map(stripTags).join(" / ")).join("; ");
    return "";
  }
  /* ask the Orin copilot to write the commentary for the current tiles (POST the snapshot) */
  async function fetchBrief() {
    if (App.src !== "live" || !App.data || !App.data.tiles || App.briefing) return;
    App.briefing = true;
    const snap = TILES.map((k) => { const t = App.data.tiles[k] || {}; return { key: k, name: NAME[k], value: tileText(t), sub: t.sub || "", status: t.status || "na" }; });
    let r = null;
    try {
      const ctl = new AbortController();
      const to = setTimeout(() => ctl.abort(), 130000);
      const resp = await fetch(COPILOT + "/dashboard", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tiles: snap }), signal: ctl.signal });
      clearTimeout(to);
      r = resp.ok ? await resp.json() : null;
    } catch (e) { r = null; } finally { App.briefing = false; }
    if (r && r.mode === "llm") {
      App.brief = { focus: r.focus, notes: r.notes || [], adjust: r.adjust || [], mode: "llm", t: Date.now() / 1000 };
      if (App.src === "live") render();
    }
    // deterministic / unreachable → leave the last brief to expire; the dashboard stays engine-read
  }
  function commitStatus(key, raw) {
    const d = App.dwell[key] || (App.dwell[key] = { committed: raw, cand: raw, n: 0 });
    if (raw === d.committed) { d.cand = raw; d.n = 0; return d.committed; }
    if (raw === d.cand) d.n++; else { d.cand = raw; d.n = 1; }
    const improving = SEV[raw] < SEV[d.committed] && raw !== "na";
    if (d.n >= (improving ? 1 : 2)) { d.committed = raw; d.cand = raw; d.n = 0; }
    return d.committed;
  }

  /* ============================ state ============================ */
  const App = {
    src: "live", demoScn: "calm",
    theme: localStorage.getItem("sr33.dash.theme") || "auto",
    pos: { lat: 45.33, lon: -82.0 },
    openTile: null, streamTimer: null, pollTimer: null, seriesTimer: null, briefTimer: null, polling: false,
    dwell: {}, data: null, windHist: [], fcstHist: [], seriesHist: [], lastPersist: 0, brief: null,
  };
  function currentData() {
    if (App.src === "demo") {
      const sc = SCENARIOS[App.demoScn];
      return { tiles: sc.tiles, focus: sc.focus, notes: sc.notes, confidence: sc.confidence, mode: sc.mode };
    }
    return App.data || { tiles: {}, focus: "Connecting to the engine…", notes: [], confidence: "engine", mode: "engine read" };
  }

  /* ============================ theme ============================ */
  function resolveTheme() {
    if (App.theme !== "auto") return App.theme;
    const day = window.Sun ? Sun.isDaylight(App.pos.lat, App.pos.lon, new Date()) : true;
    return day ? "day" : "night";
  }
  function applyTheme() {
    const r = resolveTheme();
    document.documentElement.setAttribute("data-theme", r);
    document.getElementById("themeBtn").firstChild.textContent = r === "day" ? "☀ " : "☾ ";
    document.getElementById("themeLbl").textContent = App.theme === "auto" ? "AUTO·" + (r === "day" ? "☀" : "☾") : App.theme.toUpperCase();
  }
  function cycleTheme() {
    App.theme = { auto: "day", day: "night", night: "auto" }[App.theme];
    localStorage.setItem("sr33.dash.theme", App.theme); applyTheme();
  }

  /* ============================ render ============================ */
  function STATUS_PLACEHOLDER() { return { status: "na", value: "—", sub: "", why: "", consider: "—", clears: "—", based: [] }; }
  function render() {
    const d = currentData();
    const grid = document.getElementById("grid");
    grid.innerHTML = "";
    for (const key of TILES) {
      const t = d.tiles[key] || STATUS_PLACEHOLDER();
      const st = STATUS[t.status] || STATUS.na;
      const el = document.createElement("div");
      el.className = "tile s-" + (t.status || "na");
      el.dataset.tile = key;
      el.setAttribute("role", "button"); el.setAttribute("tabindex", "0");
      el.setAttribute("aria-label", NAME[key] + " " + st.word + " " + stripTags(t.value || ""));
      const valHtml = (t.value != null && t.value !== "") ? '<div class="t-val">' + t.value + '</div>' : "";
      el.innerHTML =
        '<div class="t-head"><span class="t-name">' + NAME[key] + '</span>' +
        '<span class="t-chip"><span class="t-icon">' + st.icon + '</span><span class="t-word">' + st.word + '</span></span></div>' +
        valHtml + (t.chart ? t.chart : "") + (t.sub ? '<div class="t-sub">' + t.sub + '</div>' : "") + rowsHtml(t.rows);
      el.addEventListener("click", () => openDetail(key));
      el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail(key); } });
      grid.appendChild(el);
    }
    renderCommentary(d);
    if (App.openTile && !document.getElementById("detail").hidden) populateDetail(App.openTile, false);
  }
  function renderCommentary(d) {
    document.getElementById("commFocus").textContent = d.focus || "—";
    document.getElementById("commConf").textContent = "conf: " + (d.confidence || "—");
    const pill = document.getElementById("modePill");
    pill.dataset.mode = d.mode === "llm-live" ? "llm" : "engine";
    document.getElementById("modeLbl").textContent = d.mode || "engine read";
    const ul = document.getElementById("commNotes");
    ul.innerHTML = "";
    if (!d.notes || !d.notes.length) {
      const li = document.createElement("li"); li.className = "tg-ok";
      li.innerHTML = '<span class="note-txt">Nothing flagged — tap any tile for detail.</span>';
      ul.appendChild(li); return;
    }
    for (const n of d.notes) {
      const li = document.createElement("li");
      li.className = "tg-" + n.status;
      li.innerHTML = '<span class="note-txt">' + n.text + '</span>' +
        '<span class="note-meta"><span class="note-tag">' + NAME[n.tile] + '</span><span class="note-conf">conf: ' + n.conf + '</span></span>';
      li.addEventListener("click", () => flashTile(n.tile));
      ul.appendChild(li);
    }
  }
  function flashTile(key) {
    const el = document.querySelector('.tile[data-tile="' + key + '"]');
    if (!el) return;
    el.classList.remove("ring"); void el.offsetWidth; el.classList.add("ring");
    setTimeout(() => el.classList.remove("ring"), 1600);
  }

  /* ============================ detail slide-over ============================ */
  function openDetail(key) {
    App.openTile = key;
    document.getElementById("overlay").hidden = false;
    document.getElementById("detail").hidden = false;
    if (key === "wind" && App.src === "live") fetchSeries().then(() => { if (App.openTile === "wind") populateDetail("wind", false); });
    populateDetail(key, true);
  }
  function populateDetail(key, stream) {
    const t = currentData().tiles[key] || STATUS_PLACEHOLDER();
    const st = STATUS[t.status] || STATUS.na;
    document.getElementById("detName").textContent = NAME[key];
    const ds = document.getElementById("detStatus");
    ds.innerHTML = '<span class="t-icon">' + st.icon + '</span> ' + st.word + ' · conf: ' + (t.conf || "—");
    ds.style.color = "var(--" + (t.status === "na" ? "na" : t.status) + ")";
    const g = document.getElementById("detGauge");
    const stColor = "var(--" + (t.status === "na" ? "na" : t.status) + ")";
    if (key === "wind" && t.status !== "na") {
      const hist = dsample(App.src === "live" ? raceData() : genDemoRace(App.demoScn === "escalated"), 600);
      const tws = hist.map((p) => p.tws);
      const dur = hist.length ? hist[hist.length - 1].t - hist[0].t : 0;
      const stats = tws.length
        ? '<div class="rc-stats">now <b>' + r0(tws[tws.length - 1]) + ' kts</b> · min <b>' + r0(Math.min.apply(null, tws)) + '</b> · max <b>' + r0(Math.max.apply(null, tws)) + '</b> · avg <b>' + r0(tws.reduce((a, b) => a + b, 0) / tws.length) + '</b></div>'
        : "";
      g.innerHTML =
        '<div class="rc-title">TWS over the race · ' + fmtDur(dur) + '</div>' + stats +
        '<div style="--c:' + stColor + '">' + raceChart(hist, 512, 150) + '</div>' +
        '<div class="rc-title" style="margin-top:8px">Direction change over the race</div>' +
        '<div class="detchart" style="--c:' + stColor + '"><div class="dc-lab dc-top">↑ shifted right</div>' +
        (makeTwdSpark(hist, 512, 60) || '<span class="dc-empty">…</span>') +
        '<div class="dc-lab dc-bot">↓ shifted left</div></div>' + rowsHtml(t.rows);
    } else if (t.rows) {
      g.innerHTML = (t.value ? '<div class="dc-foot">' + t.value + (t.sub ? " · " + t.sub : "") + '</div>' : "") + rowsHtml(t.rows);
    } else if (t.components) {
      let bars = '<div class="bars">';
      for (const lbl of Object.keys(t.components)) {
        bars += '<div class="bar"><span class="barlbl">' + lbl + '</span><span class="bartrk"><span class="barfil" style="width:' + Math.round(t.components[lbl] * 100) + '%"></span></span></div>';
      }
      g.innerHTML = (t.value || "—") + " · " + (t.sub || "") + bars + "</div>";
    } else {
      g.textContent = stripTags(t.value || "—") + "  ·  " + (t.sub || "");
    }
    document.getElementById("detConsider").textContent = (t.llmNote ? "Copilot: " + t.llmNote + "  ·  " : "") + (t.consider || "—");
    document.getElementById("detClears").textContent = t.clears || "—";
    document.getElementById("detBased").textContent = (t.based || []).join("   ·   ") || "—";
    const why = t.why || "—";
    if (stream && App.src === "demo") streamWhy(why);
    else { if (App.streamTimer) { clearInterval(App.streamTimer); App.streamTimer = null; } document.getElementById("detWhy").textContent = why; }
  }
  function closeDetail() {
    if (App.streamTimer) { clearInterval(App.streamTimer); App.streamTimer = null; }
    document.getElementById("detail").hidden = true;
    document.getElementById("overlay").hidden = true;
    App.openTile = null;
  }
  function streamWhy(text) {
    const el = document.getElementById("detWhy");
    if (App.streamTimer) clearInterval(App.streamTimer);
    const words = text.split(" "); let i = 0;
    el.innerHTML = '<span class="caret"></span>';
    App.streamTimer = setInterval(() => {
      i++;
      el.innerHTML = words.slice(0, i).join(" ") + (i < words.length ? ' <span class="caret"></span>' : "");
      if (i >= words.length) { clearInterval(App.streamTimer); App.streamTimer = null; }
    }, 45);
  }

  /* ============================ live polling ============================ */
  async function poll() {
    if (App.polling) return;
    App.polling = true;
    try {
      const eps = ["/conditions", "/sail", "/navigator", "/tactics", "/fatigue", "/forecast?hours=6", "/sources"];
      const keys = ["conditions", "sail", "navigator", "tactics", "fatigue", "forecast", "sources"];
      const ms   = [5000, 5000, 5000, 5000, 5000, 9000, 5000];
      const res = await Promise.all(eps.map((e, i) => fetchJSON(e, ms[i])));
      const p = {}; keys.forEach((k, i) => (p[k] = res[i]));
      pushWind(p.conditions);
      pushForecast(p.forecast);
      App.data = buildLive(p);
      if (App.src === "live") render();
    } finally { App.polling = false; }
  }
  function startPolling() { poll(); if (!App.pollTimer) App.pollTimer = setInterval(poll, 3000); }

  /* ============================ controls ============================ */
  function cycleSource() {
    if (App.src === "live") { App.src = "demo"; App.demoScn = "calm"; }
    else if (App.demoScn === "calm") { App.demoScn = "escalated"; }
    else { App.src = "live"; }
    document.getElementById("srcLbl").textContent = App.src === "live" ? "LIVE" : "DEMO·" + App.demoScn;
    if (!document.getElementById("detail").hidden) closeDetail();
    render();
  }
  function briefMe() {
    const b = document.getElementById("briefBtn");
    b.classList.add("busy"); b.textContent = "thinking…";
    if (App.src === "live") { poll(); fetchBrief(); }
    // the LLM is slow (~45 s warm); clear the busy state on a timer — render() updates when it lands
    setTimeout(() => { b.classList.remove("busy"); b.textContent = "Brief me ↻"; if (App.src !== "live") render(); }, 1500);
  }

  /* ============================ boot ============================ */
  function init() {
    applyTheme();
    loadRace();        // restore the race wind history across a reload
    document.getElementById("srcLbl").textContent = "LIVE";
    render();
    startPolling();
    fetchSeries();     // pull the archived race series from the engine, then refresh periodically
    App.seriesTimer = setInterval(fetchSeries, 30000);
    setTimeout(fetchBrief, 4000);   // first LLM commentary once tiles exist, then on a cadence
    App.briefTimer = setInterval(fetchBrief, BRIEF_EVERY);
    document.getElementById("themeBtn").addEventListener("click", cycleTheme);
    document.getElementById("srcBtn").addEventListener("click", cycleSource);
    document.getElementById("briefBtn").addEventListener("click", briefMe);
    document.getElementById("detBack").addEventListener("click", closeDetail);
    document.getElementById("overlay").addEventListener("click", closeDetail);
    document.getElementById("detSend").addEventListener("click", () => {
      const inp = document.getElementById("detAsk");
      if (inp.value.trim()) { document.getElementById("detWhy").textContent = "(scoped LLM follow-up lands in phase 4) — you asked: " + inp.value.trim(); inp.value = ""; }
    });
    setInterval(() => { if (App.theme === "auto") applyTheme(); }, 25000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
