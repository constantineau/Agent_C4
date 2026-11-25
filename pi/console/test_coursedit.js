// COURSE CHANGES aboard (pi/console/dashboard/coursedit.js) — #406.
// Pins: nothing is sent on the first tap (the second, within 5 s, sends); the body names the
// mark by place AND name (lap 2's Windward, not lap 1's); changes a mark can't take are disabled
// (no skip/finish at an island, no side on a gate, Undo only with an edit); the revision line;
// the re-route tile names the revision a route sailed.
// Run: node pi/console/test_coursedit.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const CE = require(path.join(__dirname, "dashboard", "coursedit.js"));
const RT = require(path.join(__dirname, "dashboard", "reroute.js"));

const cases = [];
const test = (name, fn) => cases.push([name, fn]);
const T = 1793000000000;
const REV = {
  available: true, route: "wl", course_id: "c",
  revision: { number: 1, at: 1793000000, origin: "lab" }, edits: [], shortened: false,
  marks: [
    { pos: 0, name: "Windward", type: "buoy", rounding: "port", index: 1 },
    { pos: 1, name: "Leeward", type: "buoy", rounding: "port", index: 2 },
    { pos: 2, name: "Windward", type: "buoy", rounding: "port", index: 3 },
    { pos: 3, name: "Gate", type: "gate", rounding: "gate", index: 4 },
    { pos: 4, name: "Rock", type: "island", rounding: "starboard", index: null },
  ],
};
const fresh = (r) => { CE.reset(); CE.setRevision(r || REV); };
const words = (s) => String(s).trim().split(/\s+/).length;

test("no course aboard → nothing drawn", () => {
  CE.reset();
  assert.equal(CE.html(T), "");
  CE.setRevision({ available: false, note: "No course aboard to change." });
  assert.equal(CE.html(T), "");
});

test("first tap arms, second sends; a late second tap only re-arms", () => {
  fresh();
  CE.pick(2);
  assert.equal(CE.tap("finish_at", T), null);
  assert.match(CE.html(T + 100), /Tap again/);
  assert.deepEqual(CE.tap("finish_at", T + 1000),
    { op: "finish_at", pos: 2, name: "Windward", device: null });
  assert.equal(CE.tap("skip_to", T + 2000), null);
  assert.equal(CE.tap("skip_to", T + 2000 + CE.ARM_MS + 1), null, "too late: armed again, not sent");
  assert.doesNotMatch(CE.html(T + 2000 + 3 * CE.ARM_MS), /Tap again/, "the armed label times out");
});

test("a tap on another change doesn't send the armed one", () => {
  fresh();
  CE.pick(0);
  CE.tap("side:starboard", T);
  assert.equal(CE.tap("finish_at", T + 500), null);
});

test("the repeated mark is named by its place", () => {
  fresh();
  CE.pick(2);
  CE.tap("side:starboard", T);
  assert.deepEqual(CE.tap("side:starboard", T + 10),
    { op: "rounding", pos: 2, name: "Windward", side: "starboard", device: null });
});

test("what a mark can't take is disabled", () => {
  fresh();
  const rock = REV.marks[4], gate = REV.marks[3], ww = REV.marks[0];
  assert.equal(CE.allowed("skip_to", rock, REV), false);
  assert.equal(CE.allowed("finish_at", rock, REV), false);
  assert.equal(CE.allowed("side:port", rock, REV), true);
  assert.equal(CE.allowed("side:starboard", rock, REV), false, "already starboard");
  assert.equal(CE.allowed("side:port", gate, REV), false);
  assert.equal(CE.allowed("finish_at", gate, REV), true);
  assert.equal(CE.allowed("undo", ww, REV), false, "nothing to undo");
  assert.equal(CE.allowed("undo", ww, Object.assign({}, REV, { edits: [{ op: "skip_to" }] })), true);
  CE.pick(4);
  assert.match(CE.html(T), /data-ce="finish_at" disabled/);
  assert.equal(CE.tap("finish_at", T), null);
  assert.equal(CE.tap("finish_at", T + 10), null, "a disabled change never sends");
});

test("undo needs no mark", () => {
  fresh(Object.assign({}, REV, { edits: [{ op: "finish_at", pos: 2, name: "Windward" }] }));
  CE.tap("undo", T);
  assert.deepEqual(CE.tap("undo", T + 10), { op: "undo", device: null });
});

test("the revision line, short and plain", () => {
  assert.equal(CE.revText({ number: 1, origin: "lab" }), "Course rev 1 · from the Lab");
  assert.match(CE.revText({ number: 4, origin: "boat", at: 1793000000 }),
    /^Course rev 4 · changed aboard \d\d:\d\d$/);
  fresh(Object.assign({}, REV, { shortened: true, revision: { number: 4, origin: "boat" } }));
  assert.match(CE.html(T), /Course rev 4 · changed aboard · shortened/);
  assert.match(CE.html(T), /1\. Windward \(port\)/);
  assert.match(CE.html(T), /Rock \(island, stbd\)/);
});

test("the Pi's answer becomes the note", () => {
  fresh();
  assert.equal(CE.answered(null), "No answer from the boat. Try again.");
  assert.equal(CE.answered({ ok: false, note: "That mark is behind you." }), "That mark is behind you.");
  assert.equal(CE.answered({ ok: true, note: "Course rev 2 saved." }), "Course rev 2 saved.");
  assert.match(CE.html(T), /Course rev 2 saved\./);
  for (const k of ["Skip to", "Finish here", "Undo change", "Tap again"]) assert.ok(words(k) <= 2);
});

test("names are escaped", () => {
  fresh(Object.assign({}, REV, { marks: [{ pos: 0, name: "<b>X</b>", type: "buoy", rounding: "port", index: 1 }] }));
  assert.ok(!CE.html(T).includes("<b>X"));
});

test("the re-route tile names the revision a route sailed", () => {
  const r = { available: true, source: "orin", finished_at: 1793000000, warnings: [],
    course_revision: { number: 4, at: 1793000000, origin: "boat" } };
  assert.equal(RT.describe({ source: "orin", r: r }, 1793000100).rev, "Course rev 4 · changed aboard");
  const lab = Object.assign({}, r, { course_revision: { number: 1, origin: "lab" } });
  assert.equal(RT.describe({ source: "orin", r: lab }, 1793000100).rev, "Course rev 1 · from the Lab");
  assert.equal(RT.describe({ source: "orin", r: Object.assign({}, r, { course_revision: null }) },
    1793000100).rev, "");
});

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log("  [OK ] " + name); } catch (e) { failed++; console.log("  [FAIL] " + name + "\n" + e.stack); }
}
console.log(failed ? "SOME CHECKS FAILED" : "ALL OK");
process.exit(failed ? 1 : 0);
