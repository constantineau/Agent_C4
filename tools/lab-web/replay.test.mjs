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
  const st = C.windowStats(s, 93, 107, [[105, 110]], ["bsp"], 7);
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
