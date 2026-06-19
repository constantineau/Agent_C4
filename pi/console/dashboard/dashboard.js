/* SR33 Crew Dashboard — higher-order tiles (live onboard engine + deterministic status).
   Design: docs/COPILOT_DASHBOARD.md. Per the crew's direction, the grid deliberately does NOT
   repeat raw instrument numbers that already live on other boat displays (wind/speed/depth/heel/
   nav/laylines). It surfaces the HIGHER-ORDER reads the sensors alone don't show:
     VMG · WIND Δ (TWS+direction trend over time) · TACT · FCAST · SAIL · T-MARK · CHARGE · DATA
   The engine owns the truth (deterministic status, works LLM-off). WIND Δ is computed client-side
   from the live poll stream (a rolling buffer) so trends work without the archive. LLM status
   refinement + commentary + streamed deep-dives arrive in phases 3-4. */
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
    vmg: "VMG", wind: "WIND Δ", tactics: "TACT", forecast: "FCAST",
    sail: "SAIL", eta: "T-MARK", charge: "CHARGE", data: "DATA",
  };

  const D2R = Math.PI / 180, R2D = 180 / Math.PI;
  const WIND_WIN_S = 12 * 60;   // rolling wind-trend window

  /* ============================ DEMO scenarios (canned) ============================ */
  const SCENARIOS = {
    calm: {
      mode: "llm-live", focus: "Solid groove — the left phase is paying.", confidence: "high",
      notes: [
        { tile: "tactics", status: "ok", text: "Left has paid the last two oscillations — stay set up to tack on the next header.", conf: "high" },
        { tile: "vmg",     status: "ok", text: "VMG at 96% of target and pointing well. Hold the groove.", conf: "high" },
      ],
      tiles: {
        vmg:     { status: "ok", value: "5.4 ↑", sub: "96% VMG tgt", why: "VMG 5.4 kn to windward vs a 5.6 target — 96%.", consider: "Good VMG — hold the groove.", clears: "—", based: ["computed VMG = STW·cos(TWA)", "get_sail: target VMG 5.6kn"], conf: "high" },
        wind:    { status: "ok", value: "→12", sub: "osc ±5°", why: "Over ~10 min: TWS steady ~12 kn; direction oscillating ±5°.", consider: "Oscillating — work the shifts.", clears: "—", based: ["live TWS/TWD trend over 10min"], conf: "high" },
        tactics: { status: "ok", value: "◀ L", sub: "osc, favor left", why: "Oscillating; left has paid. Lifted now.", consider: "Tack on the next header.", clears: "—", based: ["get_tactics: favored left, lifted"], conf: "high" },
        forecast:{ status: "ok", value: "↗16", sub: "kn by 15:00", why: "Models build to ~16 kn by 15:00.", consider: "Plan the gear for the build.", clears: "—", based: ["fetch_forecast: 16kn @15:00"], conf: "high" },
        sail:    { status: "ok", value: "J1", sub: "in range", why: "J1 is right for 12 kn upwind.", consider: "No change.", clears: "TWS > 16 kn", based: ["get_sail: optimal J1"], conf: "high" },
        eta:     { status: "ok", value: "16m", sub: "Cove Is.", why: "~16 min to Cove Island at the current made-good.", consider: "On schedule for the mark.", clears: "—", based: ["get_navigator: ETA 16min to Cove Island"], conf: "high" },
        charge:  { status: "ok", value: "72", sub: "fresh helm", why: "Helm energy ~72% (inverse fatigue; lower = more depleted).", consider: "Driver fresh — no rotation needed.", clears: "—", based: ["get_fatigue: index 28 → charge 72%"], conf: "high" },
        data:    { status: "ok", value: "5", sub: "sources live", why: "All five sensor groups fresh.", consider: "Instruments healthy.", clears: "—", based: ["get_sources: 5 live"], conf: "high" },
      },
    },
    escalated: {
      mode: "llm-live", focus: "Bear-away coming up, and the helm tank is getting low.", confidence: "med",
      notes: [
        { tile: "sail",   status: "act",   text: "PEEL J1→A3 before the bear-away at the gate — start staging now (~4 min out).", conf: "high" },
        { tile: "charge", status: "act",   text: "Helm energy down to 28% (rotate soon) — plan a driver change in the next few minutes.", conf: "med" },
        { tile: "eta",    status: "watch", text: "Cove Island in ~4 min — begin the rounding prep.", conf: "high" },
      ],
      tiles: {
        vmg:     { status: "watch", value: "4.6 ↑", sub: "82% VMG tgt", why: "VMG 4.6 kn vs a 5.6 target — 82%. Pinching in the chop.", consider: "Down on VMG — ease the angle to rebuild made-good.", clears: "back over 90% of VMG target", based: ["computed VMG = STW·cos(TWA)", "get_sail: target VMG 5.6kn"], conf: "med" },
        wind:    { status: "watch", value: "↗16", sub: "veer 12°", why: "Over ~10 min: TWS building ~4 kn/10min; direction veering right ~12° — looks persistent.", consider: "Persistent right shift — favor the right side.", clears: "trend settles", based: ["live TWS/TWD trend over 10min"], conf: "med" },
        tactics: { status: "watch", value: "▶ R", sub: "persistent, favor R", why: "Breeze veered right and is holding — persistent, not oscillating.", consider: "Favor the right.", clears: "shift reverses", based: ["get_tactics: favored right, persistent"], conf: "med" },
        forecast:{ status: "ok",    value: "↗17", sub: "kn, holding", why: "Models hold 16-18 kn next hour.", consider: "A3-leg conditions confirmed.", clears: "—", based: ["fetch_forecast: 17kn next hour"], conf: "high" },
        sail:    { status: "act",   value: "J1→A3", sub: "peel before bear-away", why: "The leg after the gate bears away to ~135° TWA — an A3 leg. Peel before the rounding.", consider: "Stage the A3 and peel in ~4 min.", clears: "A3 hoisted", based: ["get_sail: A3 for TWA 135°"], conf: "high" },
        eta:     { status: "watch", value: "4m", sub: "Cove Is.", why: "~4 min to Cove Island at the current made-good.", consider: "Mark in ~4 min — start the rounding prep.", clears: "past the rounding", based: ["get_navigator: ETA 4min to Cove Island"], conf: "high" },
        charge:  { status: "act",   value: "28", sub: "rotate soon", why: "Helm energy ~28% (rotate soon). Heading instability + reversals up, speed deficit creeping.", consider: "Tank getting low — plan a helm rotation.", clears: "energy back above 65%", based: ["get_fatigue: index 72 → charge 28%"], conf: "med", components: { heading: 0.7, reversals: 0.8, heel: 0.4, "spd-def": 0.5 } },
        data:    { status: "watch", value: "4", sub: "1 stale", why: "Masthead wind stale ~50 s ago; on the Orca backup.", consider: "Running on backup wind — watch for it to return.", clears: "all sources fresh", based: ["get_sources: 4 live, 1 stale"], conf: "med" },
      },
    },
  };

  /* ============================ LIVE engine mapping ============================ */
  const API = "/api";
  const r0 = (x) => (x == null ? "?" : Math.round(x));
  const r1 = (x) => (x == null ? "?" : Math.round(x * 10) / 10);
  const NA = (note) => ({ status: "na", value: "—", sub: note || "no data", why: note || "No data from the engine.",
    consider: "—", clears: "—", based: [], conf: "engine" });
  const shorten = (s) => (s ? s.split(/[\s—-]/)[0].slice(0, 9) : "");
  const spanTxt = (mins) => (mins < 1 ? r0(mins * 60) + "s" : r0(mins) + "min");

  function fetchJSON(path, ms) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms || 5000);
    return fetch(API + path, { signal: ctl.signal, headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null)).catch(() => null).finally(() => clearTimeout(t));
  }

  /* ---- client-side wind-trend buffer (TWS slope + TWD oscillation/drift) ---- */
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
    const slope = (n * sxy - sx * sy) / ((n * sxx - sx * sx) || 1);   // kn per second
    const sRad = h.map((p) => p.twd * D2R);
    const mS = sRad.reduce((a, r) => a + Math.sin(r), 0) / n, mC = sRad.reduce((a, r) => a + Math.cos(r), 0) / n;
    const R = Math.hypot(mS, mC);
    const osc = R > 0 ? Math.sqrt(Math.max(0, -2 * Math.log(R))) * R2D : 0;   // circular stdev, deg
    const k = Math.max(1, Math.floor(n / 3));
    const drift = (((circMeanDeg(h.slice(-k).map((p) => p.twd)) - circMeanDeg(h.slice(0, k).map((p) => p.twd))) + 540) % 360) - 180;
    return { twsNow: h[n - 1].tws, ratePer10: slope * 600, osc, drift, mins: (h[n - 1].t - t0) / 60, samples: n };
  }

  const BUILD = {
    vmg(p) {
      const c = p.conditions, s = p.sail;
      if (!c || !c.available || c.stw == null || c.twa == null) return NA("no speed / angle");
      const twa = Math.abs(c.twa), vmg = c.stw * Math.cos(twa * D2R), absv = Math.abs(vmg);
      if (!(twa < 70 || twa > 110)) {  // on a reach — VMG-to-wind isn't the objective
        return { status: "ok", value: r1(absv), sub: "reaching",
          why: "VMG " + r1(absv) + " kn — on a reach, VMG-to-wind isn't the target (sail for the mark / VMC).",
          consider: "Reaching — sail fast, not for VMG.", clears: "—",
          based: ["computed VMG = STW·cos(TWA " + r0(twa) + "°)"], conf: "engine" };
      }
      const tgt = s && s.available && s.targets ? Math.abs(s.targets.vmg) : null;
      const pct = tgt ? Math.round((absv / tgt) * 100) : null;
      const st = pct == null ? "ok" : pct >= 90 ? "ok" : pct >= 78 ? "watch" : "act";
      return { status: st, value: r1(absv) + (vmg >= 0 ? " ↑" : " ↓"),
        sub: pct != null ? pct + "% VMG tgt" : (vmg >= 0 ? "upwind" : "downwind"),
        why: "VMG " + r1(absv) + " kn " + (vmg >= 0 ? "to windward" : "downwind") + (tgt ? " vs a " + r1(tgt) + " kn polar target (" + pct + "%)." : "."),
        consider: st === "ok" ? "Good VMG — hold the groove." : "Down on VMG — adjust angle/trim for better made-good.",
        clears: st !== "ok" ? "back over 90% of VMG target" : "—",
        based: ["computed VMG = STW·cos(TWA)"].concat(tgt ? ["get_sail: target VMG " + r1(tgt) + "kn"] : []), conf: "engine" };
    },
    wind(p) {
      const wt = windTrend();
      if (!wt) return NA("building wind history…");
      const up = wt.ratePer10 > 1.2, down = wt.ratePer10 < -1.2;
      const arrow = up ? "↗" : down ? "↘" : "→";
      const persistent = Math.abs(wt.drift) > Math.max(8, wt.osc);
      const sub = persistent ? (wt.drift > 0 ? "veer " : "back ") + r0(Math.abs(wt.drift)) + "°" : "osc ±" + r0(wt.osc) + "°";
      const st = Math.abs(wt.ratePer10) >= 8 || (persistent && Math.abs(wt.drift) >= 15) ? "watch" : "ok";
      return { status: st, value: arrow + r0(wt.twsNow), sub: sub,
        why: "Over ~" + spanTxt(wt.mins) + ": TWS " + (up ? "building" : down ? "easing" : "steady") + " " + r1(Math.abs(wt.ratePer10)) + " kn/10min; direction " +
          (persistent ? (wt.drift > 0 ? "veering right" : "backing left") + " ~" + r0(Math.abs(wt.drift)) + "°" : "oscillating ±" + r0(wt.osc) + "°") + ".",
        consider: persistent ? "Persistent " + (wt.drift > 0 ? "right" : "left") + " shift — favor that side." : "Oscillating — work the shifts.",
        clears: st === "ok" ? "—" : "trend settles",
        based: ["live TWS/TWD trend over " + spanTxt(wt.mins) + " (" + wt.samples + " samples)"], conf: "engine" };
    },
    tactics(p) {
      const t = p.tactics;
      if (!t || !t.available) return NA(t && t.note ? t.note : "no tactics");
      const side = t.favored_side;
      const arrow = side === "left" ? "◀ L" : side === "right" ? "▶ R" : "—";
      const persistent = t.shift && t.shift.oscillation_deg != null && t.shift.shift_deg != null && Math.abs(t.shift.shift_deg) > t.shift.oscillation_deg;
      const st = side && side !== "either" && persistent ? "watch" : "ok";
      return { status: st, value: arrow, sub: (t.phase || "") + (persistent ? ", persistent" : ", osc"),
        why: t.recommendation || (t.phase + ", favored " + side), consider: t.favored_reason || "Sail your phase.",
        clears: st === "ok" ? "—" : "shift reverses", based: ["get_tactics: " + (t.phase || "?") + ", favored " + (side || "?")], conf: "engine" };
    },
    forecast(p) {
      const fc = p.forecast;
      if (!fc || !fc.available || !fc.hours || !fc.hours.length) return NA("no forecast");
      const h = fc.hours, now = h[0].tws, end = h[h.length - 1], later = end.tws;
      const trend = later > now + 2 ? "↗" : later < now - 2 ? "↘" : "→";
      const peak = Math.max.apply(null, h.map((x) => x.tws));
      return { status: "ok", value: trend + peak, sub: "kn / " + r0(end.in_h) + "h",
        why: fc.source + ": TWS " + now + "→" + later + " kn over the next " + r0(end.in_h) + " h.",
        consider: trend === "↗" ? "Breeze building — plan the gear." : "Conditions holding.", clears: "—",
        based: ["fetch_forecast: " + now + "kn now → " + later + "kn in " + r0(end.in_h) + "h"], conf: "engine" };
    },
    sail(p) {
      const s = p.sail;
      if (!s || !s.available) return NA(s && s.note ? s.note : "no sail data");
      const xo = s.next_crossover;
      let st = "ok", value = s.optimal_sail || "—";
      if (s.wrong_sail) { st = "act"; value = (s.hoisted_sail || "?") + "→" + s.optimal_sail; }
      else if (xo && xo.deg_away <= 8) { st = "watch"; value = s.optimal_sail + "→" + xo.to_sail; }
      return { status: st, value: value,
        sub: s.wrong_sail ? "wrong sail up" : s.in_range ? "in range" : (xo ? xo.direction + " " + r0(xo.deg_away) + "° → " + xo.to_sail : ""),
        why: s.recommendation || "",
        consider: s.wrong_sail ? "Change to " + s.optimal_sail + "." : (xo && xo.deg_away <= 8 ? "Stage the " + xo.to_sail + "; crossover " + r0(xo.deg_away) + "° away." : "No change."),
        clears: st === "ok" ? "—" : "cross to " + (xo ? xo.to_sail : s.optimal_sail),
        based: ["get_sail: optimal " + s.optimal_sail + ", TWA " + r0(s.twa) + "°, TWS " + r0(s.tws_used) + "kn" + (xo ? ", crossover " + xo.to_sail + " " + r0(xo.deg_away) + "°" : "")], conf: "engine" };
    },
    eta(p) {
      const n = p.navigator;
      if (!n || !n.available || !n.next_mark) return NA(n && n.note ? n.note : "no active course");
      const m = n.next_mark, e = m.eta_min;
      if (e == null) return NA("ETA needs VMC to the mark");
      const st = e < 2 ? "act" : e < 5 ? "watch" : "ok";
      const mm = e >= 60 ? Math.floor(e / 60) + "h" + r0(e % 60) : r0(e) + "m";
      return { status: st, value: mm, sub: shorten(m.name),
        why: "~" + r0(e) + " min to " + m.name + " at the current made-good.",
        consider: e < 5 ? "Mark in ~" + r0(e) + " min — start the rounding prep." : "On schedule for the mark.",
        clears: st !== "ok" ? "past the rounding" : "—", based: ["get_navigator: ETA " + r0(e) + "min to " + m.name], conf: "engine" };
    },
    charge(p) {
      const f = p.fatigue;
      if (!f || !f.available || f.index == null) return NA(f && f.note ? f.note : "no helm data");
      const chg = Math.round(100 - f.index), lvl = f.level || "";
      const st = lvl === "fresh" ? "ok" : lvl === "watch" ? "watch" : "act";
      const o = { status: st, value: String(chg), sub: lvl.replace(/_/g, " ") + " helm",
        why: "Helm energy ~" + chg + "% (inverse of the fatigue index; lower = more depleted). Level: " + lvl.replace(/_/g, " ") + ".",
        consider: st === "ok" ? "Driver fresh — no rotation needed." : "Tank getting low — plan a helm rotation.",
        clears: st === "ok" ? "—" : "energy back above 65%",
        based: ["get_fatigue: index " + r0(f.index) + " → charge " + chg + "%, level " + lvl], conf: "engine" };
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
      el.setAttribute("aria-label", NAME[key] + " " + st.word + " " + (t.value || ""));
      el.innerHTML =
        '<div class="t-head"><span class="t-name">' + NAME[key] + '</span>' +
        '<span class="t-chip"><span class="t-icon">' + st.icon + '</span><span class="t-word">' + st.word + '</span></span></div>' +
        '<div class="t-val">' + (t.value != null ? t.value : "—") + '</div>' +
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
    if (t.components) {
      let bars = '<div class="bars">';
      for (const lbl of Object.keys(t.components)) {
        bars += '<div class="bar"><span class="barlbl">' + lbl + '</span><span class="bartrk"><span class="barfil" style="width:' + Math.round(t.components[lbl] * 100) + '%"></span></span></div>';
      }
      g.innerHTML = (t.value || "—") + " · " + (t.sub || "") + bars + "</div>";
    } else {
      g.textContent = (t.value || "—") + "  ·  " + (t.sub || "");
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
      pushWind(p.conditions);          // feed the client-side wind-trend buffer
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
