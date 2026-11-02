/* The collision watch on the iPad coach strip, as pure functions (no DOM, no fetch).
   Loaded by index.html ahead of dashboard.js (window.C4CollisionWatch) and required by
   pi/console/test_collisionwatch.js under node.

   The copilot callout is the tablet's only collision surface, so three rules (2026-11-02 system
   review, collision items 2, 3, 6):
     1. An active collision callout owns the strip until it clears. Every contact shows, soonest
        first, with live numbers; a routine callout voiced later never pushes it off.
     2. A watch that can't see never looks like an empty sea. No answer from the copilot, a timer
        that stopped, or a held result that stopped moving → "Collision watch OFF: <reason>".
        The copilot's own blind callouts (no GPS fix, no AIS heard, …) arrive as safety callouts.
     3. The tone re-arms: a callout that clears and comes back sounds again, and so does a worse
        level (its id carries the level). */
(function (root) {
  "use strict";

  const OFFLINE_MS = 60000;      // no good /coach answer for this long → off
  const STALL_TICKS = 3;         // held result unchanged for this many coach intervals (+ slack) → off
  const STALL_SLACK_MS = 15000;
  const MAX_LINES = 4;           // contacts shown on the strip; the rest as "+N more"
  const OFF_ID = "local:off";

  function newState(nowMs) {
    return { startMs: nowMs, lastOkMs: null, lastUpdatedAt: undefined, lastChangeMs: null };
  }

  /* fold one poll into the state: `r` is the /coach body, or null when the fetch failed */
  function track(st, r, nowMs) {
    if (!r) return st;
    st.lastOkMs = nowMs;
    if (r.updated_at !== st.lastUpdatedAt) { st.lastUpdatedAt = r.updated_at; st.lastChangeMs = nowMs; }
    return st;
  }

  /* why the watch is off from the tablet's side, or null. Uses only the tablet's clock (the Orin's
     clock may differ), by noticing when the held result last changed. */
  function offReason(coach, st, nowMs) {
    const since = st.lastOkMs != null ? st.lastOkMs : st.startMs;
    if (nowMs - since > OFFLINE_MS) return "copilot offline";
    if (!coach) return null;                       // first minute after load: still connecting
    if (coach.enabled === false || coach.running === false) return "copilot stopped";
    const iv = Math.max(10, Number(coach.interval_s) || 30) * 1000;
    if (st.lastChangeMs != null && nowMs - st.lastChangeMs > STALL_TICKS * iv + STALL_SLACK_MS) return "copilot stalled";
    return null;
  }

  /* did this coach-history entry voice a safety callout? By its categories (copilot ≥ #191), else by
     the safety headlines' own words (an older copilot's entries carry headlines only). */
  const SAFETY_WORDS = /^(Collision watch OFF|Collision risk|Traffic closing|Contact, motion unknown)/;
  function voicedSafety(e) {
    if (Array.isArray(e.categories)) return e.categories.includes("safety");
    return (e.callouts || []).some((h) => SAFETY_WORDS.test(h || "")) || SAFETY_WORDS.test(e.spoken || "");
  }

  const line = (a) => (a.headline || "") + (a.detail ? " — " + a.detail : "");

  /* what the strip shows: {kind, lines, fromHistory}. kind = off | act | watch | coach | none.
     `fromHistory` is true when the line is a voiced history entry (the strip then shows its age, from
     `at`). A history entry that voiced a now-cleared safety callout is skipped. */
  function strip(coach, off) {
    if (off) return { kind: "off", lines: ["Collision watch OFF: " + off + ". Keep a lookout."], fromHistory: false };
    if (!coach) return { kind: "none", lines: [], fromHistory: false };
    const safety = (coach.active || []).filter((a) => a.category === "safety");
    if (safety.length) {
      const shown = safety.slice(0, MAX_LINES).map(line);
      if (safety.length > MAX_LINES) shown.push("+" + (safety.length - MAX_LINES) + " more");
      return { kind: safety.some((a) => a.urgency === "now") ? "act" : "watch", lines: shown, fromHistory: false };
    }
    // nothing safety is active now, so a history line that voiced a safety callout is a cleared one
    // ("Collision watch OFF: GPS fix stale" after the GPS came back): never show it as current
    const h = (coach.history || []).find((e) => e && e.spoken && !voicedSafety(e));
    if (h) return { kind: "coach", lines: [h.spoken], fromHistory: true, at: h.at };
    if (coach.active && coach.active.length) return { kind: "coach", lines: [line(coach.active[0])], fromHistory: false };
    return { kind: "none", lines: [], fromHistory: false };
  }

  /* which tone to play now ("act" | "watch" | null), updating `alerted` (a Set of ids already
     sounded). `levelOf(callout)` maps a callout to a tone level or null. Ids that left `active`
     are forgotten, so a contact that clears and comes back sounds again. */
  function alertsFor(coach, off, alerted, levelOf) {
    let level = null;
    const bump = (l) => { if (l === "act") level = "act"; else if (l && level !== "act") level = "watch"; };
    if (off) {
      if (!alerted.has(OFF_ID)) { alerted.add(OFF_ID); bump("act"); }
      return level;                                // the held callouts are not live: no tones for them
    }
    alerted.delete(OFF_ID);
    const live = new Set(((coach && coach.active) || []).map((a) => a.id));
    for (const id of Array.from(alerted)) if (!live.has(id)) alerted.delete(id);
    for (const c of ((coach && coach.active) || [])) {
      const l = levelOf(c);
      if (!l || alerted.has(c.id)) continue;
      alerted.add(c.id);
      bump(l);
    }
    return level;
  }

  /* GET/POST `url` and parse the JSON body, all inside ONE `ms` deadline: the abort timer stays
     armed through the body read, so a server that sends its headers and then stalls mid-body can't
     hang the poll (Codex on #191: clearing the timer before resp.json() did exactly that). Resolves
     the parsed body, or null on any failure, non-2xx, timeout or bad JSON. Never rejects. */
  async function fetchJsonWithin(url, opts, ms, fetchImpl) {
    const f = fetchImpl || ((typeof fetch === "function") ? fetch : null);
    if (!f) return null;
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), ms);
    try {
      const resp = await f(url, Object.assign({}, opts || {}, { signal: ctl.signal }));
      return resp.ok ? await resp.json() : null;
    } catch (e) {
      return null;
    } finally {
      clearTimeout(to);
    }
  }

  /* the independent watchdog's one step: judge the watch on the tablet clock from the last good
     parse (track() is fed only by successful polls), whether or not a poll ever completes.
     Returns the new off reason. */
  function watchdogStep(coach, st, nowMs) {
    return offReason(coach, st, nowMs);
  }

  const api = { newState, track, offReason, strip, alertsFor, fetchJsonWithin, watchdogStep,
    OFFLINE_MS, MAX_LINES };
  root.C4CollisionWatch = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
