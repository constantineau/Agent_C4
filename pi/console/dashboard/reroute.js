/* ROUTE tile — the Re-route now button's read, as pure functions (no DOM, no fetch).
   Loaded by index.html ahead of dashboard.js (window.C4Reroute) and required by
   pi/console/test_reroute.js under node, so the states and their words are tested together.

   Button only (#125, Cole 2026-11-01): a new route is made ONLY when a crew member presses the
   button. The engine may SUGGEST one (quiet text from /strategy `reroute_suggest`); this tile
   never runs anything on its own.

   Inputs:
     rr       GET /reroute?compact=1 — the Orin's state + last route, `pi` (the Pi fallback's state +
              last route), `latest` ("orin" | "pi" | null: which route is newest)
     rrAgeMs  how old that read is (the iPad holds the last good one when a poll fails)
     suggest  {reason} from the strategy digest, or null; sugAgeMs its age
     press    what the last tap on THIS iPad came to (see afterPress)

   The Orin-unreachable fallback is the SAME button, relabelled "Use Pi route": a second,
   explicit tap. The Pi's route is simpler (one forecast, no shore check), so the crew chooses it
   knowingly. The relabelled button ignores taps for ARM_MS, so a double tap can't take it. */
(function (root) {
  "use strict";

  const STALE_MS = 30000;            // past this the read is shown with its age
  const RUN_TRUST_MS = 90000;        // a "running" read older than this is not trusted to block the button
  const SUGGEST_TTL_MS = 180000;     // a suggestion older than this is not shown
  const STARTING_MS = 10000;         // "Starting…" until the first poll after a 202 shows the run
  const ARM_MS = 1500;               // the relabelled fallback button ignores taps this long
  const FALLBACK_MS = 120000;        // the "Use Pi route" offer lasts this long
  const FALLBACK_WHY = "Pi route: one forecast, no shore check.";
  const OLD_COURSE = "Route is for an old course.";
  const INCOMPLETE = "Route incomplete.";
  const NO_ZONES = "No zones or rounding sides aboard. Redeploy the course.";

  const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");
  function hhmm(epochS) {
    const d = new Date(epochS * 1000);
    return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
  }
  function agoTxt(sec) {
    if (sec == null || !isFinite(sec)) return "";
    if (sec < 90) return "just now";
    if (sec < 90 * 60) return Math.round(sec / 60) + " min ago";
    return Math.round(sec / 3600) + " h ago";
  }
  function hoursTxt(h) {
    if (h == null) return null;
    return h < 1 ? Math.max(1, Math.round(h * 60)) + " min" : (Math.round(h * 10) / 10) + " h";
  }
  const MODEL = { gfs: "GFS", nam: "NAM", hrrr: "HRRR", ecmwf: "ECMWF", icon: "ICON", gem: "GEM",
    "open-meteo": "Open-Meteo" };
  const modelName = (m) => MODEL[String(m || "").toLowerCase()] || String(m || "").toUpperCase();

  /* the newest route either engine made → {source, r} or null */
  function lastRoute(rr) {
    if (!rr) return null;
    const pi = (rr.pi || {}).result;
    if (rr.latest === "pi" && pi) return { source: "pi", r: pi };
    if (rr.latest === "orin" && rr.result) return { source: "orin", r: rr.result };
    if (rr.result && rr.result.available !== false) return { source: "orin", r: rr.result };
    if (pi) return { source: "pi", r: pi };
    return null;
  }

  /* one route → the words the tile and its detail show */
  function describe(src, nowS) {
    if (!src || !src.r) return null;
    const r = src.r, orin = src.source === "orin";
    const fin = r.finished_at || r.computed_at || null;
    const vs = r.vs_plan || {};
    let head;
    if (r.old_course) {
      // made for another course (marks moved, a new course loaded): kept, never shown as current
      head = OLD_COURSE;
    } else if (r.complete === false) {
      // stalled short of the finish: no finish time, no gain, never sent to the GPS
      head = INCOMPLETE;
    } else if (orin && vs.available && vs.gain_min != null) {
      const g = Math.round(vs.gain_min);
      head = g > 0 ? g + " min faster than the plan" : g < 0 ? (-g) + " min slower than the plan"
        : "Same time as the plan";
    } else if (r.finish_eta) {
      head = "Finish at " + hhmm(r.finish_eta);
    } else if (!orin && r.eta_min != null && r.depart_epoch) {
      head = "Finish at " + hhmm(r.depart_epoch + r.eta_min * 60);   // the route's own departure
    } else {
      head = "New route";
    }
    const w = r.weather || {};
    let wx;
    if (orin) {
      const models = (w.models || []).map((m) => modelName(m.model)).filter(Boolean);
      const age = hoursTxt(w.newest_age_h);
      wx = (age ? "Forecast " + age + " old" : "Forecast age unknown") +
        (models.length ? " · " + models.join(", ") : "");
    } else {
      wx = String(r.wind_source || "").indexOf("live") === 0 ? "Measured wind only. No forecast."
        : "Forecast · Open-Meteo";
    }
    const who = (r.asked_by ? "Asked by " + r.asked_by : "Asked") +
      (r.asked_at ? " at " + hhmm(r.asked_at) : fin ? " · done " + hhmm(fin) : "");
    const bits = [];
    if (r.sailed_nm != null) bits.push(Math.round(r.sailed_nm) + " nm");
    if (r.tacks != null) bits.push(plural(r.tacks, "tack"));
    if (Array.isArray(r.sail_plan) && r.sail_plan.length) bits.push(r.sail_plan.join("→"));
    const warns = (r.warnings || []).filter((x) => typeof x === "string");
    const labels = [];
    if (r.off_playbook || r.label) labels.push("Off the playbook");
    const shore = !orin || warns.some((x) => x.indexOf("check the shore") >= 0) ||
      (r.chart && r.chart.land === false);
    if (shore) labels.push("Route doesn't check the shore");
    const other = warns.filter((x) => x.indexOf("check the shore") < 0 &&
      !(r.complete === false && x === "Route incomplete.")).slice(0, 2);
    return { source: src.source, engine: orin ? "Orin" : "Pi", head: head, wx: wx, who: who,
      shape: bits.join(" · "), labels: labels, warnings: other,
      age: fin ? agoTxt(nowS - fin) : "", finished_at: fin, old: !!r.old_course,
      incomplete: r.complete === false,
      noZones: !!r.no_zones || warns.indexOf(NO_ZONES) >= 0 };
  }

  /* what a tap came to, from the POST's answer (code null = no answer: no link) */
  function afterPress(engine, code, body, nowMs) {
    const b = body || {};
    if (code == null) return { phase: "nolink", engine: engine, at: nowMs };
    if (code === 202) return { phase: "started", engine: engine, at: nowMs };
    if (code === 409 && b.busy) return { phase: "busy", engine: engine, at: nowMs };
    if (code === 503 && engine === "orin")
      return { phase: "unreachable", engine: "orin", at: nowMs, armAt: nowMs + ARM_MS,
        until: nowMs + FALLBACK_MS };
    if (code === 409) return { phase: "refused", engine: engine, at: nowMs,
      detail: b.detail || "Can't re-route now." };
    return { phase: "error", engine: engine, at: nowMs, detail: b.detail || "Re-route failed. Try again." };
  }

  function view(inp) {
    const o = inp || {};
    const rr = o.rr || null, press = o.press || {}, nowMs = o.nowMs || Date.now();
    const nowS = nowMs / 1000;
    const ageMs = rr ? (o.rrAgeMs || 0) : Infinity;
    const stale = rr != null && ageMs >= STALE_MS;
    const pi = (rr && rr.pi) || {};
    const trustRun = rr != null && ageMs < RUN_TRUST_MS;
    const orinRun = trustRun && rr.state === "running";
    const piRun = trustRun && pi.state === "running";
    const running = orinRun || piRun;
    const starting = press.phase === "started" && nowMs - press.at < STARTING_MS && !running;
    const posting = press.phase === "posting";

    // the fallback offer: after the Orin refused as unreachable, until it answers again
    const orinBack = rr && rr.orin && rr.orin.reachable && (o.rrAt || 0) > (press.at || 0);
    const fallback = press.phase === "unreachable" && nowMs < (press.until || 0) && !orinBack &&
      !running;

    let progress = null;
    if (running) {
      const p = (orinRun ? rr.progress : pi.progress) || {};
      const el = p.elapsed_s != null ? Math.round(p.elapsed_s) : null;
      const exp = p.expected_s != null ? Math.round(p.expected_s) : null;
      progress = { pct: Math.max(3, Math.min(95, p.pct || 0)), engine: orinRun ? "orin" : "pi",
        text: "Re-routing" + (piRun && !orinRun ? " on the Pi" : "") + "…" +
          (el != null ? " " + el + " s" + (exp ? " of ~" + exp : "") : ""),
        who: (orinRun ? rr.asked_by : pi.asked_by) || null };
    }

    let note = null;
    if (press.phase === "unreachable" && fallback) note = "Can't reach the Orin.";
    else if (press.phase === "busy" && nowMs - press.at < 20000) note = "Already running. One at a time.";
    else if (press.phase === "refused" && nowMs - press.at < 60000) note = press.detail;
    else if (press.phase === "error" && nowMs - press.at < 60000) note = press.detail;
    else if (press.phase === "nolink" && nowMs - press.at < 60000) note = "No link to the boat. Try again.";
    if (!note && !running && rr) {
      const lastEngine = press.engine || (pi.state === "error" && rr.state !== "error" ? "pi" : "orin");
      if (lastEngine === "pi" && pi.state === "error" && pi.detail) note = "Last try failed. " + pi.detail;
      else if (rr.state === "error" && rr.note) note = "Last try failed. " + rr.note;
      else if (rr.result_stale) note = "New route not fetched yet.";
    }

    const sugOk = o.suggest && o.suggest.reason && (o.sugAgeMs || 0) < SUGGEST_TTL_MS;
    const suggest = sugOk ? String(o.suggest.reason) : null;
    const last = describe(lastRoute(rr), nowS);

    let button;
    if (running) button = { label: "Re-routing…", disabled: true, engine: null };
    else if (posting || starting) button = { label: "Starting…", disabled: true, engine: null };
    else if (fallback) button = { label: "Use Pi route", disabled: nowMs < (press.armAt || 0),
      engine: "pi" };
    else button = { label: "Re-route now", disabled: false, engine: "orin" };

    // the face's one line: what's happening, else what went wrong, else the suggestion, else the
    // last route
    let say, sayKind;
    if (progress) { say = progress.text; sayKind = "run"; }
    else if (posting || starting) { say = "Asking the Orin…"; sayKind = "run";
      if ((press.engine || "") === "pi") say = "Asking the Pi…"; }
    else if (note) { say = note; sayKind = "note"; }
    else if (suggest) { say = suggest; sayKind = "suggest"; }
    else if (last && (last.old || last.incomplete)) { say = last.head; sayKind = "note"; }
    else if (last) { say = last.head + (last.age ? " · " + last.age : ""); sayKind = "last"; }
    else if (!rr) { say = "Waiting for the boat…"; sayKind = "none"; }
    else { say = "No new route yet."; sayKind = "none"; }

    // the face's second line: why the Pi offer is different, else the last route under a note
    let sub = null;
    if (fallback) sub = FALLBACK_WHY;
    else if (last && last.old && sayKind !== "note") sub = OLD_COURSE;
    else if (last && last.incomplete && sayKind !== "note") sub = INCOMPLETE;
    // a route made without the course's zones says so on the face, never silently
    else if (last && last.noZones && !last.old && !progress) sub = NO_ZONES;
    else if (last && !last.old && !last.incomplete && sayKind !== "last" && !progress) sub = "Last: " + last.head + (last.age ? " · " + last.age : "");

    return {
      status: rr ? "ok" : "na", sub: sub,
      chip: !rr ? "NO LINK" : running ? "RUNNING" : stale ? "HELD" : "READY",
      stale: stale, ageMs: rr ? ageMs : null,
      running: running, progress: progress, button: button, fallback: fallback,
      note: note, suggest: suggest, last: last, say: say, sayKind: sayKind,
      canShowOnGps: !!(last && !last.old && !last.incomplete && !running),
    };
  }

  /* how soon to poll /reroute again: fast (3 s) while a run is going, being posted, or in the
     STARTING_MS window after a 202 (until the poll shows it running); slow (10 s) otherwise.
     A "started" press never keeps it fast past that window, so a finished run settles back. */
  const RUN_POLL_MS = 3000, IDLE_POLL_MS = 10000;
  function pollEvery(v, press, nowMs) {
    const p = press || {};
    if (v && v.running) return RUN_POLL_MS;
    if (p.phase === "posting") return RUN_POLL_MS;
    if (p.phase === "started" && (nowMs || Date.now()) - p.at < STARTING_MS) return RUN_POLL_MS;
    return IDLE_POLL_MS;
  }

  const api = { view: view, pollEvery: pollEvery, RUN_POLL_MS: RUN_POLL_MS,
    IDLE_POLL_MS: IDLE_POLL_MS, STARTING_MS: STARTING_MS, afterPress: afterPress, describe: describe, lastRoute: lastRoute,
    agoTxt: agoTxt, ARM_MS: ARM_MS, FALLBACK_MS: FALLBACK_MS, STALE_MS: STALE_MS,
    RUN_TRUST_MS: RUN_TRUST_MS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.C4Reroute = api;
})(typeof window !== "undefined" ? window : this);
