/* SR33 Crew Dashboard — higher-order tiles (live onboard engine + deterministic status).
   Design: docs/COPILOT_DASHBOARD.md. The grid does NOT repeat raw instrument numbers that
   already live on other boat displays; it surfaces the higher-order reads the sensors alone
   don't show:
     VMG · Wind Trend · Tactics · Forecast · Sail · Time to Mark · Crew Energy · Data
   Velocities always carry units (kts); names are spelled out (no cryptic abbreviations); the
   wind-direction change is drawn as a sparkline rather than described with jargon like "veer".
   The engine owns the truth (deterministic status, works LLM-off). Wind Trend is computed
   client-side from the live poll stream. LLM status refinement + commentary + streamed
   deep-dives arrive in phases 3-4. */
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
    vmg: "VMG", wind: "Wind Trend", tactics: "Tactics", forecast: "Forecast",
    sail: "Sail", eta: "Time to Mark", charge: "Crew Energy", data: "Data",
  };

  const D2R = Math.PI / 180, R2D = 180 / Math.PI;
  const WIND_WIN_S = 12 * 60;

  /* ============================ DEMO scenarios (canned) ============================ */
  const SPARK_OSC = '<svg class="spark" viewBox="0 0 160 34" width="100%" height="34" preserveAspectRatio="none"><line class="spark-mid" x1="0" y1="17" x2="160" y2="17"/><polyline class="spark-line" points="3,17 22,9 44,24 66,11 90,23 112,10 134,22 157,15"/></svg>';
  const SPARK_RIGHT = '<svg class="spark" viewBox="0 0 160 34" width="100%" height="34" preserveAspectRatio="none"><line class="spark-mid" x1="0" y1="17" x2="160" y2="17"/><polyline class="spark-line" points="3,26 40,22 80,17 120,11 157,5"/></svg>';
  const SPARK_OSC_BIG = '<svg class="spark" viewBox="0 0 520 120" width="100%" height="120" preserveAspectRatio="none"><line class="spark-mid" x1="0" y1="60" x2="520" y2="60"/><polyline class="spark-line" points="6,60 70,30 150,86 230,36 310,84 390,32 460,80 514,58"/></svg>';
  const SPARK_RIGHT_BIG = '<svg class="spark" viewBox="0 0 520 120" width="100%" height="120" preserveAspectRatio="none"><line class="spark-mid" x1="0" y1="60" x2="520" y2="60"/><polyline class="spark-line" points="6,92 130,74 260,54 390,34 514,16"/></svg>';

  const SCENARIOS = {
    calm: {
      mode: "llm-live", focus: "Solid groove — the left side is paying.", confidence: "high",
      notes: [
        { tile: "tactics", status: "ok", text: "Left has paid the last two oscillations — stay set up to tack on the next header.", conf: "high" },
        { tile: "vmg",     status: "ok", text: "VMG at 96% of target and pointing well. Hold the groove.", conf: "high" },
      ],
      tiles: {
        vmg:     { status: "ok", value: "5.4 kts", sub: "upwind · 96% of target", why: "VMG 5.4 kts to windward vs a 5.6 kts polar target — 96%.", consider: "Good VMG — hold the groove.", clears: "—", based: ["computed VMG = STW·cos(TWA)", "get_sail: target VMG 5.6 kts"], conf: "high" },
        wind:    { status: "ok", value: "→ 12 kts", sub: "oscillating ±5°", chart: SPARK_OSC, chartBig: SPARK_OSC_BIG, why: "Over ~10 min: wind speed steady ~12 kts. Direction oscillating about ±5° (chart shows the angle change over time).", consider: "Oscillating breeze — work the shifts.", clears: "—", based: ["live wind-direction trend over 10 min"], conf: "high" },
        tactics: { status: "ok", value: "◀ Left", sub: "oscillating, favor left", why: "Oscillating; the left has paid. Lifted now.", consider: "Tack on the next header.", clears: "—", based: ["get_tactics: favored left, lifted"], conf: "high" },
        forecast:{ status: "ok", value: "16 kts", sub: "↗ building · +3h", why: "Models build to ~16 kts over the next few hours.", consider: "Plan the gear for the build.", clears: "—", based: ["fetch_forecast: 16 kts in +3h"], conf: "high" },
        sail:    { status: "ok", value: "J1", sub: "in range", why: "J1 is right for 12 kts upwind.", consider: "No change.", clears: "TWS > 16 kts", based: ["get_sail: optimal J1"], conf: "high" },
        eta:     { status: "ok", value: "16 min", sub: "Cove Island", why: "~16 min to Cove Island at the current made-good.", consider: "On schedule for the mark.", clears: "—", based: ["get_navigator: ETA 16 min to Cove Island"], conf: "high" },
        charge:  { status: "ok", value: "72", sub: "fresh", why: "Crew energy ~72% (inverse of the fatigue index; lower = more depleted).", consider: "Driver fresh — no rotation needed.", clears: "—", based: ["get_fatigue: index 28 → energy 72%"], conf: "high" },
        data:    { status: "ok", value: "5", sub: "sources live", why: "All five sensor groups fresh.", consider: "Instruments healthy.", clears: "—", based: ["get_sources: 5 live"], conf: "high" },
      },
    },
    escalated: {
      mode: "llm-live", focus: "Bear-away coming up, and the crew tank is getting low.", confidence: "med",
      notes: [
        { tile: "sail",   status: "act",   text: "Peel J1 → A3 before the bear-away at the gate — start staging now (~4 min out).", conf: "high" },
        { tile: "charge", status: "act",   text: "Crew energy down to 28% (rotate soon) — plan a driver change in the next few minutes.", conf: "med" },
        { tile: "eta",    status: "watch", text: "Cove Island in ~4 min — begin the rounding prep.", conf: "high" },
      ],
      tiles: {
        vmg:     { status: "watch", value: "4.6 kts", sub: "upwind · 82% of target", why: "VMG 4.6 kts vs a 5.6 kts target — 82%. Pinching in the chop.", consider: "Down on VMG — ease the angle to rebuild made-good.", clears: "back over 90% of the VMG target", based: ["computed VMG = STW·cos(TWA)", "get_sail: target VMG 5.6 kts"], conf: "med" },
        wind:    { status: "watch", value: "↗ 16 kts", sub: "shifting right ~12°", chart: SPARK_RIGHT, chartBig: SPARK_RIGHT_BIG, why: "Over ~10 min: wind speed building ~4 kts per 10 min. Direction shifting right ~12° and holding — a persistent shift (chart shows the angle change over time).", consider: "Persistent right shift — favor the right side of the course.", clears: "the trend settles", based: ["live wind-direction trend over 10 min"], conf: "med" },
        tactics: { status: "watch", value: "Right ▶", sub: "persistent, favor right", why: "The breeze has shifted right and is holding — persistent, not oscillating.", consider: "Favor the right.", clears: "the shift reverses", based: ["get_tactics: favored right, persistent"], conf: "med" },
        forecast:{ status: "ok",    value: "17 kts", sub: "→ holding · +1h", why: "Models hold 16-18 kts over the next hour.", consider: "A3-leg conditions confirmed.", clears: "—", based: ["fetch_forecast: 17 kts next hour"], conf: "high" },
        sail:    { status: "act",   value: "J1 → A3", sub: "peel before bear-away", why: "The leg after the gate bears away to ~135° TWA — an A3 leg. Peel before the rounding.", consider: "Stage the A3 and peel in ~4 min.", clears: "A3 hoisted", based: ["get_sail: A3 for TWA 135°"], conf: "high" },
        eta:     { status: "watch", value: "4 min", sub: "Cove Island", why: "~4 min to Cove Island at the current made-good.", consider: "Mark in ~4 min — start the rounding prep.", clears: "past the rounding", based: ["get_navigator: ETA 4 min to Cove Island"], conf: "high" },
        charge:  { status: "act",   value: "28", sub: "rotate soon", why: "Crew energy ~28% (rotate soon). Heading instability and steering reversals are up, speed deficit creeping.", consider: "Tank getting low — plan a helm rotation.", clears: "energy back above 65%", based: ["get_fatigue: index 72 → energy 28%"], conf: "med", components: { heading: 0.7, reversals: 0.8, heel: 0.4, "spd-def": 0.5 } },
        data:    { status: "watch", value: "4", sub: "1 stale", why: "Masthead wind stale ~50 s ago; running on the Orca backup.", consider: "Running on backup wind — watch for it to return.", clears: "all sources fresh", based: ["get_sources: 4 live, 1 stale"], conf: "med" },
      },
    },
  };

  /* ============================ LIVE engine mapping ============================ */
  const API = "/api";
  const r0 = (x) => (x == null ? "?" : Math.round(x));
  const r1 = (x) => (x == null ? "?" : Math.round(x * 10) / 10);
  const NA = (note) => ({ status: "na", value: "—", sub: note || "no data", why: note || "No data from the engine.",
    consider: "—", clears: "—", based: [], conf: "engine" });
  const shorten = (s) => (s ? s.split(/—/)[0].trim().split(/\s+/).slice(0, 2).join(" ") : "");
  const spanTxt = (mins) => (mins < 1 ? r0(mins * 60) + " s" : r0(mins) + " min");
  const stripTags = (s) => String(s).replace(/<[^>]+>/g, "");

  function fetchJSON(path, ms) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms || 5000);
    return fetch(API + path, { signal: ctl.signal, headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null)).catch(() => null).finally(() => clearTimeout(t));
  }

  /* ---- client-side wind-trend buffer + sparkline of the direction change ---- */
  function pushWind(c) {
    if (!c || !c.available || c.tws == null || c.twd == null) return;
    const now = Date.now() / 1000;
    App.windHist.push({ t: now, tws: c.tws, twd: c.twd });
    const cut = now - WIND_WIN_S;
    while (App.windHist.length && App.windHist[0].t < cut) App.windHist.shift();
  }
  function circMeanDeg(degs) {
    const s = degs.reduce((a, d) => a + Math.sin(d * D2R), 0) / degs.length;
    const c = degs.reduce((a, d) => a + Math.cos(d * D2R), 0) / degs.length;
    return (Math.atan2(s, c) * R2D + 360) % 360;
  }
  function windTrend() {
    const h = App.windHist;
    if (h.length < 6) return null;
    const t0 = h[0].t, xs = h.map((p) => p.t - t0), ys = h.map((p) => p.tws), n = xs.length;
    const sx = xs.reduce((a, b) => a + b, 0), sy = ys.reduce((a, b) => a + b, 0);
    const sxx = xs.reduce((a, b) => a + b * b, 0), sxy = xs.reduce((a, x, i) => a + x * ys[i], 0);
    const slope = (n * sxy - sx * sy) / ((n * sxx - sx * sx) || 1);
    const sRad = h.map((p) => p.twd * D2R);
    const mS = sRad.reduce((a, r) => a + Math.sin(r), 0) / n, mC = sRad.reduce((a, r) => a + Math.cos(r), 0) / n;
    const R = Math.hypot(mS, mC);
    const osc = R > 0 ? Math.sqrt(Math.max(0, -2 * Math.log(R))) * R2D : 0;
    const k = Math.max(1, Math.floor(n / 3));
    const drift = (((circMeanDeg(h.slice(-k).map((p) => p.twd)) - circMeanDeg(h.slice(0, k).map((p) => p.twd))) + 540) % 360) - 180;
    return { twsNow: h[n - 1].tws, ratePer10: slope * 600, osc, drift, mins: (h[n - 1].t - t0) / 60, samples: n };
  }
  /* SVG polyline of TWD deviation from the window mean (right shift = up, left = down). */
  function makeTwdSpark(hist, w, h) {
    if (!hist || hist.length < 3) return "";
    const mean = circMeanDeg(hist.map((p) => p.twd));
    const devs = hist.map((p) => ((p.twd - mean + 540) % 360) - 180);
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

  const BUILD = {
    vmg(p) {
      const c = p.conditions, s = p.sail;
      if (!c || !c.available || c.stw == null || c.twa == null) return NA("no speed / angle");
      const twa = Math.abs(c.twa), vmg = c.stw * Math.cos(twa * D2R), absv = Math.abs(vmg);
      if (!(twa < 70 || twa > 110)) {
        return { status: "ok", value: r1(absv) + " kts", sub: "reaching",
          why: "VMG " + r1(absv) + " kts — on a reach, VMG-to-wind isn't the target (sail for the mark).",
          consider: "Reaching — sail fast, not for VMG.", clears: "—",
          based: ["computed VMG = STW·cos(TWA " + r0(twa) + "°)"], conf: "engine" };
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
      const wt = windTrend();
      if (!wt) return NA("building wind history…");
      const up = wt.ratePer10 > 1.2, down = wt.ratePer10 < -1.2;
      const arrow = up ? "↗" : down ? "↘" : "→";
      const persistent = Math.abs(wt.drift) > Math.max(8, wt.osc);
      const sub = persistent ? "shifting " + (wt.drift > 0 ? "right" : "left") + " ~" + r0(Math.abs(wt.drift)) + "°" : "oscillating ±" + r0(wt.osc) + "°";
      const st = Math.abs(wt.ratePer10) >= 8 || (persistent && Math.abs(wt.drift) >= 15) ? "watch" : "ok";
      return { status: st, value: arrow + " " + r0(wt.twsNow) + " kts", sub: sub,
        chart: makeTwdSpark(App.windHist, 160, 34),
        why: "Over ~" + spanTxt(wt.mins) + ": wind speed " + (up ? "building" : down ? "easing" : "steady") + " ~" + r1(Math.abs(wt.ratePer10)) + " kts per 10 min. Direction " +
          (persistent ? "shifting " + (wt.drift > 0 ? "right" : "left") + " ~" + r0(Math.abs(wt.drift)) + "° and holding" : "oscillating about ±" + r0(wt.osc) + "°") + " (chart shows the angle change over time).",
        consider: persistent ? "Persistent " + (wt.drift > 0 ? "right" : "left") + " shift — favor that side of the course." : "Oscillating breeze — work the shifts, tack on the headers.",
        clears: st === "ok" ? "—" : "the trend settles",
        based: ["live wind-direction trend over " + spanTxt(wt.mins) + " (" + wt.samples + " samples)"], conf: "engine" };
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
      const h = fc.hours, now = h[0].tws, end = h[h.length - 1], later = end.tws;
      const up = later > now + 2, down = later < now - 2;
      const arrow = up ? "↗" : down ? "↘" : "→", word = up ? "building" : down ? "easing" : "holding";
      return { status: "ok", value: later + " kts", sub: arrow + " " + word + " · +" + r0(end.in_h) + "h",
        why: fc.source + ": wind " + now + " → " + later + " kts over the next " + r0(end.in_h) + " hours.",
        consider: up ? "Breeze building — plan the gear." : "Conditions holding.", clears: "—",
        based: ["fetch_forecast: " + now + " kts now → " + later + " kts in +" + r0(end.in_h) + "h"], conf: "engine" };
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
    return { tiles, focus, notes, confidence: "engine", mode: "engine read" };
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
    openTile: null, streamTimer: null, pollTimer: null, polling: false,
    dwell: {}, data: null, windHist: [],
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
      el.innerHTML =
        '<div class="t-head"><span class="t-name">' + NAME[key] + '</span>' +
        '<span class="t-chip"><span class="t-icon">' + st.icon + '</span><span class="t-word">' + st.word + '</span></span></div>' +
        '<div class="t-val">' + (t.value != null ? t.value : "—") + '</div>' +
        (t.chart ? t.chart : "") +
        '<div class="t-sub">' + (t.sub || "") + '</div>';
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
      const big = App.src === "live" ? makeTwdSpark(App.windHist, 520, 120) : (t.chartBig || t.chart || "");
      g.innerHTML = '<div class="detchart" style="--c:' + stColor + '">' +
        '<div class="dc-lab dc-top">↑ shifted right</div>' +
        (big || '<span class="dc-empty">building chart…</span>') +
        '<div class="dc-lab dc-bot">↓ shifted left</div>' +
        '<div class="dc-foot">' + (t.value || "") + " · " + (t.sub || "") + '</div></div>';
    } else if (t.components) {
      let bars = '<div class="bars">';
      for (const lbl of Object.keys(t.components)) {
        bars += '<div class="bar"><span class="barlbl">' + lbl + '</span><span class="bartrk"><span class="barfil" style="width:' + Math.round(t.components[lbl] * 100) + '%"></span></span></div>';
      }
      g.innerHTML = (t.value || "—") + " · " + (t.sub || "") + bars + "</div>";
    } else {
      g.textContent = stripTags(t.value || "—") + "  ·  " + (t.sub || "");
    }
    document.getElementById("detConsider").textContent = t.consider || "—";
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
    b.classList.add("busy"); b.textContent = "…";
    setTimeout(() => { b.classList.remove("busy"); b.textContent = "Brief me ↻"; if (App.src === "live") poll(); else render(); }, 700);
  }

  /* ============================ boot ============================ */
  function init() {
    applyTheme();
    document.getElementById("srcLbl").textContent = "LIVE";
    render();
    startPolling();
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
