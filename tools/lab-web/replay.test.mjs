// Debrief replay + strip chart (#373, #374): the pure helpers behind the one time cursor —
// positions between fixes, markers into windows A/B, and stats that match app/replay.py
// (faulty stretches left out, angles averaged round the circle).
// Run: node --test tools/lab-web/replay.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "replay.js"), "utf8");
const ctx = {};
vm.createContext(ctx);
vm.runInContext(src + "\nthis.C = ReplayCore; this.R = Replay;", ctx);   // loads with no DOM, no Leaflet
const C = ctx.C;
const near = (a, b, tol = 0.01) => assert.ok(Math.abs(a - b) <= tol, `${a} ≈ ${b}`);

test("position between fixes, null outside the track", () => {
  const fx = [[0, 43.0, -82.0], [10, 43.1, -82.2], [20, 43.1, -82.2]];
  const p = C.posAt(fx, 5);
  near(p.lat, 43.05, 1e-9); near(p.lon, -82.1, 1e-9); assert.equal(p.i, 0);
  assert.equal(C.posAt(fx, -1), null);
  assert.equal(C.posAt(fx, 21), null);
  assert.equal(C.posAt(fx, 20).i, 2);
});

test("index lookup and wind frame at a time", () => {
  assert.equal(C.idxAt([[0], [5], [9]], 6), 1);
  assert.equal(C.idxAt([[0], [5], [9]], -1), -1);
  assert.equal(C.frameAt([100, 200, 300], 250), 1);
  assert.equal(C.frameAt([100, 200, 300], 50), 0);     // before the first frame: show the first
  assert.equal(C.frameAt([], 50), -1);
});

test("markers sort into windows A (1-2) and B (3-4); a fifth moves the nearest", () => {
  let m = [];
  for (const t of [30, 10, 50]) m = C.addMarker(m, t);
  let w = C.windows(m);
  assert.deepEqual([...w.A], [10, 30]); assert.equal(w.B, null);
  m = C.addMarker(m, 70);
  w = C.windows(m);
  assert.deepEqual([...w.B], [50, 70]);
  m = C.addMarker(m, 48);
  assert.deepEqual([...m], [10, 30, 48, 70]);
});

test("stats: mean and SD, faulty stretch left out", () => {
  const s = [5, 7, 5, 7, 100].map((v, t) => ({ t, bsp: v }));
  const st = C.windowStats(s, 4, 0, [[3.5, 4.5]], ["bsp"]);
  assert.equal(st.n, 5); assert.equal(st.left_out, 1);
  assert.deepEqual({ ...st.stats.bsp }, { mean: 6, sd: 1, n: 4 });
});

test("stats: angles round the circle, TWA keeps its sign", () => {
  const s = [{ t: 0, twd: 350, twa: -40 }, { t: 1, twd: 10, twa: -50 }];
  const st = C.windowStats(s, 0, 1, [], ["twd", "twa"]);
  assert.ok(st.stats.twd.mean < 0.01 || st.stats.twd.mean > 359.99);
  near(st.stats.twa.mean, -45);
  near(st.stats.twd.sd, 10, 0.2);
});

test("B − A takes the short way round for angles", () => {
  assert.equal(C.diff("twd", 350, 10), 20);
  assert.equal(C.diff("twd", 10, 350), -20);
  assert.equal(C.diff("bsp", 6, 6.5), 0.5);
  assert.equal(C.diff("bsp", null, 6), null);
});

test("clock and duration labels", () => {
  assert.equal(C.fmtClock(0), "00:00:00Z");
  assert.equal(C.fmtDur(150), "2:30");
  assert.equal(C.fmtDur(3700), "1h01");
});

test("stats: a constant angle has SD 0, not NaN", () => {
  for (const a of [20, 40, -40, 60, 340]) {
    for (const n of [5, 10, 19, 36, 103]) {
      const s = Array.from({ length: n }, (_, t) => ({ t, twa: a, cog: (a + 360) % 360 }));
      const st = C.windowStats(s, 0, n, [], ["twa", "cog"]);
      assert.ok(Number.isFinite(st.stats.twa.sd) && Number.isFinite(st.stats.cog.sd), `${a}° x ${n}`);
      near(st.stats.twa.sd, 0); near(st.stats.cog.mean, (a + 360) % 360);
    }
  }
});

test("a step that only partly overlaps a fault is left out (Codex on #397)", () => {
  // 7 s steps at 100 and 107; fault from 105: the step at 100 mixes healthy and faulty seconds
  const s = [{ t: 93, bsp: 6 }, { t: 100, bsp: 50 }, { t: 107, bsp: 50 }];
  const st = C.windowStats(s, 93, 114, [[105, 110]], ["bsp"], 7);
  assert.equal(st.left_out, 2); assert.equal(st.stats.bsp.mean, 6);
  assert.equal(C.inDanger(100, [[105, 110]]), false);     // a point sample at 100 is fine
  assert.equal(C.inDanger(100, [[105, 110]], 7), true);
  assert.equal(C.inDanger(98, [[105, 110]], 7), false);   // [98, 105) ends as the fault starts
});

test("only the newest channel request lands, and only for the race still open", async () => {
  const pending = [];
  const c2 = {
    requestAnimationFrame: () => 1, cancelAnimationFrame: () => {}, performance: { now: () => 0 },
    apiGet: (url) => new Promise((resolve) => pending.push({ url, resolve })),
  };
  vm.createContext(c2);
  vm.runInContext(src + "\nthis.R = Replay;", c2);
  const R = c2.R, S = R._state;
  Object.assign(S, { open: true, raceId: "A", recording: 100, data: { own: { t0: 0, t1: 1000 } } });
  const zoom = R._loadChannels(10, 20);           // slow zoom
  const whole = R._loadChannels(null, null);      // then "Whole race"
  const ans = (p, tag) => p.resolve({ json: async () => ({ available: true, tag, samples: [], channels: [] }) });
  ans(pending[1], "whole"); await whole;
  ans(pending[0], "zoom"); await zoom;            // lands late: dropped
  assert.equal(S.ch.tag, "whole");
  assert.deepEqual([...S.view], [0, 1000]);
  // another race with the same recording timestamp: its answer can't land on this one
  const old = R._loadChannels(null, null);
  S.raceId = "B";
  ans(pending[2], "race A"); await old;
  assert.equal(S.ch.tag, "whole");
});

test("only whole steps inside the window count; a short window says so (Codex on #397)", () => {
  const s = [{ t: 0, bsp: 6 }, { t: 7, bsp: 6 }, { t: 14, bsp: 99 }];   // step at 14 runs to 21
  const st = C.windowStats(s, 0, 20, [], ["bsp"], 7);
  assert.equal(st.n, 2); assert.equal(st.stats.bsp.mean, 6);
  assert.equal(st.coarse, true);                                       // 20 s at 7 s steps
  assert.equal(C.windowStats(s, 0, 200, [], ["bsp"], 7).coarse, false);
  assert.equal(C.windowStats(s, 0, 14, [], ["bsp"]).n, 3);             // points: ends inclusive
});

test("opening another recording clears a wind load in flight", async () => {
  const c3 = { requestAnimationFrame: () => 1, cancelAnimationFrame: () => {}, performance: { now: () => 0 },
    document: { getElementById: () => null }, apiGet: () => new Promise(() => {}) };
  vm.createContext(c3);
  vm.runInContext(src + "\nthis.R = Replay;", c3);
  const S = c3.R._state;
  S.windBusy = true; S.windMsg = "fetching";
  // the state resets before any DOM work; with no DOM here the rest of open() rejects, ignored
  await c3.R.open("A", 200, null, "other").catch(() => {});
  assert.equal(S.windBusy, false); assert.equal(S.windMsg, "");
});

test("covers: the loaded range must hold the whole window", () => {
  assert.equal(C.covers([100, 200], [120, 180]), true);
  assert.equal(C.covers([100, 200], [180, 120]), true);
  assert.equal(C.covers([100, 200], [90, 150]), false);
  assert.equal(C.covers(null, [1, 2]), false);
});

// a DOM just big enough for the card: every element is a stub that remembers its innerHTML
function fakeDom() {
  const made = [];
  const mk = () => {
    const kids = {};
    return { innerHTML: "", style: {}, className: "", textContent: "", value: 0, clientWidth: 0, clientHeight: 0,
      addEventListener() {}, appendChild() {}, scrollIntoView() {}, getContext: () => null,
      querySelector(sel) { return (kids[sel] ||= mk()); } };
  };
  const create = () => { const e = mk(); made.push(e); return e; };
  return { made, document: { createElement: create, getElementById: () => null, head: { appendChild() {} } },
           window: { addEventListener() {} } };
}

test("a window outside the zoomed data shows Loading and refetches the whole race (Codex on #397)", async () => {
  const pending = [];
  const dom = fakeDom();
  const c4 = { ...dom, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {}, performance: { now: () => 0 },
    esc: String, attr: String, L: undefined,
    apiGet: (url) => new Promise((resolve) => pending.push({ url, resolve })) };
  vm.createContext(c4);
  vm.runInContext(src + "\nthis.R = Replay;", c4);
  const R = c4.R, S = R._state;
  const opened = R.open("A", 0, null, "x");
  const t = [{ id: "far", name: "far", start: 700, end: 850, seconds: 150, stats: {} }];
  pending.shift().resolve({ json: async () => ({ available: true, own: { t0: 0, t1: 1000, fixes: [[0, 1, 1], [1000, 1, 1]] },
    fleet: { boats: [], note: "" }, marks: [], danger: [], tests: t, wind_grid: null }) });
  await opened.catch(() => {});                  // buildMap needs Leaflet: not here, ignored
  // pretend we zoomed to 100-300 and that data is in
  const samples = Array.from({ length: 201 }, (_, i) => ({ t: 100 + i, bsp: 6 }));
  Object.assign(S, { ch: { available: true, channels: ["bsp"], samples, step_s: 1 }, view: [100, 300], chBusy: false });
  pending.length = 0;
  R.showTest("far");                             // 700-850: outside the zoom
  const card = dom.made.find((e) => e.className === "card rp-card");
  assert.match(card.querySelector(".rp-stats").innerHTML, /Loading/);   // no stats from part of the window
  assert.equal(pending.length, 1, "one refetch");
  assert.ok(!/start=/.test(pending[0].url), "the whole race, not a window");
  const whole = Array.from({ length: 1001 }, (_, i) => ({ t: i, bsp: i >= 700 && i <= 850 ? 7 : 6 }));
  pending[0].resolve({ json: async () => ({ available: true, channels: ["bsp"], samples: whole, step_s: 1 }) });
  await new Promise((r) => setTimeout(r, 0));
  assert.deepEqual([...S.view], [0, 1000]);
  assert.match(card.querySelector(".rp-stats").innerHTML, /7\.00/);
  const st = C.windowStats(S.ch.samples, 700, 850, [], ["bsp"], 1);
  assert.equal(st.stats.bsp.mean, 7);           // now computed on the window's own samples
});

test("a failed load for recording A can't overwrite recording B's status", async () => {
  const pending = [];
  const dom = fakeDom();
  const c5 = { ...dom, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {}, performance: { now: () => 0 },
    esc: String, attr: String, apiGet: () => new Promise((resolve, reject) => pending.push({ resolve, reject })) };
  vm.createContext(c5);
  vm.runInContext(src + "\nthis.R = Replay;", c5);
  const S = c5.R._state;
  const a = c5.R.open("A", 1, null, "a");
  const b = c5.R.open("B", 2, null, "b");
  pending[0].reject(new Error("down"));
  await a.catch(() => {});
  assert.equal(S.raceId, "B"); assert.equal(S.msg, "Loading…");
  pending[1].resolve({ json: async () => ({ available: false, note: "No track." }) });
  await b.catch(() => {});
  assert.equal(S.msg, "No track.");
});

function harness2() {
  const gets = [], posts = [];
  const dom = fakeDom();
  const c = { ...dom, requestAnimationFrame: () => 1, cancelAnimationFrame: () => {}, performance: { now: () => 0 },
    esc: String, attr: String,
    apiGet: () => new Promise((resolve) => gets.push({ resolve })),
    apiPost: (url, body) => new Promise((resolve) => posts.push({ url, body, resolve })) };
  vm.createContext(c);
  vm.runInContext(src + "\nthis.R = Replay;", c);
  const build = (tests) => ({ json: async () => ({ available: true, own: { t0: 0, t1: 1000, fixes: [[0, 1, 1], [1000, 1, 1]] },
    fleet: { boats: [], note: "" }, marks: [], danger: [], tests, wind_grid: null }) });
  const open = async (race, rec, tests) => {
    const p = c.R.open(race, rec, null, race);
    gets.shift().resolve(build(tests));
    await p.catch(() => {});                     // buildMap needs Leaflet: not here
    gets.length = 0;                             // the channel fetch is left pending
  };
  return { R: c.R, S: c.R._state, posts, open, dom };
}

test("a save still pending when another recording opens never lands on it (Codex on #397)", async () => {
  const h = harness2();
  await h.open("A", 1, []);
  h.S.markers = [100, 250];
  const saving = h.R.saveTest();
  assert.equal(h.posts[0].body.race_id, "A"); assert.equal(h.posts[0].body.recording, 1);
  await h.open("B", 2, []);
  h.posts[0].resolve({ json: async () => ({ ok: true, test: { id: "t1", name: "", start: 100, end: 250 } }) });
  await saving;
  assert.equal(h.S.raceId, "B"); assert.equal(h.S.data.tests.length, 0);
});

test("delete goes to the recording whose list was clicked, and only changes that list", async () => {
  const h = harness2();
  await h.open("B", 2, [{ id: "t1", name: "x", start: 0, end: 150, seconds: 150, stats: {} }]);
  const del = h.R.delTest("t1");
  assert.equal(h.posts[0].body.race_id, "B"); assert.equal(h.posts[0].body.recording, 2);
  await h.open("C", 3, [{ id: "t1", name: "same id elsewhere", start: 0, end: 150, seconds: 150, stats: {} }]);
  h.posts[0].resolve({ json: async () => ({ ok: true }) });
  await del;
  assert.equal(h.S.data.tests.length, 1);       // C's list untouched
});

test("no instrument readout when the cursor is outside the loaded data", async () => {
  const h = harness2();
  await h.open("A", 1, []);
  const samples = Array.from({ length: 201 }, (_, i) => ({ t: 100 + i, bsp: 6, tws: 10 }));
  Object.assign(h.S, { ch: { available: true, channels: ["bsp", "tws"], samples, step_s: 1 }, view: [100, 300] });
  const card = h.dom.made.find((e) => e.className === "card rp-card");
  h.R.seek(150);
  assert.match(card.querySelector(".rp-now").textContent, /BSP/);
  h.R.seek(500);                                 // outside the zoom
  assert.equal(card.querySelector(".rp-now").textContent, "");
});
