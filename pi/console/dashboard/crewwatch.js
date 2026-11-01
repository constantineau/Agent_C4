/* CREW tile — the watch-system read, as pure functions (no DOM, no fetch).
   Loaded by index.html ahead of dashboard.js (window.C4CrewWatch) and required by
   pi/console/test_crewwatch.js under node, so the status and the words that explain it are
   tested together.

   The rule the tile must keep: whatever colour the tile shows, CONSIDER says why (#80). The
   watch tiers are T-15 = watch ("wake the next team") and T-5 = act ("hand over now"). Act is
   only for a change of watch IN PROGRESS. Before the first block (or in a gap between blocks)
   nobody is on deck to hand over, so the start of watches is a wake-up call, capped at watch. */
(function (root) {
  "use strict";

  const WAKE_MIN = 15, HANDOVER_MIN = 5;

  const fmtHM = (e) => { const d = new Date(e * 1000); return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2); };
  const fmtMins = (m) => (m == null ? "?" : m >= 90 ? (Math.round(m / 6) / 10) + "h" : Math.round(m) + "m");

  const hasCountdown = (w) => !!(w && w.plan_set && w.mins_to_change != null);
  /* inside the wake-up window (the next change, or the first block's start) */
  const soon = (w) => hasCountdown(w) && w.mins_to_change <= WAKE_MIN;

  function status(w) {
    if (!hasCountdown(w)) return "ok";
    const m = w.mins_to_change;
    if (w.active && m <= HANDOVER_MIN) return "act";
    return m <= WAKE_MIN ? "watch" : "ok";
  }

  /* the tile's countdown line when no block is active */
  function subTxt(w) {
    if (!w || !w.plan_set) return null;
    if (w.active) {
      return (w.all_hands ? "ALL HANDS" : (w.on_label || w.on) + " on") + " · change " +
        fmtMins(w.mins_to_change) + (w.next_on_label ? " → " + w.next_on_label : "");
    }
    if (!w.next_change) return "watch plan ended";
    return "watches start " + (soon(w) ? "in " + fmtMins(w.mins_to_change) : fmtHM(w.next_change));
  }

  /* CONSIDER for the CREW tile. `wake` = the next team's display text, `rotate` = the helm
     fatigue says rotate. Every non-ok status() has its reason here. */
  function consider(w, wake, rotate) {
    const set = !!(w && w.plan_set), m = set ? w.mins_to_change : null;
    if (soon(w)) {
      const head = w.active ? "Watch change in " + fmtMins(m) : "Watches start in " + fmtMins(m);
      return head + " — wake " + wake +
        (rotate ? (w.active ? "; rotate the helm at the change." : "; rotate the helm when they're up.") : ".");
    }
    if (rotate && set && m != null) {
      return "Helm getting tired. Rotate " + (w.active ? "at the watch change" : "when watches start") +
        " (" + fmtMins(m) + " out), sooner if needed.";
    }
    if (rotate) return "Helm getting tired. Plan a rotation.";
    return "Driver fresh — no rotation needed." + (set && m != null
      ? (w.active ? " Next watch change " : " Watches start ") + fmtMins(m) + " out." : "");
  }

  function clears(w, rotate) {
    if (soon(w)) return w.active ? "the watch change" : "the first watch is on deck";
    return rotate ? "energy back above 65%" : "—";
  }

  const api = { status, subTxt, consider, clears, soon, fmtHM, fmtMins, WAKE_MIN, HANDOVER_MIN };
  root.C4CrewWatch = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
