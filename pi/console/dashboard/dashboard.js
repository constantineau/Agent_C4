/* SR33 Crew Dashboard — Phase 2: live onboard-engine wiring + deterministic status.
   Design: docs/COPILOT_DASHBOARD.md. The grid reads the onboard engine (proxied at /api/* →
   :8200) and renders each tile's live value + a DETERMINISTIC threshold status (no LLM yet —
   the engine owns the dashboard's truth and it works LLM-off). A SOURCE toggle switches between
   LIVE (engine) and DEMO (the canned calm/escalated scenarios kept for eyeballing). LLM status
   refinement + commentary + streamed deep-dives arrive in phases 3-4. */
(function () {
  "use strict";

  /* ---- status vocabulary: shape + word (the two colour-independent channels) ---- */
  const STATUS = {
    ok:    { icon: "●", word: "OK"    },
    watch: { icon: "▲", word: "WATCH" },
    act:   { icon: "■", word: "ACT"   },
    na:    { icon: "◌", word: "—"     },
  };
  const SEV = { ok: 0, watch: 1, act: 2, na: 2 };   // for the anti-flicker dwell

  const TILES = [
    "wind", "speed", "sail",
    "nav", "layline", "tactics",
    "fatigue", "forecast", "route",
    "heel", "depth", "data",
  ];
  const NAME = {
    wind: "WIND", speed: "SPEED", sail: "SAIL", nav: "NAV", layline: "LAYLINE",
    tactics: "TACT", fatigue: "FATIG", forecast: "FCAST", route: "ROUTE",
    heel: "HEEL", depth: "DEPTH", data: "DATA",
  };

  /* ============================ DEMO scenarios (canned) ============================ */
  const SCENARIOS = {
    calm: {
      mode: "llm-live", focus: "Racing clean. Watch the left phase for the next tack.", confidence: "high",
      notes: [
        { tile: "tactics", status: "ok", text: "Left has paid the last two oscillations — stay set up to tack on the next header.", conf: "high" },
        { tile: "speed",   status: "ok", text: "Boatspeed 94% of polar and pointing well. Hold the groove.", conf: "high" },
      ],
      tiles: {
        wind:    { status: "ok", value: "12", sub: "263° ↗ building", why: "True wind 12 kn from 263°, gusts to 14.", consider: "Trim for the lulls.", clears: "—", based: ["get_conditions: TWS 12.1kn, TWD 263°"], conf: "high" },
        speed:   { status: "ok", value: "6.8", sub: "94% polar", why: "STW 6.8 kn vs a 7.2 target — 94% of polar.", consider: "Hold the groove.", clears: "—", based: ["get_conditions: STW 6.8kn", "get_sail: target 7.2kn"], conf: "high" },
        sail:    { status: "ok", value: "J1", sub: "in range", why: "J1 is right for 12 kn upwind.", consider: "No change.", clears: "TWS > 16 kn", based: ["get_sail: optimal J1"], conf: "high" },
        nav:     { status: "ok", value: "1.9", sub: "Cove · 16m", why: "1.9 nm to Cove Island, ETA ~16 min.", consider: "On track.", clears: "—", based: ["get_navigator: Cove Island 1.9nm"], conf: "high" },
        layline: { status: "ok", value: "9°", sub: "below stbd", why: "9° below starboard layline.", consider: "Tack when it comes to you.", clears: "—", based: ["get_navigator: 9° below stbd"], conf: "high" },
        tactics: { status: "ok", value: "◀ L", sub: "osc, favor left", why: "Oscillating; left has paid. Lifted now.", consider: "Tack on the next header.", clears: "—", based: ["get_tactics: favored left, lifted"], conf: "high" },
        fatigue: { status: "ok", value: "28", sub: "fresh", why: "Helm index 28 (fresh).", consider: "No rotation needed.", clears: "—", based: ["get_fatigue: 28, fresh"], conf: "high" },
        forecast:{ status: "ok", value: "↗16", sub: "kn by 15:00", why: "Models build to ~16 kn by 15:00.", consider: "Plan the jib change.", clears: "—", based: ["fetch_forecast: 16kn @15:00"], conf: "high" },
        route:   { status: "ok", value: "2 tk", sub: "to gate", why: "Two tacks to the gate.", consider: "Hold; tack on the header.", clears: "—", based: ["get_route: 2 tacks"], conf: "med" },
        heel:    { status: "ok", value: "22°", sub: "on target", why: "Heel 22°, on the 12-kn target.", consider: "Hold trim.", clears: "—", based: ["get_conditions: heel 22°"], conf: "high" },
        depth:   { status: "ok", value: "84", sub: "ft, steady", why: "84 ft, steady.", consider: "No concern.", clears: "depth < 20 ft", based: ["get_conditions: depth 84ft"], conf: "high" },
        data:    { status: "ok", value: "5", sub: "sources live", why: "All five sensor groups fresh.", consider: "Instruments healthy.", clears: "—", based: ["get_sources: 5 live"], conf: "high" },
      },
    },
    escalated: {
      mode: "llm-live", focus: "Two things need attention: the bear-away sail and the helm.", confidence: "med",
      notes: [
        { tile: "sail",    status: "act",   text: "PEEL J1→A3 before the bear-away at the gate — start staging now (~4 min out).", conf: "high" },
        { tile: "fatigue", status: "act",   text: "Helm index 72 (rotate soon) — plan a driver change in the next few minutes.", conf: "med" },
        { tile: "speed",   status: "watch", text: "Speed slipped to 88% of polar in the building chop — ease and foot for a moment.", conf: "med" },
      ],
      tiles: {
        wind:    { status: "ok",    value: "16", sub: "271° ↗ gusty", why: "TWS 16 kn, gusts 19; veered ~8° right.", consider: "Depower in the gusts.", clears: "—", based: ["get_conditions: TWS 16kn, TWD 271°"], conf: "high" },
        speed:   { status: "watch", value: "6.8", sub: "88% polar", why: "STW 6.8 vs 7.7 target — 88%. Pinching in chop.", consider: "Ease and foot to rebuild speed.", clears: "back over 92% of polar", based: ["get_conditions: STW 6.8kn", "get_sail: target 7.7kn"], conf: "med" },
        sail:    { status: "act",   value: "J1→A3", sub: "peel before bear-away", why: "The leg after the gate bears away to ~135° TWA — an A3 leg. Peel before the rounding.", consider: "Stage the A3 and peel in ~4 min.", clears: "A3 hoisted", based: ["get_sail: A3 for TWA 135°"], conf: "high" },
        nav:     { status: "ok",    value: "0.7", sub: "Cove · 4m", why: "0.7 nm to the gate, ETA ~4 min.", consider: "Commit to the bear-away set.", clears: "—", based: ["get_navigator: Cove 0.7nm"], conf: "high" },
        layline: { status: "ok",    value: "2°", sub: "below stbd", why: "2° below the starboard layline.", consider: "Tack onto the layline shortly.", clears: "—", based: ["get_navigator: 2° below stbd"], conf: "high" },
        tactics: { status: "watch", value: "▶ R", sub: "veer, favor right", why: "Breeze veered right and is holding — looks persistent.", consider: "Favor the right.", clears: "veer reverses", based: ["get_tactics: favored right, persistent"], conf: "med" },
        fatigue: { status: "act",   value: "72", sub: "rotate soon", why: "Helm index 72. Heading instability and reversals above baseline; speed deficit creeping.", consider: "Plan a rotation in the next few minutes.", clears: "index < 60, sustained", based: ["get_fatigue: 72, rotate_soon"], conf: "med", components: { heading: 0.7, reversals: 0.8, heel: 0.4, "spd-def": 0.5 } },
        forecast:{ status: "ok",    value: "↗17", sub: "kn, holding", why: "Models hold 16-18 kn next hour.", consider: "A3 leg conditions confirmed.", clears: "—", based: ["fetch_forecast: 17kn next hour"], conf: "high" },
        route:   { status: "ok",    value: "1 tk", sub: "to gate", why: "One short hitch to the gate.", consider: "Tack onto the layline, then bear away.", clears: "—", based: ["get_route: 1 tack"], conf: "med" },
        heel:    { status: "watch", value: "27°", sub: "2° over", why: "Heel 27° vs a 25° target — overpressed.", consider: "Ease the traveler in the puffs.", clears: "heel < 25°", based: ["get_conditions: heel 27°"], conf: "med" },
        depth:   { status: "ok",    value: "61", sub: "ft, steady", why: "61 ft, clear of the shoal.", consider: "No concern.", clears: "depth < 20 ft", based: ["get_conditions: depth 61ft"], conf: "high" },
        data:    { status: "watch", value: "4", sub: "1 stale", why: "Masthead wind stale ~50 s ago; on the Orca backup.", consider: "Running on backup wind — watch for it to return.", clears: "all sources fresh", based: ["get_sources: 4 live, 1 stale"], conf: "med" },
      },
    },
  };

  /* ============================ LIVE engine mapping ============================ */
  const API = "/api";          // console proxies /api/* → onboard engine :8200
  const r0 = (x) => (x == null ? "?" : Math.round(x));
  const r1 = (x) => (x == null ? "?" : Math.round(x * 10) / 10);
  const NA = (note) => ({ status: "na", value: "—", sub: note || "no data", why: note || "No data from the engine.",
    consider: "—", clears: "—", based: [], conf: "engine" });
  const shorten = (s) => (s ? s.split(/[\s—-]/)[0].slice(0, 9) : "");

  function fetchJSON(path, ms) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms || 5000);
    return fetch(API + path, { signal: ctl.signal, headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .finally(() => clearTimeout(t));
  }

  const BUILD = {
    wind(p) {
      const c = p.conditions;
      if (!c || !c.available || c.tws == null) return NA("no wind data");
      const st = c.tws > 30 ? "act" : c.tws > 22 ? "watch" : "ok";
      return { status: st, value: String(r0(c.tws)), sub: r0(c.twd) + "°" + (c.stale ? " · stale" : ""),
        why: "True wind " + r1(c.tws) + " kn from " + r0(c.twd) + "°" + (c.stale ? " (reading is stale)." : "."),
        consider: st === "ok" ? "Trim for the lulls." : "Breeze up — be ready to depower.",
        clears: st === "ok" ? "—" : "TWS back under 22 kn",
        based: ["get_conditions: TWS " + r1(c.tws) + "kn, TWD " + r0(c.twd) + "°, age " + r1(c.data_age_seconds) + "s"], conf: "engine" };
    },
    speed(p) {
      const c = p.conditions, s = p.sail;
      if (!c || !c.available || c.stw == null) return NA("no speed data");
      const tgt = s && s.available && s.targets ? s.targets.btv : null;
      const pct = tgt ? Math.round((c.stw / tgt) * 100) : null;
      const st = pct == null ? "ok" : pct >= 92 ? "ok" : pct >= 80 ? "watch" : "act";
      return { status: st, value: String(r1(c.stw)), sub: pct != null ? pct + "% polar" : r1(c.sog) + " sog",
        why: pct != null ? "STW " + r1(c.stw) + " kn vs a polar target of " + r1(tgt) + " (" + pct + "%)." : "STW " + r1(c.stw) + " kn; no polar target at this angle.",
        consider: st === "ok" ? "Hold the groove." : "Ease and foot to rebuild speed.",
        clears: pct != null && st !== "ok" ? "back over 92% of polar" : "—",
        based: ["get_conditions: STW " + r1(c.stw) + "kn"].concat(tgt ? ["get_sail: target " + r1(tgt) + "kn"] : []), conf: "engine" };
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
    nav(p) {
      const n = p.navigator;
      if (!n || !n.available || !n.next_mark) return NA(n && n.note ? n.note : "no active course");
      const m = n.next_mark, d = m.distance_nm;
      return { status: "ok", value: d >= 100 ? String(r0(d)) : String(r1(d)),
        sub: shorten(m.name) + (m.eta_min != null ? " · " + r0(m.eta_min) + "m" : " nm"),
        why: r1(d) + " nm to " + m.name + ", bearing " + r0(m.bearing_deg) + "°" + (m.eta_min != null ? ", ETA ~" + r0(m.eta_min) + " min." : "."),
        consider: "On track for the next mark.", clears: "—",
        based: ["get_navigator: next " + m.name + ", " + r1(d) + "nm, brg " + r0(m.bearing_deg) + "°"], conf: "engine" };
    },
    layline(p) {
      const n = p.navigator;
      if (!n || !n.available) return NA("no nav");
      const ll = n.leg && n.leg.laylines, call = n.layline_call;
      if (!ll && !call) return NA("not on a beat");
      return { status: "ok", value: call ? String(call) : "—", sub: (n.leg && n.leg.type) || "",
        why: call ? "Layline: " + call : "On the " + ((n.leg && n.leg.type) || "current") + " leg.",
        consider: "Tack onto the layline when it comes to you.", clears: "—",
        based: ["get_navigator: leg " + ((n.leg && n.leg.type) || "?")], conf: "engine" };
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
    fatigue(p) {
      const f = p.fatigue;
      if (!f || !f.available || f.index == null) return NA(f && f.note ? f.note : "no helm data");
      const lvl = f.level || "";
      const st = lvl === "fresh" ? "ok" : lvl === "watch" ? "watch" : "act";
      const o = { status: st, value: String(r0(f.index)), sub: lvl.replace(/_/g, " "),
        why: "Helm index " + r0(f.index) + " (" + lvl.replace(/_/g, " ") + ").",
        consider: st === "ok" ? "No rotation needed." : "Plan a crew rotation soon.",
        clears: st === "ok" ? "—" : "index < 60 sustained",
        based: ["get_fatigue: index " + r0(f.index) + ", level " + lvl], conf: "engine" };
      if (f.components) o.components = f.components;
      return o;
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
    route(p) {
      const rt = p.route;
      if (!rt || !rt.available) return NA(rt && rt.note ? rt.note : "no route");
      const tacks = rt.tacks != null ? rt.tacks : (rt.legs ? rt.legs.length - 1 : null);
      return { status: "ok", value: tacks != null ? tacks + " tk" : "—", sub: "to mark",
        why: "Optimal routing" + (tacks != null ? " — " + tacks + " tack(s) to the mark." : "."),
        consider: "Follow the routing.", clears: "—", based: ["get_route"], conf: "engine" };
    },
    heel(p) {
      const c = p.conditions, s = p.sail;
      if (!c || c.heel == null) return NA("no heel source");
      const tgt = s && s.available && s.targets ? s.targets.heel : null;
      const st = tgt != null ? (Math.abs(c.heel - tgt) > 4 ? "watch" : "ok") : "ok";
      return { status: st, value: r0(c.heel) + "°", sub: tgt != null ? (c.heel >= tgt ? "+" : "") + r0(c.heel - tgt) + "° vs " + r0(tgt) : "",
        why: "Heel " + r0(c.heel) + "°" + (tgt != null ? " vs a " + r0(tgt) + "° target." : "."),
        consider: st === "ok" ? "Good trim." : "Adjust to hit the target heel.", clears: st === "ok" ? "—" : "back within 4° of target",
        based: ["get_conditions: heel " + r0(c.heel) + "°"].concat(tgt != null ? ["get_sail: target heel " + r0(tgt) + "°"] : []), conf: "engine" };
    },
    depth(p) {
      const c = p.conditions;
      if (!c || c.depth == null) return NA("no depth");
      const st = c.depth < 10 ? "act" : c.depth < 20 ? "watch" : "ok";
      return { status: st, value: String(r0(c.depth)), sub: "ft" + (c.depth < 20 ? " · shoaling" : ", steady"),
        why: r0(c.depth) + " ft under the keel.", consider: st === "ok" ? "No concern." : "Shoaling — watch the depth.",
        clears: st === "ok" ? "—" : "depth back over 20 ft", based: ["get_conditions: depth " + r0(c.depth) + "ft"], conf: "engine" };
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
      raw.status = commitStatus(k, raw.status);   // anti-flicker dwell
      tiles[k] = raw;
    }
    // engine-read commentary: list the watch/act tiles (act first), no LLM prioritisation
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

  /* anti-flicker dwell: worsening needs 2 consecutive polls; improving toward OK is immediate. */
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
    src: "live",                 // live | demo
    demoScn: "calm",             // calm | escalated
    theme: localStorage.getItem("sr33.dash.theme") || "auto",
    pos: { lat: 45.33, lon: -82.0 },
    openTile: null, streamTimer: null, pollTimer: null, polling: false,
    dwell: {},
    data: null,
  };

  function currentData() {
    if (App.src === "demo") {
      const sc = SCENARIOS[App.demoScn];
      return { tiles: sc.tiles, focus: sc.focus, notes: sc.notes, confidence: sc.confidence, mode: sc.mode };
    }
    return App.data || { tiles: {}, focus: "Connecting to the engine…", notes: [], confidence: "engine", mode: "engine read" };
  }

  /* ============================ theme (reuses /sun.js) ============================ */
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
    localStorage.setItem("sr33.dash.theme", App.theme);
    applyTheme();
  }

  /* ============================ render ============================ */
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
  function STATUS_PLACEHOLDER() { return { status: "na", value: "—", sub: "", why: "", consider: "—", clears: "—", based: [] }; }

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
    // WHY: demo mode streams (LLM feel); live mode is deterministic → render instantly.
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
      const eps = ["/conditions", "/sail", "/navigator", "/tactics", "/fatigue", "/forecast?hours=6", "/route", "/sources"];
      const keys = ["conditions", "sail", "navigator", "tactics", "fatigue", "forecast", "route", "sources"];
      const ms   = [5000, 5000, 5000, 5000, 5000, 9000, 4000, 5000];   // route gets a short leash (often times out)
      const res = await Promise.all(eps.map((e, i) => fetchJSON(e, ms[i])));
      const p = {}; keys.forEach((k, i) => (p[k] = res[i]));
      App.data = buildLive(p);
      if (App.src === "live") render();
    } finally { App.polling = false; }
  }
  function startPolling() { poll(); if (!App.pollTimer) App.pollTimer = setInterval(poll, 3000); }

  /* ============================ source toggle ============================ */
  function cycleSource() {
    // live → demo:calm → demo:esc → live
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
