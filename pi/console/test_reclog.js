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

// #241: racing is its own state; the crew ends it on a separate "Race over" button
test("Race over: hidden unless racing", () => {
  assert.equal(RL.raceOverLabel(null), null);
  assert.equal(RL.raceOverLabel({ racing: false }), null);
});
test("Race over: plain, asked by the boat, armed", () => {
  assert.equal(RL.raceOverLabel({ racing: true, since: 5 }), "Race over");
  assert.equal(RL.raceOverLabel({ racing: true, since: 5, suggest_over: true }), "Race over?");
  assert.equal(RL.raceOverLabel({ racing: true, since: 5, suggest_over: true }, { armed: true }), "Tap again to end");
});
test("Race over sends the race start it shows", () =>
  assert.deepEqual(RL.raceOverBody({ racing: true, since: 1234.5 }), { since: 1234.5 }));

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
test("ending the log sends the id the button shows", () =>
  assert.match(js, /session\/end[\s\S]{0,200}JSON\.stringify\(\{ id: a\.id \}\)/));
test("the pick buttons are at least 48 px", () => {
  const m = css.match(/\.ctl\.rp\s*\{[^}]*min-height:\s*(\d+)px/);
  assert.ok(m && Number(m[1]) >= 48);
});

test("index.html has the Race over button, hidden by default, 48 px+", () => {
  assert.match(html, /id="raceOverBtn"[^>]*hidden/);
  const m = css.match(/\.ctl\.raceover\s*\{[^}]*min-height:\s*(\d+)px/);
  assert.ok(m && Number(m[1]) >= 48);
});
test("dashboard.js: Race over takes two taps and posts /race/over with the shown race start", () => {
  assert.match(js, /getElementById\("raceOverBtn"\)\.addEventListener\("click", tapRaceOver\)/);
  assert.match(js, /function tapRaceOver[\s\S]{0,400}raceOverUntil = Date\.now\(\) \+[\s\S]{0,300}return;/);
  // the arm is bound to the race it was for: a newer race showing up disarms it
  assert.match(js, /raceOverArmedSince = race\.since/);
  assert.match(js, /function raceOverArmed\(race\)[\s\S]{0,120}race\.since === raceOverArmedSince/);
  assert.match(js, /race\/over[\s\S]{0,200}C4RecLog\.raceOverBody\(race\)/);
});

console.log(failed ? "FAILED" : "ALL OK");
process.exit(failed ? 1 : 0);
