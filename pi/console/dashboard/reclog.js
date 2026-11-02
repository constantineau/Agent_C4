/* RACE LOG button — the words and the default pick, as pure functions (no DOM, no fetch).
   Loaded by index.html ahead of dashboard.js (window.C4RecLog) and required by
   pi/console/test_reclog.js under node.

   Frozen at the gun: a RACE session is what makes the boat refuse shore homework. So starting the
   log asks Race or Practice (one extra tap, two big buttons), pre-selecting Race when Deploy
   homework (a course, fleet or playbook) is aboard, and always sends the kind it got. */
(function (root) {
  "use strict";

  const PICK_MS = 8000;        // the Race / Practice choice stays open this long

  /* which kind to pre-select: Race when shore homework is aboard, else Practice */
  function preselect(session) {
    return session && session.homework_aboard ? "race" : "practice";
  }

  /* the LOG button's label: idle, choosing, running (with the kind), or the end confirmation */
  function label(active, opts) {
    const o = opts || {};
    if (active && o.confirming) return "TAP AGAIN TO END";
    if (active) {
      const kind = String(active.kind || "").toLowerCase() === "race" ? "RACE" : "PRACTICE";
      const min = Math.max(0, Math.round(((o.nowS || 0) - (active.start_ts || 0)) / 60));
      return "LOG · " + kind + " · " + min + "m";
    }
    if (o.picking) return "LOG · PICK ONE";
    return "LOG";
  }

  /* the /session/start body: always the kind the crew tapped */
  function startBody(kind) {
    if (kind !== "race" && kind !== "practice") throw new Error("kind must be race or practice");
    return { kind: kind };
  }

  const api = { preselect: preselect, label: label, startBody: startBody, PICK_MS: PICK_MS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.C4RecLog = api;
})(typeof window !== "undefined" ? window : this);
