// CREW tile watch read (pi/console/dashboard/crewwatch.js) — #80: the tile went red ACT before
// the first watch started while CONSIDER said "Driver fresh — no rotation needed". The rule
// under test: every non-ok status has its reason in CONSIDER, and ACT is only a handover.
// Run: node pi/console/test_crewwatch.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const CW = require(path.join(__dirname, "dashboard", "crewwatch.js"));

const T0 = 1784419200;   // 2026-07-19T00:00Z, the Jul 18 replay's first block
function preStart(mins) {   // shared/watchplan.status_at() before the first block
  return { plan_set: true, active: false, on: null, all_hands: false,
    next_change: T0, next_on: "A", next_on_label: "Port", mins_to_change: mins,
    teams: { A: { name: "Port", members: [] }, B: { name: "Stbd", members: [] } } };
}
function onWatch(mins) {    // mid-block, `mins` to the change
  return Object.assign(preStart(mins), { active: true, on: "B", on_label: "Stbd" });
}
const FRESH = "Driver fresh — no rotation needed.";

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test("the #80 moment: 4 min before the first block is WATCH, and CONSIDER says why", () => {
  const w = preStart(4.0);
  assert.equal(CW.status(w), "watch");
  const c = CW.consider(w, "Port", false);
  assert.match(c, /^Watches start in 4m — wake Port\.$/);
  assert.ok(!c.startsWith(FRESH));
  assert.equal(CW.clears(w, false), "the first watch is on deck");
  assert.equal(CW.subTxt(w), "watches start in 4m");
});

test("before the first block never goes ACT, even at T-1", () => {
  for (const m of [0.5, 1, 5, 5.1, 10, 15]) assert.equal(CW.status(preStart(m)), "watch", "mins " + m);
});

test("far from the first block is OK with a clock time on the face", () => {
  const w = preStart(366.5);
  assert.equal(CW.status(w), "ok");
  assert.equal(CW.consider(w, "Port", false), FRESH + " Watches start 6.1h out.");
  assert.match(CW.subTxt(w), /^watches start \d\d:\d\d$/);
  assert.equal(CW.clears(w, false), "—");
});

test("a change of watch in progress keeps the T-15 watch / T-5 act tiers", () => {
  assert.equal(CW.status(onWatch(20)), "ok");
  assert.equal(CW.status(onWatch(15)), "watch");
  assert.equal(CW.status(onWatch(6)), "watch");
  assert.equal(CW.status(onWatch(5)), "act");
  assert.equal(CW.status(onWatch(2)), "act");
  assert.equal(CW.consider(onWatch(4), "Port", false), "Watch change in 4m — wake Port.");
  assert.equal(CW.consider(onWatch(4), "Port", true), "Watch change in 4m — wake Port; rotate the helm at the change.");
  assert.equal(CW.clears(onWatch(4), false), "the watch change");
});

test("every non-ok watch status is explained by CONSIDER (never the all-clear line)", () => {
  for (const mk of [preStart, onWatch]) {
    for (let m = 0.5; m <= 400; m += 0.5) {
      const w = mk(m);
      if (CW.status(w) === "ok") continue;
      for (const rot of [false, true]) {
        const c = CW.consider(w, "Port", rot);
        assert.ok(!c.startsWith(FRESH), mk.name + " " + m + " min: " + c);
        assert.match(c, /wake Port/, mk.name + " " + m + " min: " + c);
      }
    }
  }
});

test("helm rotation before the first block is phrased against the start, not a change", () => {
  assert.equal(CW.consider(preStart(40), "Port", true),
    "Tank getting low — rotate when watches start (40m out), sooner if it slips.");
  assert.equal(CW.consider(onWatch(40), "Port", true),
    "Tank getting low — rotate at the watch change (40m out), sooner if it slips.");
});

test("no plan, an ended plan and a missing read are all OK and quiet", () => {
  assert.equal(CW.status(null), "ok");
  assert.equal(CW.status({ plan_set: false }), "ok");
  const ended = { plan_set: true, active: false, next_change: null, mins_to_change: null };
  assert.equal(CW.status(ended), "ok");
  assert.equal(CW.subTxt(ended), "watch plan ended");
  assert.equal(CW.consider(ended, "the next team", false), FRESH);
  assert.equal(CW.consider(null, "the next team", true), "Tank getting low — plan a helm rotation.");
  assert.equal(CW.subTxt(null), null);
});

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log("ok   " + name); }
  catch (e) { failed++; console.log("FAIL " + name + "\n     " + e.message); }
}
console.log(cases.length - failed + " passed, " + failed + " failed");
process.exit(failed ? 1 : 0);
