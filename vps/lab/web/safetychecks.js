/* Route checks (#405): which safety checks a route passed, one pill each — land, depth, race
   zones, marks and sides, chart covers route — so a route never reads as one "clear" when a check
   didn't run. Server: app/routesafety.py (`checks` on every route's and playbook's verdict).
   Uses app.js's esc. app.js's safetyPanel calls safetyChecksHtml(s.checks).
   What is refused (Freeze, Deploy, GPX) is not decided here: that is still the verdict's `ok`. */
"use strict";

const SAFETY_CHECK_WORD = { checked: "", not_checked: "not checked", failed: "failed" };
const SAFETY_CHECK_IDS = ["land", "depth", "zones", "marks", "chart"];
const SAFETY_CHECK_PILL = { checked: "ok", not_checked: "warn", failed: "bad" };

// a verdict from before #405 has no checks: say so, never show it as checked
function safetyChecksHtml(checks) {
  if (!Array.isArray(checks) || !checks.length) {
    return `<div class="pills"><span class="pill warn">Checks not listed. Run it again.</span></div>`;
  }
  const pills = checks.map((c) => {
    const st = SAFETY_CHECK_PILL[c.state] ? c.state : "not_checked";
    const word = SAFETY_CHECK_WORD[st];
    const tip = (c.texts || []).join(" ");
    return `<span class="pill ${SAFETY_CHECK_PILL[st]}"${tip ? ` title="${esc(tip)}"` : ""}>${st === "checked" ? "✓ " : ""}${esc(c.label || c.id)}${word ? ": " + word : ""}</span>`;
  }).join("");
  // all five must be listed and passed: a partial list is never "all checked"
  const ids = new Set(checks.filter((c) => c && c.state === "checked").map((c) => c.id));
  const all = SAFETY_CHECK_IDS.every((id) => ids.has(id));
  const failed = checks.some((c) => c.state === "failed");    // the red panel already says so
  return `<div class="pills" data-checks="${all ? "all" : "some"}">${pills}</div>` +
    (all || failed ? "" : `<div class="muted" style="font-size:12px;margin-top:4px">Not every check ran.</div>`);
}

if (typeof module !== "undefined" && module.exports) module.exports = { safetyChecksHtml };
