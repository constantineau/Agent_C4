/* The Orin's load shed (#248) on the iPad (#295): one short plain line while the Orin is shedding.
   Loaded by index.html ahead of dashboard.js (window.C4Shed) and required by
   pi/console/test_shed.js under node. Pure: no DOM, no fetch.

   Input: the copilot's GET /health (reached as /copilot/health), or null when it didn't answer.
   Its `shed` block is {level, guard, cause, paused, detail}:
     level 1  narration paused (the copilot makes no LLM calls)
     level 2  also the plume
     level 3  also new re-route runs
   guard "on" means the reading is live. "off" / "stale" / "unreadable" (the guard isn't running)
   read as level 0 on the Orin itself, so they say nothing here either.

   No answer at all → no line: the copilot being unreachable is shown elsewhere (the strategy card
   falls back to AUTO, the Re-route button offers the Pi), so this never doubles up on it. */
(function (root) {
  "use strict";

  const CAUSE = { memory: "Orin low on memory.", heat: "Orin hot.", power: "Orin short of power." };
  const WHAT = { 1: "AI paused.", 2: "AI and plume paused.", 3: "AI, plume and re-route paused." };

  /* the live shed level from a /health answer, or 0 (nothing shed, or nothing known) */
  function level(health) {
    const s = health && health.shed;
    if (!s || s.guard !== "on") return 0;
    const n = Number(s.level);
    return isFinite(n) ? Math.max(0, Math.min(3, Math.floor(n))) : 0;
  }

  /* the one line, or null to hide it */
  function line(health) {
    const n = level(health);
    if (n < 1) return null;
    return (CAUSE[health.shed.cause] || "Orin overloaded.") + " " + WHAT[n];
  }

  const api = { level: level, line: line };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.C4Shed = api;
})(typeof window !== "undefined" ? window : this);
