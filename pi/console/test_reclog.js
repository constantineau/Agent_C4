// RACE LOG button (pi/console/dashboard/reclog.js): starting the log asks Race or Practice,
// pre-selects Race when Deploy homework is aboard, always sends the kind, and the running button
// says which kind it is. Also pins the wiring in index.html / dashboard.js.
// Run: node pi/console/test_reclog.js (no deps).
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const RL = require(path.join(__dirname, "dashboard", "reclog.js"));

let failed = 0;
const test = (name, fn) => {
  try { fn(); console.log("  [OK ] " + name); }
  catch (e) { failed++; console.log("  [FAIL] " + name + ": " + e.message); }
};

test("homework aboard → Race pre-selected", () =>
  assert.equal(RL.preselect({ homework_aboard: true }), "race"));
test("no homework → Practice pre-selected", () => {
  assert.equal(RL.preselect({ homework_aboard: false }), "practice");
  assert.equal(RL.preselect(null), "practice");
});
test("start always sends the kind", () => {
  assert.deepEqual(RL.startBody("race"), { kind: "race" });
  assert.deepEqual(RL.startBody("practice"), { kind: "practice" });
  assert.throws(() => RL.startBody(undefined));
});
test("running label says the kind", () => {
  assert.equal(RL.label({ kind: "race", start_ts: 1000 }, { nowS: 1000 + 12 * 60 }), "LOG · RACE · 12m");
  assert.equal(RL.label({ kind: "practice", start_ts: 1000 }, { nowS: 1060 }), "LOG · PRACTICE · 1m");
});
test("idle, picking, confirming", () => {
  assert.equal(RL.label(null, {}), "LOG");
  assert.equal(RL.label(null, { picking: true }), "LOG · PICK ONE");
  assert.equal(RL.label({ kind: "race", start_ts: 0 }, { confirming: true, nowS: 60 }), "TAP AGAIN TO END");
});

const dir = path.join(__dirname, "dashboard");
const html = fs.readFileSync(path.join(dir, "index.html"), "utf8");
const js = fs.readFileSync(path.join(dir, "dashboard.js"), "utf8");
const css = fs.readFileSync(path.join(dir, "dashboard.css"), "utf8");
test("index.html loads reclog.js before dashboard.js, and has both pick buttons", () => {
  assert.ok(html.indexOf("reclog.js") > 0 && html.indexOf("reclog.js") < html.indexOf("dashboard.js?"));
  assert.match(html, /id="recRace"[^>]*>Race</);
  assert.match(html, /id="recPractice"[^>]*>Practice</);
});
test("dashboard.js never starts a session without the kind (no empty body)", () => {
  assert.ok(!/session\/start[\s\S]{0,200}body:\s*"\{\}"/.test(js), "an empty /session/start body is back");
  assert.match(js, /C4RecLog\.startBody\(/);
});
test("the pick buttons are at least 48 px", () => {
  const m = css.match(/\.ctl\.rp\s*\{[^}]*min-height:\s*(\d+)px/);
  assert.ok(m && Number(m[1]) >= 48);
});

console.log(failed ? "FAILED" : "ALL OK");
process.exit(failed ? 1 : 0);
