/* SR33 Crew Dashboard — higher-order tiles (live onboard engine + deterministic status).
   Design: docs/COPILOT_DASHBOARD.md + the crew's example slide. The grid surfaces the
   higher-order reads the sensors alone don't show:
     TWS Trend · Playbook · Forecast · Sail · Time to Mark · AIS / Fleet · C4 Energy · Data
   (VMG + Tactics retired 2026-07-03 — VMG repeats the boat's instruments; the on-water tactical
   read now lives in the top Strategy strip. See the TILES const below.)
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

  // VMG + Tactics retired 2026-07-03: VMG (% of polar) is repeated on the boat's own instruments, and
  // Tactics (favoured side / persistent-vs-oscillating) is now folded into the top Strategy strip —
  // the strip's synthesis consumes get_tactics and shows the favoured-side read even with no playbook
  // aboard. 8 tiles → a clean 4×2 grid. See docs/COPILOT_DASHBOARD.md.
  const TILES = ["wind", "playbook", "forecast", "sail", "eta", "ais", "charge", "data"];
  const NAME = {
    wind: "TWS Trend", playbook: "Playbook", forecast: "Forecast",
    sail: "Sail", eta: "Time to Mark", ais: "AIS / Fleet", charge: "C4 Energy", data: "Data",
  };
  /* AIS tile thresholds: nm to the closest point of approach + minutes to it (tunable). */
  const AIS_GUARD_NM = 0.5, AIS_WATCH_NM = 1.5, AIS_TCPA_ACT = 12, AIS_TCPA_WATCH = 30;
  const aisName = (t) => { const n = t.name || ("MMSI " + t.mmsi); return n.length > 12 ? n.slice(0, 11) + "…" : n; };
  /* corrected-time delta as a signed m:ss; cd<0 = the competitor is projected AHEAD of us. */
  const corrTxt = (b) => {
    const cd = b.corrected_delta_s;
    if (cd == null) return "—";
    const m = Math.floor(Math.abs(cd) / 60), s = Math.round(Math.abs(cd) % 60);
    const mag = m + ":" + (s < 10 ? "0" : "") + s;
    return (cd < 0 ? "▲ " : "▽ ") + mag + (cd < 0 ? " ahead" : " back");   // ▲ = they beat us
  };
  const fleetLine = (b) => {
    const cd = b.corrected_delta_s;
    if (cd == null) return (b.boat || "a boat") + " (no corrected-time yet)";
    const mins = (Math.abs(cd) / 60).toFixed(0);
    return (b.boat || "a rival") + " " + (cd < 0 ? mins + " min ahead" : cd > 0 ? mins + " min back" : "even") + " on corrected";
  };
  /* a delayed public-tracker fix carries source="tracker" + age_s — flag it (⌛ + age) so an
     over-the-horizon position is never read as a live call. Live own-receiver AIS has no mark. */
  const srcMark = (b) => b.source === "tracker" ? " ⌛" + (b.age_s != null ? Math.round(b.age_s / 60) + "m" : "trk") : "";

  const D2R = Math.PI / 180, R2D = 180 / Math.PI;
  const WIND_WIN_S = 12 * 3600;   // keep the whole race (~12 h) — feeds both the lookback rows and the race chart
  const FCST_WIN_S = 140 * 60;    // keep ~140 min of forecast snapshots (for the −120 min verification)
  const SERIES_MIN = 720;         // ask the engine for ~12 h of archived wind for the chart
  const COPILOT = "/copilot";     // proxied to the Orin copilot (:8300); writes the commentary
  const BRIEF_EVERY = 90000;      // ask the LLM for a fresh brief ~every 90 s (it's slow, ~45 s)
  const BRIEF_TTL = 300;          // an LLM brief stays "fresh" 5 min before reverting to engine-read
  const DEV_EVERY = 5000;         // poll the ENGINE's deterministic route-deviation ~every 5 s (Tier-1, no LLM)
  const DRIFT_EVERY = 20000;      // poll forecast-drift ~every 20 s (slow-moving; Open-Meteo is cached ~30 min)
  const COACH_EVERY = 15000;      // poll the proactive auto-coach held state ~every 15 s (no recompute — the Orin timer drives it)
  const SYN_EVERY = 15000;        // poll the in-race strategy synthesis ~every 15 s (a synthesis of the slower reads; LLM-phrased when the Orin is up)

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
        { tile: "playbook", status: "ok", text: "Left has paid the last two oscillations — the Left gameplan stands; stay set up to tack on the next header.", conf: "high" },
        { tile: "sail",     status: "ok", text: "J1 is the right sail and pointing well. Hold the groove.", conf: "high" },
      ],
      tiles: {
        wind:    { status: "ok", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(250, 12)] }, { label: "−60 min", cols: [arrowKts(246, 14)] }, { label: "−120 min", cols: [arrowKts(242, 16)] }],
                   why: "True wind speed + direction now and looking back. Arrows point the way the wind blows (north wind points down, east points left). Eased a touch and edged right over the last couple of hours.", consider: "Oscillating breeze — work the shifts.", clears: "—", based: ["engine archive + live buffer"], conf: "high" },
        playbook:{ status: "ok", value: "On plan: Left", sub: "oscillating ±6°",
                   rows: [{ hdr: true, cols: ["agree", ""] }, { label: "★ Left", emph: true, cols: ["52%", "start · now"] }, { label: "Middle", cols: ["28%", ""] }, { label: "Right", cols: ["20%", ""] }],
                   why: "Playbook recommends starting Left (52% of forecasts agree). Wind is oscillating — no persistent shift, so the Left gameplan stands; play the shifts within the band.", consider: "Hold the gameplan — tack on the headers, no branch yet.", clears: "—", based: ["playbook:left", "agreement 52%", "get_tactics"], conf: "high" },
        forecast:{ status: "ok", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(250, 10)] }, { label: "+60 min", cols: [arrowKts(256, 13)] }, { label: "+120 min", cols: [arrowKts(262, 15)] }, { sep: true, label: "FORECAST VS. ACTUAL" }, { hdr: true, cols: ["forecast", "actual"] }, { label: "−60 min", cols: [arrowKts(244, 13), arrowKts(243, 14)] }, { label: "−120 min", cols: [arrowKts(240, 16), arrowKts(238, 17)] }],
                   why: "Forecast wind speed + direction (arrows show direction; north wind points down). Building to ~15 kts and veering right. \"Forecast vs. actual\" compares the earlier forecast for −60/−120 min ago against what actually happened — within ~1 kt, verifying well.", consider: "Plan the gear for the build.", clears: "—", based: ["fetch_forecast + engine archive"], conf: "high" },
        sail:    { status: "ok", value: "J1", sub: "in range · no change ahead", why: "Reading the crew-set sail (J1 on the sails bar) — right for 12 kts upwind.", consider: "No change.", clears: "TWS > 16 kts", based: ["sails bar: crew set J1", "get_sail: optimal J1"], conf: "high" },
        eta:     { status: "ok", value: "16 min", sub: "Cove Island", why: "~16 min to Cove Island at the current made-good.", consider: "On schedule for the mark.", clears: "—", based: ["get_navigator: ETA 16 min"], conf: "high" },
        ais:     { status: "ok", value: "Windquest", sub: "▽ 1:50 back · behind corrected", rows: [{ hdr: true, cols: ["range", "CPA / TCPA"] }, { label: "Defiance", emph: true, cols: ["2.8 nm", "2.1 nm / 24m"] }, { label: "Lake Guardian", cols: ["4.1 nm", "3.6 nm · opening"] }, { label: "MMSI 3669…", cols: ["6.0 nm", "5.5 nm · opening"] }, { hdr: true, cols: ["fleet · ToT", "Δ corrected"] }, { label: "Windquest", emph: true, cols: ["3.1 nm to fin", "▽ 1:50 back"] }, { label: "Defiance", cols: ["3.4 nm to fin", "▲ 0:40 ahead"] }, { label: "Il Mostro ⌛17m", cols: ["1.9 nm to fin", "▲ 3:10 ahead"] }], why: "3 AIS contacts within 12 nm; nearest closing Defiance — CPA 2.1 nm in 24 min, comfortably clear. Fleet: 2 roster boats on AIS — Windquest 2 min back on corrected. 1 more over the horizon via the public tracker (delayed): Il Mostro is up the course and ahead on corrected.", consider: "Ahead of those near you on corrected — but the tracker shows Il Mostro leading over the horizon; keep racing the course, not just the boats in sight. (Fuzzy: partial AIS + a delayed tracker fix.)", clears: "—", based: ["get_ais: 3 contacts, min CPA 2.1 nm", "get_fleet: 2 matched, ToT", "tracker: 1 over-horizon"], conf: "high" },
        charge:  { status: "ok", value: "72", sub: "fresh", why: "Crew energy ~72% (inverse of the fatigue index; lower = more depleted).", consider: "Driver fresh — no rotation needed.", clears: "—", based: ["get_fatigue: index 28 → energy 72%"], conf: "high" },
        data:    { status: "ok", value: "5", sub: "sources live", why: "All five sensor groups fresh.", consider: "Instruments healthy.", clears: "—", based: ["get_sources: 5 live"], conf: "high" },
      },
      strategy: { available: true, status: "ok", variant: "left", variant_label: "Left start",
        value: "On the optimal track", xte_nm: 0.15, xte_side: "left", xte_trend: "steady",
        along_pct: 38, along_nm: 54.1, route_nm: 142.0, time_behind_s: -45,
        vmc_kn: 5.4, vmc_optimal_kn: 5.6, vmc_deficit_kn: 0.2,
        what_flips_it: "a persistent right shift past ~020° for two-plus oscillation cycles",
        why: "Sailing the 'Left start' variant's optimized track — 0.15 nm off the line (left), 0:45 ahead of plan pace. Hold the groove.",
        consider: "Stay on the playbook line — no branch, keep sailing the plan." },
      drift: { available: true, status: "ok", value: "Forecast holding", drift_twd_deg: 6, drift_dir: "veered", drift_tws_kn: 1.2, n_points: 8 },
      selector: { available: true, action: "hold", status: "ok", tier: 1, value: "Hold: Left start",
        target_label: "Left start", confidence: 0.75, confidence_label: "high",
        why: "No persistent shift and no material forecast drift — the recommended Left start stands. Play the shifts within the band." },
      synthesis: { available: true, mode: "llm", confidence: "high",
        assessment: "On plan — nothing across the signals argues for leaving the Left start.",
        concordance: { strength: "weak", lean: "left",
          note: "the breeze is oscillating with no persistent shift, and the mild fleet lean agrees with Left — no decisive read to act on" },
        recommendation: { action: "Hold: Left start", vs_playbook: "on-plan", target_variant: "left",
          urgency: "monitor", confidence: "high" } },
    },
    escalated: {
      mode: "llm-live", focus: "Playbook branch: the right is paying now. Bear-away coming up, and the crew tank is getting low.", confidence: "med",
      notes: [
        { tile: "playbook", status: "act",   text: "Playbook branch fired — the persistent right shift says bail to the right side; the recommended Left start no longer pays.", conf: "high" },
        { tile: "sail",     status: "act",   text: "Peel J1 → A3 before the bear-away at the gate — start staging now (~4 min out).", conf: "high" },
        { tile: "charge",   status: "act",   text: "Crew energy down to 28% (rotate soon) — plan a driver change in the next few minutes.", conf: "med" },
        { tile: "forecast", status: "watch", text: "Forecast has been under-calling the breeze by ~2-3 kts — expect a bit more than it says.", conf: "med" },
      ],
      tiles: {
        wind:    { status: "watch", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(262, 16)] }, { label: "−60 min", cols: [arrowKts(252, 12)] }, { label: "−120 min", cols: [arrowKts(246, 9)] }],
                   why: "True wind speed + direction now and looking back. Arrows point the way the wind blows (north wind points down). Built ~7 kts and veered right ~16° over the last two hours — a persistent right trend.", consider: "Persistent right shift — favor the right side of the course.", clears: "the trend settles", based: ["engine archive + live buffer"], conf: "med" },
        playbook:{ status: "act", value: "Switch → Right", sub: "branch: persistent veer favors right",
                   rows: [{ hdr: true, cols: ["agree", ""] }, { label: "★ Left", cols: ["52%", "start"] }, { label: "Right", emph: true, cols: ["20%", "now"] }, { label: "Middle", cols: ["28%", ""] }],
                   why: "A persistent veering shift now favors the right side — against the recommended Left. The playbook's branch trigger: if the breeze veers and holds right of ~020° for two-plus oscillation cycles, bail to the right.", consider: "Execute the branch — commit right per variant 'Right'.", clears: "the shift reverses / settles back toward the rhumb", based: ["playbook:right", "agreement 52%", "get_tactics"], conf: "high" },
        forecast:{ status: "watch", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(262, 16)] }, { label: "+60 min", cols: [arrowKts(268, 18)] }, { label: "+120 min", cols: [arrowKts(274, 19)] }, { sep: true, label: "FORECAST VS. ACTUAL" }, { hdr: true, cols: ["forecast", "actual"] }, { label: "−60 min", cols: [arrowKts(252, 12), arrowKts(256, 14)] }, { label: "−120 min", cols: [arrowKts(248, 10), arrowKts(252, 12)] }],
                   why: "Forecast wind speed + direction (arrows show direction; north wind points down). Building to ~19 kts and veering right. \"Forecast vs. actual\" shows it has under-called the wind by ~2 kts and the right shift — trust the trend over the model.", consider: "Forecast running light — plan for more than it says.", clears: "forecast comes back in line", based: ["fetch_forecast + engine archive"], conf: "med" },
        sail:    { status: "act",   value: "J1 → A3", sub: "in range · at Cove Island (bear away, ~4 min)", why: "Reading the crew-set sail (J1). The leg after the gate bears away to ~135° TWA — an A3 leg. Stage the peel before the rounding.", consider: "Stage the A3 and peel in ~4 min.", clears: "A3 hoisted", based: ["sails bar: crew set J1", "get_sail: A3 for TWA 135°"], conf: "high" },
        eta:     { status: "watch", value: "4 min", sub: "Cove Island", why: "~4 min to Cove Island at the current made-good.", consider: "Mark in ~4 min — start the rounding prep.", clears: "past the rounding", based: ["get_navigator: ETA 4 min"], conf: "high" },
        ais:     { status: "watch", value: "Algoma", sub: "CPA 0.8 nm in 11 min", rows: [{ hdr: true, cols: ["range", "CPA / TCPA"] }, { label: "Algoma", emph: true, cols: ["2.2 nm", "0.8 nm / 11m"] }, { label: "Defiance", cols: ["3.5 nm", "3.1 nm · opening"] }, { label: "MMSI 3661…", cols: ["7.4 nm", "7.0 nm · opening"] }, { hdr: true, cols: ["fleet · ToT", "Δ corrected"] }, { label: "Defiance", emph: true, cols: ["2.9 nm to fin", "▲ 1:20 ahead"] }, { label: "Windquest", cols: ["4.0 nm to fin", "▽ 2:10 back"] }], why: "3 AIS contacts within 12 nm; the closing one — Algoma, a laker — has a CPA of 0.8 nm in 11 min, crossing near the gate. Fleet: 2 roster boats on AIS — Defiance 1 min ahead on corrected.", consider: "A target is closing — watch the CPA and plan to keep clear at the rounding. On handicap, Defiance is your rival — cover when the crossing allows.", clears: "the CPA opens back up", based: ["get_ais: 3 contacts, min CPA 0.8 nm", "get_fleet: 2 matched, ToT"], conf: "high" },
        charge:  { status: "act",   value: "28", sub: "rotate soon", why: "Crew energy ~28% (rotate soon). Heading instability and steering reversals up, speed deficit creeping.", consider: "Tank getting low — plan a helm rotation.", clears: "energy back above 65%", based: ["get_fatigue: index 72 → energy 28%"], conf: "med", components: { heading: 0.7, reversals: 0.8, heel: 0.4, "spd-def": 0.5 } },
        data:    { status: "watch", value: "4", sub: "1 stale", why: "Masthead wind stale ~50 s ago; running on the Orca backup.", consider: "Running on backup wind — watch for it to return.", clears: "all sources fresh", based: ["get_sources: 4 live, 1 stale"], conf: "med" },
      },
      strategy: { available: true, status: "act", variant: "left", variant_label: "Left start",
        value: "Off track · 1.3 nm right", xte_nm: 1.3, xte_side: "right", xte_trend: "diverging",
        along_pct: 61, along_nm: 86.6, route_nm: 142.0, time_behind_s: 180,
        vmc_kn: 5.0, vmc_optimal_kn: 5.9, vmc_deficit_kn: 0.9,
        what_flips_it: "a persistent right shift past ~020° for two-plus oscillation cycles",
        why: "The boat is 1.3 nm to the right of the 'Left start' variant's optimal track and diverging. This is a genuine departure from the frozen line.",
        consider: "You're right of the plan — decide: rejoin the 'Left start' line, or if the breeze has genuinely changed sides, check the branch trigger (the playbook tile is calling the right)." },
      drift: { available: true, status: "act", value: "Forecast moved · 28° veered", drift_twd_deg: 28, drift_twd_max_deg: 41, drift_dir: "veered", drift_tws_kn: 3.5, n_points: 7 },
      selector: { available: true, action: "switch", status: "act", tier: 1, value: "Switch → Right start",
        target_variant: "right", target_label: "Right start", confidence: 0.9, confidence_label: "high",
        driven_by: ["get_tactics", "get_drift", "get_deviation"],
        why: "A persistent veering shift now favours the right — against the recommended Left. That's the playbook's branch trigger. Reinforced: the forecast has veered ~28° the same way; you're already working the right side (1.3 nm right)." },
      reoptimize: { available: true, off_playbook: true, eta_min: 254, tacks: 9, sailed_nm: 46.2, avoids_islands: 2, avoids_zones: 1,
        marks: ["Cove Island", "Finish"], sail_plan: ["J1", "A3", "S2"], vs_playbook: { available: true, max_divergence_nm: 2.4, mean_divergence_nm: 0.9 } },
      synthesis: { available: true, mode: "llm", confidence: "med",
        assessment: "The right has genuinely taken over — this outruns the pre-authored branches. Commit right, off the book.",
        concordance: { strength: "strong", lean: "right",
          note: "the persistent veer, the forecast drift and your position all point right — and you're already 1.3 nm right of the plan; high concordance" },
        recommendation: { action: "Off-book: commit right and lay the mark", vs_playbook: "departs", target_variant: null,
          reoptimize: "ready", urgency: "now", confidence: "med",
          rationale: "all three directional reads agree right and the situation has passed even the Right-start branch — sail the favoured side, onboard re-route ready" },
        play_matches: [
          { play_id: "shift_right_20", name: "Breeze 20°+ right of forecast", match: "strong", status: "armed",
            why: "the persistent veer + 28° forecast drift are exactly this play's described bust" },
          { play_id: "pressure_up", name: "More pressure than forecast", match: "partial", status: "quiet",
            why: "breeze running ~2-3 kts over the frozen forecast, not yet sustained" },
        ],
        reoptimize: { available: true, off_playbook: true, eta_min: 254, tacks: 9, sailed_nm: 46.2, avoids_islands: 2, avoids_zones: 1,
          marks: ["Cove Island", "Finish"], sail_plan: ["J1", "A3", "S2"], vs_playbook: { available: true, max_divergence_nm: 2.4, mean_divergence_nm: 0.9 } } },
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
    /* SAIL: reads the CREW-SET sail (the sails bar) and says how it fits the conditions —
       in range / overpowered / underpowered / wrong angle — with an arrow to the next change
       and WHEN: change now · at the next mark (leg-after geometry) · at an approaching
       crossover. Falls back to the optimal read when the crew hasn't set a sail. */
    sail(p) {
      const s = p.sail;
      if (!s || !s.available) return NA(s && s.note ? s.note : "no sail data");
      const crew = s.crew_sail;
      const fit = s.fit;
      const xo = s.next_crossover;
      const FIT = { in_range: "in range", overpowered: "OVERPOWERED", underpowered: "underpowered",
                    wrong_angle: "wrong sail for this angle", no_model: "logged (no rating model)" };
      let to = null, when = null, st = "ok";
      if (crew && fit && fit !== "in_range" && fit !== "no_model") {
        to = s.change_to || s.optimal_sail;
        when = "change now";
        st = "act";
      } else {
        const cur = s.hoisted_sail || s.optimal_sail;
        // next-mark look-ahead: what does the leg AFTER the rounding want?
        const nr = (p.navigator || {}).next_rounding;
        if (nr && nr.exit_twa_deg != null) {
          const z = (s.zones || []).find((z) => z.twa_min <= nr.exit_twa_deg && nr.exit_twa_deg <= z.twa_max);
          if (z && z.sail !== cur) {
            to = z.sail;
            const eta = ((p.navigator || {}).next_mark || {}).eta_min;
            when = "at " + (nr.exit_mark || "the next mark") + " (" + nr.maneuver + (eta != null ? ", ~" + r0(eta) + " min" : "") + ")";
            st = eta != null && eta <= 15 ? "watch" : "ok";
          }
        }
        if (!to && xo && xo.deg_away <= 8 && xo.to_sail !== cur) {
          to = xo.to_sail;
          when = "as you " + xo.direction + " (" + r0(xo.deg_away) + "° from here)";
          st = "watch";
        }
      }
      const base = crew || s.optimal_sail || "—";
      const value = to ? base + " → " + to : base;
      const sub = (crew ? (FIT[fit] || "crew-set") : "crew sail not set — optimal shown")
        + (when ? " · " + when : (crew && fit === "in_range" ? " · no change ahead" : ""));
      return { status: st, value: value,
        sub: sub,
        why: (crew ? "Reading the crew-set sail (" + crew + " on the sails bar). " : "No crew sail set — tap what's flying on the SAILS bar. ")
          + (s.recommendation || ""),
        consider: to ? (when === "change now" ? "Change to " + to + " now." : "Stage the " + to + " — " + when + ".") : "No change.",
        clears: st === "ok" ? "—" : (to ? to + " hoisted" : "—"),
        based: ["sails bar: crew set " + (crew || "—"),
                "get_sail: optimal " + s.optimal_sail + ", TWA " + r0(s.twa) + "°, TWS " + r0(s.tws_used) + " kts" + (xo ? ", crossover " + xo.to_sail + " " + r0(xo.deg_away) + "°" : "")], conf: "engine" };
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
    ais(p) {
      const a = p.ais;
      if (!a) return NA("no AIS feed");
      const tgts = a.targets || [];
      if (!tgts.length) {
        return { status: "ok", value: "Clear", sub: "no traffic",
          why: "No AIS contacts within " + r0(a.max_range_nm) + " nm of own ship.",
          consider: "No vessels to manage — keep monitoring.", clears: "a target comes within range",
          based: ["get_ais: 0 contacts"], conf: "engine" };
      }
      // no own-ship fix → we can list targets but not range/CPA; honest NA on the geometry.
      if (a.own_fix === false) {
        const rows = [{ hdr: true, cols: ["vessel", "SOG"] }].concat(
          tgts.slice(0, 6).map((t, i) => ({ label: aisName(t), emph: i === 0,
            cols: [t.sog != null ? r1(t.sog) + " kts" : "—"] })));
        return { status: "watch", value: String(tgts.length), sub: "no own fix", rows: rows,
          why: tgts.length + " AIS contact" + (tgts.length === 1 ? "" : "s") + " heard, but no own-ship position fix — range/CPA/TCPA unavailable.",
          consider: "AIS up but no GPS fix — geometry is blind; verify own position.",
          clears: "own-ship fix returns", based: ["get_ais: " + tgts.length + " contacts, no own fix"], conf: "engine" };
      }
      const rows = [{ hdr: true, cols: ["range", "CPA / TCPA"] }].concat(
        tgts.slice(0, 6).map((t, i) => ({ label: aisName(t), emph: i === 0,
          cols: [t.range_nm != null ? r1(t.range_nm) + " nm" : "—",
                 t.cpa_nm != null ? (r1(t.cpa_nm) + " nm" + (t.closing && t.tcpa_min != null ? " / " + r0(t.tcpa_min) + "m" : t.closing ? "" : " · opening")) : "—"] })));
      // threat = the nearest CLOSING target (the list is already threat-sorted by the engine).
      const threat = tgts.find((t) => t.closing && t.cpa_nm != null);
      let st = "ok", value = String(tgts.length), sub = "contact" + (tgts.length === 1 ? "" : "s") + " · all clear";
      if (threat) {
        const cpa = threat.cpa_nm, tcpa = threat.tcpa_min;
        if (cpa <= AIS_GUARD_NM && tcpa != null && tcpa <= AIS_TCPA_ACT) st = "act";
        else if (cpa <= AIS_WATCH_NM && tcpa != null && tcpa <= AIS_TCPA_WATCH) st = "watch";
        if (st !== "ok") { value = aisName(threat); sub = "CPA " + r1(cpa) + " nm" + (tcpa != null ? " in " + r0(tcpa) + " min" : ""); }
        else { sub = tgts.length + " contact" + (tgts.length === 1 ? "" : "s") + " · closest CPA " + r1(cpa) + " nm"; }
      }
      // FLEET overlay: corrected-time standings for roster-matched competitors (the handicap layer).
      // Collision keeps status primacy (safety); fleet enriches the detail + takes the face when clear.
      const fl = p.fleet, based = ["get_ais: " + tgts.length + " contacts" + (threat ? ", min CPA " + r1(threat.cpa_nm) + " nm" : ", none closing")];
      let why = tgts.length + " AIS contact" + (tgts.length === 1 ? "" : "s") + " within " + r0(a.max_range_nm) + " nm" +
        (threat ? "; nearest closing " + aisName(threat) + " — CPA " + r1(threat.cpa_nm) + " nm" + (threat.tcpa_min != null ? " in " + r0(threat.tcpa_min) + " min." : ".")
                : ", none closing inside the guard.");
      let consider = st === "act" ? "Close-quarters developing — resolve the crossing now, keep clear." :
        st === "watch" ? "A target is closing — watch the CPA and plan to keep clear." : "Traffic is clear — keep monitoring.";
      if (fl && fl.available && fl.fleet && fl.fleet.length) {
        const F = fl.fleet, top = F[0], nTrk = fl.count_tracker || 0;
        rows.push({ hdr: true, cols: ["fleet · " + (fl.scoring_method || "corrected"), "Δ corrected"] });
        F.slice(0, 4).forEach((b, i) => rows.push({ label: (b.boat || ("MMSI " + b.mmsi)) + srcMark(b), emph: i === 0,
          cols: [b.dtf_nm != null ? r1(b.dtf_nm) + " nm to fin" : (b.range_nm != null ? r1(b.range_nm) + " nm" : "—"), corrTxt(b)] }));
        based.push("get_fleet: " + fl.count_matched + " matched, " + (fl.scoring_method || "corrected"));
        why += " Fleet: " + (fl.count_ais != null ? fl.count_ais : fl.count_matched) + " roster boat" +
          ((fl.count_ais === 1) ? "" : "s") + " on AIS — " + fleetLine(top) + ".";
        if (nTrk) {
          why += " " + nTrk + " more over the horizon via the public tracker (delayed).";
          based.push("tracker: " + nTrk + " over-horizon" + (fl.tracker && fl.tracker.error ? " (feed issue)" : ""));
        }
        if (st === "ok") {                       // no traffic threat → fleet takes the face
          value = (top.boat || ("MMSI " + top.mmsi)) + srcMark(top);
          sub = corrTxt(top) + " · " + (top.tag === "rival" ? "your rival" : top.tag === "ahead_corrected" ? "ahead corrected" : "behind corrected");
          consider = top.tag === "ahead_corrected" ? "Cover " + (top.boat || "the leader") + " — they're projected ahead on handicap." :
                     top.tag === "rival" ? "Tight on corrected with " + (top.boat || "a rival") + " — sail your race, stay between them and the next shift." :
                     "Ahead on corrected — consolidate, don't take a flyer. (Fuzzy: partial AIS + projection.)";
        }
      } else if (fl && fl.available === false && tgts.length) {
        why += " (No fleet roster loaded — corrected-time standings unavailable.)";
      }
      return { status: st, value: value, sub: sub, rows: rows, why: why, consider: consider,
        clears: st === "ok" ? "—" : "the CPA opens back up", based: based, conf: "engine" };
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
    /* PLAYBOOK: are we on the frozen homework, and has a branch fired? Driven by the engine's
       unified SELECTOR (Tier-1, always reachable — no Orin needed), so the tile and the Strategy-card
       recommendation banner always agree. (The old copilot /adherence fallback is retired — the
       selector has been the single source of truth since the tile was unified onto it.) */
    playbook() {
      const s = App.selector;
      if (!s) return NA("loading playbook…");
      if (s.available === false || s.action === "na") return NA(s.why || s.value || "no playbook aboard");
      const act = s.action, conf = s.confidence_label ? s.confidence_label + " conf" : "";
      // the tile face carries the WHOLE strategy stack's headline now (the old top strip is
      // retired — tap the tile for synthesis/triggers/plays/re-route detail)
      const armed = ((App.plays || {}).armed || []).length;
      const synRec = ((App.synthesis || {}).recommendation) || {};
      const synOff = synRec.vs_playbook === "departs" || synRec.vs_playbook === "off-book";
      let status = s.status || "ok";
      if (synOff) status = "act";
      else if (armed && status === "ok") status = "watch";
      const bits = [(act === "switch" ? "branch fired" : act === "off_script" ? "off the playbook" : "on plan")];
      if (armed) bits.push(armed + " play" + (armed === 1 ? "" : "s") + " ARMED");
      if (synOff) bits.push("off-book · re-route ready");
      if (conf) bits.push(conf);
      return { status: status, value: s.value || "—", sub: bits.join(" · "), rows: s.rows || [],
        why: s.why || "", consider: s.consider || "—", clears: s.clears || "—",
        based: s.based || [], conf: "engine" };
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
  /* poll the proactive auto-coach: the Orin runs the narration engine on a timer and HOLDS the
     latest coach line; we just read it (no recompute) and show what the copilot last volunteered.
     A failure leaves the last good read. */
  async function fetchCoach() {
    if (App.src !== "live") return;
    let r = null;
    try {
      const ctl = new AbortController();
      const to = setTimeout(() => ctl.abort(), 12000);
      const resp = await fetch(COPILOT + "/coach", { headers: { Accept: "application/json" }, signal: ctl.signal });
      clearTimeout(to);
      r = resp.ok ? await resp.json() : null;
    } catch (e) { r = null; }
    if (r) App.coach = r;
    if (r) maybeAlert(r);           // sound a short tone if a new safety/urgent callout arrived
    if (App.src === "live") render();
  }
  function agoText(epochSec) {
    if (!epochSec) return "";
    const s = Math.max(0, Math.round(Date.now() / 1000 - epochSec));
    return s < 60 ? s + "s ago" : Math.round(s / 60) + "m ago";
  }

  /* poll the ENGINE's deterministic route-deviation read (Lab-3 Strategy card). Tier-1, no LLM —
     the boat's own GPS vs the frozen playbook variant's optimal track, so it's cheap + always
     available in-race. A failure leaves the last good read (or a clear na on the first failure). */
  async function fetchDeviation() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/deviation", 8000);
    if (r) App.deviation = r;
    else if (!App.deviation) App.deviation = { available: false, status: "na", value: "—",
      why: "The onboard engine (:8200) is unreachable — route-deviation needs it." };
    if (App.src === "live") render();
  }
  const behindTxt = (s) => {
    if (s == null) return null;
    const a = Math.abs(s), m = Math.floor(a / 60), ss = Math.round(a % 60);
    const mag = m + ":" + (ss < 10 ? "0" : "") + ss;
    return (s >= 0 ? "+" + mag + " behind" : "−" + mag + " ahead");
  };
  function currentDeviation() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).strategy || null;
    return App.deviation;
  }
  /* forecast-drift (Lab-3 branch trigger b): live common forecast vs the plan's frozen reference.
     Engine-computed, deterministic; polled slowly (it moves slowly + Open-Meteo is cached). */
  async function fetchDrift() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/drift", 9000);
    if (r) App.forecastDrift = r;
    if (App.src === "live") render();
  }
  function currentDrift() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).drift || null;
    return App.forecastDrift;
  }
  /* Playbook v2 Phase D — the Tier-1 PLAY MATCHER: armed/arming plays from the frozen v2 bundle
     (engine-deterministic Schmitt sustain). Plus the crew GEAR toggle: a tapped kite = declared
     out of service (the gear-loss plays' arming signal — no instrument for a blown sail). */
  async function fetchPlays() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/plays", 9000);
    if (r) App.plays = r;
    if (App.src === "live") render();
  }
  function currentPlays() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).plays || null;
    return App.plays;
  }
  const GEAR = ["A2", "A3", "S2"];
  /* the boat's sail inventory for the SAILS bar — cert sails + the crew-config overlays
     (C0/J2/J3/SS aren't in the ORC cert; the boat flies COMBINATIONS, so chips multi-select) */
  const SAILS_INV = ["J1", "J2", "J3", "C0", "SS", "A2", "A3", "S2"];
  async function fetchSailState() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/sails/state", 8000);
    if (r) { App.sailState = r; renderSailsBar(); }
  }
  window.toggleFlying = async function (sail) {
    const fly = new Set(((App.sailState || {}).flying) || []);
    if (fly.has(sail)) fly.delete(sail); else fly.add(sail);
    try {
      const resp = await fetch(API + "/sails/state", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ flying: [...fly] }) });
      if (resp.ok) { App.sailState = await resp.json(); renderSailsBar(); fetchPlays(); }
    } catch (e) { /* engine unreachable — the next poll re-syncs */ }
  };
  window.toggleReef = async function () {
    const cur = ((App.sailState || {}).reef) || "";
    try {
      const resp = await fetch(API + "/sails/state", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reef: cur ? "" : "R1" }) });
      if (resp.ok) { App.sailState = await resp.json(); renderSailsBar(); fetchPlays(); }
    } catch (e) { /* re-sync on next poll */ }
  };
  /* RACE LOG (sessions): one-tap record switch. Start needs nothing (the engine defaults the
     name to the date and picks up the loaded playbook's race_id if one is aboard). */
  async function fetchSession() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/session", 8000);
    if (r) { App.session = r; renderRec(); }
  }
  function renderRec() {
    const b = document.getElementById("recBtn"), l = document.getElementById("recLbl");
    if (!b || !l) return;
    const a = (App.session || {}).active;
    b.classList.toggle("on", !!a);
    if (a) {
      const min = Math.max(0, Math.round((Date.now() / 1000 - a.start_ts) / 60));
      l.textContent = (a.kind === "race" ? "REC·RACE " : "REC ") + min + "m";
      b.title = "Logging \"" + a.name + "\" — tap to END the session";
    } else {
      l.textContent = "LOG";
      b.title = "Race log — tap to start a logged session (races AND practice sails; " +
        "data outside sessions is pruned and never leaves the boat)";
    }
  }
  async function toggleRec() {
    const a = (App.session || {}).active;
    try {
      if (a) {
        if (!confirm('End the race log "' + a.name + '"?')) return;
        await fetch(API + "/session/end", { method: "POST" });
      } else {
        await fetch(API + "/session/start", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: "{}" });
      }
      fetchSession();
    } catch (e) { /* engine unreachable — next poll re-syncs */ }
  }
  function renderSailsBar() {
    const el = document.getElementById("sailsBar");
    if (!el) return;
    if (App.src !== "live") { el.hidden = true; return; }
    el.hidden = false;
    const st = App.sailState || {};
    const fly = new Set(st.flying || []);
    const out = new Set(st.out_of_service || []);
    document.getElementById("sbChips").innerHTML = SAILS_INV.map((s) =>
      `<button class="sb-chip${fly.has(s) ? " on" : ""}" onclick="toggleFlying('${s}')" ` +
      `title="Tap = ${fly.has(s) ? "douse" : "hoist"} the ${s}${out.has(s) ? " (declared out of service)" : ""}">` +
      `${s}${out.has(s) ? "✕" : ""}</button>`).join("");
    document.getElementById("sbReef").innerHTML =
      `<button class="sb-chip reef${st.reef ? " on" : ""}" onclick="toggleReef()" ` +
      `title="${st.reef ? "Shake out reef 1" : "Tuck in reef 1"}">R1${st.reef ? " ▽" : ""}</button>`;
    document.getElementById("sbNote").textContent = fly.size
      ? "flying " + [...fly].join(" + ") + (st.reef ? " · reef 1 in" : "") + " — logged"
      : "tap what's flying — it's logged for the debrief";
  }
  window.toggleGear = async function (sail) {
    const st = ((currentPlays() || {}).sail_state) || {};
    const out = new Set(st.out_of_service || []);
    if (out.has(sail)) out.delete(sail); else out.add(sail);
    try {
      await fetch(API + "/sails/state", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ out_of_service: [...out] }) });
      fetchPlays();
    } catch (e) { /* engine unreachable — the next poll re-syncs */ }
  };
  /* the branch SELECTOR: the executor's unified call — hold / switch to a pre-authored variant /
     off-script. Engine-computed (unifies wind-shift + deviation + drift over the frozen bundle). */
  async function fetchSelector() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/selector", 9000);
    if (r) App.selector = r;
    // only run the heavy onboard re-optimizer when the plan has actually run out (off-script)
    if (r && r.action === "off_script") fetchReoptimize();
    if (App.src === "live") render();
  }
  function currentSelector() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).selector || null;
    return App.selector;
  }
  /* the onboard RE-OPTIMIZER (graceful-degradation fallback route). Heavy (isochrone) → fetched
     on demand ONLY when the selector says off-script (server-cached), never on every poll. */
  async function fetchReoptimize() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/reoptimize", 20000);
    if (r) App.reoptimize = r;
    if (App.src === "live") render();
  }
  function currentReoptimize() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).reoptimize || null;
    return App.reoptimize;
  }
  /* the in-race SYNTHESIS: the higher-order cross-signal read. Try the copilot's LLM-phrased brief
     (POST /copilot/strategy) when the Orin is up; fall back to the ENGINE's deterministic digest
     (GET /strategy, always there on the Pi). Mirrors the playbook tile's selector wiring.
     Own ~15 s cadence — it's a synthesis of the slower reads, no need to poll it fast. */
  async function fetchSynthesis() {
    if (App.src !== "live") return;
    let r = null;
    try {                                            // 1) the copilot (LLM phrasing) — absent on the bench
      const ctl = new AbortController();
      const to = setTimeout(() => ctl.abort(), 15000);
      const resp = await fetch(COPILOT + "/strategy", { method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({}), signal: ctl.signal });
      clearTimeout(to);
      r = resp.ok ? await resp.json() : null;
    } catch (e) { r = null; }
    if (!r) r = await fetchJSON("/strategy", 12000);  // 2) engine deterministic digest (Tier-1 fallback)
    if (r) App.synthesis = r;
    if (App.src === "live") render();
  }
  function currentSynthesis() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).synthesis || null;
    return App.synthesis;
  }
  /* map the synthesis to a card status colour: an off-book departure or a 'now' rec = act; a split
     read or a 'soon' rec = watch; else ok (na when there's nothing to synthesise). */
  function synthStatus(s) {
    if (!s || s.available === false) return "na";
    const rec = s.recommendation || {}, conc = s.concordance || {};
    const off = rec.vs_playbook === "departs" || rec.vs_playbook === "off-book";
    if (off || rec.urgency === "now") return "act";
    if (conc.strength === "split" || rec.urgency === "soon") return "watch";
    return "ok";
  }
  /* ============ the STRATEGY STACK — now lives INSIDE the PLAYBOOK tile's tap-detail ============
     (the old top-of-screen Strategy strip was information overflow — crew request 2026-07-08).
     Same content, composed as HTML for the detail overlay: synthesis apex → selector banner →
     deviation progress/metrics → drift line → armed plays (+ gear toggles) → off-book re-route. */
  function synthHtml() {
    const s = currentSynthesis();
    if (!s || s.available === false) return "";
    const rec = s.recommendation || {}, conc = s.concordance || {};
    const off = rec.vs_playbook === "departs" || rec.vs_playbook === "off-book";
    const conf = s.confidence || rec.confidence || "";
    const note = conc.note && conc.strength !== "none" ? conc.note : "";
    const pm = Array.isArray(s.play_matches) ? s.play_matches.slice(0, 3) : [];
    const lib = {};
    ((currentPlays() || {}).plays || []).forEach((p) => { lib[p.id] = p.name; });
    const pmHtml = pm.length ? '<div class="st-syn-plays">' + pm.map((m) => {
      const name = stripTags(m.name || lib[m.play_id] || m.play_id);
      const live = m.status === "armed" ? "armed" : m.status === "arming" ? "arming…" : "";
      return '<div class="st-pm' + (m.match === "strong" ? " strong" : "") + '">' +
        '<span class="st-pm-tag">' + (m.match === "strong" ? "◆" : "◇") + ' play</span> ' +
        "<b>" + name + "</b>" +
        '<i>' + (m.match || "partial") + " match" + (live ? " · " + live : "") + "</i>" +
        (m.why ? '<span class="st-pm-why">' + stripTags(m.why) + "</span>" : "") + "</div>";
    }).join("") + "</div>" : "";
    return '<div class="st-syn y-' + synthStatus(s) + '"><div class="st-syn-head">' +
      '<span class="st-syn-tag">SYNTHESIS</span>' +
      (off ? '<span class="st-syn-badge">OFF-BOOK</span>' : "") +
      '<span class="st-spacer"></span>' +
      '<span class="st-syn-mode">' + (s.mode === "llm" ? "LLM" : "ENGINE") + '</span>' +
      (conf ? '<span class="st-syn-conf">' + conf + ' conf</span>' : "") + "</div>" +
      '<p class="st-syn-assess">' + stripTags(s.assessment || rec.action || "") + "</p>" +
      (note ? '<p class="st-syn-conc">' + stripTags(note) + "</p>" : "") + pmHtml + "</div>";
  }
  function selectorHtml() {
    const s = currentSelector();
    if (!s || s.available === false || s.action === "na") return "";
    const pill = { hold: "HOLD", switch: "SWITCH", off_script: "OFF-SCRIPT" }[s.action] || "—";
    return '<div class="st-rec sr-' + (s.status || "ok") + '">' +
      '<span class="sr-pill">' + pill + '</span>' +
      '<span class="sr-val">' + stripTags(s.value || s.target_label || "") + '</span>' +
      (s.confidence_label ? '<span class="sr-conf">' + s.confidence_label + ' conf</span>' : "") +
      (s.why ? '<span class="sr-why">' + stripTags(s.why) + '</span>' : "") + "</div>";
  }
  function deviationHtml() {
    const d = currentDeviation();
    if (!d || d.available === false) return "";
    const status = d.status || "na";
    const pct = d.along_pct != null ? Math.max(0, Math.min(100, d.along_pct)) : null;
    const metric = (lbl, val, bad) => '<span class="st-m' + (bad ? " bad" : "") + '">' + lbl + ' <b>' + val + '</b></span>';
    const off = status !== "ok";
    const m = [];
    if (pct != null) {
      const dist = (d.along_nm != null && d.route_nm != null) ? " · " + r1(d.along_nm) + "/" + r1(d.route_nm) + " nm" : "";
      m.push(metric("Along", pct + "%" + dist, false));
    }
    if (d.xte_nm != null) {
      const arrow = d.xte_trend === "diverging" ? " ↗" : d.xte_trend === "converging" ? " ↘" : "";
      m.push(metric("XTE", r1(d.xte_nm) + " nm " + (d.xte_side || "") + arrow, off && (d.xte_trend !== "converging")));
    }
    const bt = behindTxt(d.time_behind_s);
    if (bt) m.push(metric("Pace", bt, d.time_behind_s > 0 && off));
    if (d.vmc_kn != null && d.vmc_optimal_kn != null)
      m.push(metric("VMC", r1(d.vmc_kn) + "/" + r1(d.vmc_optimal_kn) + " kts", d.vmc_deficit_kn != null && d.vmc_deficit_kn > 0.3));
    const prog = pct != null
      ? '<div class="st-progress"><div class="st-fill" style="width:' + pct + '%"></div><div class="st-boat" style="left:' + pct + '%"></div></div>' : "";
    return '<div class="st-dev" style="--c:var(--' + (status === "na" ? "na" : status) + ')">' +
      '<div class="st-dev-head">TRACK — variant ' + stripTags(d.variant_label || d.variant || "—") + "</div>" +
      prog + (m.length ? '<div class="st-metrics">' + m.join("") + "</div>" : "") +
      (d.why || d.sub ? '<p class="st-why">' + stripTags(d.why || d.sub) + "</p>" : "") +
      (d.what_flips_it ? '<p class="st-flip">Branch trigger — <b>' + stripTags(d.what_flips_it) + "</b></p>" : "") + "</div>";
  }
  function driftHtml() {
    const fd = currentDrift();
    if (!fd || fd.available === false) return "";
    const status = fd.status || "na";
    const deg = fd.drift_twd_deg != null ? Math.round(fd.drift_twd_deg) : null;
    const tag = { ok: "holding", watch: "watch", act: "moved" }[status] || "";
    let body;
    if (status === "ok") {
      body = 'Forecast <b>holding</b> — ~' + (deg != null ? deg + "° drift" : "on plan");
    } else {
      const tws = (fd.drift_tws_kn != null && Math.abs(fd.drift_tws_kn) >= 1)
        ? " · " + (fd.drift_tws_kn >= 0 ? "+" : "−") + Math.abs(Math.round(fd.drift_tws_kn)) + " kts" : "";
      body = 'Forecast drift <b>' + deg + "° " + (fd.drift_dir || "shifted") + '</b>' + tws +
        ' <span class="sd-tag">' + tag + '</span>';
    }
    return '<p class="st-drift sd-' + status + '"><span class="sd-dot"></span><span>' + body + '</span></p>';
  }
  function playsHtml() {
    const pl = currentPlays();
    if (!pl || pl.available === false) return "";
    const rows = (pl.plays || []).filter((x) => x.status !== "quiet").slice(0, 4);
    const out = new Set(((pl.sail_state || {}).out_of_service) || []);
    const gear = GEAR.map((s) =>
      `<button class="st-gear${out.has(s) ? " oos" : ""}" onclick="toggleGear('${s}')" ` +
      `title="Tap = declare the ${s} ${out.has(s) ? "back in service" : "OUT of service (blown/damaged)"}">${s}${out.has(s) ? "✕" : ""}</button>`).join("");
    const items = rows.map((x) => {
      const call = x.guidance || x.summary || "";
      const badge = x.status === "armed" ? "ARMED" : "arming…";
      return `<div class="st-play y-${x.status === "armed" ? "act" : "watch"}">` +
        `<b>${x.name}</b> <span class="st-play-badge">${badge}</span>` +
        (x.stakes_min ? ` <span class="st-play-stakes">~${x.stakes_min}m at stake</span>` : "") +
        (x.corroborated ? ` <span class="st-play-stakes" title="A confidence-raising signal agrees — it never gates arming">✓ ${x.corroborated_by || "corroborated"}</span>` : "") +
        (call ? `<span class="st-play-call">${call}</span>` : "") + `</div>`;
    }).join("");
    return `<div class="st-plays"><div class="st-plays-head"><span>PLAYS</span><span class="st-gearbox" ` +
      `title="Gear out-of-service toggles — arms the pre-authored gear-loss plays">gear: ${gear}</span></div>` +
      (items || `<div class="st-play-none">No plays armed — the library is watching ` +
       `${pl.n_plays ?? 0} conditions.</div>`) + `</div>`;
  }
  function reoptHtml() {
    const sel = currentSelector();
    const syn = currentSynthesis();
    const offscript = sel && sel.action === "off_script";
    const synRec = (syn && syn.recommendation) || {};
    const synOff = synRec.vs_playbook === "departs" || synRec.vs_playbook === "off-book";
    const ro = (syn && syn.reoptimize && syn.reoptimize.available) ? syn.reoptimize : currentReoptimize();
    const show = offscript || synOff || App.src !== "live";
    if (!ro || ro.available === false || !show) return "";
    const h = ro.eta_min != null ? (Math.floor(ro.eta_min / 60) + "h " + Math.round(ro.eta_min % 60) + "m") : "?";
    const vs = ro.vs_playbook && ro.vs_playbook.available ? "up to " + r1(ro.vs_playbook.max_divergence_nm) + " nm off the plan" : "";
    const sp = Array.isArray(ro.sail_plan) && ro.sail_plan.length ? ro.sail_plan.join("→") : "";
    const avb = [];
    if (ro.avoids_islands) avb.push(ro.avoids_islands + " island" + (ro.avoids_islands > 1 ? "s" : ""));
    if (ro.avoids_zones) avb.push(ro.avoids_zones + " zone" + (ro.avoids_zones > 1 ? "s" : ""));
    return '<p class="st-reopt"><span class="ro-ico">⟳</span><span>Onboard re-route ready — <b>' + h + '</b>' +
      (ro.tacks != null ? ' · ' + ro.tacks + ' tacks' : '') +
      (ro.sailed_nm != null ? ' · ' + r1(ro.sailed_nm) + ' nm' : '') +
      (sp ? ' · ⛵ ' + sp : '') +
      (avb.length ? ' · avoids ' + avb.join(" + ") : '') +
      (vs ? ' · ' + vs : '') + ' <span class="ro-tag">off-book</span></span></p>';
  }
  function strategyStackHtml() {
    const parts = [synthHtml(), selectorHtml(), deviationHtml(), driftHtml(), playsHtml(), reoptHtml()]
      .filter(Boolean);
    if (!parts.length) return "";
    return '<div class="st-stack">' + parts.join("") + "</div>";
  }
  /* the coach line in the commentary panel — the last thing the copilot volunteered (from the
     timer's callout history), or, if nothing's been shown yet, the top of the current callout banner.
     Hidden when there's nothing to coach or off the live source. */
  function renderCoach() {
    const el = document.getElementById("coachLine");
    if (!el) return;
    const c = App.coach;
    if (App.src !== "live" || !c) { el.hidden = true; return; }
    let text = "", ago = "";
    if (c.history && c.history.length && c.history[0].spoken) {
      text = c.history[0].spoken; ago = agoText(c.history[0].at);
    } else if (c.active && c.active.length) {
      const a = c.active[0];
      text = a.headline + (a.detail ? " — " + a.detail : "");
    }
    if (!text) { el.hidden = true; return; }
    document.getElementById("coachSay").textContent = text;
    document.getElementById("coachWhen").textContent = ago;
    el.hidden = false;
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
    openTile: null, streamTimer: null, pollTimer: null, seriesTimer: null, briefTimer: null,
    adhereTimer: null, coachTimer: null, devTimer: null, driftTimer: null, selTimer: null, polling: false,
    dwell: {}, data: null, windHist: [], fcstHist: [], seriesHist: [], lastPersist: 0, brief: null,
    coach: null, deviation: null, forecastDrift: null, selector: null, reoptimize: null,
    detailStreamKey: null, detailAbort: null,
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
    renderCoach();
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
    } else if (key === "playbook") {
      // the full strategy stack lives here now (synthesis → selector → track → drift → plays →
      // re-route), followed by the variant-agreement rows
      g.innerHTML = strategyStackHtml() + (t.rows ? rowsHtml(t.rows) : "");
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
    const whyEl = document.getElementById("detWhy");
    if (App.src === "demo") {
      if (stream) streamWhy(why); else { stopStream(); whyEl.textContent = why; }
    } else if (stream) {
      // tile just opened: show the deterministic read instantly, then let the LLM stream over it
      stopStream(); whyEl.textContent = why;
      if (t.status !== "na") streamDetail(key, "");
    } else if (App.detailStreamKey !== key) {
      // periodic re-render of a tile the LLM isn't streaming → keep the deterministic read fresh
      whyEl.textContent = why;
    }
    // else: re-render while the LLM stream owns this tile → leave WHY as-is
  }
  function closeDetail() {
    stopStream();
    App.detailStreamKey = null;
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
  const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  function stopStream() {
    if (App.streamTimer) { clearInterval(App.streamTimer); App.streamTimer = null; }
    if (App.detailAbort) { try { App.detailAbort.abort(); } catch (e) {} App.detailAbort = null; }
  }
  /* stream the copilot's scoped deep-dive for one tile into the WHY slot, token-by-token. The
     deterministic WHY shows first and is only replaced once real tokens arrive (so a slow/absent
     LLM leaves the engine read in place). */
  function streamDetail(key, question) {
    App.detailStreamKey = key;
    if (App.detailAbort) { try { App.detailAbort.abort(); } catch (e) {} }
    const ctl = new AbortController(); App.detailAbort = ctl;
    const el = document.getElementById("detWhy");
    const snap = TILES.map((k) => { const t = (App.data && App.data.tiles[k]) || {}; return { key: k, name: NAME[k], value: tileText(t), sub: t.sub || "", status: t.status || "na" }; });
    fetch(COPILOT + "/detail", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ domain: key, question: question || "", tiles: snap }), signal: ctl.signal })
      .then((resp) => {
        if (!resp.ok || !resp.body) return;
        const reader = resp.body.getReader(), dec = new TextDecoder();
        let acc = "", started = false;
        const pump = () => reader.read().then(({ done, value }) => {
          if (App.openTile !== key) { try { reader.cancel(); } catch (e) {} return; }
          if (done) { if (started) el.textContent = acc.trim(); return; }
          acc += dec.decode(value, { stream: true });
          if (acc.trim()) { started = true; el.innerHTML = escapeHtml(acc.trim()) + ' <span class="caret"></span>'; }
          return pump();
        });
        return pump();
      })
      .catch(() => { /* keep the deterministic WHY already shown */ });
  }

  /* ============================ live polling ============================ */
  async function poll() {
    if (App.polling) return;
    App.polling = true;
    try {
      const eps = ["/conditions", "/sail", "/navigator", "/tactics", "/fatigue", "/forecast?hours=6", "/sources", "/ais", "/fleet"];
      const keys = ["conditions", "sail", "navigator", "tactics", "fatigue", "forecast", "sources", "ais", "fleet"];
      const ms   = [5000, 5000, 5000, 5000, 5000, 9000, 5000, 5000, 6000];
      const res = await Promise.all(eps.map((e, i) => fetchJSON(e, ms[i])));
      const p = {}; keys.forEach((k, i) => (p[k] = res[i]));
      pushWind(p.conditions);
      pushForecast(p.forecast);
      App.data = buildLive(p);
      if (App.src === "live") render();
    } finally { App.polling = false; }
  }
  function startPolling() { poll(); if (!App.pollTimer) App.pollTimer = setInterval(poll, 3000); }

  /* ============================ audio ALERT signal ============================
     Narration is VISUAL — but a screen can't grab the eye when the crew is looking at the water. So a
     new SAFETY/urgent callout also fires a short synthesized attention TONE (no speech — a signal that
     says "look at the screen", which cuts through wind noise better than words). Client-only; it hooks
     the /coach `new` stream (already deduped + priority-sorted on the Orin) and de-dups by callout id
     (status is baked into the id, so a watch→act escalation re-alerts). iOS Safari blocks audio until a
     user gesture, so it's armed by the 🔔 toggle (which also plays a test chime). Live source only. */
  const Sound = { on: localStorage.getItem("sr33.dash.sound") === "on", ctx: null, alerted: new Set() };
  function audioCtx() {
    if (!Sound.ctx) { const AC = window.AudioContext || window.webkitAudioContext; if (AC) Sound.ctx = new AC(); }
    if (Sound.ctx && Sound.ctx.state === "suspended") Sound.ctx.resume();
    return Sound.ctx;
  }
  function tone(freq, startMs, durMs, peak) {
    const ctx = Sound.ctx; if (!ctx) return;
    const t0 = ctx.currentTime + startMs / 1000, t1 = t0 + durMs / 1000;
    const osc = ctx.createOscillator(), g = ctx.createGain();
    osc.type = "sine"; osc.frequency.value = freq;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(peak, t0 + 0.012);   // fast attack
    g.gain.exponentialRampToValueAtTime(0.0001, t1);         // decay to silence
    osc.connect(g).connect(ctx.destination);
    osc.start(t0); osc.stop(t1 + 0.02);
  }
  function playAlert(level) {
    if (!audioCtx()) return;
    if (level === "act") { tone(784, 0, 110, 0.5); tone(988, 150, 110, 0.5); tone(1319, 300, 150, 0.55); }  // 3 rising beeps
    else { tone(659, 0, 120, 0.32); tone(880, 140, 160, 0.32); }                                            // soft two-note chime
  }
  function alertLevel(c) {   // which callouts are worth a sound (avoid alert fatigue: only the top tiers)
    if (!c) return null;
    if (c.urgency === "now") return "act";       // act-level: collision/rounding/branch firing NOW
    if (c.category === "safety") return "watch"; // a closing contact not yet in the guard
    return null;                                 // routine coaching stays silent (visual only)
  }
  function maybeAlert(coach) {
    if (!Sound.on || App.src !== "live" || !coach) return;
    let level = null;
    for (const c of (coach.new || [])) {
      const lvl = alertLevel(c);
      if (!lvl || Sound.alerted.has(c.id)) continue;
      Sound.alerted.add(c.id);
      if (lvl === "act") level = "act"; else if (level !== "act") level = "watch";
    }
    if (level) playAlert(level);
  }
  function toggleSound() {
    Sound.on = !Sound.on;
    localStorage.setItem("sr33.dash.sound", Sound.on ? "on" : "off");
    document.getElementById("alertLbl").textContent = Sound.on ? "ON" : "OFF";
    document.getElementById("alertBtn").classList.toggle("armed", Sound.on);
    if (Sound.on) { audioCtx(); playAlert("watch"); }   // the tap unlocks iOS audio + confirms it works
  }

  /* ============================ controls ============================ */
  function cycleSource() {
    if (App.src === "live") { App.src = "demo"; App.demoScn = "calm"; }
    else if (App.demoScn === "calm") { App.demoScn = "escalated"; }
    else { App.src = "live"; }
    document.getElementById("srcLbl").textContent = App.src === "live" ? "LIVE" : "DEMO·" + App.demoScn;
    renderSailsBar();
    if (!document.getElementById("detail").hidden) closeDetail();
    render();
  }
  function briefMe() {
    const b = document.getElementById("briefBtn");
    b.classList.add("busy"); b.textContent = "thinking…";
    if (App.src === "live") { poll(); fetchBrief(); fetchCoach(); fetchDeviation(); fetchDrift(); fetchSelector(); fetchSynthesis(); fetchPlays(); fetchSailState(); }
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
    fetchCoach();                   // proactive auto-coach held state (no recompute), then on its own cadence
    App.coachTimer = setInterval(fetchCoach, COACH_EVERY);
    fetchDeviation();               // route-deviation (engine, deterministic), then on its own cadence
    App.devTimer = setInterval(fetchDeviation, DEV_EVERY);
    fetchDrift();                   // forecast-drift (engine, common-data), then on its own cadence
    App.driftTimer = setInterval(fetchDrift, DRIFT_EVERY);
    fetchSelector();                // branch selector (engine, unified recommendation), own cadence
    App.selTimer = setInterval(fetchSelector, DEV_EVERY);
    fetchSynthesis();               // in-race strategy synthesis (copilot LLM → engine fallback), own cadence
    App.synTimer = setInterval(fetchSynthesis, SYN_EVERY);
    fetchPlays();                   // Phase-D play matcher (engine, deterministic), own cadence
    fetchSailState();               // the SAILS bar (crew sail configuration), own cadence
    App.sailTimer = setInterval(fetchSailState, 12000);
    fetchSession();                 // the RACE LOG control, own cadence
    App.sessionTimer = setInterval(fetchSession, 15000);
    App.playsTimer = setInterval(fetchPlays, SYN_EVERY);
    document.getElementById("themeBtn").addEventListener("click", cycleTheme);
    document.getElementById("srcBtn").addEventListener("click", cycleSource);
    document.getElementById("recBtn").addEventListener("click", toggleRec);
    document.getElementById("alertBtn").addEventListener("click", toggleSound);
    // reflect a persisted armed state, and re-unlock iOS audio on the first interaction after a reload
    document.getElementById("alertLbl").textContent = Sound.on ? "ON" : "OFF";
    document.getElementById("alertBtn").classList.toggle("armed", Sound.on);
    ["click", "touchend"].forEach((ev) =>
      document.addEventListener(ev, () => { if (Sound.on) audioCtx(); }, { once: true, passive: true }));
    document.getElementById("briefBtn").addEventListener("click", briefMe);
    document.getElementById("detBack").addEventListener("click", closeDetail);
    document.getElementById("overlay").addEventListener("click", closeDetail);
    const ask = () => {
      const inp = document.getElementById("detAsk");
      const q = inp.value.trim();
      if (!q || !App.openTile) return;
      inp.value = "";
      if (App.src === "live") streamDetail(App.openTile, q);   // scoped, grounded, streamed answer
      else document.getElementById("detWhy").textContent = "(scoped follow-up runs live against the onboard copilot) — you asked: " + q;
    };
    document.getElementById("detSend").addEventListener("click", ask);
    document.getElementById("detAsk").addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });
    setInterval(() => { if (App.theme === "auto") applyTheme(); }, 25000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
