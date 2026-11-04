// The copilot's dashboard brief can only RAISE a tile (pi/console/dashboard/briefguard.js) — #176.
// Pins: the LLM can't turn a tile green or lower a warning; a raise ends as soon as the engine moves
// that tile, and ADJUST_TTL_S after the snapshot the model read (not after the slow reply); the
// snapshot sent to the model is the engine's own status, never a raise fed back as fact.
// Run: node pi/console/test_briefguard.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const BG = require(path.join(__dirname, "dashboard", "briefguard.js"));

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

const T = 1793000000;                                  // snapshot time, epoch s
const tile = (status, extra) => Object.assign({ status, value: "x", why: "engine" }, extra || {});
const read = (tiles) => ({ tiles, focus: "1 item needs attention.", notes: [{ tile: "bank", text: "engine" }],
  confidence: "engine", mode: "engine read" });
const brief = (adjust, extra) => Object.assign({ mode: "llm", t: T, focus: "Bank low, shed load.",
  notes: [{ tile: "bank", text: "Bank 11.9 V. Shed load.", conf: "high" }], adjust }, extra || {});

test("the LLM can't turn the bank green (act → ok refused)", () => {
  const d = BG.overlay(read({ bank: tile("act") }), brief([{ tile: "bank", status: "ok", reason: "charging", from: "act" }]), T + 10);
  assert.equal(d.tiles.bank.status, "act");
  assert.equal(d.tiles.bank.llmNote, undefined);
  assert.equal(d.focus, "Bank low, shed load.");       // the commentary still shows
});

test("…nor lower a warning (watch → ok, act → watch refused)", () => {
  const d = BG.overlay(read({ data: tile("watch"), crew: tile("act") }), brief([
    { tile: "data", status: "ok", reason: "fine", from: "watch" },
    { tile: "crew", status: "watch", reason: "fine", from: "act" }]), T + 10);
  assert.equal(d.tiles.data.status, "watch");
  assert.equal(d.tiles.crew.status, "act");
});

test("a raise is shown (ok → watch) with its reason", () => {
  const d = BG.overlay(read({ wind: tile("ok") }), brief([{ tile: "wind", status: "watch", reason: "building fast", from: "ok" }]), T + 10);
  assert.equal(d.tiles.wind.status, "watch");
  assert.equal(d.tiles.wind.llmNote, "building fast");
  assert.equal(d.tiles.wind.engineStatus, "ok");
});

test("an n/a tile is never touched", () => {
  const d = BG.overlay(read({ fleet: tile("na") }), brief([{ tile: "fleet", status: "act", reason: "?", from: "na" }]), T + 10);
  assert.equal(d.tiles.fleet.status, "na");
});

test("a raise ends for good once the engine moves the tile, even if it moves back", () => {
  const b = brief([{ tile: "wind", status: "act", reason: "squall", from: "ok" }]);
  assert.equal(BG.overlay(read({ wind: tile("ok") }), b, T + 10).tiles.wind.status, "act");
  assert.equal(BG.overlay(read({ wind: tile("watch") }), b, T + 20).tiles.wind.status, "watch");
  assert.equal(BG.overlay(read({ wind: tile("ok") }), b, T + 30).tiles.wind.status, "ok");   // no comeback (ok → watch → ok)
});

test("…also when the engine itself reached the raised status and dropped back (ok → act → ok)", () => {
  const b = brief([{ tile: "wind", status: "act", reason: "squall", from: "ok" }]);
  assert.equal(BG.overlay(read({ wind: tile("act") }), b, T + 10).tiles.wind.status, "act");
  assert.equal(BG.overlay(read({ wind: tile("ok") }), b, T + 20).tiles.wind.status, "ok");
});

test("a tile that moved while the model was answering voids the raise (ok, watch, ok, then reply)", () => {
  const track = BG.newTrack();
  BG.overlay(read({ wind: tile("ok") }), null, T, track);                 // snapshot polled
  const moves = Object.assign({}, track.moves);                            // taken with the snapshot
  BG.overlay(read({ wind: tile("watch") }), null, T + 5, track);          // engine moves...
  BG.overlay(read({ wind: tile("ok") }), null, T + 10, track);            // ...and back, reply pending
  const b = Object.assign(brief([{ tile: "wind", status: "act", reason: "squall", from: "ok" }]), { moves });
  assert.equal(BG.overlay(read({ wind: tile("ok") }), b, T + 15, track).tiles.wind.status, "ok");
  // control: no move during the reply → the raise shows
  const track2 = BG.newTrack();
  BG.overlay(read({ wind: tile("ok") }), null, T, track2);
  const b2 = Object.assign(brief([{ tile: "wind", status: "act", reason: "squall", from: "ok" }]),
    { moves: Object.assign({}, track2.moves) });
  assert.equal(BG.overlay(read({ wind: tile("ok") }), b2, T + 15, track2).tiles.wind.status, "act");
});

test("a raise expires ADJUST_TTL_S after the snapshot, not after the reply", () => {
  const b = brief([{ tile: "wind", status: "watch", reason: "building", from: "ok" }]);
  assert.ok(BG.ADJUST_TTL_S <= 180);
  assert.equal(BG.overlay(read({ wind: tile("ok") }), b, T + BG.ADJUST_TTL_S).tiles.wind.status, "watch");
  const late = BG.overlay(read({ wind: tile("ok") }), b, T + BG.ADJUST_TTL_S + 1);
  assert.equal(late.tiles.wind.status, "ok");
  assert.equal(late.mode, "llm-live");                  // commentary may outlive the raise
  const gone = BG.overlay(read({ wind: tile("ok") }), b, T + BG.BRIEF_TTL_S + 1);
  assert.equal(gone.mode, "engine read");               // then the engine commentary is back
});

test("a raise with no `from` (older copilot) is not applied", () => {
  const d = BG.overlay(read({ wind: tile("ok") }), brief([{ tile: "wind", status: "act", reason: "x" }]), T + 10);
  assert.equal(d.tiles.wind.status, "ok");
});

test("the source tile object is never mutated (a reused build object can't keep a raise)", () => {
  const src = tile("ok");
  const d = BG.overlay(read({ wind: src }), brief([{ tile: "wind", status: "act", reason: "x", from: "ok" }]), T + 10);
  assert.equal(d.tiles.wind.status, "act");
  assert.equal(src.status, "ok");
  assert.equal(src.llmNote, undefined);
});

test("the snapshot status is the engine's, even on a raised tile", () => {
  const d = BG.overlay(read({ wind: tile("ok") }), brief([{ tile: "wind", status: "act", reason: "x", from: "ok" }]), T + 10);
  assert.equal(BG.engineStatus(d.tiles.wind), "ok");
});

test("dashboard.js wires it: overlay via briefguard, engine status in the snapshot, stamped at the snapshot", () => {
  const dir = path.join(__dirname, "dashboard");
  const js = fs.readFileSync(path.join(dir, "dashboard.js"), "utf8");
  const html = fs.readFileSync(path.join(dir, "index.html"), "utf8");
  assert.ok(html.indexOf("briefguard.js") > 0 && html.indexOf("briefguard.js") < html.indexOf("dashboard.js?"));
  assert.match(js, /return BG\.overlay\(d, App\.brief, Date\.now\(\) \/ 1000, App\.track\);/);
  assert.match(js, /track: BG\.newTrack\(\)/);
  assert.match(js, /const moves = Object\.assign\(\{\}, App\.track\.moves\)/);
  assert.match(js, /status: BG\.engineStatus\(t\)/);
  assert.match(js, /mode: "llm", t: snapAt, moves \}/);
  assert.doesNotMatch(js, /t\.status = a\.status/);
});

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log("  [OK ] " + name); }
  catch (e) { failed++; console.log("  [FAIL] " + name + "\n         " + String(e.message).split("\n")[0]); }
}
console.log(failed ? `\n${failed} FAILED` : "\nPASS");
process.exit(failed ? 1 : 0);
