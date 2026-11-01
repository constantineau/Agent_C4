// ROUTE tile (pi/console/dashboard/reroute.js) — #125, the Re-route now button.
// Pins: nothing runs on its own; the button's busy / done / error states and their words; the
// refusal of a second press; the Orin-unreachable fallback (a second, explicit tap on the Pi);
// stale-safety (a held read shows its age and never blocks the button forever); the labels.
// Run: node pi/console/test_reroute.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const RT = require(path.join(__dirname, "dashboard", "reroute.js"));

const NOW = 1793000000000;                 // ms
const S = NOW / 1000;
const cases = [];
const test = (name, fn) => cases.push([name, fn]);
const words = (s) => String(s).trim().split(/\s+/).length;

const IDLE = { orin: { reachable: true }, state: "idle", result: null, latest: null, pi: { state: "idle", result: null } };
const ORIN_ROUTE = { available: true, source: "orin", label: "Off the playbook", off_playbook: true,
  asked_by: "Nav iPad", asked_at: S - 1500, finished_at: S - 1440, sailed_nm: 181.3, tacks: 2,
  sail_plan: ["J1", "A3"], vs_plan: { available: true, gain_min: 12 },
  weather: { newest_age_h: 2.4, models: [{ model: "gfs" }, { model: "nam" }, { model: "hrrr" }] },
  warnings: ["Chart is out of date. Route doesn't check the shore.", "Route incomplete."],
  chart: { land: false } };
const v = (o) => RT.view(Object.assign({ nowMs: NOW, rrAgeMs: 1000, rrAt: NOW - 1000, press: {} }, o));

test("nothing runs on its own: the console only posts a run from the button", () => {
  const src = fs.readFileSync(path.join(__dirname, "dashboard", "dashboard.js"), "utf8");
  // the old automatic paths are gone
  assert.ok(!/fetchJSON\(\s*["']\/reoptimize/.test(src), "no GET /reoptimize from the console");
  assert.ok(!/fetchReoptimize/.test(src), "no fetchReoptimize");
  assert.ok(!/gpsShow\(\s*'reoptimize'/.test(src), "GPS sends the stored route, by 'reroute'");
  // the run endpoints appear once, inside the button's handler
  const handler = src.slice(src.indexOf("window.rerouteNow"), src.indexOf("function routeTileEl"));
  for (const ep of ["/reroute/run", "/reoptimize/run"]) {
    const q = '"' + ep + '"';
    assert.equal(src.split(q).length - 1, 1, ep + " is called from one place");
    assert.ok(handler.includes(q), ep + " only in rerouteNow");
  }
  assert.ok(!/setInterval\([^)]*rerouteNow/.test(src) && !/setTimeout\([^)]*rerouteNow/.test(src),
    "no timer presses the button");
});

test("idle with no route: big button ready, plain line", () => {
  const x = v({ rr: IDLE });
  assert.equal(x.button.label, "Re-route now");
  assert.equal(x.button.disabled, false);
  assert.equal(x.button.engine, "orin");
  assert.equal(x.say, "No new route yet.");
  assert.equal(x.chip, "READY");
  assert.equal(x.status, "ok");
});

test("a suggestion is quiet text with its reason, not a status", () => {
  const x = v({ rr: Object.assign({}, IDLE, { latest: "orin", result: ORIN_ROUTE }),
    suggest: { reason: "Off the playbook. The right is paying." }, sugAgeMs: 5000 });
  assert.equal(x.say, "Off the playbook. The right is paying.");
  assert.equal(x.sayKind, "suggest");
  assert.equal(x.sub, "Last: 12 min faster than the plan · 24 min ago");
  assert.equal(x.status, "ok");
  assert.equal(x.button.disabled, false);
  const old = v({ rr: IDLE, suggest: { reason: "Off the playbook." }, sugAgeMs: 10 * 60000 });
  assert.equal(old.suggest, null, "an old suggestion is dropped");
});

test("pressing: Starting… until the run shows; a second press is refused (button disabled)", () => {
  const posting = v({ rr: IDLE, press: { phase: "posting", engine: "orin", at: NOW } });
  assert.equal(posting.button.disabled, true);
  assert.equal(posting.button.label, "Starting…");
  const started = v({ rr: IDLE, press: RT.afterPress("orin", 202, { ok: true }, NOW - 2000) });
  assert.equal(started.button.disabled, true);
  assert.equal(started.say, "Asking the Orin…");
});

test("busy: progress, elapsed of expected, button disabled", () => {
  const rr = Object.assign({}, IDLE, { state: "running", asked_by: "Bow iPad",
    progress: { phase: "routing", elapsed_s: 34, expected_s: 70, pct: 48 } });
  const x = v({ rr: rr });
  assert.equal(x.running, true);
  assert.equal(x.button.disabled, true);
  assert.equal(x.button.label, "Re-routing…");
  assert.equal(x.chip, "RUNNING");
  assert.equal(x.say, "Re-routing… 34 s of ~70");
  assert.equal(x.progress.pct, 48);
  assert.equal(x.progress.who, "Bow iPad");
});

test("a press another iPad beat us to: 409 busy says so", () => {
  const p = RT.afterPress("orin", 409, { busy: true, detail: "A re-route is already running." }, NOW);
  assert.equal(p.phase, "busy");
  const x = v({ rr: IDLE, press: p });
  assert.equal(x.note, "Already running. One at a time.");
});

test("done: the last route, gain vs the plan, forecast age, models, who asked, labels", () => {
  const rr = Object.assign({}, IDLE, { state: "done", latest: "orin", result: ORIN_ROUTE });
  const x = v({ rr: rr });
  assert.equal(x.button.disabled, false);
  assert.equal(x.say, "12 min faster than the plan · 24 min ago");
  const L = x.last;
  assert.equal(L.head, "12 min faster than the plan");
  assert.equal(L.wx, "Forecast 2.4 h old · GFS, NAM, HRRR");
  assert.match(L.who, /^Asked by Nav iPad at \d\d:\d\d$/);
  assert.equal(L.shape, "181 nm · 2 tacks · J1→A3");
  assert.deepEqual(L.labels, ["Off the playbook", "Route doesn't check the shore"]);
  assert.deepEqual(L.warnings, ["Route incomplete."]);
  const slower = RT.describe({ source: "orin", r: Object.assign({}, ORIN_ROUTE, { vs_plan: { available: true, gain_min: -8 } }) }, S);
  assert.equal(slower.head, "8 min slower than the plan");
});

test("error: a failed run says why, the button stays usable, the old route stays", () => {
  const rr = Object.assign({}, IDLE, { state: "error", note: "Re-route took too long. Try again.",
    latest: "orin", result: ORIN_ROUTE });
  const x = v({ rr: rr });
  assert.equal(x.button.disabled, false);
  assert.equal(x.say, "Last try failed. Re-route took too long. Try again.");
  assert.equal(x.last.head, "12 min faster than the plan");
  const refused = v({ rr: IDLE, press: RT.afterPress("orin", 409, { detail: "No boat position yet." }, NOW) });
  assert.equal(refused.say, "No boat position yet.");
  const nolink = v({ rr: IDLE, press: RT.afterPress("orin", null, null, NOW) });
  assert.equal(nolink.say, "No link to the boat. Try again.");
  assert.equal(nolink.button.disabled, false);
});

test("Orin unreachable: the same button offers the Pi, armed after a beat", () => {
  const p = RT.afterPress("orin", 503, { orin: "unreachable", detail: "Can't reach the Orin. Re-route not started." }, NOW);
  assert.equal(p.phase, "unreachable");
  const off = Object.assign({}, IDLE, { orin: { reachable: false } });
  const now = v({ rr: off, press: p });
  assert.equal(now.fallback, true);
  assert.equal(now.button.label, "Use Pi route");
  assert.equal(now.button.engine, "pi");
  assert.equal(now.button.disabled, true, "a double tap can't take it");
  assert.equal(now.say, "Can't reach the Orin.");
  assert.equal(now.sub, "Pi route: one forecast, no shore check.");
  const armed = v({ rr: off, press: p, nowMs: NOW + RT.ARM_MS + 1 });
  assert.equal(armed.button.disabled, false);
  // the offer lapses, or ends when the Orin answers again
  const lapsed = v({ rr: off, press: p, nowMs: NOW + RT.FALLBACK_MS + 1 });
  assert.equal(lapsed.button.label, "Re-route now");
  const back = v({ rr: IDLE, press: p, nowMs: NOW + 5000, rrAt: NOW + 4000 });
  assert.equal(back.button.label, "Re-route now");
});

test("the Pi fallback runs and reports like the Orin", () => {
  const off = { orin: { reachable: false }, state: null, result: null, latest: null,
    pi: { state: "running", asked_by: "Nav iPad", progress: { elapsed_s: 6, expected_s: 30, pct: 20 } } };
  const p = RT.afterPress("pi", 202, { ok: true, engine: "pi" }, NOW - 1000);
  const run = v({ rr: off, press: p });
  assert.equal(run.say, "Re-routing on the Pi… 6 s of ~30");
  assert.equal(run.button.disabled, true);
  const done = { orin: { reachable: false }, state: null, result: null, latest: "pi",
    pi: { state: "done", result: { available: true, source: "pi", off_playbook: true, asked_by: "Nav iPad",
      asked_at: S - 100, computed_at: S - 80, depart_epoch: S - 98, finish_eta: S - 98 + 254 * 60,
      eta_min: 254, tacks: 9, sailed_nm: 46.2, wind_source: "forecast" } } };
  const x = v({ rr: done, press: p });
  assert.equal(x.last.engine, "Pi");
  assert.match(x.last.head, /^Finish at \d\d:\d\d$/);
  const hm = (e) => { const d = new Date(e * 1000); return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2); };
  assert.equal(x.last.head, "Finish at " + hm(S - 98 + 254 * 60), "the route's own arrival, not compute end + duration");
  const noEta = RT.describe({ source: "pi", r: { available: true, computed_at: S - 80, depart_epoch: S - 200, eta_min: 60 } }, S);
  assert.equal(noEta.head, "Finish at " + hm(S - 200 + 3600), "older stored route: from its departure");
  assert.equal(x.last.wx, "Forecast · Open-Meteo");
  assert.deepEqual(x.last.labels, ["Off the playbook", "Route doesn't check the shore"]);
  const failed = v({ rr: Object.assign({}, done, { pi: Object.assign({}, done.pi, { state: "error",
    detail: "No new route. Couldn't reach A (3 nm short)." }) }), press: Object.assign({}, p, { at: NOW - 70000 }) });
  assert.equal(failed.say, "Last try failed. No new route. Couldn't reach A (3 nm short).");
});

test("a route for another course: never looks current, button stays, GPS off", () => {
  const rr = Object.assign({}, IDLE, { state: "done", latest: "orin",
    result: Object.assign({}, ORIN_ROUTE, { old_course: true }) });
  const x = v({ rr: rr });
  assert.equal(x.say, "Route is for an old course.");
  assert.equal(x.sayKind, "note");
  assert.equal(x.last.head, "Route is for an old course.");
  assert.equal(x.last.old, true);
  assert.equal(x.button.label, "Re-route now");
  assert.equal(x.button.disabled, false);
  assert.equal(x.canShowOnGps, false);
  assert.ok(!/faster|slower/.test(x.say + (x.sub || "")));
  const withSug = v({ rr: rr, suggest: { reason: "Off the playbook." }, sugAgeMs: 0 });
  assert.equal(withSug.sub, "Route is for an old course.");
  const current = v({ rr: Object.assign({}, rr, { result: Object.assign({}, ORIN_ROUTE, { old_course: false }) }) });
  assert.equal(current.canShowOnGps, true);
});

test("an incomplete Orin route: says so, never READY-looking, GPS off", () => {
  const stall = Object.assign({}, ORIN_ROUTE, { complete: false, finish_eta: null,
    vs_plan: { available: false, note: "Route incomplete." }, warnings: ["Route incomplete."] });
  const x = v({ rr: Object.assign({}, IDLE, { state: "done", latest: "orin", result: stall }) });
  assert.equal(x.say, "Route incomplete.");
  assert.equal(x.sayKind, "note");
  assert.equal(x.last.head, "Route incomplete.");
  assert.equal(x.last.incomplete, true);
  assert.equal(x.canShowOnGps, false);
  assert.equal(x.button.label, "Re-route now");
  assert.equal(x.button.disabled, false);
  const withSug = v({ rr: Object.assign({}, IDLE, { latest: "orin", result: stall }),
    suggest: { reason: "Off the playbook." }, sugAgeMs: 0 });
  assert.equal(withSug.sub, "Route incomplete.");
  assert.ok(!/^Last:/.test(withSug.sub));
});

test("a route made without zones says so plainly on the face and in the detail", () => {
  const nz = Object.assign({}, ORIN_ROUTE, { no_zones: true,
    warnings: ["No zones or rounding sides aboard. Redeploy the course."] });
  const x = v({ rr: Object.assign({}, IDLE, { state: "done", latest: "orin", result: nz }) });
  assert.equal(x.sub, "No zones or rounding sides aboard. Redeploy the course.");
  assert.deepEqual(x.last.warnings, ["No zones or rounding sides aboard. Redeploy the course."]);
  const pi = RT.describe({ source: "pi", r: { available: true, computed_at: S - 10,
    warnings: ["No zones or rounding sides aboard. Redeploy the course."] } }, S);
  assert.equal(pi.noZones, true);
});

test("poll cadence: fast while running or just started, back to 10 s once done", () => {
  const p = RT.afterPress("orin", 202, { ok: true }, NOW);
  assert.equal(RT.pollEvery(v({ rr: IDLE, press: p }), p, NOW + 1000), RT.RUN_POLL_MS);
  const runRr = Object.assign({}, IDLE, { state: "running", progress: { elapsed_s: 30, pct: 40 } });
  assert.equal(RT.pollEvery(v({ rr: runRr, press: p, nowMs: NOW + 40000 }), p, NOW + 40000), RT.RUN_POLL_MS);
  // the run finished: the press stays "started", but the window has passed → idle cadence
  const later = NOW + RT.STARTING_MS + 1;
  const doneRr = Object.assign({}, IDLE, { state: "done", latest: "orin", result: ORIN_ROUTE });
  assert.equal(RT.pollEvery(v({ rr: doneRr, press: p, nowMs: later }), p, later), RT.IDLE_POLL_MS);
  assert.equal(RT.pollEvery(v({ rr: doneRr, press: p, nowMs: NOW + 3600000 }), p, NOW + 3600000), RT.IDLE_POLL_MS);
  assert.equal(RT.pollEvery(v({ rr: IDLE }), { phase: "posting", at: NOW }, NOW), RT.RUN_POLL_MS);
  assert.equal(RT.pollEvery(v({ rr: IDLE }), {}, NOW), RT.IDLE_POLL_MS);
  const src = fs.readFileSync(path.join(__dirname, "dashboard", "dashboard.js"), "utf8");
  assert.ok(/setTimeout\(fetchReroute, RT\.pollEvery\(/.test(src), "the console schedules by RT.pollEvery");
});

test("stale-safe: a held read shows its age; an old 'running' never blocks the button", () => {
  const rr = Object.assign({}, IDLE, { state: "running", progress: { elapsed_s: 30, expected_s: 70, pct: 40 } });
  const held = v({ rr: rr, rrAgeMs: 45000 });
  assert.equal(held.stale, true);
  assert.equal(held.chip, "RUNNING");
  assert.equal(held.button.disabled, true);
  const old = v({ rr: rr, rrAgeMs: RT.RUN_TRUST_MS + 1000 });
  assert.equal(old.running, false);
  assert.equal(old.button.disabled, false, "the engine refuses a real second run itself");
  assert.equal(old.chip, "HELD");
  const none = v({ rr: null });
  assert.equal(none.status, "na");
  assert.equal(none.chip, "NO LINK");
  assert.equal(none.say, "Waiting for the boat…");
});

test("words stay short: every line the tile can show is 12 words or fewer", () => {
  const lines = [];
  const add = (x) => { lines.push(x.say, x.button.label); if (x.note) lines.push(x.note); if (x.sub) lines.push(x.sub);
    if (x.last) lines.push(x.last.head, x.last.wx, ...x.last.labels); };
  add(v({ rr: IDLE }));
  add(v({ rr: Object.assign({}, IDLE, { state: "done", latest: "orin", result: ORIN_ROUTE }) }));
  add(v({ rr: IDLE, press: RT.afterPress("orin", 503, {}, NOW) }));
  add(v({ rr: IDLE, press: RT.afterPress("orin", 409, { busy: true }, NOW) }));
  add(v({ rr: IDLE, press: RT.afterPress("orin", null, null, NOW) }));
  for (const l of lines) assert.ok(words(l) <= 12, "too long: " + l);
  for (const l of lines) assert.ok(!/veer|back(ed|ing)/i.test(l), "say right/left: " + l);
});

let failed = 0;
for (const [name, fn] of cases) {
  try { fn(); console.log("ok   " + name); }
  catch (e) { failed++; console.log("FAIL " + name + "\n     " + e.message); }
}
console.log(cases.length - failed + " passed, " + failed + " failed");
process.exit(failed ? 1 : 0);
