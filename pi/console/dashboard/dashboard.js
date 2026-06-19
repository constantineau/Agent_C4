/* SR33 Crew Dashboard — Phase 1 static prototype (fake data).
   Goal: eyeball the locked design (docs/COPILOT_DASHBOARD.md) — the fixed status grid, the
   accessible >=3-channel status encoding, the commentary panel, day/night themes, and the
   tap-a-tile streamed deep-dive. NO live engine / copilot wiring yet (that's phases 2-4); all
   data here is canned so the look + interaction can be reviewed. */
(function () {
  "use strict";

  /* ---- status vocabulary: shape + word, the two color-independent channels ---- */
  const STATUS = {
    ok:    { icon: "●", word: "OK"    },   // ● filled circle
    watch: { icon: "▲", word: "WATCH" },   // ▲ triangle
    act:   { icon: "■", word: "ACT"   },   // ■ filled square
    na:    { icon: "◌", word: "—" },  // ◌ dotted circle, em-dash label
  };

  /* ---- the 12 locked tiles, in fixed grid order (geography never changes) ---- */
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

  /* ---- two canned scenarios so both calm + escalated states are reviewable ---- */
  const SCENARIOS = {
    calm: {
      mode: "llm-live",
      focus: "Racing clean. Watch the left phase for the next tack.",
      confidence: "high",
      notes: [
        { tile: "tactics", status: "ok", text: "Left has paid the last two oscillations — stay set up to tack on the next header.", conf: "high" },
        { tile: "speed",   status: "ok", text: "Boatspeed 94% of polar and pointing well. Hold the groove.", conf: "high" },
      ],
      tiles: {
        wind:    { status: "ok", value: "12", sub: "263° ↗ building", why: "True wind 12 kn from 263°, gusts to 14. Direction steady within a 6° oscillation band over the last 10 min.", consider: "Nothing to do — trim for the lulls.", clears: "—", based: ["get_conditions: TWS 12.1kn, TWD 263°", "get_tactics: osc band ±6°"], conf: "high" },
        speed:   { status: "ok", value: "6.8", sub: "94% polar", why: "STW 6.8 kn against an ORC target of 7.2 at this TWS/TWA — 94% of polar, normal for the chop.", consider: "Hold the groove; ease for the lulls.", clears: "—", based: ["get_conditions: STW 6.8kn", "get_polar_target: 7.2kn @ TWA 42°"], conf: "high" },
        sail:    { status: "ok", value: "J1", sub: "in range", why: "J1 is the right sail for 12 kn TWS upwind — mid-band, no crossover near.", consider: "No change.", clears: "TWS > 16 kn → stage smaller jib", based: ["get_sail_advice: optimal J1, crossover at 16kn"], conf: "high" },
        nav:     { status: "ok", value: "1.9", sub: "Cove Is. nm", why: "1.9 nm to the Cove Island gate, bearing 028°, ETA ~16 min at current VMC.", consider: "On track for the gate.", clears: "—", based: ["get_navigator: next Cove Island, 1.9nm, ETA 16min"], conf: "high" },
        layline: { status: "ok", value: "9°", sub: "below stbd", why: "9° below the starboard layline — comfortable margin, no overstand.", consider: "Tack when the layline comes to you.", clears: "—", based: ["get_navigator: laylines, 9° below stbd"], conf: "high" },
        tactics: { status: "ok", value: "◀ L", sub: "osc, favor left", why: "Oscillating breeze; the left has paid the last two phases. Currently lifted — a header is the tack signal.", consider: "Set up to tack on the next header to the left.", clears: "—", based: ["get_tactics: oscillating, favored left, currently lifted"], conf: "high" },
        fatigue: { status: "ok", value: "28", sub: "fresh", why: "Helm index 28 (fresh). Heading variance and reversal rate both at the driver's baseline.", consider: "No rotation needed.", clears: "—", based: ["get_fatigue: index 28, level fresh"], conf: "high" },
        forecast:{ status: "ok", value: "↗16", sub: "kn by 15:00", why: "GFS/NAM/HRRR agree the breeze builds to ~16 kn by 15:00, veering ~10°. Models tight (low spread).", consider: "Plan the jib change before the gate.", clears: "—", based: ["fetch_forecast: 16kn @15:00, veer +10°, model spread 1.2kn"], conf: "high" },
        route:   { status: "ok", value: "2 tk", sub: "to gate", why: "Two tacks to the gate on the current routing; first tack recommended in ~6 min.", consider: "Hold; tack on the next header.", clears: "—", based: ["get_route: 2 tacks, first tack in 6min"], conf: "med" },
        heel:    { status: "ok", value: "22°", sub: "on target", why: "Heel 22°, right on the Speed Guide target for 12 kn upwind.", consider: "Good trim — hold it.", clears: "—", based: ["get_conditions: heel 22°", "speed guide target 21-23°"], conf: "high" },
        depth:   { status: "ok", value: "84", sub: "ft, steady", why: "84 ft under the keel, steady — well off any charted shoal.", consider: "No concern.", clears: "depth < 20 ft → shoaling watch", based: ["get_conditions: depth 84ft"], conf: "high" },
        data:    { status: "ok", value: "5/5", sub: "sources live", why: "All five sensor groups reporting fresh; no source disagreement flagged.", consider: "Instruments healthy.", clears: "—", based: ["get_sources: 5 live, 0 stale, 0 disagree"], conf: "high" },
      },
    },
    escalated: {
      mode: "llm-live",
      focus: "Two things need attention: the bear-away sail and the helm.",
      confidence: "med",
      notes: [
        { tile: "sail",    status: "act",   text: "PEEL J1→A3 before the bear-away at the gate — start staging now (~4 min out).", conf: "high" },
        { tile: "fatigue", status: "act",   text: "Helm index 72 (rotate soon) — plan a driver change in the next few minutes.", conf: "med" },
        { tile: "speed",   status: "watch", text: "Speed slipped to 88% of polar in the building chop — ease and foot for a moment.", conf: "med" },
      ],
      tiles: {
        wind:    { status: "ok",    value: "16", sub: "271° ↗ gusty", why: "TWS up to 16 kn, gusts 19; veered ~8° right over 15 min as forecast.", consider: "Depower in the gusts; the veer favors the right now.", clears: "—", based: ["get_conditions: TWS 16kn, TWD 271°", "get_tactics: veer +8°/15min"], conf: "high" },
        speed:   { status: "watch", value: "6.8", sub: "88% polar", why: "STW 6.8 vs a 7.7 target at 16 kn — 88%. Down a few points in the building chop and pinching slightly.", consider: "Ease and foot to rebuild speed before the gate.", clears: "back over 92% of polar, sustained", based: ["get_conditions: STW 6.8kn, TWA 38°", "get_polar_target: 7.7kn @ TWA 43°"], conf: "med" },
        sail:    { status: "act",   value: "J1→A3", sub: "peel before bear-away", why: "The leg after the gate bears away to ~135° TWA at 16 kn — that's an A3 leg. J1 is wrong for it; peel before the rounding, not after.", consider: "Stage the A3 and peel in the next ~4 min, ahead of the bear-away.", clears: "A3 hoisted on the new leg", based: ["get_sail_advice: A3 for TWA 135° @16kn", "get_navigator: gate in 4min, exit brg 135° TWA"], conf: "high" },
        nav:     { status: "ok",    value: "0.7", sub: "Cove Is. nm", why: "0.7 nm to the gate, ETA ~4 min. Lined up for a clean rounding.", consider: "Commit to the bear-away set.", clears: "—", based: ["get_navigator: next Cove Island 0.7nm, ETA 4min"], conf: "high" },
        layline: { status: "ok",    value: "2°", sub: "below stbd", why: "2° below the starboard layline — one more short hitch and you're on it.", consider: "Tack onto the layline shortly.", clears: "—", based: ["get_navigator: laylines, 2° below stbd"], conf: "high" },
        tactics: { status: "watch", value: "▶ R", sub: "veer, favor right", why: "The breeze has veered right and is holding — looks more persistent than oscillating now. Right side is favored.", consider: "Favor the right; don't get caught on a leftward flyer.", clears: "veer reverses or osc resumes", based: ["get_tactics: persistent veer, favored right"], conf: "med" },
        fatigue: { status: "act",   value: "72", sub: "rotate soon", why: "Helm index 72. Heading instability and steering reversals are both above the driver's 40-min baseline; speed deficit is creeping. Classic tiring-helm signature.", consider: "Plan a rotation in the next few minutes — ideally after the rounding settles.", clears: "index < 60, sustained", based: ["get_fatigue: index 72, level rotate_soon, heading+reversals elevated"], conf: "med", components: { heading: 0.7, reversals: 0.8, heel: 0.4, "spd-def": 0.5 } },
        forecast:{ status: "ok",    value: "↗17", sub: "kn, holding", why: "Models hold 16-18 kn through the next hour, veering slightly. Spread still low.", consider: "The A3 leg conditions are confirmed.", clears: "—", based: ["fetch_forecast: 17kn next hour, model spread 1.5kn"], conf: "high" },
        route:   { status: "ok",    value: "1 tk", sub: "to gate", why: "One short hitch left to the gate; routing is stable.", consider: "Tack onto the layline, then bear away.", clears: "—", based: ["get_route: 1 tack to gate"], conf: "med" },
        heel:    { status: "watch", value: "27°", sub: "2° over", why: "Heel 27° vs a 25° target for 16 kn — overpressed in the gusts, costing height.", consider: "Ease the traveler / depower in the puffs.", clears: "heel back under 25°", based: ["get_conditions: heel 27°", "speed guide target 24-25°"], conf: "med" },
        depth:   { status: "ok",    value: "61", sub: "ft, steady", why: "61 ft, steady; clear of the gate-side shoal.", consider: "No concern.", clears: "depth < 20 ft → shoaling watch", based: ["get_conditions: depth 61ft"], conf: "high" },
        data:    { status: "watch", value: "4/5", sub: "1 stale", why: "The masthead wind source went stale ~50 s ago; true wind is on the Orca backup. Cross-check before trusting a single reading.", consider: "Running on the backup wind source — fine, but watch for it to come back.", clears: "all sources fresh", based: ["get_sources: 4 live, 1 stale (gWind), failover→Orca"], conf: "med" },
      },
    },
  };

  /* ---------- state ---------- */
  const App = {
    scenario: "calm",
    theme: localStorage.getItem("sr33.dash.theme") || "auto",
    pos: { lat: 45.33, lon: -82.0 },   // near Cove Island; for the sun/day-night calc
    openTile: null,
    streamTimer: null,
  };

  /* ---------- theme (auto day/night, reusing /sun.js) ---------- */
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

  /* ---------- render the grid ---------- */
  function renderGrid() {
    const sc = SCENARIOS[App.scenario];
    const grid = document.getElementById("grid");
    grid.innerHTML = "";
    for (const key of TILES) {
      const t = sc.tiles[key];
      const st = STATUS[t.status] || STATUS.na;
      const el = document.createElement("div");
      el.className = "tile s-" + t.status;
      el.dataset.tile = key;
      el.setAttribute("role", "button");
      el.setAttribute("tabindex", "0");
      el.setAttribute("aria-label", NAME[key] + " " + st.word + " " + t.value);
      el.innerHTML =
        '<div class="t-head">' +
          '<span class="t-name">' + NAME[key] + '</span>' +
          '<span class="t-chip"><span class="t-icon">' + st.icon + '</span>' +
          '<span class="t-word">' + st.word + '</span></span>' +
        '</div>' +
        '<div class="t-val">' + t.value + '</div>' +
        '<div class="t-sub">' + (t.sub || "") + '</div>';
      el.addEventListener("click", () => openDetail(key));
      el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail(key); } });
      grid.appendChild(el);
    }
  }

  /* ---------- render the commentary panel ---------- */
  function renderCommentary() {
    const sc = SCENARIOS[App.scenario];
    document.getElementById("commFocus").textContent = sc.focus;
    document.getElementById("commConf").textContent = "conf: " + sc.confidence;
    const pill = document.getElementById("modePill");
    pill.dataset.mode = sc.mode === "llm-live" ? "llm" : "engine";
    document.getElementById("modeLbl").textContent = sc.mode;

    const ul = document.getElementById("commNotes");
    ul.innerHTML = "";
    for (const n of sc.notes) {
      const li = document.createElement("li");
      li.className = "tg-" + n.status;
      li.innerHTML =
        '<span class="note-txt">' + n.text + '</span>' +
        '<span class="note-meta"><span class="note-tag">' + NAME[n.tile] + '</span>' +
        '<span class="note-conf">conf: ' + n.conf + '</span></span>';
      li.addEventListener("click", () => flashTile(n.tile));
      ul.appendChild(li);
    }
  }

  /* tap a note -> the LLM "points" at its tile with a ring (nothing moves) */
  function flashTile(key) {
    const el = document.querySelector('.tile[data-tile="' + key + '"]');
    if (!el) return;
    el.classList.remove("ring");
    void el.offsetWidth;        // restart the transition
    el.classList.add("ring");
    setTimeout(() => el.classList.remove("ring"), 1600);
  }

  /* ---------- tap a tile -> detail slide-over ---------- */
  function openDetail(key) {
    const sc = SCENARIOS[App.scenario];
    const t = sc.tiles[key];
    const st = STATUS[t.status] || STATUS.na;
    App.openTile = key;

    document.getElementById("detName").textContent = NAME[key];
    const ds = document.getElementById("detStatus");
    ds.innerHTML = '<span class="t-icon">' + st.icon + '</span> ' + st.word + ' · conf: ' + (t.conf || "—");
    ds.style.color = "var(--" + (t.status === "na" ? "na" : t.status) + ")";

    // gauge / component bars (reuse the fatigue-style component view when present)
    const g = document.getElementById("detGauge");
    if (t.components) {
      let bars = '<div class="bars">';
      for (const [lbl, v] of Object.entries(t.components)) {
        bars += '<div class="bar"><span class="barlbl">' + lbl + '</span>' +
          '<span class="bartrk"><span class="barfil" style="width:' + Math.round(v * 100) + '%"></span></span></div>';
      }
      bars += '</div>';
      g.innerHTML = t.value + " · " + (t.sub || "") + bars;
    } else {
      g.textContent = t.value + "  ·  " + (t.sub || "");
    }

    document.getElementById("detConsider").textContent = t.consider || "—";
    document.getElementById("detClears").textContent = t.clears || "—";
    document.getElementById("detBased").textContent = (t.based || []).join("   ·   ");

    document.getElementById("overlay").hidden = false;
    document.getElementById("detail").hidden = false;

    // WHY streams in token-by-token (the key latency lever in the real build)
    streamWhy(t.why || "");
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
    const words = text.split(" ");
    let i = 0;
    el.innerHTML = '<span class="caret"></span>';
    App.streamTimer = setInterval(() => {
      i++;
      const shown = words.slice(0, i).join(" ");
      el.innerHTML = shown + (i < words.length ? ' <span class="caret"></span>' : "");
      if (i >= words.length) { clearInterval(App.streamTimer); App.streamTimer = null; }
    }, 45);
  }

  /* ---------- prototype controls ---------- */
  function cycleScenario() {
    App.scenario = App.scenario === "calm" ? "escalated" : "calm";
    document.getElementById("scenarioLbl").textContent = App.scenario;
    if (!document.getElementById("detail").hidden) closeDetail();
    renderGrid();
    renderCommentary();
  }
  function briefMe() {
    const b = document.getElementById("briefBtn");
    b.classList.add("busy"); b.textContent = "thinking…";
    // simulate the ~brief latency + LLM catch-up
    setTimeout(() => {
      b.classList.remove("busy"); b.textContent = "Brief me ↻";
      renderCommentary();
    }, 900);
  }

  /* ---------- boot ---------- */
  function init() {
    applyTheme();
    renderGrid();
    renderCommentary();
    document.getElementById("themeBtn").addEventListener("click", cycleTheme);
    document.getElementById("scenarioBtn").addEventListener("click", cycleScenario);
    document.getElementById("briefBtn").addEventListener("click", briefMe);
    document.getElementById("detBack").addEventListener("click", closeDetail);
    document.getElementById("overlay").addEventListener("click", closeDetail);
    document.getElementById("detSend").addEventListener("click", () => {
      const inp = document.getElementById("detAsk");
      if (inp.value.trim()) { streamWhy("(scoped follow-up is wired in phase 4) — you asked: " + inp.value.trim()); inp.value = ""; }
    });
    // re-evaluate auto day/night a couple times a minute
    setInterval(() => { if (App.theme === "auto") applyTheme(); }, 25000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
