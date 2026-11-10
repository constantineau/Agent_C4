// The Orin's load shed on the iPad (pi/console/dashboard/shed.js) — #295, follow-up to #248.
// Pins: one short plain line while the shed level is 1+, per cause and level; nothing at level 0,
// with the guard off / stale / unreadable, or with no answer from the copilot at all (no Orin
// aboard, the Orin off): that case is shown elsewhere and must not show here too.
// Run: node pi/console/test_shed.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const SHED = require(path.join(__dirname, "dashboard", "shed.js"));

const cases = [];
const test = (name, fn) => cases.push([name, fn]);
const H = (shed) => ({ status: "ok", service: "onboard-copilot", shed: shed });

test("level 1-3: one short plain line, the cause first, then what's paused", () => {
  assert.equal(SHED.line(H({ level: 1, guard: "on", cause: "memory", paused: ["narration"] })),
    "Orin low on memory. AI paused.");
  assert.equal(SHED.line(H({ level: 2, guard: "on", cause: "heat", paused: ["narration", "plume"] })),
    "Orin hot. AI and plume paused.");
  assert.equal(SHED.line(H({ level: 3, guard: "on", cause: "power" })),
    "Orin short of power. AI, plume and re-route paused.");
  assert.equal(SHED.line(H({ level: 1, guard: "on", cause: "who-knows" })), "Orin overloaded. AI paused.");
  assert.equal(SHED.level(H({ level: 3, guard: "on", cause: "memory" })), 3);
});

test("level 0, or the guard not running: no line", () => {
  assert.equal(SHED.line(H({ level: 0, guard: "on", cause: null })), null);
  for (const g of ["off", "stale", "unreadable"])
    assert.equal(SHED.line(H({ level: 2, guard: g, cause: "memory" })), null, g);
  assert.equal(SHED.level(H({ level: 2, guard: "stale" })), 0);
});

test("no Orin / no copilot answer / an older copilot without `shed`: no line, no error", () => {
  for (const h of [null, undefined, {}, { status: "ok" }, H(null), H({}), H({ level: "x", guard: "on" })]) {
    assert.equal(SHED.line(h), null, JSON.stringify(h));
    assert.equal(SHED.level(h), 0, JSON.stringify(h));
  }
});

test("the lines are short and plain", () => {
  for (const cause of ["memory", "heat", "power", null])
    for (const level of [1, 2, 3]) {
      const l = SHED.line(H({ level: level, guard: "on", cause: cause }));
      assert.ok(l.split(/\s+/).length <= 9, "too long: " + l);
    }
});

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log("ok   " + name); }
  catch (e) { failed++; console.log("FAIL " + name + "\n     " + e.message); }
}
console.log(cases.length - failed + " passed, " + failed + " failed");
process.exit(failed ? 1 : 0);
