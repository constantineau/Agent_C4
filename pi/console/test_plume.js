// Forecast plume drawing (pi/console/dashboard/plume.js) — the pure half of the Forecast tile.
// Pins: models in the router's order; the latest run bold and older runs fading; points outside
// the window dropped; a run's value never extrapolated past its own horizon; direction lines
// unwrapped through north (no 359→0 jump); the boat's reading pinned at now; the model picker;
// the age wording; and an empty plume drawing nothing.
// Run: node pi/console/test_plume.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const PL = require(path.join(__dirname, "dashboard", "plume.js"));

const NOW = 1793480400;          // 2026-10-31T21:00Z
const H = 3600;
const run = (cycle, base, dir) => ({ cycle: cycle, at: cycle + 2 * H,
  pts: [0, 3, 6, 12, 18, 24].map((h) => [NOW + h * H, base + h / 6, (dir + h * 2 + 360) % 360]) });
const PLUME = { available: true, schema: "c4.plume/v1", basis: "route", label: "along the plan", age_s: 1200,
  models: [
    { model: "icon", runs: [Object.assign(run(NOW - 4 * H, 14, 350), { est: true })] },
    { model: "hrrr", runs: [run(NOW - H, 10, 355), run(NOW - 2 * H, 11, 5), run(NOW - 3 * H, 12, 10)] },
    { model: "gfs", runs: [run(NOW - 6 * H, 12, 0)] },
  ] };

const cases = [];
const test = (name, fn) => cases.push([name, fn]);

test("models come out in the router's order (gfs, nam, hrrr, ecmwf, icon, gem)", () => {
  assert.deepEqual(PL.models(PLUME).map((m) => m.model), ["gfs", "hrrr", "icon"]);
  assert.equal(PL.available(PLUME), true);
  assert.equal(PL.available({ available: false }), false);
  assert.equal(PL.available({ models: [{ model: "gfs", runs: [] }] }), false);
});

test("a run's value interpolates inside its span and is null outside it", () => {
  const pts = PLUME.models[2].runs[0].pts;
  assert.equal(PL.valueAt(pts, NOW + 1.5 * H, "tws"), 12.25);
  assert.equal(PL.valueAt(pts, NOW - H, "tws"), null);
  assert.equal(PL.valueAt(pts, NOW + 30 * H, "tws"), null);
  // direction the short way round: 355 → 5 halfway is 0, not 180
  const d = PL.valueAt([[0, 10, 355], [10, 10, 5]], 5, "twd");
  assert.ok(Math.abs(((d + 180) % 360) - 180) < 1e-9);
});

test("spread across the models' latest runs", () => {
  const sp = PL.spreadAt(PLUME, NOW + 6 * H);
  assert.equal(sp.n, 3);
  assert.equal(sp.lo, 11);           // hrrr latest 10 + 1
  assert.equal(sp.hi, 15);           // icon 14 + 1
  assert.ok(sp.twd_spread < 20, "350..12 is a 22-ish° band through north, not 340°");
  assert.equal(PL.summary(PLUME, NOW, 6), "+6 h: 11–15 kts · 3 models");
});

test("latest run bold, older runs of the same model fading, oldest faintest", () => {
  const svg = PL.chartSvg(PLUME, { now: NOW, allRuns: true });
  assert.equal((svg.match(/pl-latest/g) || []).length, 3);
  const ops = [...svg.matchAll(/pl-hrrr pl-old" points="[^"]*" style="opacity:([0-9.]+)"/g)].map((m) => +m[1]);
  assert.equal(ops.length, 2);
  assert.ok(ops[0] < ops[1], "drawn oldest first, and the oldest is the faintest: " + ops);
  // latest only (the tile face)
  const tile = PL.chartSvg(PLUME, { now: NOW, compact: true });
  assert.equal((tile.match(/pl-old/g) || []).length, 0);
  assert.ok(!/pl-end/.test(tile), "no end labels on the small tile face (the legend carries the names)");
  assert.ok(/class="pl-end pl-hrrr"[^>]*>HRRR</.test(svg), "detail lines are named at their end");
});

test("the model picker shows one model's runs only", () => {
  const svg = PL.chartSvg(PLUME, { now: NOW, allRuns: true, only: "hrrr" });
  assert.ok(!/pl-gfs/.test(svg) && !/pl-icon/.test(svg));
  assert.equal((svg.match(/pl-run pl-hrrr/g) || []).length, 3);
  assert.ok(!/pl-gfs/.test(PL.legendHtml(PLUME, { only: "hrrr" })));
});

test("the boat's own reading is pinned at now, history drawn behind it", () => {
  const obs = [{ t: NOW - 2 * H, tws: 9, twd: 350 }, { t: NOW - H, tws: 10, twd: 352 }, { t: NOW, tws: 11, twd: 354 }];
  const svg = PL.chartSvg(PLUME, { now: NOW, obs: obs, nowObs: { tws: 11, twd: 354 } });
  assert.ok(/class="pl-nowdot"/.test(svg));
  assert.ok(/class="pl-obs"/.test(svg));
  const none = PL.chartSvg(PLUME, { now: NOW, nowObs: { tws: null, twd: null } });
  assert.ok(!/pl-nowdot/.test(none), "no reading → no dot (never a dot at 0 kts)");
});

test("direction lines don't jump across north", () => {
  const svg = PL.chartSvg(PLUME, { now: NOW, field: "twd", w: 500, h: 200 });
  for (const m of svg.matchAll(/class="pl-run[^"]*" points="([^"]+)"/g)) {
    const ys = m[1].split(" ").map((p) => +p.split(",")[1]);
    for (let i = 1; i < ys.length; i++) assert.ok(Math.abs(ys[i] - ys[i - 1]) < 120, "jump in " + m[1]);
  }
  assert.ok(/>(350|345|0|15)°</.test(svg), "axis labels are real bearings (mod 360)");
});

test("points outside the window are dropped", () => {
  const svg = PL.chartSvg(PLUME, { now: NOW, back_h: 0, ahead_h: 6, w: 300 });
  const xs = [...svg.matchAll(/pl-run[^"]*" points="([^"]+)"/g)].flatMap((m) => m[1].split(" ").map((p) => +p.split(",")[0]));
  assert.ok(xs.every((x) => x <= 300), "nothing drawn past the right edge");
});

test("age wording + old-data flag", () => {
  assert.equal(PL.ageText(600), "10 min");
  assert.equal(PL.ageText(5 * H), "5 h");
  assert.equal(PL.ageText(3 * 86400), "3 d");
  assert.equal(PL.isOld(PLUME), false);
  assert.equal(PL.isOld(Object.assign({}, PLUME, { age_s: 4 * H })), true);
});

test("run times: latest first, estimated cycles marked", () => {
  const html = PL.runTimesHtml(PLUME, NOW);
  assert.ok(/HRRR<\/span><\/span><span class="rc"><b>20Z<\/b> · then 19Z, 18Z/.test(html), html);
  assert.ok(/ICON[\s\S]*<b>~/.test(html), "the Open-Meteo-served cycle is an estimate");
});

test("an empty plume draws nothing", () => {
  assert.equal(PL.chartSvg({ models: [] }, { now: NOW }), "");
  assert.equal(PL.chartSvg(null, { now: NOW }), "");
});

test("only supported model ids are drawn, and names are escaped", () => {
  const odd = { models: [{ model: "<b>x</b>", runs: [run(NOW, 10, 0)] }, PLUME.models[2]] };
  assert.deepEqual(PL.models(odd).map((m) => m.model), ["gfs"]);
  const svg = PL.chartSvg(odd, { now: NOW, allRuns: true });
  assert.ok(!/<b>/.test(svg) && !/<b>/.test(PL.legendHtml(odd)));
  assert.equal(PL.esc('a<"b">&\''), "a&lt;&quot;b&quot;&gt;&amp;&#39;");
  const badPts = { models: [{ model: "gfs", runs: [{ cycle: NOW, at: NOW, pts: [[NOW, "9", 10]] }] }] };
  assert.equal(PL.available(badPts), false, "a run with non-number points is ignored");
});

test("frozen at the start: from the gun until the race is over", () => {
  const pl = Object.assign({}, PLUME, { race_start: NOW + H, race_end: NOW + 30 * H });
  assert.equal(PL.isFrozen(pl, NOW), false);
  assert.equal(PL.isFrozen(pl, NOW + H), true);
  assert.equal(PL.isFrozen(pl, NOW + 29 * H), true);
  assert.equal(PL.isFrozen(pl, NOW + 30 * H), false);
  assert.equal(PL.isFrozen(PLUME, NOW), false, "no race window → never frozen");
  const open = Object.assign({}, PLUME, { race_start: NOW + H, race_end: null });
  assert.equal(PL.isFrozen(open, NOW + 30 * 86400), true, "no recorded finish → frozen until there is one");
  assert.equal(PL.isFrozen(Object.assign({}, PLUME, { frozen: true }), NOW), true, "the engine's flag wins");
  assert.match(PL.frozenText(pl), /^Frozen at the start · \d\d:\d\d$/);
});

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log("  [OK ] " + name); }
  catch (e) { failed++; console.log("  [FAIL] " + name + "\n         " + e.message); }
}
console.log(failed ? "\n" + failed + " FAILED" : "\nALL OK");
process.exit(failed ? 1 : 0);
