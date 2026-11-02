// Collision watch on the iPad coach strip (pi/console/dashboard/collisionwatch.js) — 2026-11-02
// system review, collision items 2, 3, 6. Pins: an active collision warning holds the strip until
// it clears (a routine line voiced later can't push it off), every contact shows with live
// numbers, a watch that can't see says "Collision watch OFF: <reason>" (never a quiet strip), and
// the tone re-arms when a contact clears and returns or gets worse.
// Run: node pi/console/test_collisionwatch.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const COL = require(path.join(__dirname, "dashboard", "collisionwatch.js"));

const cases = [];
const test = (name, fn) => cases.push([name, fn]);
const words = (s) => String(s).trim().split(/\s+/).length;

const T0 = 1793000000000;
const risk = (id, name, cpa, tcpa, urgency = "now") => ({ id: "ais:" + id + ":" + (urgency === "now" ? "act" : "watch"),
  category: "safety", urgency, headline: (urgency === "now" ? "Collision risk: " : "Traffic closing: ") + name,
  detail: "CPA " + cpa + " nm / " + tcpa + " min" });
const routine = { id: "checklist:navlights", category: "checklist", urgency: "soon", headline: "Nav lights on", detail: "sunset" };
const coach = (o) => Object.assign({ enabled: true, running: true, interval_s: 30, updated_at: 1000, active: [], new: [], history: [] }, o);
// the tone levels dashboard.js uses (alertLevel)
const levelOf = (c) => c.urgency === "now" ? "act" : (["safety", "watch", "checklist"].includes(c.category) ? "watch" : null);

// a live state that has just polled successfully
function live(c, nowMs = T0) {
  const st = COL.newState(nowMs - 1000);
  COL.track(st, c, nowMs);
  return st;
}

test("the warning holds the strip while a routine callout is voiced later", () => {
  const ferry = risk("1", "FERRY", 0.2, 6);
  // T0: the collision was voiced; T+30 s: the nav-lights checklist is the newest history line
  const c = coach({ active: [ferry, routine],
    history: [{ at: 1030, spoken: "Nav lights on — sunset" }, { at: 1000, spoken: "Collision risk: FERRY — CPA 0.2 nm / 6 min" }] });
  const v = COL.strip(c, COL.offReason(c, live(c), T0));
  assert.equal(v.kind, "act");
  assert.deepEqual(v.lines, ["Collision risk: FERRY — CPA 0.2 nm / 6 min"]);
  assert.equal(v.fromHistory, false);
});

test("numbers on the strip are the live ones from `active`, not the first-voiced line", () => {
  const c = coach({ active: [risk("1", "FERRY", 0.1, 3)], history: [{ at: 1000, spoken: "Collision risk: FERRY — CPA 0.2 nm / 6 min" }] });
  assert.deepEqual(COL.strip(c, null).lines, ["Collision risk: FERRY — CPA 0.1 nm / 3 min"]);
});

test("every contact shows, in the copilot's order (soonest first); more than 4 → '+N more'", () => {
  const list = [risk("1", "TUG", 0.4, 3), risk("2", "BARGE", 0.3, 7), risk("3", "FAR", 0.05, 11), risk("4", "FERRY", 1.2, 8, "soon")];
  let v = COL.strip(coach({ active: list }), null);
  assert.deepEqual(v.lines.map((l) => l.split(" — ")[0]),
    ["Collision risk: TUG", "Collision risk: BARGE", "Collision risk: FAR", "Traffic closing: FERRY"]);
  v = COL.strip(coach({ active: list.concat([risk("5", "TANKER", 0.4, 9), risk("6", "LAKER", 0.4, 10)]) }), null);
  assert.equal(v.lines.length, 5);
  assert.equal(v.lines[4], "+2 more");
});

test("watch-level only → kind 'watch'; it still beats routine history", () => {
  const c = coach({ active: [routine, risk("1", "BARGE", 1.2, 25, "soon")], history: [{ at: 1000, spoken: "Nav lights on" }] });
  const v = COL.strip(c, null);
  assert.equal(v.kind, "watch");
  assert.match(v.lines[0], /^Traffic closing: BARGE/);
});

test("the warning clears → the strip falls back to the coach history", () => {
  const c = coach({ active: [routine], history: [{ at: 1030, spoken: "Nav lights on — sunset" }] });
  const v = COL.strip(c, null);
  assert.equal(v.kind, "coach");
  assert.deepEqual(v.lines, ["Nav lights on — sunset"]);
  assert.equal(v.fromHistory, true);
});

test("GPS outage recovers: a cleared 'Collision watch OFF' in history is not shown (no stuck OFF)", () => {
  // the OFF line was the last thing voiced; the fix is back, so `active` has no safety callout
  const offEntry = { at: 1060, spoken: "Collision watch OFF: GPS fix stale — Last fix 45 s ago. Keep a lookout.",
    callouts: ["Collision watch OFF: GPS fix stale"], ids: ["blind:fix"], categories: ["safety"] };
  let v = COL.strip(coach({ active: [], history: [offEntry] }), null);
  assert.equal(v.kind, "none");
  assert.deepEqual(v.lines, []);
  // an older routine line is still fine to show, with its own time
  v = COL.strip(coach({ active: [routine], history: [offEntry, { at: 1000, spoken: "Nav lights on — sunset",
    callouts: ["Nav lights on"], ids: ["checklist:navlights"], categories: ["checklist"] }] }), null);
  assert.equal(v.kind, "coach");
  assert.deepEqual(v.lines, ["Nav lights on — sunset"]);
  assert.equal(v.at, 1000);
  // a cleared collision warning is skipped the same way
  v = COL.strip(coach({ active: [], history: [{ at: 1000, spoken: "Collision risk: FERRY — CPA 0.2 nm / 6 min",
    callouts: ["Collision risk: FERRY"], ids: ["ais:1:act"], categories: ["safety"] }] }), null);
  assert.equal(v.kind, "none");
});

test("an older copilot's history (headlines only, no categories) is judged by the safety words", () => {
  const v = COL.strip(coach({ active: [], history: [{ at: 1060, spoken: "Collision watch OFF: no GPS fix — Keep a lookout.",
    callouts: ["Collision watch OFF: no GPS fix"] }] }), null);
  assert.equal(v.kind, "none");
});

test("the copilot's own blind callout shows as a safety line", () => {
  const blind = { id: "blind:fix", category: "safety", urgency: "now", headline: "Collision watch OFF: GPS fix stale", detail: "Last fix 45 s ago. Keep a visual lookout." };
  const v = COL.strip(coach({ active: [blind] }), null);
  assert.equal(v.kind, "act");
  assert.match(v.lines[0], /^Collision watch OFF: GPS fix stale/);
});

test("blind: no answer from the copilot for a minute → OFF: copilot offline (old warning not shown)", () => {
  const c = coach({ active: [risk("1", "FERRY", 0.2, 6)] });
  const st = live(c, T0);
  COL.track(st, null, T0 + 30000);                       // failed polls
  assert.equal(COL.offReason(c, st, T0 + 30000), null);  // 30 s: one dropped poll isn't "off"
  COL.track(st, null, T0 + 61000);
  const off = COL.offReason(c, st, T0 + 61000);
  assert.equal(off, "copilot offline");
  const v = COL.strip(c, off);
  assert.equal(v.kind, "off");
  assert.deepEqual(v.lines, ["Collision watch OFF: copilot offline. Keep a lookout."]);
});

test("blind: never reached the copilot since the page loaded → OFF after a minute, not a blank strip", () => {
  const st = COL.newState(T0);
  COL.track(st, null, T0 + 5000);
  assert.equal(COL.offReason(null, st, T0 + 5000), null);
  assert.equal(COL.offReason(null, st, T0 + 61000), "copilot offline");
  assert.equal(COL.strip(null, "copilot offline").kind, "off");
});

test("blind: the copilot answers but its result stopped moving (a stuck tick) → OFF: copilot stalled", () => {
  const c = coach({ updated_at: 1000 });
  const st = live(c, T0);
  for (let t = 15000; t <= 120000; t += 15000) COL.track(st, c, T0 + t);   // same updated_at every poll
  assert.equal(COL.offReason(c, st, T0 + 90000), null);                   // under 3 ticks + slack
  assert.equal(COL.offReason(c, st, T0 + 120000), "copilot stalled");
  COL.track(st, coach({ updated_at: 1120 }), T0 + 121000);                 // it moves again
  assert.equal(COL.offReason(coach({ updated_at: 1120 }), st, T0 + 121000), null);
});

test("blind: the coach timer is stopped or disabled → OFF: copilot stopped", () => {
  for (const c of [coach({ running: false }), coach({ enabled: false, running: false })]) {
    assert.equal(COL.offReason(c, live(c), T0), "copilot stopped");
  }
});

test("repeated HTTP 200 `{}` replies are failed polls: the watchdog still trips (Codex on #191)", () => {
  const st = COL.newState(T0);
  for (let t = 0; t <= 75000; t += 15000) assert.equal(COL.track(st, {}, T0 + t), false);
  assert.equal(COL.offReason(null, st, T0 + 75000), "copilot offline");
  // junk shapes too
  for (const bad of [[], "ok", { active: [] }, { active: [], history: [], running: "yes", updated_at: 1 },
    { active: [], history: [], running: true, updated_at: "1000" }, { active: {}, history: [], running: true, updated_at: 1 }]) {
    assert.equal(COL.validCoach(bad), false, JSON.stringify(bad));
  }
  assert.equal(COL.validCoach(coach({})), true);
  assert.equal(COL.validCoach(coach({ updated_at: null })), true);    // before the copilot's first tick
});

test("updated_at must ADVANCE: the same (or an older) stamp replayed goes 'copilot stalled'", () => {
  const c = coach({ updated_at: 1000 });
  const st = live(c, T0);
  for (let t = 15000; t <= 120000; t += 15000) COL.track(st, coach({ updated_at: t % 30000 ? 1000 : 900 }), T0 + t);
  assert.equal(COL.offReason(c, st, T0 + 120000), "copilot stalled");
});

test("the Orin's clock jumps back (NTP at boot): healthy ticks with rising seq are NOT 'stalled'", () => {
  const st = COL.newState(T0 - 1000);
  // updated_at jumps back an hour after the first reply and then creeps up below the old value
  let c = coach({ updated_at: 5000, seq: 10, instance: "a1" });
  COL.track(st, c, T0);
  for (let i = 1; i <= 10; i++) {
    c = coach({ updated_at: 1400 + 30 * i, seq: 10 + i, instance: "a1" });
    assert.equal(COL.track(st, c, T0 + 15000 * i), true);
  }
  assert.equal(COL.offReason(c, st, T0 + 150000), null);
});

test("seq not rising goes 'stalled'; a copilot restart (new instance, seq back to 1) is progress", () => {
  const st = COL.newState(T0 - 1000);
  let c = coach({ updated_at: 1000, seq: 7, instance: "a1" });
  COL.track(st, c, T0);
  for (let t = 15000; t <= 120000; t += 15000) COL.track(st, coach({ updated_at: 1000 + t, seq: 7, instance: "a1" }), T0 + t);
  assert.equal(COL.offReason(c, st, T0 + 120000), "copilot stalled", "updated_at rising alone isn't progress");
  c = coach({ updated_at: 10, seq: 1, instance: "b2" });
  COL.track(st, c, T0 + 125000);
  assert.equal(COL.offReason(c, st, T0 + 125000), null);
});

test("a bad seq or instance is a malformed reply", () => {
  assert.equal(COL.validCoach(coach({ seq: "3", instance: "a1" })), false);
  assert.equal(COL.validCoach(coach({ seq: 3, instance: "" })), false);
  assert.equal(COL.validCoach(coach({ seq: 3, instance: "a1" })), true);
});

test("a working, quiet copilot is not OFF (and the strip is hidden)", () => {
  const c = coach({});
  assert.equal(COL.offReason(c, live(c), T0), null);
  assert.equal(COL.strip(c, null).kind, "none");
});

test("the tone re-arms when a contact clears and comes back", () => {
  const alerted = new Set();
  const ferry = risk("1", "FERRY", 0.2, 6);
  assert.equal(COL.alertsFor(coach({ active: [ferry] }), null, alerted, levelOf), "act");
  assert.equal(COL.alertsFor(coach({ active: [ferry] }), null, alerted, levelOf), null, "no repeat while it stays");
  assert.equal(COL.alertsFor(coach({ active: [] }), null, alerted, levelOf), null);
  assert.equal(COL.alertsFor(coach({ active: [ferry] }), null, alerted, levelOf), "act", "it came back: sound again");
});

test("the tone sounds again when the risk worsens (watch → act is a new id)", () => {
  const alerted = new Set();
  assert.equal(COL.alertsFor(coach({ active: [risk("1", "BARGE", 1.2, 20, "soon")] }), null, alerted, levelOf), "watch");
  assert.equal(COL.alertsFor(coach({ active: [risk("1", "BARGE", 0.4, 9)] }), null, alerted, levelOf), "act");
});

test("the watch going OFF sounds once per episode; recovery re-arms it", () => {
  const alerted = new Set();
  const c = coach({ active: [risk("1", "FERRY", 0.2, 6)] });
  assert.equal(COL.alertsFor(c, "copilot offline", alerted, levelOf), "act");
  assert.equal(COL.alertsFor(c, "copilot offline", alerted, levelOf), null);
  COL.alertsFor(coach({ active: [] }), null, alerted, levelOf);
  assert.equal(COL.alertsFor(c, "copilot offline", alerted, levelOf), "act");
});

test("strip words stay short (12 words max per OFF line)", () => {
  for (const r of ["copilot offline", "copilot stalled", "copilot stopped"]) {
    assert.ok(words(COL.strip(null, r).lines[0]) <= 12, r);
  }
});

test("dashboard.js wires it: renderCoach uses the strip, fetchCoach judges OFF on every poll", () => {
  const src = fs.readFileSync(path.join(__dirname, "dashboard", "dashboard.js"), "utf8");
  const html = fs.readFileSync(path.join(__dirname, "dashboard", "index.html"), "utf8");
  assert.ok(/COL\.strip\(App\.coach, App\.coachOff\)/.test(src));
  assert.ok(/if \(COL\.track\(colState, r, Date\.now\(\)\)\) App\.coach = r;/.test(src) && /App\.coachOff = COL\.watchdogStep/.test(src));
  assert.ok(/COL\.alertsFor\(/.test(src));
  assert.ok(!/c\.history\[0\]\.spoken/.test(src), "the old history-first strip is gone");
  assert.ok(html.indexOf("collisionwatch.js") > -1 && html.indexOf("collisionwatch.js") < html.indexOf("dashboard.js?"));
});

/* ---- a stalled body (Codex on #191 at dedaf39): headers sent, body never finishes ---- */
function server(handler) {
  return new Promise((resolve) => {
    const srv = http.createServer(handler);
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });
}
const urlOf = (srv, p) => "http://127.0.0.1:" + srv.address().port + p;

test("a /coach body that stalls after the headers → null within the deadline (no hung poll)", async () => {
  const srv = await server((req, res) => {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.write('{"active":[{"id":"ais:1:act","category":"safety"');   // ...and never ends
  });
  try {
    const t0 = Date.now();
    const r = await COL.fetchJsonWithin(urlOf(srv, "/coach"), { headers: { Accept: "application/json" } }, 400);
    const took = Date.now() - t0;
    assert.equal(r, null);
    assert.ok(took >= 350 && took < 3000, "returned after " + took + " ms");
  } finally { srv.closeAllConnections(); srv.close(); }
});

test("fetchJsonWithin: a good body parses; non-2xx, bad JSON and no server are null, never a throw", async () => {
  const srv = await server((req, res) => {
    if (req.url === "/ok") { res.writeHead(200, { "Content-Type": "application/json" }); res.end('{"a":1}'); }
    else if (req.url === "/bad") { res.writeHead(200); res.end("{not json"); }
    else { res.writeHead(503); res.end("{}"); }
  });
  try {
    assert.deepEqual(await COL.fetchJsonWithin(urlOf(srv, "/ok"), {}, 2000), { a: 1 });
    assert.equal(await COL.fetchJsonWithin(urlOf(srv, "/bad"), {}, 2000), null);
    assert.equal(await COL.fetchJsonWithin(urlOf(srv, "/down"), {}, 2000), null);
  } finally { srv.closeAllConnections(); srv.close(); }
  assert.equal(await COL.fetchJsonWithin("http://127.0.0.1:9/coach", {}, 2000), null);
});

test("stalled polls: the watchdog alone takes the watch OFF a minute after the last good parse", () => {
  const c = coach({ active: [risk("1", "FERRY", 0.2, 6)] });
  const st = live(c, T0);                                // last good parse at T0
  // no poll completes after this (each one stalls); only the watchdog runs, every 5 s
  let off = null;
  for (let t = 5000; t <= 65000; t += 5000) off = COL.watchdogStep(c, st, T0 + t);
  assert.equal(off, "copilot offline");
  assert.equal(COL.strip(c, off).kind, "off");           // the old collision result is not on screen
});

test("dashboard.js: every copilot fetch keeps its deadline through the body, and the watchdog runs on its own timer", () => {
  const src = fs.readFileSync(path.join(__dirname, "dashboard", "dashboard.js"), "utf8");
  // the old shape: clearTimeout(to) right after `await fetch(...)`, before resp.json()
  assert.ok(!/clearTimeout\(to\);\s*\n\s*(r = |const r = )resp\.ok \? await resp\.json\(\)/.test(src));
  for (const ep of ["/coach", "/dashboard", "/strategy", "/briefs/"]) {
    assert.ok(new RegExp('COL\\.fetchJsonWithin\\(COPILOT \\+ "' + ep.replace(/\//g, "\\/")).test(src), ep);
  }
  assert.ok(/setInterval\(coachWatchdog, \d+\)/.test(src));
  assert.ok(/COL\.watchdogStep\(App\.coach, colState, Date\.now\(\)\)/.test(src));
});

(async () => {
  let fails = 0;
  for (const [name, fn] of cases) {
    try { await fn(); console.log("  [PASS] " + name); }
    catch (e) { fails++; console.log("  [FAIL] " + name + "\n         " + (e && e.message)); }
  }
  console.log(fails ? "\nFAIL (" + fails + ")" : "\nPASS");
  process.exit(fails ? 1 : 0);
})();
