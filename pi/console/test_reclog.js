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
  assert.equal(RL.raceOverLabel({ racing: true, since: null }), null);   // which race? unknown
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
  // live only: never from DEMO (it would end the real race on simulated data)
  assert.match(js, /function tapRaceOver[\s\S]{0,200}App\.src !== "live"/);
  assert.match(js, /function renderRaceOver[\s\S]{0,300}App\.src === "live" \?/);
  assert.match(js, /function cycleSource[\s\S]{0,400}renderRaceOver\(\)/);
  assert.match(js, /function raceOverArmed\(race\)[\s\S]{0,120}race\.since === raceOverArmedSince/);
  assert.match(js, /race\/over[\s\S]{0,200}C4RecLog\.raceOverBody\(race\)/);
});

// #320: the boat picker at LOG — starts on the last boat used, one tap changes it, Other boat
const SES = { homework_aboard: true, boats: {
  current: { boat_id: "sr33", name: "SR33", has_polar: true },
  boats: [{ boat_id: "bravo", name: "Bravo", has_polar: true },
          { boat_id: "sr33", name: "SR33", has_polar: true },
          { boat_id: "nopol", name: "No Polar", has_polar: false }],
  playbook_boat: "sr33", playbook_boat_name: "SR33" } };
test("picker starts on the last boat used (the engine's current boat)", () => {
  assert.deepEqual(RL.defaultBoat(SES), { boat_id: "sr33", name: "SR33" });
  assert.equal(RL.defaultBoat({}), null);
});
test("the list is the library aboard", () =>
  assert.deepEqual(RL.boatChoices(SES).map((b) => b.boat_id), ["bravo", "sr33", "nopol"]));
test("start sends the picked boat: a library id, or a typed name for an Other boat", () => {
  assert.deepEqual(RL.startBody("practice", { boat_id: "bravo", name: "Bravo" }),
    { kind: "practice", boat_id: "bravo" });
  assert.deepEqual(RL.startBody("practice", { boat_id: null, name: "  Mystery  " }),
    { kind: "practice", boat_name: "Mystery" });
  assert.deepEqual(RL.startBody("race", null), { kind: "race" });
});
test("the LOG button names the boat: the session's while running, else the pick", () => {
  assert.equal(RL.boatName({ boat_id: "bravo", boat_name: "Bravo" }, { name: "SR33" }), "Bravo");
  assert.equal(RL.boatName({ boat_id: null, boat_name: "Mystery" }, null), "Mystery");
  assert.equal(RL.boatName(null, { boat_id: "sr33", name: "SR33" }), "SR33");
});
test("another boat than the playbook's: one plain line", () => {
  assert.equal(RL.playbookNote({ boat_id: "sr33" }, SES), null);
  assert.equal(RL.playbookNote({ boat_id: "bravo" }, SES), "Playbook is for SR33.");
  assert.equal(RL.playbookNote({ boat_id: null, name: "X" }, SES), "Playbook is for SR33.");
  assert.equal(RL.playbookNote({ boat_id: "bravo" }, Object.assign({}, SES, { homework_aboard: false })), null);
});
test("a boat with no polar says so", () => {
  assert.equal(RL.polarNote({ boat_id: null, name: "X" }, SES), "No polar for this boat.");
  assert.equal(RL.polarNote({ boat_id: "nopol" }, SES), "No polar for this boat.");
  assert.equal(RL.polarNote({ boat_id: "bravo" }, SES), null);
});
test("index.html: boat button in the pick row, the boat name on LOG; dashboard sends the pick", () => {
  assert.match(html, /id="recPick"[^>]*>[\s\S]{0,120}id="recBoatBtn"/);
  assert.match(html, /id="recBtn"[\s\S]{0,200}id="recBoat"/);
  assert.match(js, /startBody\(kind, boat\)/);
  assert.match(js, /getElementById\("recBoatBtn"\)\.addEventListener\("click", toggleBoats\)/);
  assert.match(js, /data-other="1">Other boat</);
});
test("console cache-bust tags bumped for this change", () => {
  assert.match(html, /reclog\.js\?v=bp1110a/);
  assert.match(html, /dashboard\.js\?v=bp1110a/);
});

console.log(failed ? "FAILED" : "ALL OK");
process.exit(failed ? 1 : 0);
