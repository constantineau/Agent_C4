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
  const TILES = ["wind", "playbook", "forecast", "sail", "eta", "ais", "charge", "data", "bank"];
  // "charge" stayed the internal id when the tile grew into CREW (energy + the watch system) —
  // demo scenarios, CSS and the copilot brief all key on it. Note that "charge" is the CREW's
  // tank, not the boat's: the house bank is "bank", added 2026-09-08.
  //
  // BANK is the 9th tile on an 8-cell grid. Rather than reflow the 4x2 the crew has learned,
  // the two SYSTEMS reads — "are the instruments telling the truth" (data) and "will the boat
  // still be powered in six hours" (bank) — share the last cell, stacked. Both are a single
  // number plus a line, neither carries a chart, and they are the two tiles you want next to
  // each other: on Jul 18 the flat bank is what killed the instruments.
  const PAIR = ["data", "bank"];
  const NAME = {
    wind: "TWS Trend", playbook: "Playbook", forecast: "Forecast",
    sail: "Sail", eta: "Time to Mark", ais: "Fleet", charge: "Crew", data: "Data",
    bank: "House Bank", coach: "Coach", checklist: "Race Checklist", route: "Route",
  };
  /* The grid's cells. ROUTE (#125, the Re-route now button) shares the TIME TO MARK cell, the same
     way the two SYSTEMS reads share the last one: the 4x2 the crew has learned stays put, and the
     two course reads sit together. ROUTE isn't in TILES: it isn't built from the 3 s poll and never
     raises a status (a suggestion is quiet text, not an alarm). */
  const CELLS = ["wind", "playbook", "forecast", "sail", ["eta", "route"], "ais", "charge", PAIR];
  const RT = window.C4Reroute;
  // the re-route status poll: ~every 10 s, every 3 s while one runs (RT.pollEvery)
  const boatName = (r) => { const n = r.boat || r.name || ("MMSI " + (r.mmsi || "?")); return n.length > 14 ? n.slice(0, 13) + "…" : n; };
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
  const srcMark = (b) => b.source === "tracker"
    ? " ⌛" + (b.age_s != null ? Math.round(b.age_s / 60) + "m" : "trk") + (b.dr_nm ? "→DR" : "")
    : b.source === "ais" ? "" : b.source === "own" ? "" : b.source == null ? " ·no fix" : "";
  const ordinal = (n) => { const v = n % 100; return n + (v >= 11 && v <= 13 ? "th" : ["th", "st", "nd", "rd"][n % 10] || "th"); };

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
  const CHK_EVERY = 15000;        // poll the race checklist ~every 15 s (engine-deterministic triggers; the bar only shows when something is due)
  /* The bank moves over hours (a 45-min trend window, 0.7 Hz source) and the health checks read
     three 20-min series — neither belongs in the 3 s loop on a Pi 4. 10 s is still ~2,000 reads
     per race and nothing either one measures can change meaningfully in between. */
  const POWER_EVERY = 10000;
  const HEALTH_EVERY = 10000;
  /* Readout HOLD — see hold() below. Bayview Mackinac 2026: readouts flapped between a value
     and "no data" all race. The archive shows that was never the sensors or the link (every
     strip path recorded 12,990 samples, one per second, zero gaps over 1 s) — it was this poll
     turning a slow ENGINE response into a blank tile. */
  const HOLD_MS = 30000;          // keep showing the last good reading this long after a poll fails
  const STALE_AFTER_MS = 7000;    // ...and show its age once it is older than ~2 missed polls
  /* LINK state. An iPad that locks or backgrounds Safari freezes the poll; on wake the Wi-Fi takes
     a few seconds to rejoin, so the first polls fail and every held value is already past HOLD_MS.
     That read as "Engine unreachable" with blank tiles even though nothing was wrong. So after a
     wake, or a fresh poll that got nothing back, say "Reconnecting" and keep the last values (with
     their age) for a grace window; only call the link lost after LOST_AFTER_MS of silence. */
  const RECONNECT_GRACE_MS = 20000;
  const LOST_AFTER_MS = 20000;

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

  /* ---- watch-system formatting (CREW tile) ---- */
  // the watch read (status, countdown, CONSIDER) is pure and tested: crewwatch.js (#80)
  const CW = window.C4CrewWatch;
  const fmtHM = CW.fmtHM, fmtMins = CW.fmtMins;
  const teamTxt = (w, id) => { const t = ((w || {}).teams || {})[id]; return t ? t.name + ((t.members || []).length ? " (" + t.members.join(", ") + ")" : "") : id; };
  /* a canned watch read for the demo scenarios: 4-on/4-off, mid-block on calm, 4 min from the
     change on escalated (exercises the T-5 act state + the wake-the-next-team consider) */
  function genDemoWatch(esc) {
    const now = Math.floor(Date.now() / 1000);
    const chgS = (esc ? 4 : 110) * 60;
    let t = now + chgS - 4 * 3600, on = "B";
    const blocks = [];
    for (let i = 0; i < 6; i++) { blocks.push({ start: t, end: t + 4 * 3600, on: on, note: null }); t += 4 * 3600; on = on === "A" ? "B" : "A"; }
    return { plan_set: true, active: true, on: "B", on_label: "Stbd", all_hands: false,
      next_change: now + chgS, next_on: "A", next_on_label: "Port", mins_to_change: Math.round(chgS / 60),
      teams: { A: { name: "Port", members: ["Grant", "Elise"] }, B: { name: "Stbd", members: ["CJ", "Mo"] } },
      blocks: blocks, upcoming: blocks.filter((b) => b.start > now).slice(0, 3) };
  }
  /* who the tile trusts for the watch read: canned in demo, the engine /watch poll live */
  function watchNow() { return App.src === "demo" ? (SCENARIOS[App.demoScn] || {}).watch || null : App.watch; }
  /* escalation: T-15 = watch (wake the next team), T-5 = act (hand over) — act only while a
     block is on; before the first block the start of watches caps at watch (crewwatch.js) */
  const watchTileStatus = CW.status, watchSubTxt = CW.subTxt;

  /* Tile-sized TWS trend: the last 3.5 h with 30-min gridlines (−210 … now, crew request
     2026-07-08), the TWS line + min/max, and a direction-arrow strip sampled at each 30-min
     point. Fits the tile face; the tap-detail keeps the full-race chart. */
  function twsTrendTile(hist) {
    if (!hist || hist.length < 5) return "";
    const now = hist[hist.length - 1].t;
    const WIN = 210 * 60;
    const h = hist.filter((p) => p.t >= now - WIN);
    if (h.length < 5) return "";
    const w = 232, ht = 66, padL = 24, padR = 8, padT = 6, padB = 14;
    const t0 = now - WIN, span = WIN;
    const tws = h.map((p) => p.tws);
    let lo = Math.min.apply(null, tws), hi = Math.max.apply(null, tws);
    if (hi - lo < 3) { const m = (hi + lo) / 2; lo = m - 1.5; hi = m + 1.5; }
    lo = Math.max(0, Math.floor(lo)); hi = Math.ceil(hi);
    const X = (t) => padL + ((t - t0) / span) * (w - padL - padR);
    const Y = (v) => padT + (1 - (v - lo) / ((hi - lo) || 1)) * (ht - padT - padB);
    let gridv = "", ticks = "";
    const arrows = [];
    for (let m = 210; m >= 0; m -= 30) {
      const t = now - m * 60, x = X(t).toFixed(1);
      gridv += '<line class="ax-grid" x1="' + x + '" y1="' + padT + '" x2="' + x + '" y2="' + (ht - padB) + '"/>';
      if (m % 60 === 0)          // hourly labels only — the 30-min gridlines + arrows mark the rest
        ticks += '<text class="ax-lab" x="' + x + '" y="' + (ht - 3) + '" text-anchor="middle">' +
                 (m === 0 ? "now" : "−" + (m / 60) + "h") + '</text>';
      let best = null;
      for (const p of h) if (best === null || Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
      arrows.push(best && Math.abs(best.t - t) <= 900
        ? '<span class="tt-a" title="−' + m + ' min · ' + r0(best.tws) + ' kts">' + windArrow(best.twd) + '</span>'
        : '<span class="tt-a tt-gap">·</span>');
    }
    const ylab = '<text class="ax-lab" x="' + (padL - 3) + '" y="' + (Y(hi) + 3).toFixed(1) + '" text-anchor="end">' + hi + '</text>' +
                 '<text class="ax-lab" x="' + (padL - 3) + '" y="' + (Y(lo) + 3).toFixed(1) + '" text-anchor="end">' + lo + '</text>';
    const pts = dsample(h, 90).map((p) => X(p.t).toFixed(1) + "," + Y(p.tws).toFixed(1)).join(" ");
    const last = h[h.length - 1];
    return '<div class="tt-wrap"><svg class="rchart" viewBox="0 0 ' + w + " " + ht + '" width="100%" preserveAspectRatio="xMidYMid meet">' +
      gridv + ylab + '<polyline class="rc-line" points="' + pts + '"/>' +
      '<circle class="rc-dot" cx="' + X(last.t).toFixed(1) + '" cy="' + Y(last.tws).toFixed(1) + '" r="2.5"/>' + ticks +
      '</svg><div class="tt-arrows">' + arrows.join("") + '</div></div>';
  }

  /* ============================ DEMO scenarios (canned) ============================ */
  const SCENARIOS = {
    calm: {
      mode: "llm-live", focus: "In the groove. Left is paying.", confidence: "high",
      watch: genDemoWatch(false),
      notes: [
        { tile: "playbook", status: "ok", text: "Left paid the last two shifts. Stay left, tack on the next header.", conf: "high" },
        { tile: "sail",     status: "ok", text: "J1 is right. Hold the groove.", conf: "high" },
      ],
      tiles: {
        wind:    { status: "ok", value: arrowKts(250, 12), sub: "16→12 kts · 8° right", chart: twsTrendTile(genDemoRace(false)),
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(250, 12)] }, { label: "−60 min", cols: [arrowKts(246, 14)] }, { label: "−120 min", cols: [arrowKts(242, 16)] }],
                   why: "Arrows point downwind (a north wind points down). Wind eased from 16 to 12 kts.", consider: "Wind is oscillating. Play the shifts.", clears: "—", based: ["boat wind log"], conf: "high" },
        playbook:{ status: "ok", value: "On plan: Left", sub: "oscillating ±6°",
                   rows: [{ hdr: true, cols: ["agree", ""] }, { label: "★ Left", emph: true, cols: ["52%", "start · now"] }, { label: "Middle", cols: ["28%", ""] }, { label: "Right", cols: ["20%", ""] }],
                   why: "Start left (52% of forecasts). Wind is oscillating.", consider: "Stay with left, play the shifts.", clears: "—", based: ["playbook: left", "52% of forecasts"], conf: "high" },
        forecast:{ status: "ok", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(250, 10)] }, { label: "+60 min", cols: [arrowKts(256, 13)] }, { label: "+120 min", cols: [arrowKts(262, 15)] }, { sep: true, label: "FORECAST VS. ACTUAL" }, { hdr: true, cols: ["forecast", "actual"] }, { label: "−60 min", cols: [arrowKts(244, 13), arrowKts(243, 14)] }, { label: "−120 min", cols: [arrowKts(240, 16), arrowKts(238, 17)] }],
                   why: "Building to ~15 kts, wind shifting right. Matching within 1 kt.", consider: "Plan sails for more wind.", clears: "—", based: ["forecast + boat wind"], conf: "high" },
        sail:    { status: "ok", value: "J1", sub: "in range · no change ahead", why: "You set J1. Right for 12 kts upwind.", consider: "No change.", clears: "TWS > 16 kts", based: ["you set J1", "best sail J1"], conf: "high" },
        eta:     { status: "ok", value: "16 min", sub: "Cove Island", why: "~16 min to Cove Island at current speed.", consider: "On schedule for the mark.", clears: "—", based: ["ETA 16 min"], conf: "high" },
        ais:     { status: "ok", value: "2nd of 4 (div)", sub: "▲ Defiance 0:40 · ▽ Windquest 1:50", rows: [{ hdr: true, cols: ["to fin", "Δ corrected"] }, { label: "1. Il Mostro ⌛17m", cols: ["1.9 nm", "▲ 3:10 ahead"] }, { label: "2. ◆ Defiance", cols: ["3.4 nm", "▲ 0:40 ahead"] }, { label: "3. ◆ C4 (us)", emph: true, cols: ["3.2 nm", "—"] }, { label: "4. ◆ Windquest", cols: ["3.1 nm", "▽ 1:50 back"] }, { label: "5. Vayu ⌛22m→DR", cols: ["4.8 nm", "▽ 4:05 back"] }], why: "Estimated corrected standings, 5 boats. 2 on AIS, 2 from the tracker (delayed, 1 estimated forward). ◆ = our division (B).", consider: "Chase Defiance. Stay ahead of Windquest.", clears: "—", based: ["5 of 5 boats ranked", "AIS 2 · tracker 2 · estimated 1"], conf: "high" },
        charge:  { status: "ok", value: '72<div class="t-val2">Stbd on</div>', energy: 72, level: "fresh", sub: "change 1.8h → Port · fresh", why: "Crew energy 72% (fresh). Stbd on deck. Port takes over in 1.8 h.", consider: "Driver fresh — no rotation needed. Next watch change 1.8h out.", clears: "—", based: ["helm steering: energy 72%", "watch plan: Stbd on · Port next"], conf: "high" },
        data:    { status: "ok", value: "5", sub: "5 sources live · 2 on backup", why: "5 sensor groups reporting. All sensor checks pass.", consider: "Instruments agree. Safe to use.", clears: "—", based: ["5 sources", "checks: all pass"], conf: "high" },
        bank:    { status: "ok", value: "12.68 V", sub: "-0.04 V/h · ~42 h to 11.0 V", why: "Bank 12.68 V, -0.04 V/h. Reaches 11.0 V in about 42 h.", consider: "Bank healthy.", clears: "—", based: ["12.68 V, -0.04 V/h"], conf: "high" },
      },
      // the two SYSTEMS endpoints behind the DATA chip and the BANK tile, in their healthy shape
      power: { available: true, status: "ok", volts: 12.68, volts_decided: 12.69, volts_last: 12.66,
        volts_min: 12.61, sustain_min: 10,
        trend_v_per_h: -0.04, hours_to_floor: 42.0, charging: false, samples: 1890, window_min: 45,
        path: "electrical.batteries.0.voltage", reason: "bank 12.68 V, -0.04 V/h",
        thresholds: { warn_v: 12.0, danger_v: 11.6, floor_v: 11.0, drain_warn_v_per_h: 0.15 } },
      // Note the standing `policy_unmet` note: the Orca Core is ranked first for heel and
      // heading and published neither during the Jul 18 race, so the calm scenario shows the
      // real thing — an ok chip that is still explicit about running on the 24xd.
      health: { available: true, status: "ok", reason: "instruments cross-check clean", flags: [],
        notes: ["2 channel(s) never see their ranked lead (heading_true→orca, heel→orca) — " +
                "the policy names a device that does not publish them"], window_min: 10,
        attitude: { ok: true, status: "ok", reason: "attitude within limits", roll_deg: 14.8, pitch_deg: 1.7 },
        heading: { available: true, status: "ok", bias_deg: -2.1, spread_deg: 4.3, samples: 1180,
          reference: { heading: "GPS24xd-NMEA2000", course: "Orca Core", independent: true },
          note: null, reason: "heading within -2° of GPS course (vs Orca Core)" },
        provenance: { available: true, status: "ok", flags: [],
          notes: ["2 channel(s) never see their ranked lead (heading_true→orca, heel→orca) — " +
                  "the policy names a device that does not publish them"],
          checks: {
            policy_binds: { status: "ok", unresolvable: [], reason: "all 9 priority matchers bind" },
            lead_source: { status: "ok", unranked: [], went_silent: [],
              policy_unmet: [{ channel: "heading_true", expected: "orca", using: "GPS24xd-NMEA2000", age_s: 1.0 },
                              { channel: "heel", expected: "orca", using: "GPS24xd-NMEA2000", age_s: 1.0 }],
              reason: "every ranked channel is on its rank-1 device",
              note: "2 channel(s) never see their ranked lead (heading_true→orca, heel→orca) — " +
                    "the policy names a device that does not publish them" },
            own_ship: { status: "ok", ais_excluded: ["n2k-socketcan.43"], ais_leading: [],
              synthetic: [], reason: "AIS filter excluding n2k-socketcan.43 from own-ship reads" },
          },
          channels: {
            tws: { source: "n2k-socketcan.15", device: "Orca Core", value: 12.1, unit: "kn", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 2, lead_publishes: true },
            twd: { source: "n2k-socketcan.15", device: "Orca Core", value: 250, unit: "°", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 2, lead_publishes: true },
            aws: { source: "n2k-socketcan.0", device: "GND10", value: 14.2, unit: "kn", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 2, lead_publishes: true },
            awa: { source: "n2k-socketcan.0", device: "GND10", value: -38.4, unit: "°", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 2, lead_publishes: true },
            stw: { source: "n2k-socketcan.4", device: "GST10", value: 7.2, unit: "kn", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 1, lead_publishes: true },
            sog: { source: "n2k-socketcan.3", device: "GPS24xd-NMEA2000", value: 7.4, unit: "kn", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 2, lead_publishes: true },
            cog: { source: "n2k-socketcan.3", device: "GPS24xd-NMEA2000", value: 12.5, unit: "°", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 2, lead_publishes: true },
            heading_true: { source: "n2k-socketcan.3", device: "GPS24xd-NMEA2000", value: 14.3, unit: "°", age_s: 1.0, rank: 2, fell_back: true, measured: true, sources: 1, lead_publishes: false },
            heel: { source: "n2k-socketcan.3", device: "GPS24xd-NMEA2000", value: 14.8, unit: "°", age_s: 1.0, rank: 2, fell_back: true, measured: true, sources: 2, lead_publishes: false },
            depth: { source: "n2k-socketcan.2", device: "Intelliducer Thru-hull", value: 26.2, unit: "m", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 1, lead_publishes: true },
            bank_voltage: { source: "n2k-socketcan.15", device: "Orca Core", value: 12.68, unit: "V", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 1, lead_publishes: true },
          } } },
      strategy: { available: true, status: "ok", variant: "left", variant_label: "Left start",
        value: "On the optimal track", xte_nm: 0.15, xte_side: "left", xte_trend: "steady",
        along_pct: 38, along_nm: 54.1, route_nm: 142.0, time_behind_s: -45,
        vmc_kn: 5.4, vmc_optimal_kn: 5.6, vmc_deficit_kn: 0.2,
        what_flips_it: "wind holds right of 020° for 2+ shifts",
        why: "On the Left start track. 0.15 nm left, 0:45 ahead of plan.",
        consider: "Stay on the plan." },
      drift: { available: true, status: "ok", value: "Forecast holding", drift_twd_deg: 6, drift_dir: "right", drift_tws_kn: 1.2, n_points: 8 },
      selector: { available: true, action: "hold", status: "ok", tier: 1, value: "Hold: Left start",
        target_label: "Left start", confidence: 0.75, confidence_label: "high",
        why: "No steady shift, forecast holding. Stay with Left start, play the shifts." },
      synthesis: { available: true, mode: "llm", confidence: "high",
        assessment: "On plan. Nothing says leave the Left start.",
        concordance: { strength: "weak", lean: "left",
          note: "Wind oscillating, fleet leaning left. Nothing to act on." },
        recommendation: { action: "Hold: Left start", vs_playbook: "on-plan", target_variant: "left",
          urgency: "monitor", confidence: "high" } },
    },
    escalated: {
      mode: "llm-live", focus: "Switch to the right. Bear-away soon. Crew getting tired.", confidence: "med",
      watch: genDemoWatch(true),
      notes: [
        { tile: "playbook", status: "act",   text: "Wind shifted right and is holding. Switch to the right side.", conf: "high" },
        { tile: "sail",     status: "act",   text: "Peel J1 → A3 before the bear-away at the gate (~4 min). Get it ready.", conf: "high" },
        { tile: "bank",     status: "act",   text: "Bank 11.58 V, falling 0.22 V/h. 2.6 h to 11.0 V. Shed load or charge.", conf: "high" },
        { tile: "charge",   status: "act",   text: "Crew energy 28%. Change drivers in the next few minutes.", conf: "med" },
        { tile: "forecast", status: "watch", text: "Forecast is 2-3 kts light. Expect more wind.", conf: "med" },
      ],
      tiles: {
        wind:    { status: "watch", value: arrowKts(262, 16), sub: "9→16 kts · 16° right", chart: twsTrendTile(genDemoRace(true)),
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(262, 16)] }, { label: "−60 min", cols: [arrowKts(252, 12)] }, { label: "−120 min", cols: [arrowKts(246, 9)] }],
                   why: "Arrows point downwind (a north wind points down). Wind built 7 kts and shifted right 16° in 2 h.", consider: "Wind shifted right and holding. Favour the right side.", clears: "the trend settles", based: ["boat wind log"], conf: "med" },
        playbook:{ status: "act", value: "Switch → Right", sub: "wind shifted right · go right",
                   rows: [{ hdr: true, cols: ["agree", ""] }, { label: "★ Left", cols: ["52%", "start"] }, { label: "Right", emph: true, cols: ["20%", "now"] }, { label: "Middle", cols: ["28%", ""] }],
                   why: "Wind shifted right and is holding. The plan says go right.", consider: "Switch to the Right plan.", clears: "the wind shifts back left", based: ["playbook: right", "52% of forecasts"], conf: "high" },
        forecast:{ status: "watch", value: null,
                   rows: [{ label: "Now", emph: true, cols: [arrowKts(262, 16)] }, { label: "+60 min", cols: [arrowKts(268, 18)] }, { label: "+120 min", cols: [arrowKts(274, 19)] }, { sep: true, label: "FORECAST VS. ACTUAL" }, { hdr: true, cols: ["forecast", "actual"] }, { label: "−60 min", cols: [arrowKts(252, 12), arrowKts(256, 14)] }, { label: "−120 min", cols: [arrowKts(248, 10), arrowKts(252, 12)] }],
                   why: "Building to ~19 kts, shifting right. Forecast ~2 kts light.", consider: "Plan for more wind than forecast.", clears: "forecast matches again", based: ["forecast + boat wind"], conf: "med" },
        sail:    { status: "act",   value: "J1 → A3", sub: "in range · at Cove Island (bear away, ~4 min)", why: "You set J1. Next leg is 135° TWA, an A3 leg.", consider: "Get the A3 ready. Peel in ~4 min.", clears: "A3 hoisted", based: ["you set J1", "best sail A3 at TWA 135°"], conf: "high" },
        eta:     { status: "watch", value: "4 min", sub: "Cove Island", why: "~4 min to Cove Island at current speed.", consider: "Mark in ~4 min. Prep the rounding.", clears: "after the rounding", based: ["ETA 4 min"], conf: "high" },
        ais:     { status: "watch", value: "2nd of 4 (div)", sub: "▲ Defiance 1:20 · ▽ Windquest 2:10", rows: [{ hdr: true, cols: ["to fin", "Δ corrected"] }, { label: "1. ◆ Defiance", cols: ["2.9 nm", "▲ 1:20 ahead"] }, { label: "2. ◆ C4 (us)", emph: true, cols: ["3.0 nm", "—"] }, { label: "3. ◆ Windquest", cols: ["4.0 nm", "▽ 2:10 back"] }, { label: "4. Il Mostro ⌛31m→DR", cols: ["1.2 nm", "▽ 0:55 back"] }], why: "Estimated corrected standings, 4 boats. 2 on AIS, 1 from the tracker (delayed, estimated forward). ◆ = our division (B).", consider: "Close with Defiance on corrected. Cover at the crossings.", clears: "—", based: ["4 of 4 boats ranked", "AIS 2 · tracker 1 · estimated 1"], conf: "high" },
        charge:  { status: "act",   value: '28<div class="t-val2">Stbd on</div>', energy: 28, level: "rotate soon", sub: "change 4m → Port · rotate soon", why: "Crew energy 28% (rotate soon). Steering getting ragged, speed dropping. Port up in 4 min.", consider: "Watch change in 4m — wake Port (Grant, Elise); rotate the helm at the change.", clears: "energy back above 65%", based: ["helm steering: energy 28%", "watch plan: change in 4m → Port"], conf: "med", components: { heading: 0.7, reversals: 0.8, heel: 0.4, "spd-def": 0.5 } },
        data:    { status: "watch", value: "4", sub: "⚑ aws", why: "4 sensor groups reporting, one stale. Check failed: masthead wind silent 78 s, using Orca Core.", consider: "Don't trust the flagged reading. Check another instrument first.", clears: "the check passes again", based: ["4 sources", "checks: masthead wind silent 78 s"], conf: "med" },
        /* The Jul 18 bank, rehearsed: 11.58 V and falling 0.22 V/h with ~2.6 h to the 11.0 V
           floor. This is the state nobody could see during the real race, three hours before the
           archiver and the AIS transceiver died together at the bottom of the curve. */
        bank:    { status: "act", value: "11.58 V", sub: "-0.22 V/h · ~2.6 h to 11.0 V", why: "Bank 11.58 V, at or under 11.6 V. At this rate, 11.0 V in about 2.6 h. Below 11.0 V the recorder and AIS can fail.", consider: "Shed load and start charging. Recorder and AIS fail first.", clears: "bank above 11.6 V for over half of the last 10 min", based: ["11.58 V, -0.22 V/h", "⚠ voltage limits not checked on this boat. Trust the trend."], conf: "high" },
      },
      power: { available: true, status: "danger", volts: 11.58, volts_decided: 11.57, volts_last: 11.52,
        volts_min: 11.31, sustain_min: 10,
        trend_v_per_h: -0.22, hours_to_floor: 2.64, charging: false, samples: 1890, window_min: 45,
        path: "electrical.batteries.0.voltage",
        reason: "bank 11.58 V, at or under 11.60 V",
        thresholds: { warn_v: 12.0, danger_v: 11.6, floor_v: 11.0, drain_warn_v_per_h: 0.15 } },
      health: { available: true, status: "warn", window_min: 10,
        reason: "aws on Orca Core — ranked gnd went silent (78 s ago)",
        flags: ["aws on Orca Core — ranked gnd went silent (78 s ago)"],
        notes: ["2 channel(s) never see their ranked lead (heading_true→orca, heel→orca) — " +
                "the policy names a device that does not publish them"],
        attitude: { ok: true, status: "ok", reason: "attitude within limits", roll_deg: 27.4, pitch_deg: -2.1 },
        heading: { available: true, status: "ok", bias_deg: -4.4, spread_deg: 5.1, samples: 1150,
          reference: { heading: "GPS24xd-NMEA2000", course: "Orca Core", independent: true },
          note: null, reason: "heading within -4° of GPS course (vs Orca Core)" },
        provenance: { available: true, status: "warn",
          flags: ["aws on Orca Core — ranked gnd went silent (78 s ago)"],
          notes: ["2 channel(s) never see their ranked lead (heading_true→orca, heel→orca) — " +
                  "the policy names a device that does not publish them"],
          checks: {
            policy_binds: { status: "ok", unresolvable: [], reason: "all 9 priority matchers bind" },
            lead_source: { status: "warn", unranked: [],
              went_silent: [{ channel: "aws", expected: "gnd", using: "Orca Core", age_s: 1.0, lead_age_s: 78.0 }],
              policy_unmet: [{ channel: "heading_true", expected: "orca", using: "GPS24xd-NMEA2000", age_s: 1.0 },
                             { channel: "heel", expected: "orca", using: "GPS24xd-NMEA2000", age_s: 1.0 }],
              reason: "aws on Orca Core — ranked gnd went silent (78 s ago)",
              note: "2 channel(s) never see their ranked lead (heading_true→orca, heel→orca) — " +
                    "the policy names a device that does not publish them" },
            own_ship: { status: "ok", ais_excluded: ["n2k-socketcan.43"], ais_leading: [],
              synthetic: ["twd"], reason: "AIS filter excluding n2k-socketcan.43 from own-ship reads",
              note: "twd computed, not measured" },
          },
          channels: {
            tws: { source: "n2k-socketcan.15", device: "Orca Core", value: 16.4, unit: "kn", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 2, lead_publishes: true },
            twd: { source: "derived-data", device: null, value: 262, unit: "°", age_s: 2.0, rank: 2, fell_back: true, measured: false, sources: 2, lead_publishes: false },
            aws: { source: "n2k-socketcan.15", device: "Orca Core", value: 19.8, unit: "kn", age_s: 1.0, rank: 2, fell_back: true, measured: true, sources: 2, spread: 5.9, disagreement: true, lead_publishes: true },
            awa: { source: "n2k-socketcan.15", device: "Orca Core", value: -44.1, unit: "°", age_s: 1.0, rank: 2, fell_back: true, measured: true, sources: 2, lead_publishes: true },
            stw: { source: "n2k-socketcan.4", device: "GST10", value: 6.9, unit: "kn", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 1, lead_publishes: true },
            sog: { source: "n2k-socketcan.3", device: "GPS24xd-NMEA2000", value: 6.6, unit: "kn", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 2, lead_publishes: true },
            cog: { source: "n2k-socketcan.3", device: "GPS24xd-NMEA2000", value: 53.6, unit: "°", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 2, lead_publishes: true },
            heading_true: { source: "n2k-socketcan.3", device: "GPS24xd-NMEA2000", value: 47.2, unit: "°", age_s: 1.0, rank: 2, fell_back: true, measured: true, sources: 1, lead_publishes: false },
            heel: { source: "n2k-socketcan.3", device: "GPS24xd-NMEA2000", value: 27.4, unit: "°", age_s: 1.0, rank: 2, fell_back: true, measured: true, sources: 2, lead_publishes: false },
            depth: { source: "n2k-socketcan.2", device: "Intelliducer Thru-hull", value: 44.3, unit: "m", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 1, lead_publishes: true },
            bank_voltage: { source: "n2k-socketcan.15", device: "Orca Core", value: 11.58, unit: "V", age_s: 1.0, rank: 1, fell_back: false, measured: true, sources: 1, lead_publishes: true },
          } } },
      strategy: { available: true, status: "act", variant: "left", variant_label: "Left start",
        value: "Off track · 1.3 nm right", xte_nm: 1.3, xte_side: "right", xte_trend: "diverging",
        along_pct: 61, along_nm: 86.6, route_nm: 142.0, time_behind_s: 180,
        vmc_kn: 5.0, vmc_optimal_kn: 5.9, vmc_deficit_kn: 0.9,
        what_flips_it: "wind holds right of 020° for 2+ shifts",
        why: "1.3 nm right of the Left start track and moving away.",
        consider: "Rejoin the Left start line, or switch right if the wind has changed." },
      drift: { available: true, status: "act", value: "Forecast moved · 28° right", drift_twd_deg: 28, drift_twd_max_deg: 41, drift_dir: "right", drift_tws_kn: 3.5, n_points: 7 },
      selector: { available: true, action: "switch", status: "act", tier: 1, value: "Switch → Right start",
        target_variant: "right", target_label: "Right start", confidence: 0.9, confidence_label: "high",
        driven_by: ["get_tactics", "get_drift", "get_deviation"],
        why: "Wind shifted right and holding. Forecast also shifted 28° right. Go right." },
      reroute: { orin: { reachable: true }, state: "done", latest: "orin", pi: { state: "idle" },
        result: { available: true, source: "orin", label: "Off the playbook", off_playbook: true,
          asked_by: "Nav iPad", asked_at: Math.floor(Date.now() / 1000) - 1500,
          finished_at: Math.floor(Date.now() / 1000) - 1440, sailed_nm: 46.2, tacks: 9,
          sail_plan: ["J1", "A3", "S1"], vs_plan: { available: true, gain_min: 12 },
          weather: { newest_age_h: 2.4, models: [{ model: "gfs" }, { model: "nam" }, { model: "hrrr" }] },
          warnings: ["No chart aboard. Route doesn't check the shore."] } },
      synthesis: { available: true, mode: "llm", confidence: "med",
        assessment: "Right has taken over, past what the plan covers. Commit right.",
        concordance: { strength: "strong", lean: "right",
          note: "Wind, forecast and position all point right. You're 1.3 nm right already." },
        recommendation: { action: "Off plan: go right, lay the mark", vs_playbook: "departs", target_variant: null,
          urgency: "now", confidence: "med",
          rationale: "Everything points right." },
        play_matches: [
          { play_id: "shift_right_20", name: "Breeze 20°+ right of forecast", match: "strong", status: "armed",
            why: "Wind shifted right, forecast shifted 28° right. This is the play." },
          { play_id: "pressure_up", name: "More pressure than forecast", match: "partial", status: "quiet",
            why: "Wind 2-3 kts over forecast, not steady yet." },
        ],
        reroute_suggest: { reason: "Off the playbook. The right is paying.", why: "off_playbook" } },
    },
  };

  /* ============================ helpers ============================ */
  const API = "/api";
  const NA = (note) => ({ status: "na", value: "—", sub: note || "no data", why: note || "No data.",
    consider: "—", clears: "—", based: [], conf: "engine" });
  const stripTags = (s) => String(s).replace(/<[^>]+>/g, "");

  function fetchJSON(path, ms) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms || 5000);
    return fetch(API + path, { signal: ctl.signal, headers: { Accept: "application/json" } })
      .then((r) => (r.ok ? r.json() : null)).catch(() => null).finally(() => clearTimeout(t));
  }
  function postJSON(path, body, ms) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms || 8000);
    return fetch(API + path, { method: "POST", signal: ctl.signal,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}) })
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
  /* UP-COURSE OBSERVATIONS (engine /buoys): live NDBC/METAR stations + actual-vs-forecast — the
     wind detail's ladder. Fetched when the wind detail opens (slow-moving; engine caches ~10 min). */
  async function fetchBuoys() {
    const r = await fetchJSON("/buoys", 9000);
    if (r) App.buoys = r;
  }
  function buoysLadderHtml() {
    const b = App.buoys;
    if (!b || !b.available) return "";
    const dd = (v, unit, digits) => v == null ? "—"
      : (v > 0 ? "+" : "") + (digits ? v.toFixed(1) : Math.round(v)) + unit;
    const rows = (b.stations || []).map((s) => {
      const obs = s.tws_kn != null
        ? r0(s.tws_kn) + " kts @ " + s.twd_deg + "°" + (s.stale ? " ⚠" : "")
        : "<span class='muted'>dark</span>";
      const vsOwn = s.delta ? dd(s.delta.tws_kn, "k", 1) + " / " + dd(s.delta.twd_deg, "°") : "—";
      const f = s.forecast, big = f && (Math.abs(f.vs.tws_kn) >= 4 || Math.abs(f.vs.twd_deg) >= 20);
      const vsFc = f ? dd(f.vs.tws_kn, "k", 1) + " / " + dd(f.vs.twd_deg, "°") : "—";
      return "<tr" + (s.up_course ? " class='bl-up'" : "") + "><td>" + esc2(s.name) +
        (s.shore ? " <span class='bl-shore'>shore</span>" : "") +
        (s.up_course ? " ▲" : "") + "</td>" +
        "<td>" + s.range_nm + "nm @" + s.bearing + "°</td>" +
        "<td>" + obs + (s.age_min != null ? " <span class='muted'>" + s.age_min + "m</span>" : "") + "</td>" +
        "<td>" + vsOwn + "</td>" +
        "<td" + (big ? " style='color:var(--act);font-weight:700'" : "") + ">" + vsFc + "</td></tr>";
    }).join("");
    if (!rows) return "";
    const up = b.upcourse;
    const head = up
      ? "<div class='dc-foot'>▲ " + esc2(up.name) + " up-course " + up.range_nm + " nm: " +
        dd(up.tws_delta_kn, " kts", 1) + " vs here, " + dd(up.twd_shift_deg, "°") + " shift" +
        (up.vs_forecast ? " · vs plan " + dd(up.vs_forecast.tws_kn, "k", 1) + " / " + dd(up.vs_forecast.twd_deg, "°") : "") +
        (up.shore ? " (shore)" : "") + "</div>" : "";
    return "<div class='rc-title' style='margin-top:10px'>Stations up the course</div>" +
      head +
      "<div class='bl-wrap'><table class='bl-tbl'><thead><tr><th>station</th><th>where</th><th>obs</th>" +
      "<th title='station minus our wind'>vs us</th>" +
      "<th title='station minus the plan forecast'>vs plan</th></tr></thead>" +
      "<tbody>" + rows + "</tbody></table></div>" +
      "<div class='dc-foot'>Shore stations read light. Trust their direction more.</div>";
  }
  function esc2(s) { return String(s == null ? "" : s).replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c])); }
  /* ============ COURSE MAP (Time-to-Mark detail): the plan course to scale + the boat ============
     Equirectangular nm projection (x = lon·cos(midlat)·60, y = −lat·60) — plenty accurate at
     course scale, zero tiles/network. Marks + legs come from the loaded course (/course, memoized
     until the next homework push), the boat is the live /navigator position; the leg being sailed
     is highlighted and the dashed line is boat → next mark. Redraws with the 3-s poll. */
  async function fetchCourse() {
    if (App.courseMap) return;
    const r = await fetchJSON("/course", 8000);
    if (r && r.available && (r.marks || []).length >= 2) App.courseMap = r;
  }
  async function fetchTrack() {   // NOT memoized — a mid-day re-push replaces the playbook
    const r = await fetchJSON("/playbook/track", 8000);
    App.pbTrack = r && r.available && (r.points || []).length >= 2 ? r : null;
  }
  function courseMapHtml() {
    const c = App.courseMap, nav = App.nav;
    if (!c || !nav || !nav.position || nav.position.lat == null) return "";
    const marks = c.marks, boat = nav.position;
    const trk = App.pbTrack ? App.pbTrack.points : [];   // the optimizer route, when loaded
    const lats = marks.map((m) => m.lat).concat([boat.lat], trk.map((p) => p.lat));
    const lons = marks.map((m) => m.lon).concat([boat.lon], trk.map((p) => p.lon));
    const midLat = (Math.min.apply(null, lats) + Math.max.apply(null, lats)) / 2;
    const KX = Math.cos(midLat * Math.PI / 180) * 60;                 // nm per °lon here
    const nm = (lat, lon) => [lon * KX, -lat * 60];                   // planar nm coords
    const ptsNm = marks.map((m) => nm(m.lat, m.lon)).concat([nm(boat.lat, boat.lon)]);
    const minX = Math.min.apply(null, ptsNm.map((p) => p[0])), maxX = Math.max.apply(null, ptsNm.map((p) => p[0]));
    const minY = Math.min.apply(null, ptsNm.map((p) => p[1])), maxY = Math.max.apply(null, ptsNm.map((p) => p[1]));
    const spanX = Math.max(maxX - minX, 0.5), spanY = Math.max(maxY - minY, 0.5);
    const W = 512, PAD = 40;
    const H = Math.round(Math.min(430, Math.max(220, (W - 2 * PAD) * spanY / spanX + 2 * PAD)));
    const s = Math.min((W - 2 * PAD) / spanX, (H - 2 * PAD) / spanY);
    const ox = (W - s * spanX) / 2, oy = (H - s * spanY) / 2;
    const P = (lat, lon) => { const q = nm(lat, lon); return [ox + (q[0] - minX) * s, oy + (q[1] - minY) * s]; };
    const ni = (nav.next_mark && nav.next_mark.index) || 0;           // active leg = marks[ni-1]→[ni]
    let svg = "";
    for (let i = 1; i < marks.length; i++) {
      const a = P(marks[i - 1].lat, marks[i - 1].lon), b = P(marks[i].lat, marks[i].lon);
      const on = i === ni && !trk.length;   // with a plan line aboard, accent belongs to it
      svg += '<line x1="' + a[0].toFixed(1) + '" y1="' + a[1].toFixed(1) + '" x2="' + b[0].toFixed(1) +
        '" y2="' + b[1].toFixed(1) + '" style="stroke:var(--' + (on ? "accent" : "na") + ');stroke-width:' +
        (on ? 3 : 2) + ';opacity:' + (on ? 1 : 0.55) + '" />';
    }
    if (trk.length) {                       // the frozen variant's optimizer route
      const pts = trk.map((p) => P(p.lat, p.lon).map((x) => x.toFixed(1)).join(",")).join(" ");
      svg += '<polyline points="' + pts + '" style="fill:none;stroke:var(--accent);stroke-width:2.5;stroke-linejoin:round" />';
    }
    const bp = P(boat.lat, boat.lon);
    if (nav.next_mark) {                                              // dashed: us → the next mark
      const t = P(marks[ni].lat, marks[ni].lon);
      svg += '<line x1="' + bp[0].toFixed(1) + '" y1="' + bp[1].toFixed(1) + '" x2="' + t[0].toFixed(1) +
        '" y2="' + t[1].toFixed(1) + '" style="stroke:var(--text);stroke-width:1.5;stroke-dasharray:5 4;opacity:.7" />';
    }
    marks.forEach((m, i) => {
      const p = P(m.lat, m.lon);
      const name = m.name.length > 14 ? m.name.slice(0, 13) + "…" : m.name;
      const left = p[0] > W - 110;                                    // keep labels on-canvas
      svg += '<circle cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) + '" r="4.5" style="fill:var(--panel);stroke:var(--text);stroke-width:1.6" />' +
        '<text x="' + (p[0] + (left ? -9 : 9)).toFixed(1) + '" y="' + (p[1] + 4).toFixed(1) +
        '" text-anchor="' + (left ? "end" : "start") + '" style="fill:var(--muted);font:11px system-ui">' + esc2(name) + '</text>';
    });
    svg += '<circle cx="' + bp[0].toFixed(1) + '" cy="' + bp[1].toFixed(1) + '" r="5.5" style="fill:var(--accent);stroke:var(--bg);stroke-width:2" />' +
      '<text x="' + (bp[0] + (bp[0] > W - 70 ? -10 : 10)).toFixed(1) + '" y="' + (bp[1] - 8).toFixed(1) +
      '" text-anchor="' + (bp[0] > W - 70 ? "end" : "start") + '" style="fill:var(--text);font:bold 11px system-ui">C4</text>';
    // north tick + a round-number scale bar so distances read at a glance
    const nice = [5, 10, 20, 50, 100].reduce((a, v) => (v * s <= (W - 2 * PAD) / 3 ? v : a), 5);
    svg += '<text x="10" y="18" style="fill:var(--muted);font:11px system-ui">N ↑</text>' +
      '<line x1="12" y1="' + (H - 12) + '" x2="' + (12 + nice * s).toFixed(1) + '" y2="' + (H - 12) +
      '" style="stroke:var(--muted);stroke-width:2" />' +
      '<text x="' + (16 + nice * s).toFixed(1) + '" y="' + (H - 8) + '" style="fill:var(--muted);font:10px system-ui">' + nice + ' nm</text>';
    // foot line: engine-computed range/bearing + our planar offset from the active leg line
    let foot = "";
    if (nav.next_mark && nav.next_mark.distance_nm != null)
      foot = r1(nav.next_mark.distance_nm) + " nm @ " + r0(nav.next_mark.bearing_deg) + "° to " + esc2(marks[ni].name);
    if (ni >= 1) {
      const a = nm(marks[ni - 1].lat, marks[ni - 1].lon), b = nm(marks[ni].lat, marks[ni].lon), q = nm(boat.lat, boat.lon);
      const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1;
      const cross = (dx * (q[1] - a[1]) - dy * (q[0] - a[0])) / len;  // +ve = right of the leg (screen y down)
      foot += " · " + r1(Math.abs(cross)) + " nm " + (cross > 0 ? "right" : "left") + " of the leg line";
    }
    if (trk.length) foot += (foot ? " · " : "") + "line = plan route (" +
      esc2(App.pbTrack.variant_label || App.pbTrack.variant) + ")";
    return '<div class="rc-title" style="margin-top:10px">Course and plan</div>' +
      '<svg viewBox="0 0 ' + W + " " + H + '" style="width:100%;height:auto;display:block;background:var(--panel2);border-radius:8px">' + svg + "</svg>" +
      (foot ? '<div class="dc-foot">' + foot + "</div>" : "");
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
      if (!nowS) return NA("collecting wind history…");
      if (!samples[0][1]) samples[0][1] = nowS;
      const rows = samples.map(([lbl, s], i) => ({ label: lbl, emph: i === 0, cols: [s ? arrowKts(s.twd, s.tws) : "—"] }));
      let oldest = null;
      for (let i = samples.length - 1; i >= 1; i--) { if (samples[i][1]) { oldest = samples[i][1]; break; } }
      let st = "ok", sub = "";
      if (oldest) {
        const dT = nowS.tws - oldest.tws, dD = angDiff(nowS.twd, oldest.twd);
        if (Math.abs(dT) >= 6 || Math.abs(dD) >= 12) st = "watch";
        sub = r0(oldest.tws) + "→" + r0(nowS.tws) + " kts" +
          (Math.abs(dD) >= 5 ? " · " + r0(Math.abs(dD)) + "° " + (dD > 0 ? "right" : "left") : " · steady dir");
      }
      const chart = twsTrendTile(App.src === "live" ? raceData() : null) || null;
      return { status: st, value: arrowKts(nowS.twd, nowS.tws), sub: sub, chart: chart, rows: rows,
        // the one place the arrow legend is explained (the forecast tile uses the same arrows)
        why: "Arrows point downwind (a north wind points down)." +
          (oldest ? " Wind " + (nowS.tws >= oldest.tws ? "built" : "eased") + " from " + r0(oldest.tws) + " to " + r0(nowS.tws) + " kts." : ""),
        consider: st === "ok" ? "Steady. Play the shifts." : "Wind is building or shifting. Favour the new side, check sails.",
        clears: st === "ok" ? "—" : "the trend settles",
        based: [App.seriesHist && App.seriesHist.length ? "boat wind log (" + App.seriesHist.length + " points)" : "boat wind (" + App.windHist.length + " readings)"], conf: "engine" };
    },
    /* FORECAST: the multi-model PLUME from the Lab (engine /plume, cached aboard) on the face —
       the latest run of every router model along the planned route, the boat's own wind pinned
       at now — and the Open-Meteo rows (+60/+120, forecast vs actual) under it in the detail.
       Either alone is enough; with neither the tile is honestly empty. */
    forecast(p) {
      const fc = p.forecast && p.forecast.available && p.forecast.hours && p.forecast.hours.length ? p.forecast : null;
      const pl = plumeNow();
      const wait = App.src === "live" && !pl ? App.plumeWait || "" : "";
      if (!fc && !pl) return NA(wait || "no forecast");
      // the boat's measured wind — only a REAL reading (a null tws is "no wind", not 0 kts)
      const live = p.conditions && p.conditions.available && p.conditions.tws != null && p.conditions.twd != null
        ? { tws: p.conditions.tws, twd: p.conditions.twd } : null;
      const o0 = observedAt(0);
      const obs = o0 && o0.tws != null && o0.twd != null ? o0 : live;
      App.fcObs = obs;                    // the plume detail pins the same reading at now
      const nowS = Date.now() / 1000;
      const fromPlume = (h) => {          // the models' mean at +h when there's no Open-Meteo series
        const sp = pl ? PL.spreadAt(pl, nowS + h * 3600) : null;
        return sp ? { tws: sp.mean_tws, twd: sp.mean_twd } : null;
      };
      let f60, f120;
      if (fc) {
        const aug = obs ? [{ in_h: 0, tws: obs.tws, twd: obs.twd }].concat(fc.hours) : fc.hours.slice();
        f60 = fcstAt(aug, 1.0); f120 = fcstAt(aug, 2.0);
      } else { f60 = fromPlume(1); f120 = fromPlume(2); }
      const v60 = fc ? fcstVsActual(3600) : null, v120 = fc ? fcstVsActual(7200) : null;
      const fwd = (x) => (x && x.tws != null ? arrowKts(x.twd, x.tws) : "—");
      const rows = [
        { label: "Now", emph: true, cols: [obs ? arrowKts(obs.twd, obs.tws) : "no wind reading"] },
        { label: "+60 min", cols: [fwd(f60)] },
        { label: "+120 min", cols: [fwd(f120)] },
      ];
      if (fc) rows.push(
        { sep: true, label: "FORECAST VS. ACTUAL" },
        { hdr: true, cols: ["forecast", "actual"] },
        { label: "−60 min", cols: v60 ? [fwd(v60.fcst), fwd(v60.actual)] : ["—", "collecting"] },
        { label: "−120 min", cols: v120 ? [fwd(v120.fcst), fwd(v120.actual)] : ["—", "collecting"] });
      // WATCH needs real numbers on both sides — a missing reading never raises it
      const big = (a, b, kt, deg) => !!(a && b && a.tws != null && b.tws != null &&
        (Math.abs(a.tws - b.tws) >= kt || Math.abs(angDiff(a.twd, b.twd)) >= deg));
      const bigChange = big(f120, obs, 6, 20);
      const badSkill = (v60 && big(v60.actual, v60.fcst, 4, 15)) || (v120 && big(v120.actual, v120.fcst, 5, 20));
      const st = bigChange || badSkill ? "watch" : "ok";
      const frozen = pl && PL.isFrozen(pl, nowS);
      const old = pl && !frozen && PL.isOld(pl);
      const chart = pl ? '<div class="pl-tile">' + PL.chartSvg(pl, { w: 270, h: 104, now: nowS, back_h: 3, ahead_h: 24,
        compact: true, field: "tws", obs: App.src === "live" ? raceData() : null, nowObs: obs }) + PL.legendHtml(pl) + "</div>" : null;
      const sub = !pl ? PL.esc(wait) : frozen ? '<b class="pl-old-tag">' + PL.esc(PL.frozenText(pl)) + "</b>"
        : pl.source === "onboard" ? PL.summary(pl, nowS, 6) + " · " + (old ? '<b class="pl-old-tag">' + PL.esc(PL.sourceText(pl)) + "</b>" : PL.esc(PL.sourceText(pl)))
        : PL.summary(pl, nowS, 6) + (old ? ' · <b class="pl-old-tag">data ' + PL.ageText(pl.age_s) + " old</b>" : "");
      const head = f60 && f60.tws != null ? "+60 min " + r0(f60.tws) + " kts" + (f120 && f120.tws != null ? ", +120 min " + r0(f120.tws) + " kts." : ".") : "";
      return { status: st, value: null, rows: rows, chart: chart, sub: sub,
        why: pl ? "Each model's forecast along your route. Bold = latest run." + (frozen ? " Frozen at the start." : "")
          : head ? "Forecast: " + head : "Forecast wind speed and direction.",
        consider: badSkill ? "Forecast has been off. Trust the live wind." : bigChange ? "Big change coming. Plan your sails." :
          frozen ? PL.frozenText(pl) + ". No updates while racing." :
          old ? "Forecast is " + PL.ageText(pl.age_s) + " old. Updates when back online." :
          obs ? "Forecast steady and matching." : "No wind reading. Check the wind sensor.",
        clears: st === "ok" ? "—" : "forecast matches again",
        based: (pl ? [(pl.source === "onboard" ? "Onboard, " : "") + PL.models(pl).length + " forecast models, " + (pl.age_s != null ? PL.ageText(pl.age_s) + " old" : "age ?")] : [])
          .concat(fc ? ["forecast + boat wind"] : []), conf: "engine" };
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
                    wrong_angle: "wrong sail for this angle", no_model: "logged" };
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
      const sub = (crew ? (FIT[fit] || "crew-set") : "not set · best sail shown")
        + (when ? " · " + when : (crew && fit === "in_range" ? " · no change ahead" : ""));
      // advisory watch coupling: a pending change with the watch boundary close by = extra
      // hands soon — say so (the crew decides; nothing is re-timed)
      const w = p.watch;
      const wNote = to && w && w.plan_set && w.active && w.mins_to_change != null && w.mins_to_change <= 30
        ? " Watch change in " + fmtMins(w.mins_to_change) + ": extra hands then."
        : "";
      return { status: st, value: value,
        sub: sub,
        why: (crew ? "You set " + crew + ". " : "Tap what's flying on the sails bar. ")
          + (s.recommendation || "") + wNote,
        consider: (to ? (when === "change now" ? "Change to " + to + " now." : "Get the " + to + " ready: " + when + ".") : "No change.") + wNote,
        clears: st === "ok" ? "—" : (to ? to + " hoisted" : "—"),
        based: ["you set " + (crew || "—"),
                "best sail " + s.optimal_sail + " at TWA " + r0(s.twa) + "°, " + r0(s.tws_used) + " kts" + (xo ? "; " + xo.to_sail + " in " + r0(xo.deg_away) + "°" : "")], conf: "engine" };
    },
    eta(p) {
      const n = p.navigator;
      if (!n || !n.available || !n.next_mark) return NA(n && n.note ? n.note : "no active course");
      const m = n.next_mark, e = m.eta_min;
      if (e == null) return NA("no ETA: not closing on the mark");
      const st = e < 2 ? "act" : e < 5 ? "watch" : "ok";
      // no-break space keeps "30 min" together when the value wraps in a narrow tile (#73)
      const txt = e >= 60 ? Math.floor(e / 60) + "h " + r0(e % 60) + "\u00a0min" : r0(e) + "\u00a0min";
      return { status: st, value: txt, sub: m.name,
        why: "~" + r0(e) + " min to " + m.name + " at current speed.",
        consider: e < 5 ? "Mark in ~" + r0(e) + " min. Prep the rounding." : "On schedule for the mark.",
        clears: st !== "ok" ? "after the rounding" : "—", based: ["ETA " + r0(e) + " min to " + m.name], conf: "engine" };
    },
    /* FLEET: whole-fleet corrected-time STANDINGS estimate (crew decision 2026-07-08). Collision
       avoidance lives on the boat's dedicated gear — NOT here; the copilot's compact safety
       callout is the tablet's only collision surface. Face = our division standing; detail =
       the whole fleet ranked (◆ = a boat in OUR division), each row with its position source
       (live AIS · ⌛aged tracker fix, →DR = dead-reckoned forward · no fix) and the corrected
       delta vs us. Ranks are the REMAINING race on corrected time — start-time-free. */
    ais(p) {
      const fl = p.fleet;
      if (!fl) return NA("no fleet feed");
      if (fl.available === false) return NA(fl.note || "no fleet list. Load it in the Lab.");
      const st = fl.standings || [];
      if (!st.length) return NA("no boat positions yet");
      const orank = fl.own_rank || {};
      const ranked = st.filter((r) => r.rank != null);
      const meIdx = ranked.findIndex((r) => r.source === "own");
      const me = ranked[meIdx] || {};
      const fmtD = (cd) => { const m = Math.floor(Math.abs(cd) / 60), ss = Math.round(Math.abs(cd) % 60);
        return m + ":" + (ss < 10 ? "0" : "") + ss; };
      // face: our division standing (fall back to overall), rivals around us as the subtitle
      const value = orank.division != null && orank.division_of > 1
        ? ordinal(orank.division) + " of " + orank.division_of + " (div)"
        : orank.overall != null ? ordinal(orank.overall) + " of " + orank.of : "—";
      const ahead = meIdx > 0 ? ranked[meIdx - 1] : null;
      const behind = meIdx >= 0 && meIdx < ranked.length - 1 ? ranked[meIdx + 1] : null;
      const subBits = [];
      if (ahead) subBits.push("▲ " + boatName(ahead) + " " + fmtD(ahead.corrected_delta_s));
      if (behind) subBits.push("▽ " + boatName(behind) + " " + fmtD(behind.corrected_delta_s));
      const basisTag = fl.corrected_basis === "full" ? "" : " · rest of race";
      const sub = (subBits.join(" · ") || "est. standings") + basisTag;
      // tight racing on either side = watch (informational — there is no safety state here)
      const near = [ahead, behind].some((b) => b && Math.abs(b.corrected_delta_s) <= 120);
      const status = near ? "watch" : "ok";
      const rows = [{ hdr: true, cols: ["to fin", "Δ corrected"] }].concat(
        st.slice(0, 10).map((r) => ({
          label: (r.rank != null ? r.rank + ". " : "—  ") + (r.our_division ? "◆ " : "") +
                 boatName(r) + srcMark(r) + (r.source === "own" ? " (us)" : ""),
          emph: r.source === "own" || r.our_division,
          cols: [r.dtf_nm != null ? r1(r.dtf_nm) + " nm" : "—",
                 r.source === "own" ? "—" : corrTxt(r)] })));
      const nAis = fl.count_ais || 0, nTrk = fl.count_tracker || 0;
      const nDr = st.filter((r) => r.dr_nm).length;
      const unranked = orank.unranked || 0;
      const basisTxt = fl.corrected_basis === "full"
        ? (fl.gun_source === "session" ? "Start time from the race log, so rough. " : "")
        : "Rest of race only. Load start times for full standings. ";
      const why = "Estimated corrected standings, " + (fl.roster_size || st.length) + " boats. " + basisTxt +
        nAis + " on AIS, " + nTrk + " from the tracker (delayed" + (nDr ? ", " + nDr + " estimated forward" : "") + ")" +
        (unranked ? ", " + unranked + " with no position" : "") + ". ◆ = our division" +
        (fl.own_division ? " (" + fl.own_division + ")" : "") + ".";
      const consider = near
        ? "Close with " + boatName(Math.abs((ahead || {}).corrected_delta_s || 9e9) <= Math.abs((behind || {}).corrected_delta_s || 9e9) ? ahead : behind) + " on corrected. Cover at the crossings."
        : ahead ? "Chase " + boatName(ahead) + ". Stay ahead of " + (behind ? boatName(behind) : "the boats behind") + "."
        : "Leading on corrected. Cover, no flyers.";
      return { status: status, value: value, sub: sub, rows: rows, why: why, consider: consider,
        clears: "—",
        based: [ranked.length + " of " + st.length + " boats ranked",
                "AIS " + nAis + " · tracker " + nTrk + (nDr ? " · estimated " + nDr : "")].concat(fl.gaps || []), conf: "engine" };
    },
    /* CREW: helm energy (fatigue) + the WATCH SYSTEM on one tile — who's on, countdown to the
       change, T-15/T-5 escalation. Rotation advice and the watch boundary compose ("rotate at
       the change"). Fatigue-less but plan-aboard still shows the watch read (bench archives are
       stale; the schedule is a clock, not a sensor). */
    charge(p) {
      const f = p.fatigue, w = p.watch;
      const wSet = w && w.plan_set, wSub = watchSubTxt(w), wSt = watchTileStatus(w);
      const hasF = f && f.available && f.index != null;
      if (!hasF && !wSet) return NA(f && f.note ? f.note : "no helm data · no watch plan");
      const chg = hasF ? Math.round(100 - f.index) : null;
      const lvl = hasF ? (f.level || "").replace(/_/g, " ") : null;
      const stF = !hasF ? "ok" : f.level === "fresh" ? "ok" : f.level === "watch" ? "watch" : "act";
      const st = SEV[wSt] > SEV[stF] ? wSt : stF;
      const rotate = hasF && f.level !== "fresh";
      const wake = w && w.next_on ? teamTxt(w, w.next_on) : "the next team";
      const consider = CW.consider(w, wake, rotate);
      // face: the C4 energy score leads (as the old C4 Energy tile), the on-watch team rides
      // just under it at value weight; countdown + level go to the sub line
      const teamLine = wSet && w.active ? (w.all_hands ? "ALL HANDS" : (w.on_label || w.on) + " on") : null;
      const countdown = wSet
        ? (w.active && w.mins_to_change != null
            ? "change " + fmtMins(w.mins_to_change) + (w.next_on_label ? " → " + w.next_on_label : "")
            : watchSubTxt(w))
        : null;
      const o = { status: st,
        energy: chg, level: lvl,      // the detail's CREW ENERGY section reads these explicitly
        value: (hasF ? String(chg) : "—") + (teamLine ? '<div class="t-val2">' + teamLine + "</div>" : ""),
        sub: [countdown, lvl].filter(Boolean).join(" · "),
        why: (hasF ? "Crew energy " + chg + "% (" + lvl + "). " : "No helm data yet. ") +
             (wSet ? (w.active ? (w.all_hands ? "ALL HANDS" : teamTxt(w, w.on) + " on deck") +
                 (w.next_change ? ". Change at " + fmtHM(w.next_change) + (w.next_on_label ? " to " + w.next_on_label : "") + "." : ".")
               : (w.next_change ? "Watches start " + fmtHM(w.next_change) + "." : "No watch on now.")) : "No watch plan."),
        consider: consider,
        clears: CW.clears(w, rotate),
        based: (hasF ? ["helm steering: energy " + chg + "%"] : [])
          .concat(wSet ? ["watch plan: " + (wSub || "loaded")] : []), conf: "engine" };
      if (hasF && f.components) o.components = f.components;
      return o;
    },
    /* DATA is now the INSTRUMENT-HEALTH tile: a source count was never the question. The
       question is "is what I'm reading true", and the engine answers it in five ways at
       /health/sensors — unbindable priority matchers, a rank-1 source that went silent, an
       attitude outside what a monohull can do, heading-vs-GPS-course bias, and AIS or computed
       sources leading an own-ship channel. All five collapse into this one chip; tap for the
       per-channel provenance table. Every one of those checks existed on 2026-09-07 and none of
       them was on a screen, which is the same failure as the bank. */
    data(p) {
      const c = p.conditions, src = p.sources, h = currentHealth();
      const count = src && src.count != null ? src.count : 0;
      const anyStale = src && src.sources && src.sources.some((s) => s.last_seen_s > 45);
      const linkSt = !c || !c.available ? "act" : (c.stale || anyStale) ? "watch" : "ok";
      const noHealth = !h || !h.available || h.status === "unknown";
      const hSt = noHealth ? "na" : h.status === "danger" ? "act"
        : h.status === "warn" ? "watch" : "ok";
      // The link's own state still wins when the engine has gone quiet — a health verdict
      // computed from data we can no longer see is not a reassurance. But an *absent* health
      // read must not blank a perfectly good source count either (SEV ranks "na" alongside
      // "act"), so it falls through to the link state and says so on the face instead.
      const st = noHealth ? linkSt : (SEV[linkSt] >= SEV[hSt] ? linkSt : hSt);
      const flags = (h && h.flags) || [];
      const notes = (h && h.notes) || [];
      const chans = ((h || {}).provenance || {}).channels || {};
      const backups = Object.keys(chans).filter((k) => chans[k].fell_back);
      // The face stays the live-source count — that is what the tile has always meant, and a
      // bare "1" for one flag reads like one sensor. The chips say what is wrong; the status
      // colour and word say how much it matters.
      const chips = healthChips(h);
      const face = String(count);
      const sub = chips.length
        ? "⚑ " + chips.slice(0, 2).join(", ") + (chips.length > 2 ? " +" + (chips.length - 2) : "")
        : (count + " source" + (count === 1 ? "" : "s") + " live"
           + (noHealth ? " · checks offline"
              : backups.length ? " · " + backups.length + " on backup" : ""));
      // No provenance rows on the FACE: at half height the device names truncate to "GPS24…"
      // and the label column collapses, which is noise, not information. The full table is one
      // tap away — and the backlog's loudest v2 note is that the console showed too much.
      return { status: st, value: face, sub: sub,
        flags: flags, notes: notes, health: h || null,
        why: (!c || !c.available ? "No live data from the boat. " :
              count + " sensor group" + (count === 1 ? "" : "s") + " reporting"
              + (anyStale ? ", one stale. " : ". "))
          + (!h || !h.available ? "Sensor checks offline."
             : flags.length ? "Check failed: " + flags.join("; ") + "."
             : "All sensor checks pass.")
          + (notes.length ? " Note: " + notes.join("; ") + "." : ""),
        consider: flags.length ? "Don't trust the flagged reading. Check another instrument first."
          : st === "ok" ? "Instruments agree. Safe to use."
          : "Check a second instrument before trusting one reading.",
        clears: flags.length ? "the check passes again" : st === "ok" ? "—" : "all sources fresh",
        based: [count + " sources",
                h && h.available ? "checks: " + h.reason : "checks: offline"]
          .concat(provLine("data") ? [provLine("data")] : []), conf: "engine" };
    },
    /* HOUSE BANK — the tile that did not exist on 2026-07-18. `/power` decides on a smoothed
       level with a robust trend, so the face is a voltage the crew can act on and the sub is the
       part that actually predicts the failure: the drain rate and how long until 11.0 V, where
       the Pi's SD writes and the AIS transceiver start browning out. */
    bank(p) {
      const b = currentPower();
      if (!b || b.available === false) return NA(b && b.note ? b.note : "no bank voltage");
      const st = b.status === "danger" ? "act" : b.status === "warn" ? "watch" : "ok";
      const trend = b.trend_v_per_h;
      // two bits at most on the face — the projection is what you act on, the window minimum is
      // in the detail. A wrapping third line pushed the number off a half-height tile.
      const bits = [];
      if (trend != null) bits.push((trend >= 0 ? "+" : "") + trend.toFixed(2) + " V/h");
      if (b.charging) bits.push("charging");
      // hours to a decimal only while it matters — "~42.0 h" wrapped the half-height tile's
      // sub-line onto a second row for a number nobody acts on
      else if (b.hours_to_floor != null) bits.push("~" + (b.hours_to_floor >= 10 ? r0(b.hours_to_floor) : r1(b.hours_to_floor))
        + " h to " + r1(b.thresholds.floor_v) + " V");
      else if (b.volts_min != null && b.volts_min < b.volts) bits.push("min " + b.volts_min + " V");
      return { status: st, value: (b.volts != null ? b.volts.toFixed(2) : "—") + " V", sub: bits.join(" · "),
        // Status uses a sustain_min-minute median (a winch load can't trip it, a short bounce
        // can't clear it); the trend is over window_min. Below ~11.0 V the Pi's SD writes and the
        // AIS transceiver brown out — how the Jul 18 archive and AIS feed died together.
        why: b.reason + "."
          + (b.hours_to_floor != null ? " At this rate, " + r1(b.thresholds.floor_v) + " V in about "
             + r1(b.hours_to_floor) + " h." : "")
          + " Below " + r1(b.thresholds.floor_v) + " V the recorder and AIS can fail.",
        consider: st === "act" ? "Shed load and start charging. Recorder and AIS fail first."
          : st === "watch" ? (b.charging ? "Charging but still low. Keep charging."
              : "Bank draining. Plan a charge before " + r1(b.thresholds.floor_v) + " V.")
          : "Bank healthy.",
        // the release condition IS the decision condition now — the dwell median coming back
        // above the line it crossed. No separate band to explain (see power.py `_tripped`).
        clears: st === "ok" ? "—"
          : "bank above "
            + r1(b.status === "danger" ? b.thresholds.danger_v : b.thresholds.warn_v) + " V for over half of the last " + r0(b.sustain_min) + " min",
        based: [b.volts + " V, " + (trend == null ? "no trend yet" : trend.toFixed(2) + " V/h"),
                "⚠ voltage limits not checked on this boat. Trust the trend."]
          .concat(provLine("bank") ? [provLine("bank")] : []), conf: "engine" };
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
      if (synOff) bits.push("off plan");
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
      // PROVENANCE on every number, in one place rather than eight: the BASED ON slot names the
      // device behind each of the tile's inputs. (`data` and `bank` add their own richer lines.)
      const prov = PAIR.indexOf(k) < 0 ? provLine(k) : null;
      if (prov) raw.based = (raw.based || []).concat([prov]);
      // Held-but-stale is a THIRD state, distinct from fresh and from absent: the number is
      // real, just not current, and the crew is entitled to know which it is looking at.
      const age = tileAge(k);
      if (age !== Infinity && age >= STALE_AFTER_MS) raw.staleMs = age;
      tiles[k] = raw;
    }
    const flagged = TILES.filter((k) => tiles[k].status === "watch" || tiles[k].status === "act")
      .sort((a, b) => SEV[tiles[b].status] - SEV[tiles[a].status]);
    const reachable = p.conditions || p.navigator || p.sources;
    const notes = flagged.slice(0, 3).map((k) => ({ tile: k, status: tiles[k].status, text: tiles[k].consider || tiles[k].why, conf: "engine" }));
    const held = TILES.filter((k) => tiles[k].staleMs);
    const link = linkMessage(held.length ? Math.max.apply(null, held.map((k) => tiles[k].staleMs)) : 0);
    const focus = link ? link.text : !reachable ? "No live data from the boat." :
      held.length ? "Showing last values (" + fmtAge(Math.max.apply(null, held.map((k) => tiles[k].staleMs))) + " old). Boat slow to answer." :
      flagged.length === 0 ? "Nothing flagged." :
      flagged.length === 1 ? "1 item needs attention." :
      flagged.length + " items need attention.";
    const d = applyBrief({ tiles, focus, notes, confidence: "engine", mode: "engine read" });
    if (link) { d.focus = link.text; d.link = link.state; }    // a link problem outranks the LLM's line
    return d;
  }
  /* null while polls are answering; otherwise what to tell the crew about the link to the Pi */
  function linkMessage(oldestHeldMs) {
    if (App.lastPollOk) return null;
    const now = Date.now();
    if (!App.lastOkAt && now - App.startedAt < LOST_AFTER_MS) return { state: "reconnecting", text: "Connecting to the boat…" };
    const silent = now - (App.lastOkAt || App.startedAt);
    if (now < App.graceUntil || silent < LOST_AFTER_MS)
      return { state: "reconnecting", text: "Reconnecting… showing last values" +
        (oldestHeldMs ? " (" + fmtAge(oldestHeldMs) + " old)" : "") + "." };
    return { state: "lost", text: "No link for " + fmtAge(silent) +
      ". Check the iPad is on boat Wi-Fi. Still trying." };
  }
  /* the iPad woke (screen on, Safari back in front, or Wi-Fi back): say so at once and poll now
     instead of showing the frozen pre-sleep screen until the next 3 s tick */
  function onWake() {
    if (App.src !== "live") return;
    App.graceUntil = Date.now() + RECONNECT_GRACE_MS;
    if (App.data && App.lastOkAt && Date.now() - App.lastOkAt > STALE_AFTER_MS) {
      const now = Date.now();
      // re-age everything held from before the sleep so old numbers don't look current, both now
      // and in rebuilds that land before the slower endpoints (power, health) refresh
      for (const k in App.fresh) App.ages[k] = now - App.fresh[k].t;
      for (const k of TILES) {
        const t = App.data.tiles[k], age = tileAge(k);
        if (t && age !== Infinity && age >= STALE_AFTER_MS) t.staleMs = age;
      }
      App.data.focus = "Reconnecting…"; App.data.link = "reconnecting"; render();
    }
    poll();
    fetchReroute();
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
      why: "Can't reach the boat. No track check." };
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
  /* FORECAST PLUME (engine /plume — the Lab's multi-model run history, cached on the Pi's disk).
     Slow-moving (new model runs arrive hourly at most), so a minute's poll is plenty. A failed
     poll keeps the last plume; its age keeps counting so an offline boat shows how old it is. */
  const PL = window.C4Plume;
  const PLUME_EVERY = 60000;
  function fetchPlume() {
    if (App.src !== "live") return Promise.resolve(null);
    return fetchJSON("/plume", 8000).then((r) => {
      if (r && r.available && PL.available(r)) { App.plume = r; App.plumeAt = Date.now(); App.plumeWait = ""; }
      else if (r && r.available === false) { App.plume = null; App.plumeWait = PL.waitingText(r); }
      if (App.openTile === "forecast") populateDetail("forecast", false);
      return r;
    });
  }
  /* the plume with its age brought up to now (the engine stamped age_s when it answered) */
  function plumeNow() {
    if (App.src !== "live" || !App.plume) return null;
    const extra = App.plumeAt ? (Date.now() - App.plumeAt) / 1000 : 0;
    return Object.assign({}, App.plume, { age_s: App.plume.age_s != null ? App.plume.age_s + extra : null });
  }
  /* the detail's Speed/Direction + model buttons: data- attributes, one delegated listener */
  function plumeView(k, v) {
    if (k !== "field" && k !== "only") return;
    if (k === "field" && v !== "tws" && v !== "twd") return;
    App.plumeView[k] = k === "only" && (!v || App.plumeView.only === v) ? null : v;
    if (App.openTile === "forecast") populateDetail("forecast", false);
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest(".pl-btn[data-pl-k]");
    if (b) plumeView(b.dataset.plK, b.dataset.plV);
  });
  function plumeDetailHtml(nowObs) {
    const pl = plumeNow();
    if (!pl) return "";
    const v = App.plumeView, now = Date.now() / 1000;
    const btn = (k, val, lab, on) => '<button class="pl-btn' + (on ? " on" : "") + '" data-pl-k="' + PL.esc(k) +
      '" data-pl-v="' + PL.esc(val) + '">' + lab + "</button>";
    const fields = btn("field", "tws", "Speed", v.field === "tws") + btn("field", "twd", "Direction", v.field === "twd");
    const picks = btn("only", "", "All", !v.only) + PL.models(pl).map((m) =>
      btn("only", m.model, '<span class="pl-key pl-' + m.model + '"><i></i>' + PL.label(m.model) + "</span>", v.only === m.model)).join("");
    const only = v.only && PL.models(pl).some((m) => m.model === v.only) ? v.only : null;
    const frozen = PL.isFrozen(pl, now);
    const old = !frozen && PL.isOld(pl);
    return '<div class="rc-title">Forecast plume · ' + PL.esc(pl.label || "planned route") + "</div>" +
      (pl.source === "onboard" ? '<div class="dc-foot">' + PL.esc(PL.sourceText(pl)) + "</div>" : "") +
      (frozen ? '<div class="pl-age-warn pl-frozen">' + PL.esc(PL.frozenText(pl)) + ". No updates while racing.</div>" : "") +
      (old ? '<div class="pl-age-warn">Forecast is <b>' + PL.ageText(pl.age_s) + " old</b>. Updates when the boat is online.</div>" : "") +
      '<div class="pl-ctl">' + fields + '</div><div class="pl-ctl">' + picks + "</div>" +
      PL.chartSvg(pl, { w: 512, h: 230, now: now, back_h: 6, ahead_h: 24, field: v.field, only: only, allRuns: true,
        obs: App.src === "live" ? raceData() : null, nowObs: nowObs }) +
      PL.legendHtml(pl, { only: only, cycles: true, now: now, boat: true }) +
      '<div class="dc-foot">Faded = older runs. Dot = your wind.</div>' +
      PL.runTimesHtml(pl, now);
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
  /* PLAN GAP — own observed wind vs the frozen promise (the read drift can't make); engine /plangap */
  async function fetchPlangap() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/plangap", 9000);
    if (r) App.plangap = r;
    if (App.src === "live") render();
  }
  function currentPlangap() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).plangap || null;
    return App.plangap;
  }
  /* TREND — 1h/3h build/walk rates of own observed wind; engine /trend (sail-detail read) */
  async function fetchTrend() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/trend", 9000);
    if (r) App.trend = r;
  }
  /* ---- HOUSE BANK + INSTRUMENT HEALTH (engine /power, /health/sensors) ----
     Both endpoints existed before this surface did, and that was the whole problem: on Jul 18
     the bank fell 12.91 -> 11.08 V over six hours, took the archiver and the AIS transceiver
     with it, and no screen aboard carried the number. `hold()` them like the main poll so a slow
     engine shows a held value with its age rather than a blank tile. */
  async function fetchPower() {
    if (App.src !== "live") return;
    App.power = hold("power", await fetchJSON("/power", 8000), Date.now());
    if (App.src === "live") render();
  }
  async function fetchHealth() {
    if (App.src !== "live") return;
    App.health = hold("health", await fetchJSON("/health/sensors", 8000), Date.now());
    if (App.src === "live") render();
  }
  function currentPower() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).power || null;
    return App.power;
  }
  function currentHealth() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).health || null;
    return App.health;
  }
  /* Which channels each tile's numbers actually come from — the map behind "provenance on every
     number". Keyed to `/health/sensors` -> provenance.channels, which names the DEVICE (not the
     N2K address) plus its rank and age. */
  const TILE_CHANNELS = {
    wind: ["tws", "twd"], forecast: ["tws", "twd"], sail: ["aws", "awa", "tws", "twa"],
    eta: ["sog", "cog", "lat", "lon"], ais: ["lat", "lon", "sog", "cog"],
    playbook: ["tws", "twd", "lat", "lon"], charge: ["heading_true", "heel", "stw"],
    bank: ["bank_voltage"],
  };
  const CH_LABEL = { tws: "TWS", twd: "TWD", twa: "TWA", aws: "AWS", awa: "AWA", stw: "STW",
    sog: "SOG", cog: "COG", lat: "position", lon: "position", heel: "heel", pitch: "pitch",
    heading_true: "heading", heading_mag: "heading (mag)", rate_of_turn: "rate of turn",
    depth: "depth", water_temp: "water temp", rudder_angle: "rudder", bank_voltage: "bank" };
  /* one "TWS Orca Core (rank 1) · TWD Orca Core" line for a tile's BASED ON slot */
  function provLine(key) {
    const chans = ((currentHealth() || {}).provenance || {}).channels;
    const want = TILE_CHANNELS[key];
    if (!chans || !want) return null;
    const seen = new Set(), bits = [];
    for (const ch of want) {
      const e = chans[ch];
      if (!e || seen.has(CH_LABEL[ch] || ch)) continue;
      seen.add(CH_LABEL[ch] || ch);
      bits.push((CH_LABEL[ch] || ch) + " " + (e.device || e.source || "?") +
        (e.fell_back ? " ⚑backup" : e.rank ? "" : " (unranked)") +
        (e.measured === false ? " (computed)" : ""));
    }
    return bits.length ? "from: " + bits.join(" · ") : null;
  }
  /* Short labels for whatever the health checks are flagging — the DATA tile face has room for
     "⚑ aws, compass vs GPS", not for the engine's full sentence (which is in the detail). */
  function healthChips(h) {
    if (!h || !h.available) return [];
    const out = [];
    if ((h.attitude || {}).status !== "ok" && (h.attitude || {}).status) out.push("attitude");
    if (["warn", "danger"].indexOf((h.heading || {}).status) >= 0) out.push("compass vs GPS");
    const c = (h.provenance || {}).checks || {};
    for (const f of (c.lead_source || {}).went_silent || []) out.push(f.channel);
    for (const f of (c.own_ship || {}).ais_leading || []) out.push("AIS in " + f.channel);
    if (((c.policy_binds || {}).unresolvable || []).length) out.push("priority unbound");
    return out;
  }
  /* does any number on this tile come from a backup sensor? the tile head shows ⚑ if so */
  function provFlagged(key) {
    const chans = ((currentHealth() || {}).provenance || {}).channels;
    const want = TILE_CHANNELS[key];
    if (!chans || !want) return null;
    const fb = want.filter((ch) => (chans[ch] || {}).fell_back);
    if (!fb.length) return null;
    return fb.map((ch) => (CH_LABEL[ch] || ch) + " on " +
      ((chans[ch].device || chans[ch].source || "a backup"))).join(", ");
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
  /* the boat's sail inventory for the SAILS bar — cert sails + the crew-config overlays
     (C0/J2/J3/SS aren't in the ORC cert; the boat flies COMBINATIONS, so chips multi-select) */
  const SAILS_INV = ["J1", "J2", "J3", "C0", "SS", "A3", "S1"];
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
  let recConfirmUntil = 0;   // two-step end: first tap arms this window, second tap ends
  function renderRec() {
    const b = document.getElementById("recBtn"), l = document.getElementById("recLbl");
    if (!b || !l) return;
    const a = (App.session || {}).active;
    // The archiver writing nothing (disk full, damaged file, stopped) beats "Logging": a
    // session only marks what to KEEP. Absent field (older engine, cloud) = no claim either way.
    const arch = (App.session || {}).archiver;
    const down = !!arch && arch.recording === false;
    // Red banner + button label only while a LOG session runs (that is when it costs a race).
    // With no session, instruments off at the dock is normal: tooltip only.
    const loud = down && !!a;
    const warn = document.getElementById("archWarn");
    if (warn) { warn.hidden = !loud; warn.textContent = loud ? arch.text : ""; }
    b.classList.toggle("on", !!a && !down);
    b.classList.toggle("confirm", !!a && Date.now() < recConfirmUntil);
    if (a && Date.now() < recConfirmUntil) {
      l.textContent = "TAP AGAIN TO END";
      b.title = "Tap again to end \"" + a.name + "\". Or wait to keep logging.";
    } else if (loud) {
      l.textContent = arch.text;
      b.title = arch.text + ". Tap twice to end \"" + a.name + "\".";
    } else if (a) {
      const min = Math.max(0, Math.round((Date.now() / 1000 - a.start_ts) / 60));
      l.textContent = (a.kind === "race" ? "REC·RACE " : "REC ") + min + "m";
      b.title = "Logging \"" + a.name + "\". Tap twice to end.";
    } else {
      l.textContent = "LOG";
      b.title = "Tap to start logging. Unlogged data is deleted after 7 days.";
    }
    // quiet: not recording with no session, and housekeeping notes ("Archive check overdue")
    if (down && !a) b.title += " " + arch.text + ".";
    if (arch && arch.note && !down) b.title += " " + arch.note + ".";
  }
  async function toggleRec() {
    const a = (App.session || {}).active;
    try {
      if (a) {
        // in-button confirmation — window.confirm is unreliable in iOS standalone
        // mode and its dialog buttons are a worse target than the button itself
        if (Date.now() >= recConfirmUntil) {
          recConfirmUntil = Date.now() + 5000;
          renderRec();
          setTimeout(renderRec, 5200);   // revert the label when the window lapses
          return;
        }
        recConfirmUntil = 0;
        await fetch(API + "/session/end", { method: "POST" });
      } else {
        const l = document.getElementById("recLbl");
        if (l) l.textContent = "STARTING…";   // instant feedback — the poll confirms
        await fetch(API + "/session/start", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: "{}" });
      }
      fetchSession();
    } catch (e) { fetchSession(); /* engine unreachable — re-sync restores the true state */ }
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
      `title="Tap = ${fly.has(s) ? "douse" : "hoist"} the ${s}${out.has(s) ? " (marked out)" : ""}">` +
      `${s}${out.has(s) ? "✕" : ""}</button>`).join("");
    document.getElementById("sbReef").innerHTML =
      `<button class="sb-chip reef${st.reef ? " on" : ""}" onclick="toggleReef()" ` +
      `title="${st.reef ? "Shake out reef 1" : "Tuck in reef 1"}">R1${st.reef ? " ▽" : ""}</button>`;
    // gear OUT✕ toggles live HERE (always visible in live mode), not in the plays block —
    // a sail blows regardless of which playbook generation is aboard
    document.getElementById("sbGear").innerHTML = SAILS_INV.map((s) =>
      `<button class="st-gear${out.has(s) ? " oos" : ""}" onclick="toggleGear('${s}')" ` +
      `title="Tap = mark the ${s} ${out.has(s) ? "back in use" : "out (blown or damaged)"}">${s}</button>`).join("");
    const note = document.getElementById("sbNote");   // may be cut short on a narrow bar
    note.textContent = note.title = fly.size
      ? "flying " + [...fly].join(" + ") + (st.reef ? " · reef 1 in" : "")
      : "Tap what's flying. Changes are logged.";
  }
  window.toggleGear = async function (sail) {
    const st = App.sailState || ((currentPlays() || {}).sail_state) || {};
    const out = new Set(st.out_of_service || []);
    if (out.has(sail)) out.delete(sail); else out.add(sail);
    try {
      const resp = await fetch(API + "/sails/state", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ out_of_service: [...out] }) });
      if (resp.ok) { App.sailState = await resp.json(); renderSailsBar(); }
      fetchPlays();
    } catch (e) { /* engine unreachable — the next poll re-syncs */ }
  };
  /* the branch SELECTOR: the executor's unified call — hold / switch to a pre-authored variant /
     off-script. Engine-computed (unifies wind-shift + deviation + drift over the frozen bundle). */
  async function fetchSelector() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/selector", 9000);
    if (r) App.selector = r;
    if (App.src === "live") render();
  }
  function currentSelector() {
    if (App.src === "demo") return (SCENARIOS[App.demoScn] || {}).selector || null;
    return App.selector;
  }
  /* ============ ROUTE — the Re-route now button (#125) ============
     BUTTON ONLY: a new route is made only when a crew member presses it. The engine may suggest
     one (/strategy `reroute_suggest`); nothing here runs on its own. The read and its words are
     pure and tested: reroute.js. One press posts /reroute/run (the Orin's full optimizer); when the
     Orin can't be reached the same button offers the Pi's own route (/reoptimize/run). */
  function deviceLabel() {
    let d = null;
    try {
      const q = new URLSearchParams(location.search).get("device");
      if (q) localStorage.setItem("sr33.dash.device", q.slice(0, 40));
      d = localStorage.getItem("sr33.dash.device");
      if (!d) {
        d = "iPad " + Math.random().toString(16).slice(2, 6);
        localStorage.setItem("sr33.dash.device", d);
      }
    } catch (e) { d = d || "iPad"; }
    return d;
  }
  async function fetchReroute() {
    clearTimeout(App.rrTimer);
    if (App.src === "live") {
      const r = await fetchJSON("/reroute?compact=1", 12000);
      if (r) { App.rr = r; App.rrAt = Date.now(); }
      render();
    }
    App.rrTimer = setTimeout(fetchReroute, RT.pollEvery(rerouteView(), App.rtPress, Date.now()));
  }
  function rerouteView() {
    const demo = App.src !== "live";
    const syn = currentSynthesis() || {};
    return RT.view({
      rr: demo ? (SCENARIOS[App.demoScn] || {}).reroute || { orin: { reachable: true }, pi: {} } : App.rr,
      rrAgeMs: demo ? 0 : Date.now() - App.rrAt, rrAt: App.rrAt,
      suggest: syn.reroute_suggest || null, sugAgeMs: demo ? 0 : Date.now() - App.synAt,
      press: App.rtPress, nowMs: Date.now(),
    });
  }
  /* POST that keeps the status code: {code, body}, or null with no answer at all */
  function postStatus(path, body, ms) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), ms || 15000);
    return fetch(API + path, { method: "POST", signal: ctl.signal,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}) })
      .then((r) => r.json().catch(() => ({})).then((b) => ({ code: r.status, body: b })))
      .catch(() => null).finally(() => clearTimeout(t));
  }
  window.rerouteNow = async function (ev, engine) {
    if (ev) { ev.stopPropagation(); ev.preventDefault(); }
    const v = rerouteView();
    if (v.button.disabled) return;
    const eng = engine || v.button.engine || "orin";
    if (App.src !== "live") {
      App.rtPress = { phase: "refused", engine: eng, at: Date.now(), detail: "Demo only. Nothing sent." };
      render(); return;
    }
    App.rtPress = { phase: "posting", engine: eng, at: Date.now() };
    render();
    const res = await postStatus(eng === "pi" ? "/reoptimize/run" : "/reroute/run",
      { device: deviceLabel() }, 15000);
    App.rtPress = RT.afterPress(eng, res ? res.code : null, res ? res.body : null, Date.now());
    render();
    fetchReroute();
    if (App.rtPress.phase === "unreachable") setTimeout(render, RT.ARM_MS + 50);   // arm the Pi offer
  };
  /* the ROUTE tile: a mini tile whose face carries the button itself (one tap, wet fingers);
     a tap anywhere else opens the detail */
  function routeTileEl() {
    const v = rerouteView();
    const el = document.createElement("div");
    el.className = "tile mini rt-tile s-" + v.status + (v.running ? " rt-running" : "");
    el.dataset.tile = "route";
    el.setAttribute("role", "button"); el.setAttribute("tabindex", "0");
    el.setAttribute("aria-label", "Route. " + v.say);
    const age = v.stale ? '<span class="t-age" title="No update for ' + fmtAge(v.ageMs) +
      '. Last good reading.">⏱ ' + fmtAge(v.ageMs) + "</span>" : "";
    const bar = v.progress ? '<div class="rt-bar"><span style="width:' + v.progress.pct + '%"></span></div>' : "";
    el.innerHTML =
      '<div class="t-head"><span class="t-name">' + NAME.route + "</span>" + age +
      '<span class="t-chip"><span class="t-word">' + v.chip + "</span></span></div>" +
      '<div class="rt-say rt-' + v.sayKind + '">' + esc2(v.say) + "</div>" + bar +
      (v.sub ? '<div class="rt-last">' + esc2(v.sub) + "</div>" : "") +
      rerouteBtnHtml(v);
    el.addEventListener("click", () => openDetail("route"));
    el.addEventListener("keydown", (e) => {
      if (e.target !== el) return;
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail("route"); }
    });
    return el;
  }
  function rerouteBtnHtml(v, engine) {
    const b = v.button;
    const lbl = engine === "orin" ? "Try the Orin again" : b.label;
    const eng = engine || b.engine || "orin";
    return '<button class="rt-btn' + (eng === "pi" ? " rt-pi" : "") + '"' +
      (b.disabled ? " disabled" : "") + ' onclick="rerouteNow(event,\'' + eng + '\')">' +
      esc2(lbl) + "</button>";
  }
  /* the ROUTE detail: the state, the button, the last route in full, and "show on GPS" */
  function routeDetailHtml() {
    const v = rerouteView();
    let h = '<div class="rt-detail">';
    if (v.progress) {
      h += '<p class="rt-line"><b>' + esc2(v.progress.text) + "</b>" +
        (v.progress.who ? " · asked by " + esc2(v.progress.who) : "") + "</p>" +
        '<div class="rt-bar"><span style="width:' + v.progress.pct + '%"></span></div>';
    } else if (v.note) {
      h += '<p class="rt-line rt-note">' + esc2(v.note) + "</p>" +
        (v.fallback ? '<p class="rt-line">' + esc2(v.sub) + "</p>" : "");
    }
    if (v.suggest && !v.progress) h += '<p class="rt-line rt-suggest">' + esc2(v.suggest) + "</p>";
    h += '<div class="rt-btns">' + rerouteBtnHtml(v) +
      (v.fallback ? rerouteBtnHtml({ button: { disabled: false, engine: "orin" } }, "orin") : "") + "</div>";
    const L = v.last;
    if (L) {
      h += '<div class="rc-title" style="margin-top:10px">' + (L.old ? "Old route · " : "Last route · ") + L.engine +
        (L.age ? " · " + esc2(L.age) : "") + "</div>" +
        '<p class="rt-head">' + esc2(L.head) + "</p>" +
        (L.labels.length ? '<p class="rt-labels">' + L.labels.map((x) =>
          '<span class="rt-label">' + esc2(x) + "</span>").join("") + "</p>" : "") +
        (L.shape ? '<p class="rt-line">' + esc2(L.shape) + "</p>" : "") +
        '<p class="rt-line">' + esc2(L.wx) + "</p>" +
        '<p class="rt-line">' + esc2(L.who) + "</p>" +
        L.warnings.map((w) => '<p class="rt-line rt-warn">' + esc2(w) + "</p>").join("");
    } else {
      h += '<p class="rt-line" style="margin-top:10px">No new route yet.</p>';
    }
    if (App.src === "live") {
      const g = App.gps, on = g && g.broadcasting;
      h += '<div class="st-gps"><div class="st-plays-head"><span>GARMIN 943</span>' +
        (!g || g.available === false ? '<span class="gps-note">plotter link off</span>'
          : on ? '<span class="gps-on">● showing “' + esc2(g.route || "route") + "”</span>"
            : '<span class="gps-note">not showing</span>') + "</div>" +
        '<div class="rt-btns"><button class="rt-btn rt-gps"' + (App.gpsBusy || !v.canShowOnGps ? " disabled" : "") +
        ' onclick="gpsShow(\'reroute\')">Show on GPS</button></div>' +
        (App.gpsNote ? '<span class="gps-note">' + esc2(App.gpsNote) + "</span>" : "") + "</div>";
    }
    return h + "</div>";
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
    if (r) { App.synthesis = r; App.synAt = Date.now(); }
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
      '<span class="st-syn-tag">OVERALL</span>' +
      (off ? '<span class="st-syn-badge">OFF-BOOK</span>' : "") +
      '<span class="st-spacer"></span>' +
      '<span class="st-syn-mode">' + (s.mode === "llm" ? "AI" : "AUTO") + '</span>' +
      (conf ? '<span class="st-syn-conf">' + conf + ' conf</span>' : "") + "</div>" +
      '<p class="st-syn-assess">' + stripTags(s.assessment || rec.action || "") + "</p>" +
      (note ? '<p class="st-syn-conc">' + stripTags(note) + "</p>" : "") + pmHtml + "</div>";
  }
  function selectorHtml() {
    const s = currentSelector();
    if (!s || s.available === false || s.action === "na") return "";
    const pill = { hold: "HOLD", switch: "SWITCH", off_script: "OFF-BOOK" }[s.action] || "—";
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
      m.push(metric("Done", pct + "%" + dist, false));
    }
    if (d.xte_nm != null) {
      const arrow = d.xte_trend === "diverging" ? " ↗" : d.xte_trend === "converging" ? " ↘" : "";
      m.push(metric("Off line", r1(d.xte_nm) + " nm " + (d.xte_side || "") + arrow, off && (d.xte_trend !== "converging")));
    }
    const bt = behindTxt(d.time_behind_s);
    if (bt) m.push(metric("Pace", bt, d.time_behind_s > 0 && off));
    if (d.vmc_kn != null && d.vmc_optimal_kn != null)
      m.push(metric("VMC", r1(d.vmc_kn) + "/" + r1(d.vmc_optimal_kn) + " kts", d.vmc_deficit_kn != null && d.vmc_deficit_kn > 0.3));
    const prog = pct != null
      ? '<div class="st-progress"><div class="st-fill" style="width:' + pct + '%"></div><div class="st-boat" style="left:' + pct + '%"></div></div>' : "";
    return '<div class="st-dev" style="--c:var(--' + (status === "na" ? "na" : status) + ')">' +
      '<div class="st-dev-head">TRACK · ' + stripTags(d.variant_label || d.variant || "—") + "</div>" +
      prog + (m.length ? '<div class="st-metrics">' + m.join("") + "</div>" : "") +
      (d.why || d.sub ? '<p class="st-why">' + stripTags(d.why || d.sub) + "</p>" : "") +
      (d.what_flips_it ? '<p class="st-flip">Switch if: <b>' + stripTags(d.what_flips_it) + "</b></p>" : "") + "</div>";
  }
  function driftHtml() {
    const fd = currentDrift();
    if (!fd || fd.available === false) return "";
    const status = fd.status || "na";
    const deg = fd.drift_twd_deg != null ? Math.round(fd.drift_twd_deg) : null;
    const tag = { ok: "holding", watch: "watch", act: "moved" }[status] || "";
    // always say right/left on deck — an older engine may still send veered/backed
    const side = { right: "right", left: "left", veered: "right", backed: "left" }[fd.drift_dir] || "";
    let body;
    if (status === "ok") {
      body = 'Forecast <b>holding</b>' + (deg != null ? " (" + deg + "° off plan)" : "");
    } else {
      const tws = (fd.drift_tws_kn != null && Math.abs(fd.drift_tws_kn) >= 1)
        ? " · " + (fd.drift_tws_kn >= 0 ? "+" : "−") + Math.abs(Math.round(fd.drift_tws_kn)) + " kts" : "";
      body = 'Forecast <b>' + (side ? "shifted " + deg + "° " + side : "moved " + deg + "°") + '</b>' + tws +
        ' <span class="sd-tag">' + tag + '</span>';
    }
    return '<p class="st-drift sd-' + status + '"><span class="sd-dot"></span><span>' + body + '</span></p>';
  }
  /* PLAN GAP — promised vs actual breeze, one stack line, only when straying (in its band) */
  function plangapHtml() {
    const pg = currentPlangap();
    if (!pg || pg.available === false || pg.status === "ok" || pg.status === "na") return "";
    const tag = pg.status === "act" ? "different wind" : "drifting";
    const body = 'Plan <b>' + pg.plan_twd + "°/" + pg.plan_tws_kn + ' kts</b> · now <b>' +
      pg.obs_twd + "°/" + pg.obs_tws_kn + ' kts</b> <span class="sd-tag">' + tag + '</span>';
    return '<p class="st-drift sd-' + pg.status + '" title="Our wind vs the plan forecast here">' +
      '<span class="sd-dot"></span><span>' + body + '</span></p>';
  }
  function playsHtml() {
    const pl = currentPlays();
    if (!pl || pl.available === false) return "";
    const rows = (pl.plays || []).filter((x) => x.status !== "quiet").slice(0, 4);
    // (the gear OUT✕ toggles moved to the always-visible CURRENT SAILS bar — a sail blows
    // regardless of which playbook generation is aboard)
    const out = new Set(((pl.sail_state || {}).out_of_service) || []);
    const gear = [...out].map((s) =>
      `<span class="st-gear oos" title="${s} marked out (change it on the sails bar)">${s}</span>`).join("");
    const items = rows.map((x) => {
      const call = x.guidance || x.summary || "";
      const badge = x.status === "armed" ? "ARMED" : "arming…";
      return `<div class="st-play y-${x.status === "armed" ? "act" : "watch"}">` +
        `<b>${x.name}</b> <span class="st-play-badge">${badge}</span>` +
        (x.stakes_min ? ` <span class="st-play-stakes">~${x.stakes_min}m at stake</span>` : "") +
        (x.corroborated ? ` <span class="st-play-stakes" title="Another signal agrees">✓ ${x.corroborated_by || "confirmed"}</span>` : "") +
        (call ? `<span class="st-play-call">${call}</span>` : "") + `</div>`;
    }).join("");
    // the WATCHLIST — quiet plays measurably close to arming ("what flips the plan"), with the
    // live number against the threshold (the matcher's distance-to-trigger)
    const wl = (pl.watchlist || []).map((w) => {
      const g = w.nearest_gap || {};
      const num = (g.actual != null && g.value != null)
        ? ` — ${g.signal} ${g.actual} vs ${g.op} ${g.value}` : "";
      const pct = Math.round((w.closeness || 0) * 100);
      return `<div class="st-watch"><span class="st-watch-pct">${pct}%</span>` +
        `<b>${w.name}</b>${num}</div>`;
    }).join("");
    return `<div class="st-plays"><div class="st-plays-head"><span>PLAYS</span>` +
      (gear ? `<span class="st-gearbox" title="Gear marked out. Arms the gear-loss plays.">` +
        `out: ${gear}</span>` : "") + `</div>` +
      (items || `<div class="st-play-none">No plays armed. Watching ` +
       `${pl.n_plays ?? 0} conditions.</div>`) +
      (wl ? `<div class="st-watch-head" title="How close each play is to firing">CLOSE TO FIRING</div>` + wl : "") + `</div>`;
  }
  function reoptHtml() {
    const v = rerouteView();
    if (!v.suggest) return "";
    return '<p class="st-reopt"><span class="ro-ico">⟳</span><span>' + esc2(v.suggest) +
      ' <span class="ro-tag">Route tile: Re-route now</span></span></p>';
  }
  /* ============ GARMIN 943 — push the gameplan onto the chartplotter (engine /gps/*) ============
     The one WRITE surface on the stack: crew-initiated, crew-cleared. The engine assembles the
     frozen variant path (or the onboard re-route) and the Pi's n2kout service broadcasts it on
     the boat's own N2K bus — pixels on the crew's own display, RRS-41 clean. */
  async function fetchGps() {
    if (App.src !== "live") return;
    App.gps = await fetchJSON("/gps/status", 6000);
    if (App.openTile === "playbook" || App.openTile === "route") populateDetail(App.openTile, false);
  }
  window.gpsShow = async function (source, variant) {
    App.gpsBusy = true;
    if (App.openTile === "playbook") populateDetail("playbook", false);
    if (App.openTile === "route") populateDetail("route", false);
    const r = await postJSON("/gps/show", { source: source, variant: variant || null }, 12000);
    App.gpsBusy = false;
    // shown without a land chart → say so ("GPS route doesn't check the shore.")
    App.gpsNote = r ? (r.shown ? (r.warning || "") : (r.note || "could not show")) : "no link to the boat";
    await fetchGps();
  };
  window.gpsClear = async function () {
    App.gpsBusy = true;
    await postJSON("/gps/clear", {}, 8000);
    App.gpsBusy = false;
    App.gpsNote = "";
    await fetchGps();
  };
  function gpsHtml() {
    if (App.src !== "live") return "";
    const g = App.gps;
    const on = g && g.broadcasting;
    const busy = App.gpsBusy ? " disabled" : "";
    const status = !g || g.available === false
      ? '<span class="gps-note">plotter link off</span>'
      : on ? `<span class="gps-on">● showing “${g.route || "route"}” (${g.n_waypoints} pts)</span>`
           : '<span class="gps-note">not showing</span>';
    const note = App.gpsNote ? ` <span class="gps-note">${App.gpsNote}</span>` : "";
    return '<div class="st-gps"><div class="st-plays-head"><span>GARMIN 943</span>' + status +
      '</div><div class="cw-btns">' +
      `<button class="cw-btn"${busy} onclick="gpsShow('playbook')">▶ Gameplan route</button>` +
      `<button class="cw-btn"${busy} onclick="gpsShow('reroute')">⟳ New route</button>` +
      `<button class="cw-btn"${busy} onclick="gpsClear()">✕ Clear</button>` +
      '</div>' + note + '</div>';
  }
  function strategyStackHtml() {
    const parts = [synthHtml(), selectorHtml(), deviationHtml(), driftHtml(), plangapHtml(),
      playsHtml(), reoptHtml(), gpsHtml()].filter(Boolean);
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
  /* HOLD a failed poll's endpoint at its last good value for a bounded window.
   *
   * fetchJSON maps any failure — including its own 5 s abort — to null, and the tile builders
   * turn null into NA("—"). One slow engine response was therefore indistinguishable from a
   * dead sensor, and blanked the readout until the next cycle happened to succeed. commitStatus
   * did not save us: it dwells the STATUS enum, while the tile's value/sub/why come straight
   * off the NA object, so a missed poll showed a dash under a still-committed "OK" dot.
   *
   * The secondary pollers (deviation/drift/trend/selector/checklist) already do `if (r) App.x = r`,
   * i.e. hold indefinitely. This brings the main poll into line — but BOUNDED, and with the age
   * surfaced, because silently showing minutes-old wind is its own way to lose a race. Past
   * HOLD_MS the endpoint really is absent and NA is the honest answer.
   */
  function hold(key, val, now) {
    const f = App.fresh[key];
    if (val != null) { App.fresh[key] = { v: val, t: now }; App.ages[key] = 0; return val; }
    if (f && (now - f.t <= HOLD_MS || now < App.graceUntil)) { App.ages[key] = now - f.t; return f.v; }
    App.ages[key] = Infinity;
    return null;
  }
  /* which poll endpoints each tile is built from — a tile is only as fresh as its slowest one */
  const TILE_SRC = {
    wind: ["conditions"], playbook: ["tactics"], forecast: ["forecast"],
    sail: ["sail", "conditions"], eta: ["navigator"], ais: ["fleet"],
    charge: ["fatigue", "watch"], data: ["sources", "health"], bank: ["power"],
  };
  function tileAge(key) {
    let worst = 0;
    for (const src of TILE_SRC[key] || []) {
      const a = App.ages[src];
      if (a === Infinity) return Infinity;
      if (a > worst) worst = a;
    }
    return worst;
  }
  const fmtAge = (ms) => (ms < 60000 ? Math.round(ms / 1000) + "s" : Math.round(ms / 60000) + "m");

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
    powerTimer: null, healthTimer: null, power: null, health: null,
    dwell: {}, fresh: {}, ages: {}, data: null, windHist: [], fcstHist: [], seriesHist: [], lastPersist: 0, brief: null,
    coach: null, deviation: null, forecastDrift: null, selector: null, synAt: 0,
    rr: null, rrAt: 0, rrTimer: null, rtPress: {},
    plangap: null, trend: null, briefBusy: {}, gps: null, gpsBusy: false, gpsNote: "",
    checklist: null, chkTimer: null,
    plume: null, plumeAt: 0, plumeWait: "", plumeTimer: null, plumeView: { field: "tws", only: null },
    detailStreamKey: null, detailAbort: null,
    lastOkAt: 0, lastPollOk: false, graceUntil: 0, startedAt: Date.now(),
  };
  function currentData() {
    if (App.src === "demo") {
      const sc = SCENARIOS[App.demoScn];
      return { tiles: sc.tiles, focus: sc.focus, notes: sc.notes, confidence: sc.confidence, mode: sc.mode };
    }
    return App.data || { tiles: {}, focus: "Connecting to the boat…", notes: [], confidence: "engine", mode: "engine read" };
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
  /* one tile element; `mini` is the half-height form used by the stacked SYSTEMS pair */
  function tileEl(key, t, mini) {
    const st = STATUS[t.status] || STATUS.na;
    const el = document.createElement("div");
    el.className = "tile s-" + (t.status || "na") + (mini ? " mini" : "");
    el.dataset.tile = key;
    el.setAttribute("role", "button"); el.setAttribute("tabindex", "0");
    el.setAttribute("aria-label", NAME[key] + " " + st.word + " " + stripTags(t.value || ""));
    const valHtml = (t.value != null && t.value !== "") ? '<div class="t-val">' + t.value + '</div>' : "";
    const ageHtml = t.staleMs
      ? '<span class="t-age" title="No update for ' + fmtAge(t.staleMs)
        + '. Last good reading.">⏱ ' + fmtAge(t.staleMs) + '</span>'
      : "";
    // PROVENANCE, on the tile face: a number coming off a backup sensor is still a number, and
    // the crew is entitled to know which sensor it came from before they steer on it.
    const flagged = provFlagged(key);         // demo-aware: currentHealth() serves the scenario
    const flagHtml = flagged
      ? '<span class="t-flag" title="On a backup sensor: ' + esc2(flagged) + '">⚑</span>'
      : "";
    el.innerHTML =
      '<div class="t-head"><span class="t-name">' + NAME[key] + '</span>' + flagHtml + ageHtml +
      '<span class="t-chip"><span class="t-icon">' + st.icon + '</span><span class="t-word">' + st.word + '</span></span></div>' +
      valHtml + (t.chart ? t.chart : "") + (t.sub ? '<div class="t-sub">' + t.sub + '</div>' : "") + (t.chart ? "" : rowsHtml(t.rows));
    el.addEventListener("click", () => openDetail(key));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail(key); } });
    return el;
  }
  function render() {
    const d = currentData();
    const grid = document.getElementById("grid");
    grid.innerHTML = "";
    const one = (k, mini) => (k === "route" ? routeTileEl()
      : tileEl(k, d.tiles[k] || STATUS_PLACEHOLDER(), mini));
    for (const cell of CELLS) {
      if (!Array.isArray(cell)) { grid.appendChild(one(cell, false)); continue; }
      const pair = document.createElement("div");
      pair.className = "tile-pair";
      for (const k of cell) pair.appendChild(one(k, true));
      grid.appendChild(pair);
    }
    renderCommentary(d);
    if (App.openTile && !document.getElementById("detail").hidden) populateDetail(App.openTile, false);
  }
  function renderCommentary(d) {
    renderCoach();
    const focusEl = document.getElementById("commFocus");
    focusEl.textContent = d.focus || "—";
    if (d.link) focusEl.dataset.link = d.link; else delete focusEl.dataset.link;
    document.getElementById("commConf").textContent = "conf: " + (d.confidence || "—");
    const pill = document.getElementById("modePill");
    pill.dataset.mode = d.mode === "llm-live" ? "llm" : "engine";
    document.getElementById("modeLbl").textContent = d.mode || "engine read";
    const ul = document.getElementById("commNotes");
    ul.innerHTML = "";
    if (!d.notes || !d.notes.length) {
      const li = document.createElement("li"); li.className = "tg-ok";
      li.innerHTML = '<span class="note-txt">Nothing flagged. Tap a tile for detail.</span>';
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
  /* ---- WATCH SYSTEM panel (CREW tile detail): schedule + the live quick edits ----
     Editing surfaces: the Lab authors the plan; aboard, the quick actions cover what crews
     actually do mid-race (hold a watch, swap teams, all hands), and tapping a block's team
     cycles A → B → ALL for anything else. Every edit POSTs the engine /watch (kv-persisted,
     logged) — demo mode shows the panel read-only. */
  function watchPanelHtml() {
    const w = watchNow(), live = App.src === "live";
    const btn = (label, act, mins) =>
      '<button class="wq-btn" onclick="watchAct(\'' + act + "'" + (mins != null ? ", " + mins : "") + ')">' + label + "</button>";
    if (!w || !w.plan_set) {
      return '<div class="rc-title" style="margin-top:8px">Watch system</div>' +
        '<div class="dc-foot">No watch plan. Make one in the Lab (Races → Watch plan).</div>' +
        (live ? '<div class="wq-actions">' + btn("Start ALL HANDS 4h", "all_hands", 240) + "</div>" : "");
    }
    const now = Math.floor(Date.now() / 1000);
    const head = w.active
      ? "<b>" + (w.all_hands ? "ALL HANDS" : teamTxt(w, w.on) + " on deck") + "</b>" +
        (w.next_change ? " · change " + fmtHM(w.next_change) + " (" + fmtMins(w.mins_to_change) + ")" +
          (w.next_on_label ? " → " + w.next_on_label : "") : " · last block")
      : "No block active" + (w.next_change ? " — watches start " + fmtHM(w.next_change) : " — plan ended");
    let rows = "";
    (w.blocks || []).forEach(function (b, i) {
      if (b.end <= now || rows.split("t-row").length > 7) return;   // past blocks out; ~6 rows max
      const cur = b.start <= now && now < b.end;
      const lbl = b.on === "ALL" ? "ALL HANDS" : ((w.teams || {})[b.on] || {}).name || b.on;
      rows += '<div class="t-row' + (cur ? " emph" : "") + '"><span class="rl">' +
        (cur ? "▶ " : "") + fmtHM(b.start) + "–" + fmtHM(b.end) + '</span><span class="rc">' +
        (live ? '<button class="wq-team" onclick="watchCycleBlock(' + i + ')">' + lbl + "</button>" : lbl) +
        "</span>" + (b.note ? '<span class="rc">' + b.note + "</span>" : "") + "</div>";
    });
    return '<div class="rc-title" style="margin-top:8px">Watch system</div>' +
      '<div class="dc-foot">' + head + "</div>" +
      '<div class="t-rows">' + rows + "</div>" +
      '<div class="dc-foot">A = ' + teamTxt(w, "A") + " · B = " + teamTxt(w, "B") + "</div>" +
      (live ? '<div class="wq-actions">' + btn("Hold +30m", "hold", 30) + btn("Hold +1h", "hold", 60) +
        btn("Swap teams", "swap") + btn("All hands 30m", "all_hands", 30) + "</div>" : "");
  }
  window.watchAct = async function (action, minutes) {
    try {
      const resp = await fetch(API + "/watch", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(minutes != null ? { action: action, minutes: minutes } : { action: action }) });
      if (resp.ok) {
        App.watch = await resp.json();
        if (App.openTile === "charge") populateDetail("charge", false);
      }
    } catch (e) { /* engine unreachable — the next poll re-syncs */ }
  };
  window.watchCycleBlock = async function (i) {
    const w = App.watch;
    if (!w || !w.blocks || !w.blocks[i]) return;
    const order = { A: "B", B: "ALL", ALL: "A" };
    const blocks = w.blocks.map(function (b, j) {
      return j === i ? Object.assign({}, b, { on: order[b.on] || "A" }) : b;
    });
    try {
      const resp = await fetch(API + "/watch", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan: { teams: w.teams, blocks: blocks, log: w.log } }) });
      if (resp.ok) {
        App.watch = await resp.json();
        if (App.openTile === "charge") populateDetail("charge", false);
      }
    } catch (e) { /* re-sync on next poll */ }
  };

  function openDetail(key) {
    App.openTile = key;
    document.getElementById("overlay").hidden = false;
    document.getElementById("detail").hidden = false;
    if (key === "wind" && App.src === "live") {
      fetchSeries().then(() => { if (App.openTile === "wind") populateDetail("wind", false); });
      fetchBuoys().then(() => { if (App.openTile === "wind") populateDetail("wind", false); });
    }
    if ((key === "playbook" || key === "route") && App.src === "live") fetchGps();   // the GARMIN 943 row's live state
    if (key === "eta" && App.src === "live") {
      fetchCourse().then(() => { if (App.openTile === "eta") populateDetail("eta", false); });
      fetchTrack().then(() => { if (App.openTile === "eta") populateDetail("eta", false); });
    }
    // the gauge scrolls with the text now (#77): a fresh tile opens at its top
    document.querySelector("#detail .det-body").scrollTop = 0;
    populateDetail(key, true);
  }
  /* ============ the COACH WINDOW — the commentary panel's tap-detail ============
     Everything the copilot volunteers, in one place: the latest coach line + active callouts,
     the held CREW BRIEFS (handover / recap / mark / watchlist — timer-scheduled on the Orin),
     a "Brief me" button per kind, and the recent-brief rail. */
  const BRIEF_KINDS = [["handover", "Watch handover"], ["recap", "Last hour"],
                       ["mark", "Mark approach"], ["watchlist", "What flips the plan"]];
  window.openCoachBrief = function (kind) {   // a tile hook: jump to the coach window + fetch
    openDetail("coach");
    window.crewBrief(kind);
  };
  window.crewBrief = async function (kind) {
    App.briefBusy[kind] = true;
    const btn = document.getElementById("cwb-" + kind);
    if (btn) { btn.classList.add("busy"); btn.textContent = "thinking…"; }
    try {
      const ctl = new AbortController();
      const to = setTimeout(() => ctl.abort(), 90000);   // LLM phrasing can take ~a minute
      const resp = await fetch(COPILOT + "/briefs/" + kind, {
        headers: { Accept: "application/json" }, signal: ctl.signal });
      clearTimeout(to);
      const r = resp.ok ? await resp.json() : null;
      if (r && r.available) {
        App.coach = App.coach || {};
        App.coach.briefs = App.coach.briefs || {};
        App.coach.briefs[kind] = r;
      }
    } catch (e) { /* the held copy (if any) stands */ }
    App.briefBusy[kind] = false;
    if (App.openTile === "coach") populateDetail("coach", false);
  };
  function briefCardHtml(kind, label, b) {
    if (!b) return "";
    const mode = b.mode === "llm" ? "AI" : "auto";
    const when = b.generated_at ? agoText(b.generated_at) : "";
    return '<div class="cw-card"><div class="cw-card-head"><b>' + label + '</b>' +
      '<span class="cw-tag">' + mode + '</span><span class="cw-when">' + when + '</span></div>' +
      '<div class="cw-head2">' + escapeHtml(b.headline || "") + '</div>' +
      '<div class="cw-body">' + escapeHtml(b.body || "") + '</div></div>';
  }
  function coachWindowHtml() {
    const c = App.coach;
    let out = '<div class="cw-btns">' + BRIEF_KINDS.map(([k, lbl]) =>
      '<button class="cw-btn' + (App.briefBusy[k] ? " busy" : "") + '" id="cwb-' + k +
      '" onclick="crewBrief(\'' + k + '\')">' +
      (App.briefBusy[k] ? "thinking…" : lbl + " ↻") + '</button>').join("") + '</div>';
    if (!c) {
      return out + '<div class="dc-foot">Copilot offline. No briefs yet.</div>';
    }
    const briefs = c.briefs || {};
    out += BRIEF_KINDS.map(([k, lbl]) => briefCardHtml(k, lbl, briefs[k])).join("");
    if (c.active && c.active.length) {
      out += '<div class="rc-title" style="margin-top:8px">Active callouts</div>' +
        c.active.slice(0, 5).map((a) => '<div class="cw-callout y-' +
          (a.urgency === "now" ? "act" : "watch") + '">' + escapeHtml(a.headline || "") +
          (a.detail ? " — " + escapeHtml(a.detail) : "") + '</div>').join("");
    }
    const hist = (c.brief_history || []).slice(0, 6);
    if (hist.length) {
      out += '<div class="rc-title" style="margin-top:8px">Recent briefs</div>' +
        hist.map((h) => '<div class="cw-hist">' + agoText(h.at) + ' · ' + h.kind + ' — ' +
          escapeHtml(h.headline || "") + '</div>').join("");
    }
    return out;
  }
  /* ============ INSTRUMENT HEALTH + PROVENANCE (the DATA tile's detail) ============
     Five checks, each shown with its own verdict rather than rolled into one word, because they
     fail for different reasons and the crew's response differs: a misaligned compass is a
     navigation problem, an unbindable priority matcher is a configuration problem, and AIS
     leading own-ship position is a "stop trusting this screen" problem.
     What each check means (kept here, not on the iPad):
       attitude     — a monohull cannot sit at 133° of roll; one sample is enough (the kicked
                      sensor at 22:58Z on Jul 18).
       heading      — under sail heading and GPS course agree within leeway + current; a steady
                      bias for hours means the compass is misaligned.
       policy_binds — every ranked device name in the policy must match a device on this bus; an
                      unmatched rule looks exactly like a satisfied one.
       lead_source  — channels running on a backup because the ranked sensor went quiet.
       own_ship     — the AIS transceiver publishes other vessels on the same bus; if one leads an
                      own-ship channel, the number is another boat's. */
  const HEALTH_ROWS = [
    ["attitude", "Heel and pitch possible", (h) => h.attitude],
    ["heading", "Compass vs GPS", (h) => h.heading],
    ["policy_binds", "Sensor list matches boat", (h) => (h.provenance || {}).checks?.policy_binds],
    ["lead_source", "Main sensors in use", (h) => (h.provenance || {}).checks?.lead_source],
    ["own_ship", "No other boats' data", (h) => (h.provenance || {}).checks?.own_ship],
  ];
  const HEALTH_WORD = { ok: "OK", warn: "WATCH", danger: "ACT", unknown: "—" };
  function healthPanelHtml() {
    const h = currentHealth();
    if (!h || !h.available) {
      return '<div class="dc-foot">Sensor checks offline' + (h && h.note ? " (" + esc2(h.note) + ")" : "") + '.</div>';
    }
    let out = '<div class="rc-title">Sensor checks</div>';
    for (const [, label, pick] of HEALTH_ROWS) {
      let c = null;
      try { c = pick(h); } catch (e) { c = null; }
      const s = (c && c.status) || "unknown";
      out += '<div class="hc-row st-' + s + '">' +
        '<span class="hc-word">' + (HEALTH_WORD[s] || s) + '</span>' +
        '<span class="hc-txt"><b>' + label + '</b>' +
        '<span class="hc-sub">' + esc2((c && c.reason) || "no reading") +
        ((c && c.note) ? " — " + esc2(c.note) : "") + '</span></span></div>';
    }
    return out;
  }
  /* PROVENANCE: every channel the iPad reads, and the device it came from. This is the table
     that would have answered "which wind are we looking at?" — the GND10 masthead and the Orca's
     computed apparent wind disagreed by up to 5.9 kn on Jul 18 and the dashboard showed whichever
     had written last, unlabelled. */
  function provenanceTableHtml() {
    const p = (currentHealth() || {}).provenance || {};
    const chans = p.channels || {};
    const keys = Object.keys(chans);
    if (!keys.length) return "";
    const order = ["tws", "twd", "twa", "aws", "awa", "stw", "sog", "cog", "heading_true",
                   "heading_mag", "heel", "pitch", "rate_of_turn", "lat", "lon", "depth",
                   "water_temp", "rudder_angle", "bank_voltage"];
    keys.sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99));
    let out = '<div class="rc-title" style="margin-top:10px">Where each number comes from</div>' +
      '<div class="t-rows"><div class="t-row hdr"><span class="rl"></span>' +
      '<span class="rc">value</span><span class="rc">device</span><span class="rc">age</span></div>';
    for (const k of keys) {
      const e = chans[k];
      const marks = (e.fell_back ? " ⚑" : "") + (e.measured === false ? " ƒ" : "") +
        (e.disagreement ? " ≠" : "");
      out += '<div class="t-row' + (e.fell_back ? " emph" : "") + '">' +
        '<span class="rl">' + (CH_LABEL[k] || k) + '</span>' +
        '<span class="rc">' + (e.value == null ? "—" : e.value) + " " + esc2(e.unit || "") + '</span>' +
        '<span class="rc">' + esc2(e.device || e.source || "?") +
        (e.rank ? " #" + e.rank : "") + marks + '</span>' +
        '<span class="rc">' + (e.age_s == null ? "—" : Math.max(0, Math.round(e.age_s)) + "s") +
        (e.sources > 1 ? " /" + e.sources : "") + '</span></div>';
    }
    out += '</div><div class="dc-legend">⚑ backup sensor · ƒ computed · ≠ sources disagree · ' +
      '#n priority · /n sources reporting</div>';
    return out;
  }
  /* the BANK detail: the number, the trend, and the projection, plus the unconfirmed-threshold
     warning — the absolute levels are 12 V lead-acid guesses until somebody checks the bank */
  function bankPanelHtml() {
    const b = currentPower();
    if (!b || b.available === false) {
      return '<div class="dc-foot">No bank voltage' + (b && b.note ? " (" + esc2(b.note) + ")" : "") +
        '. Check the Orca Core is sharing it.</div>';
    }
    const st = b.status === "danger" ? "act" : b.status === "warn" ? "watch" : "ok";
    const rows = [
      ["now", b.volts != null ? b.volts.toFixed(2) + " V" : "—", "5-min smoothed"],
      ["status uses", b.volts_decided != null ? b.volts_decided.toFixed(2) + " V" : "—",
       r0(b.sustain_min) + "-min smoothed"],
      ["last sample", b.volts_last != null ? b.volts_last + " V" : "—", "raw"],
      ["lowest", b.volts_min != null ? b.volts_min + " V" : "—",
       "in the last " + r0(b.window_min) + " min"],
      ["trend", b.trend_v_per_h == null ? "—"
        : (b.trend_v_per_h >= 0 ? "+" : "") + b.trend_v_per_h.toFixed(2) + " V/h",
       b.charging ? "charging" : "over " + r0(b.window_min) + " min"],
      ["to " + r1(b.thresholds.floor_v) + " V", b.hours_to_floor == null ? "—"
        : "~" + r1(b.hours_to_floor) + " h",
       b.dark_at_epoch ? "about " + fmtHM(b.dark_at_epoch) : "at the current rate"],
    ];
    let out = '<div class="rc-title">House bank</div>' +
      '<div class="en-row"><span class="en-score" style="color:var(--' + st + ')">' +
      (b.volts != null ? b.volts.toFixed(2) : "—") + '</span><span class="en-side">V · ' +
      esc2(b.status) + '</span></div><div class="t-rows">';
    for (const [l, v, note] of rows) {
      out += '<div class="t-row"><span class="rl">' + l + '</span><span class="rc">' + v +
        '</span><span class="rc">' + esc2(note) + '</span></div>';
    }
    // the absolute levels are 12 V lead-acid defaults; nothing records this bank's chemistry
    // or capacity — someone at the boat needs to check it and set POWER_*
    out += '</div><div class="dc-foot">Limits: warn ' + r1(b.thresholds.warn_v) +
      ' V · danger ' + r1(b.thresholds.danger_v) + ' V · floor ' + r1(b.thresholds.floor_v) + ' V</div>' +
      '<div class="dc-foot" style="color:var(--watch)">⚠ Generic 12 V limits, not checked on this boat. ' +
      'Trust the trend.</div>';
    return out;
  }

  /* ============ RACE CHECKLIST — the SI/NOR requirement reminders ============
     The engine /checklist evaluates the homework's `deliver_to_ipad` items against the live
     picture (sunset window, mark proximity, finish approach). ACTIVE items ride the bar under
     the sails bar until acked; the tap-detail lists the whole race checklist. Live source only
     (the bar is an action surface, not a demo prop). */
  async function fetchChecklist() {
    if (App.src !== "live") return;
    const r = await fetchJSON("/checklist", 6000);
    if (r) { App.checklist = r; renderChkBar(); }
  }
  window.chkAck = async function (id, undo) {
    try {
      const resp = await fetch(API + "/checklist/ack", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(undo ? { id: id, undo: true } : { id: id }) });
      if (resp.ok) { App.checklist = await resp.json(); renderChkBar(); }
    } catch (e) { /* re-sync on the next poll */ }
    if (App.openTile === "checklist") populateDetail("checklist", false);
  };
  window.openChecklist = function () { openDetail("checklist"); };   // inline-onclick hook (IIFE scope)
  const chkShort = (t) => { t = t || ""; const cut = t.indexOf(". "); if (cut > 10 && cut < 70) return t.slice(0, cut); return t.length > 70 ? t.slice(0, 67) + "…" : t; };
  function renderChkBar() {
    const bar = document.getElementById("chkBar"), c = App.checklist;
    const act = (c && App.src === "live" && c.active) || [];
    bar.hidden = !act.length;
    if (!act.length) return;
    document.getElementById("chkItems").innerHTML = act.map((it) =>
      '<span class="chk-item' + (it.critical ? " crit" : "") + '" onclick="openChecklist()">' +
      escapeHtml(chkShort(it.text)) +
      (it.measure ? ' <span class="chk-meas">' + escapeHtml(it.measure) + '</span>' : "") +
      '<button class="chk-ack" title="Mark done (logged)" onclick="event.stopPropagation();chkAck(\'' + it.id + '\')">✓ DONE</button></span>').join("");
  }
  function checklistHtml() {
    const c = App.checklist;
    if (!c || !c.plan_set) {
      return '<div class="dc-foot">No race checklist. Load it from the Lab.</div>';
    }
    const order = { active: 0, pending: 1, manual: 2, done: 3 };
    const items = (c.items || []).slice().sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
    const word = { active: "DUE NOW", pending: "later", manual: "manual", done: "done ✓" };
    return items.map((it) =>
      '<div class="chk-row st-' + it.status + '"><span class="chk-txt">' + escapeHtml(it.text) +
      '<span class="chk-sub">' + [word[it.status] || it.status, it.measure, it.source]
        .filter(Boolean).map(escapeHtml).join(" · ") + '</span></span>' +
      (it.status === "done"
        ? '<button class="wq-btn" onclick="chkAck(\'' + it.id + '\', true)">undo</button>'
        : '<button class="chk-ack" onclick="chkAck(\'' + it.id + '\')">✓ DONE</button>') +
      '</div>').join("");
  }
  function populateDetail(key, stream) {
    // the checklist and coach windows ARE their list — it leads; every tile leads with the text
    document.getElementById("detail").classList.toggle("gauge-first", key === "checklist" || key === "coach" || key === "route");
    if (key === "checklist") {
      document.getElementById("detName").textContent = NAME.checklist;
      const c = App.checklist || {};
      const n = (c.counts && c.counts.active) || 0;
      const ds = document.getElementById("detStatus");
      ds.innerHTML = c.plan_set ? (n ? n + " due now" : "nothing due") : "not loaded";
      ds.style.color = n ? "var(--act)" : "var(--na)";
      document.getElementById("detGauge").innerHTML = checklistHtml();
      // items arm at their trigger (sunset, mark approach, finish approach) and stay until
      // acked; acks are logged onboard and a sunset item re-arms the next evening
      document.getElementById("detWhy").textContent = "Reminders from the race rules (SIs/NOR).";
      document.getElementById("detConsider").textContent = "Tap ✓ DONE when it's done.";
      document.getElementById("detClears").textContent = "—";
      document.getElementById("detBased").textContent = "race rules (SIs/NOR)";
      return;
    }
    if (key === "route") {
      const v = rerouteView();
      document.getElementById("detName").textContent = NAME.route;
      const ds = document.getElementById("detStatus");
      ds.textContent = v.chip.toLowerCase();
      ds.style.color = "var(--na)";
      document.getElementById("detGauge").innerHTML = routeDetailHtml();
      document.getElementById("detWhy").textContent = v.suggest || "No re-route suggested now.";
      document.getElementById("detConsider").textContent =
        "Re-route now runs once. About a minute. Nothing runs by itself.";
      document.getElementById("detClears").textContent = "—";
      document.getElementById("detBased").textContent = v.last && v.last.engine === "Pi"
        ? "Pi re-router: own polars, Open-Meteo forecast"
        : "Orin optimizer: own polars, public forecasts";
      return;
    }
    if (key === "coach") {
      document.getElementById("detName").textContent = NAME.coach;
      const c = App.coach || {};
      const ds = document.getElementById("detStatus");
      ds.innerHTML = c.running ? "auto · every " + Math.round(c.interval_s || 30) + " s"
                               : "auto off";
      ds.style.color = "var(--na)";
      document.getElementById("detGauge").innerHTML = coachWindowHtml();
      // scheduled briefs: watch handover at T-15, hourly recap while racing, mark pre-brief
      // at ~T-20, plus the live callouts; engine numbers first, LLM phrasing when the Orin is up
      document.getElementById("detWhy").textContent = "Briefs and callouts from the copilot.";
      document.getElementById("detConsider").textContent = "Tap a button for a fresh brief.";
      document.getElementById("detClears").textContent = "—";
      document.getElementById("detBased").textContent = "copilot";
      return;
    }
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
        '<div class="rc-title">Wind speed over the race · ' + fmtDur(dur) + '</div>' + stats +
        '<div style="--c:' + stColor + '">' + raceChart(hist, 512, 150) + '</div>' +
        '<div class="rc-title" style="margin-top:8px">Direction change over the race</div>' +
        '<div class="detchart" style="--c:' + stColor + '"><div class="dc-lab dc-top">↑ shifted right</div>' +
        (makeTwdSpark(hist, 512, 60) || '<span class="dc-empty">…</span>') +
        '<div class="dc-lab dc-bot">↓ shifted left</div></div>' +
        (App.src === "live" ? buoysLadderHtml() : "") + rowsHtml(t.rows);
    } else if (key === "forecast" && plumeNow()) {
      // the full plume (every model, older runs fading, picker + speed/direction toggle), then
      // the Open-Meteo rows (+60/+120, forecast vs actual) underneath
      g.innerHTML = plumeDetailHtml(App.fcObs) + rowsHtml(t.rows);
    } else if (key === "playbook") {
      // the full strategy stack lives here now (synthesis → selector → track → drift → plays →
      // re-route), followed by the variant-agreement rows
      g.innerHTML = strategyStackHtml() + (t.rows ? rowsHtml(t.rows) : "");
    } else if (key === "charge") {
      // CREW: the ENERGY section (score + level + gauge, always shown), then the watch panel
      // (schedule + live quick edits), then the fatigue component bars
      let energy = '<div class="rc-title">Crew energy</div>';
      if (t.energy != null) {
        const eLvl = t.level || "";
        const eSt = /rotate/.test(eLvl) ? "act" : eLvl === "watch" ? "watch" : "ok";
        energy += '<div class="en-row"><span class="en-score" style="color:var(--' + eSt + ')">' + t.energy + '</span>' +
          '<span class="en-side">/ 100 · ' + eLvl +
          '<span class="bartrk en-trk"><span class="barfil" style="width:' + Math.max(0, Math.min(100, t.energy)) + '%;background:var(--' + eSt + ')"></span></span></span></div>' +
          '<div class="dc-foot">Lower = more tired. ' +
          (eSt === "ok" ? "Driver fresh." : eSt === "watch" ? "Keep an eye on it." : "Rotate the helm.") + '</div>';
      } else {
        energy += '<div class="dc-foot">No helm data yet.</div>';
      }
      let bars = "";
      if (t.components) {
        bars = '<div class="rc-title" style="margin-top:10px">What is tiring the helm</div><div class="bars">';
        for (const lbl of Object.keys(t.components)) {
          bars += '<div class="bar"><span class="barlbl">' + lbl + '</span><span class="bartrk"><span class="barfil" style="width:' + Math.round(t.components[lbl] * 100) + '%"></span></span></div>';
        }
        bars += "</div>";
      }
      // the handover brief on demand — the watch-change synthesis lives in the coach window
      const hbtn = '<button class="cw-btn cw-inline" onclick="openCoachBrief(\'handover\')">' +
        'Watch handover brief ↻</button>';
      g.innerHTML = energy + hbtn + watchPanelHtml() + bars;
    } else if (key === "data") {
      // the instrument-health detail: the five cross-checks, then the full provenance table
      g.innerHTML = healthPanelHtml() + provenanceTableHtml();
    } else if (key === "bank") {
      g.innerHTML = bankPanelHtml();
    } else if (key === "eta" && App.src === "live" && courseMapHtml()) {
      g.innerHTML = '<div class="dc-foot">' + stripTags(t.value || "—") +
        (t.sub ? " · " + t.sub : "") + '</div>' + courseMapHtml();
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
    // per-tile coach hooks — appended whatever branch built the gauge above
    if (key === "sail" && App.trend && App.trend.available && App.trend.read) {
      // the observed wind trend (the full threshold clock lives in the handover brief)
      g.insertAdjacentHTML("beforeend", '<div class="dc-foot">Trend: ' +
        escapeHtml(App.trend.read) + '</div>');
    }
    if (key === "forecast") {
      const pg = currentPlangap();
      if (pg && pg.available) {   // did the promised breeze show up on deck?
        g.insertAdjacentHTML("beforeend", '<div class="dc-foot" title="Our wind vs the plan forecast here">' + escapeHtml(pg.value || "") +
          (pg.sub ? " · " + escapeHtml(pg.sub) : "") + '</div>');
      }
    }
    if (key === "eta") {
      g.insertAdjacentHTML("beforeend", '<button class="cw-btn cw-inline" ' +
        'onclick="openCoachBrief(\'mark\')">Mark pre-brief ↻</button>');
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
      const eps = ["/conditions", "/sail", "/navigator", "/tactics", "/fatigue", "/forecast?hours=6", "/sources", "/fleet", "/watch"];
      const keys = ["conditions", "sail", "navigator", "tactics", "fatigue", "forecast", "sources", "fleet", "watch"];
      const ms   = [5000, 5000, 5000, 5000, 5000, 9000, 5000, 6000, 5000];
      const res = await Promise.all(eps.map((e, i) => fetchJSON(e, ms[i])));
      const now = Date.now();
      App.lastPollOk = res.some((r) => r != null);
      if (App.lastPollOk) App.lastOkAt = now;
      const p = {}; keys.forEach((k, i) => (p[k] = hold(k, res[i], now)));
      App.watch = p.watch;               // the CREW detail panel + quick actions read it
      App.nav = p.navigator;             // the eta-detail course map reads position live
      pushWind(p.conditions);
      pushForecast(p.forecast);
      App.data = buildLive(p);
      if (App.src === "live") render();
    } finally { App.polling = false; }
  }
  function startPolling() { poll(); if (!App.pollTimer) App.pollTimer = setInterval(poll, 3000); }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) onWake(); });
  window.addEventListener("pageshow", (e) => { if (e.persisted) onWake(); });
  window.addEventListener("online", onWake);

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
    if (c.category === "watch") return "watch";  // T-15 watch change — the wake-the-next-team tone
    if (c.category === "checklist") return "watch"; // a race requirement came due (DSQ-risk action item)
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
    fetchPlume();                   // the Lab's forecast plume, cached on the Pi — slow-moving
    App.plumeTimer = setInterval(fetchPlume, PLUME_EVERY);
    fetchDrift();                   // forecast-drift (engine, common-data), then on its own cadence
    App.driftTimer = setInterval(fetchDrift, DRIFT_EVERY);
    fetchPlangap();                 // plan-gap (own wind vs the frozen promise), drift's cadence
    App.plangapTimer = setInterval(fetchPlangap, DRIFT_EVERY);
    fetchTrend();                   // wind trend (sail-detail read), slow-moving
    App.trendTimer = setInterval(fetchTrend, 60000);
    fetchReroute();                 // the ROUTE tile: re-route status + last route; faster while one runs
    fetchSelector();                // branch selector (engine, unified recommendation), own cadence
    App.selTimer = setInterval(fetchSelector, DEV_EVERY);
    fetchSynthesis();               // in-race strategy synthesis (copilot LLM → engine fallback), own cadence
    App.synTimer = setInterval(fetchSynthesis, SYN_EVERY);
    fetchPlays();                   // Phase-D play matcher (engine, deterministic), own cadence
    fetchSailState();               // the SAILS bar (crew sail configuration), own cadence
    App.sailTimer = setInterval(fetchSailState, 12000);
    fetchSession();                 // the RACE LOG control, own cadence
    App.sessionTimer = setInterval(fetchSession, 15000);
    fetchChecklist();               // race-checklist reminders (engine triggers), own cadence
    App.chkTimer = setInterval(fetchChecklist, CHK_EVERY);
    fetchPower();                   // house bank (engine /power), slow-moving — see POWER_EVERY
    App.powerTimer = setInterval(fetchPower, POWER_EVERY);
    fetchHealth();                  // instrument cross-checks + provenance (engine /health/sensors)
    App.healthTimer = setInterval(fetchHealth, HEALTH_EVERY);
    document.getElementById("chkTag").addEventListener("click", () => openDetail("checklist"));
    App.playsTimer = setInterval(fetchPlays, SYN_EVERY);
    document.getElementById("themeBtn").addEventListener("click", cycleTheme);
    // Demo scenarios are a bench/dev surface — on the boat the crew wants LIVE only (user call,
    // 2026-07-15). Opt back in with ?demo in the URL (the Playwright harness + bench walkthroughs).
    if (new URLSearchParams(location.search).has("demo")) {
      document.getElementById("srcBtn").addEventListener("click", cycleSource);
    } else {
      document.getElementById("srcBtn").hidden = true;
    }
    document.getElementById("recBtn").addEventListener("click", toggleRec);
    document.getElementById("alertBtn").addEventListener("click", toggleSound);
    // reflect a persisted armed state, and re-unlock iOS audio on the first interaction after a reload
    document.getElementById("alertLbl").textContent = Sound.on ? "ON" : "OFF";
    document.getElementById("alertBtn").classList.toggle("armed", Sound.on);
    ["click", "touchend"].forEach((ev) =>
      document.addEventListener(ev, () => { if (Sound.on) audioCtx(); }, { once: true, passive: true }));
    document.getElementById("briefBtn").addEventListener("click", briefMe);
    // the commentary panel IS the coach window's face — tap anywhere on it (except the
    // Brief-me button) to open the full coach window
    document.getElementById("commentary").addEventListener("click", (e) => {
      if (e.target.closest("#briefBtn")) return;
      openDetail("coach");
    });
    document.getElementById("detBack").addEventListener("click", closeDetail);
    document.getElementById("overlay").addEventListener("click", closeDetail);
    const ask = () => {
      const inp = document.getElementById("detAsk");
      const q = inp.value.trim();
      if (!q || !App.openTile) return;
      inp.value = "";
      if (App.src === "live") streamDetail(App.openTile, q);   // scoped, grounded, streamed answer
      else document.getElementById("detWhy").textContent = "Questions work in live mode only. You asked: " + q;
    };
    document.getElementById("detSend").addEventListener("click", ask);
    document.getElementById("detAsk").addEventListener("keydown", (e) => { if (e.key === "Enter") ask(); });
    setInterval(() => { if (App.theme === "auto") applyTheme(); }, 25000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
