// iPad console (pi/console/dashboard/dashboard.js): the display fixes of 2026-11-06.
// Pins: #45.5a "Now" has a distance bound; #45.5b/c the PLAYBOOK tile ages with the selector,
// synthesis and plays; #45.7 held slow reads say how old they are; #46.1 "unknown" health is grey
// "not checked", never "All checks pass" / "Safe to use", and never hides a known warning; #56 two
// quick sail taps keep both sails; #184-4 a reading is stamped when asked for, a wake poll is never
// swallowed, a held wind is not logged again; #184-5 the course map follows a new course and
// survives a stale mark index; #40 the selector's `settling` is on screen; #95 G14/G52/G53/G55/G56;
// #185-4 a sensor missing from the N2K bus is on the DATA tile; FLEET claims no lead without a ranking;
// #246 the navigator card's Start / Undo last mark / next-mark picker.
//
// dashboard.js is a browser IIFE. It is loaded here in a vm with a do-nothing DOM and a scripted
// fetch; it hands its parts to `window.__C4DashTest` (a hook nothing on the iPad defines).
// Run: node pi/console/test_dashboard.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const DIR = path.join(__dirname, "dashboard");

/* ---- a DOM that accepts anything ---- */
function stubEl() {
  const store = { dataset: {}, style: {}, hidden: false, textContent: "", innerHTML: "", value: "" };
  const fn = function () { return proxy; };
  const proxy = new Proxy(fn, {
    get(t, k) {
      if (k in store) return store[k];
      if (k === "classList") return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
      if (k === Symbol.toPrimitive) return () => "";
      if (k === "then") return undefined;
      if (k === "closest" || k === "querySelector" || k === "getElementById") return () => null;
      if (k === "querySelectorAll" || k === "children") return [];
      return proxy;
    },
    set(t, k, v) { store[k] = v; return true; },
    apply() { return proxy; },
  });
  return proxy;
}

function load() {
  let nowMs = 1793000000000;
  const els = {};
  let timers = [], tid = 0;          // timers run only when a test calls fire(), on the fake clock
  const ctx = {
    console, JSON, Math, Promise, Set, Map, URLSearchParams, AbortController, TextDecoder,
    setTimeout: (fn, ms) => { timers.push({ id: ++tid, fn: fn, at: nowMs + (ms || 0) }); return tid; },
    clearTimeout: (id) => { timers = timers.filter((t) => t.id !== id); },
    setInterval: () => 0, clearInterval: () => {},
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { search: "" },
    navigator: { userAgent: "test" },
    addEventListener() {},
    document: {
      readyState: "loading", hidden: false, addEventListener() {},
      getElementById: (id) => els[id] || (els[id] = stubEl()),
      querySelector: () => stubEl(), querySelectorAll: () => [], createElement: () => stubEl(),
      documentElement: stubEl(), body: stubEl(),
    },
    fetchLog: [],
    fetchImpl: null,
  };
  ctx.window = ctx;
  ctx.fetch = (url, opts) => {
    ctx.fetchLog.push({ url: url, opts: opts || {} });
    return ctx.fetchImpl ? ctx.fetchImpl(url, opts || {}) : Promise.resolve(null).then(() => { throw new Error("offline"); });
  };
  vm.createContext(ctx);
  ctx.__clock = () => nowMs;
  vm.runInContext("Date.now = function () { return __clock(); };", ctx);
  for (const f of ["crewwatch.js", "collisionwatch.js", "briefguard.js", "plume.js", "shed.js", "reroute.js", "reclog.js"])
    vm.runInContext(fs.readFileSync(path.join(DIR, f), "utf8"), ctx, { filename: f });
  let D = null;
  ctx.__C4DashTest = (api) => { D = api; };
  vm.runInContext(fs.readFileSync(path.join(DIR, "dashboard.js"), "utf8"), ctx, { filename: "dashboard.js" });
  if (!D) throw new Error("dashboard.js did not hand over its test seam");
  const fire = () => { const due = timers.filter((t) => t.at <= nowMs); timers = timers.filter((t) => t.at > nowMs); due.forEach((t) => t.fn()); };
  return { D: D, ctx: ctx, now: () => nowMs, set: (t) => { nowMs = t; }, tick: (ms) => { nowMs += ms; }, fire: fire };
}

const json = (body) => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
const flush = () => new Promise((r) => setImmediate(r));

const cases = [];
const test = (name, fn) => cases.push([name, fn]);
function ok(cond, msg) { if (!cond) throw new Error(msg); }

/* ---------------- #45.5a ---------------- */
test("#45.5a observedAt(0): the newest sample 2 h ago is not 'now'", () => {
  const T = load();
  const s = T.now() / 1000;
  T.D.App.windHist = [{ t: s - 7300, tws: 10, twd: 200 }, { t: s - 7200, tws: 11, twd: 210 }];
  ok(T.D.observedAt(0) === null, "observedAt(0) returned a 2 h old sample as now");
  ok(T.D.observedAt(7200) && T.D.observedAt(7200).tws === 11, "the -120 min lookback still finds its sample");
  T.D.App.windHist.push({ t: s - 2, tws: 12, twd: 220 });
  ok(T.D.observedAt(0).tws === 12, "a sample 2 s old is now");
});
test("#45.5a a lookback point inside a gap is null, not the far side of the gap", () => {
  const T = load();
  const s = T.now() / 1000;
  T.D.App.windHist = [{ t: s - 7200, tws: 8, twd: 180 }, { t: s - 1, tws: 12, twd: 220 }];
  ok(T.D.observedAt(3600) === null, "-60 min fell in a 2 h gap but got a sample");
});

/* ---------------- #184-4 ---------------- */
const COND = { available: true, tws: 12, twd: 220 };
function pollAnswers(T, answers, freezeMs) {
  T.ctx.fetchImpl = (url) => {
    const k = Object.keys(answers).find((a) => url.indexOf("/api/" + a) === 0);
    const body = k ? answers[k] : null;
    return new Promise((res, rej) => {
      if (freezeMs) T.tick(freezeMs);     // the page froze between the ask and the answer
      freezeMs = 0;
      if (body == null) rej(new Error("down")); else res({ ok: true, status: 200, json: () => Promise.resolve(body) });
    });
  };
}
test("#184-4 an answer is stamped when it was asked for, not when the frozen page resumed", async () => {
  const T = load();
  const asked = T.now();
  pollAnswers(T, { conditions: COND }, 3600e3);
  await T.D.poll();
  ok(T.D.App.ages.conditions >= 3600e3, "age after a 1 h freeze = " + T.D.App.ages.conditions + " ms");
  const last = T.D.App.windHist[T.D.App.windHist.length - 1];
  ok(last && Math.abs(last.t - asked / 1000) < 1, "wind history logged the reading at wake time, not ask time");
});
test("#184-4 a slow answer from one endpoint doesn't age the others (no false stale)", async () => {
  const T = load();
  T.ctx.fetchImpl = (url) => {
    if (url === "/api/conditions") return json(COND);
    if (url.indexOf("/api/forecast") === 0)
      return new Promise((res) => setImmediate(() => { T.tick(8000); res({ ok: true, status: 200, json: () => Promise.resolve({ available: false }) }); }));
    return Promise.reject(new Error("down"));
  };
  await T.D.poll();
  ok(T.D.App.ages.conditions < 1000, "conditions aged " + T.D.App.ages.conditions + " ms by a slow forecast");
  ok(T.D.App.ages.forecast < 1000, "an 8 s answer inside its 9 s timeout is not old: " + T.D.App.ages.forecast);
});
test("#184-4 an answer that landed before a freeze keeps its landing time", async () => {
  const T = load();
  let releaseForecast;
  T.ctx.fetchImpl = (url) => {
    if (url === "/api/conditions") return json(COND);
    if (url.indexOf("/api/forecast") === 0) return new Promise((res) => { releaseForecast = () => res({ ok: true, status: 200, json: () => Promise.resolve({ available: false }) }); });
    return Promise.reject(new Error("down"));
  };
  const landed = T.now();
  const p = T.D.poll();
  await flush(); await flush();
  T.tick(3600e3);                        // the page froze with /forecast still pending
  releaseForecast();
  await p;
  ok(T.D.App.ages.conditions >= 3600e3 - 1000, "conditions landed before an hour's freeze, aged " + T.D.App.ages.conditions + " ms");
  const last = T.D.App.windHist[T.D.App.windHist.length - 1];
  ok(last && Math.abs(last.t - landed / 1000) < 1, "wind history logged the pre-freeze reading as now");
});
test("#184-4 a held wind reading is not logged as a new one", async () => {
  const T = load();
  pollAnswers(T, { conditions: COND });
  await T.D.poll();
  const n = T.D.App.windHist.length;
  T.tick(3000);
  pollAnswers(T, {});                    // the next poll fails: conditions are HELD
  await T.D.poll();
  ok(T.D.App.windHist.length === n, "held conditions were appended to the wind history again");
});
test("#184-4 a poll frozen mid-await does not swallow the wake poll", async () => {
  const T = load();
  T.ctx.fetchImpl = () => new Promise(() => {});   // pre-sleep poll never answers
  T.D.poll();
  const before = T.ctx.fetchLog.filter((f) => f.url === "/api/conditions").length;
  pollAnswers(T, { conditions: COND });
  T.D.onWake();
  await flush(); await flush();
  const after = T.ctx.fetchLog.filter((f) => f.url === "/api/conditions").length;
  ok(after === before + 1, "the wake did not start a poll (conditions asked " + (after - before) + " more times)");
});

/* ---------------- #45.5b, #45.5c, #45.7 ---------------- */
const SEL = { available: true, action: "hold", status: "ok", value: "Hold Plan A", why: "No lasting shift." };
test("#45.5b/c the PLAYBOOK tile ages with the selector, not /tactics", async () => {
  const T = load();
  T.ctx.fetchImpl = (url) => (url === "/api/selector" ? json(SEL) : Promise.reject(new Error("down")));
  await T.D.fetchSelector();
  ok(T.D.tileAge("playbook") === 0, "fresh selector already counted as old");
  T.tick(60e3);
  T.ctx.fetchImpl = () => Promise.reject(new Error("down"));
  await T.D.fetchSelector();
  ok(T.D.App.selector && T.D.App.selector.value === "Hold Plan A", "last good selector is still held");
  ok(T.D.tileAge("playbook") >= 60e3, "playbook tile age " + T.D.tileAge("playbook") + " ms after 60 s of failed polls");
  ok(/st-old/.test(T.D.selectorHtml()), "the selector banner doesn't say it's old");
});
test("#45.7 a held deviation read says how old it is", async () => {
  const T = load();
  T.ctx.fetchImpl = (url) => (url === "/api/deviation" ? json({ available: true, status: "ok", variant: "A", xte_nm: 0.2 }) : Promise.reject(new Error("down")));
  await T.D.fetchDeviation();
  ok(T.D.oldTag("deviation") === "", "fresh deviation tagged old");
  T.tick(5 * 60e3);
  T.ctx.fetchImpl = () => Promise.reject(new Error("down"));
  await T.D.fetchDeviation();
  ok(/5m old/.test(T.D.oldTag("deviation")), "no age on a 5 min old deviation: " + T.D.oldTag("deviation"));
  ok(/st-old/.test(T.D.deviationHtml()), "the TRACK block doesn't show its age");
});

/* ---------------- #46.1 ---------------- */
const LIVE = { conditions: { available: true }, sources: { count: 4, sources: [{ last_seen_s: 1 }] } };
function dataTile(health, p) {
  const T = load();
  T.D.App.health = health;
  return T.D.BUILD.data(Object.assign({}, LIVE, p || {}));
}
test("#46.1 health 'unknown' + fresh sources: grey, not OK, no 'All checks pass' / 'Safe to use'", () => {
  const t = dataTile({ available: true, status: "unknown", flags: [], notes: [], reason: "instruments agree" });
  ok(t.status === "na", "status " + t.status);
  ok(!/All sensor checks pass/.test(t.why), "why: " + t.why);
  ok(!/Safe to use/.test(t.consider), "consider: " + t.consider);
  ok(!t.based.some((b) => /instruments agree/.test(b)), "based: " + t.based.join(" | "));
  ok(/not checked/.test(t.sub), "sub: " + t.sub);
});
test("#46.1 health offline + fresh sources: grey, not OK", () => {
  const t = dataTile(null);
  ok(t.status === "na", "status " + t.status);
  ok(!/Safe to use/.test(t.consider), "consider: " + t.consider);
});
test("#46.1 unknown health never hides a known link warning", () => {
  const t = dataTile({ available: true, status: "unknown", flags: [], notes: [] },
    { sources: { count: 4, sources: [{ last_seen_s: 120 }] } });
  ok(t.status === "watch", "a stale source under unknown health showed " + t.status);
  const dead = dataTile({ available: false }, { conditions: { available: false } });
  ok(dead.status === "act", "no live data under offline health showed " + dead.status);
});
test("#46.1 checks that ran and pass still read OK / safe", () => {
  const t = dataTile({ available: true, status: "ok", flags: [], notes: [], reason: "instruments agree" });
  ok(t.status === "ok" && /All sensor checks pass/.test(t.why) && /Safe to use/.test(t.consider), JSON.stringify(t));
});

/* ---------------- #56 ---------------- */
test("#56 two quick sail taps keep both sails", async () => {
  const T = load();
  T.D.App.sailState = { flying: [], out_of_service: [], reef: "" };
  const posted = [];
  T.ctx.fetchImpl = (url, opts) => {
    if (url === "/api/sails/state" && opts.method === "POST") {
      const b = JSON.parse(opts.body);
      posted.push(b);
      const prev = T.D.App.sailState || {};
      return new Promise((res) => setImmediate(() => res({ ok: true, status: 200,
        json: () => Promise.resolve(Object.assign({}, prev, b)) })));
    }
    return Promise.reject(new Error("down"));
  };
  await Promise.all([T.ctx.toggleFlying("J1"), T.ctx.toggleFlying("A3")]);
  const last = posted[posted.length - 1];
  ok(last.flying.indexOf("J1") >= 0 && last.flying.indexOf("A3") >= 0,
    "second tap lost the first sail: " + JSON.stringify(posted));
  ok(T.D.App.sailState.flying.length === 2, "state after both taps: " + JSON.stringify(T.D.App.sailState));
});
test("#56 a stalled sail POST can't hold the queue; the next tap builds on the re-read state", async () => {
  const T = load();
  T.D.App.sailState = { flying: [], out_of_service: [], reef: "" };
  const posted = [];
  let first = true;
  T.ctx.fetchImpl = (url, opts) => {
    if (url === "/api/sails/state" && opts.method === "POST") {
      posted.push(JSON.parse(opts.body));
      if (first) {                       // the first POST stalls until its deadline aborts it
        first = false;
        return new Promise((res, rej) => opts.signal.addEventListener("abort", () => rej(new Error("aborted"))));
      }
      return json(Object.assign({}, T.D.App.sailState, JSON.parse(opts.body)));
    }
    if (url === "/api/sails/state") return json({ flying: ["J1"], out_of_service: [], reef: "" });   // it did land
    return Promise.reject(new Error("down"));
  };
  const a = T.ctx.toggleFlying("J1"), b = T.ctx.toggleFlying("A3");
  await flush(); await flush();
  ok(posted.length === 1, "second tap sent while the first was pending");
  T.tick(9000); T.fire();                // past the POST deadline
  await a; await b;
  ok(posted.length === 2, "the queue stayed blocked after a stalled POST: " + JSON.stringify(posted));
  const last = posted[1].flying || [];
  ok(last.indexOf("J1") >= 0 && last.indexOf("A3") >= 0, "second tap didn't build on the re-read state: " + JSON.stringify(posted[1]));
  ok(T.D.App.sailQ === null, "queue not released");
});
test("#56 a tap whose answer and re-read both fail drops the queued taps instead of guessing", async () => {
  const T = load();
  T.D.App.sailState = { flying: [], out_of_service: [], reef: "" };
  const posted = [];
  T.ctx.fetchImpl = (url, opts) => {
    if (url === "/api/sails/state" && opts.method === "POST") { posted.push(JSON.parse(opts.body)); return Promise.reject(new Error("lost")); }
    return Promise.reject(new Error("down"));
  };
  await Promise.all([T.ctx.toggleFlying("J1"), T.ctx.toggleFlying("A3")]);
  ok(posted.length === 1, "a queued tap was sent built on unknown state: " + JSON.stringify(posted));
  ok(T.D.App.sailQ === null, "queue not released");
  // a later tap, still with no way to read the state, is dropped too ...
  await T.ctx.toggleFlying("S1");
  ok(posted.length === 1, "a later tap was sent built on unknown state: " + JSON.stringify(posted));
  // ... and once a read works, it builds on what the engine really has
  T.ctx.fetchImpl = (url, opts) => {
    if (url === "/api/sails/state" && opts.method === "POST") { posted.push(JSON.parse(opts.body)); return json(JSON.parse(opts.body)); }
    if (url === "/api/sails/state") return json({ flying: ["J1"], out_of_service: [], reef: "" });
    return Promise.reject(new Error("down"));
  };
  await T.ctx.toggleFlying("A3");
  const last = posted[posted.length - 1].flying;
  ok(last.indexOf("J1") >= 0 && last.indexOf("A3") >= 0, "tap after recovery: " + JSON.stringify(last));
});
test("#56 a sail-state read started during a tap never overwrites it", async () => {
  const T = load();
  T.D.App.sailState = { flying: [], out_of_service: [], reef: "" };
  let releaseGet = null;
  T.ctx.fetchImpl = (url, opts) => {
    if (url === "/api/sails/state" && opts.method === "POST")
      return new Promise((res) => setImmediate(() => res({ ok: true, status: 200, json: () => Promise.resolve({ flying: ["J1"], out_of_service: [], reef: "" }) })));
    if (url === "/api/sails/state") return new Promise((res) => { releaseGet = () => res({ ok: true, status: 200, json: () => Promise.resolve({ flying: [], out_of_service: [], reef: "" }) }); });
    return Promise.reject(new Error("down"));
  };
  const tap = T.ctx.toggleFlying("J1");
  const get = T.D.fetchSailState();      // started while the POST is in flight
  await tap;
  if (releaseGet) releaseGet();
  await get;
  ok((T.D.App.sailState.flying || []).indexOf("J1") >= 0, "an old read overwrote the tap: " + JSON.stringify(T.D.App.sailState));
});
test("#56 a sail-state read in flight during a tap does not undo the tap", async () => {
  const T = load();
  T.D.App.sailState = { flying: [], out_of_service: [], reef: "" };
  let releaseGet;
  T.ctx.fetchImpl = (url, opts) => {
    if (url === "/api/sails/state" && opts.method === "POST")
      return json(Object.assign({}, T.D.App.sailState, JSON.parse(opts.body)));
    if (url === "/api/sails/state") return new Promise((res) => { releaseGet = () => res({ ok: true, status: 200, json: () => Promise.resolve({ flying: [], out_of_service: [], reef: "" }) }); });
    return Promise.reject(new Error("down"));
  };
  const get = T.D.fetchSailState();
  await T.ctx.toggleFlying("J1");
  releaseGet();
  await get;
  ok((T.D.App.sailState.flying || []).indexOf("J1") >= 0, "the old read overwrote the tap: " + JSON.stringify(T.D.App.sailState));
});

/* ---------------- #184-5 ---------------- */
const COURSE = (names) => ({ available: true, marks: names.map((n, i) => ({ name: n, lat: 45 + i * 0.1, lon: -82 + i * 0.1 })) });
test("#184-5 the course map follows a course pushed mid-day", async () => {
  const T = load();
  T.ctx.fetchImpl = () => json(COURSE(["Start", "A", "Finish"]));
  await T.D.fetchCourse();
  T.ctx.fetchImpl = () => json(COURSE(["Start", "B", "C", "Finish"]));
  await T.D.fetchCourse();
  ok(T.D.App.courseMap.marks[1].name === "B", "still the first course: " + T.D.App.courseMap.marks.map((m) => m.name));
  T.ctx.fetchImpl = () => Promise.reject(new Error("down"));
  await T.D.fetchCourse();
  ok(T.D.App.courseMap && T.D.App.courseMap.marks.length === 4, "a failed read dropped the map");
});
test("#184-5 a next-mark index past the course's marks doesn't break the map", () => {
  const T = load();
  T.D.App.courseMap = COURSE(["Start", "A", "Finish"]);
  T.D.App.nav = { position: { lat: 45.05, lon: -81.95 }, next_mark: { index: 5, name: "Mark 6", distance_nm: 3.2, bearing_deg: 90 } };
  let html;
  try { html = T.D.courseMapHtml(); } catch (e) { throw new Error("courseMapHtml threw: " + e.message); }
  ok(/svg/.test(html) && /Mark 6/.test(html), "map missing or foot lost the mark name");
});

/* ---------------- #246 navigator Start / Undo / pick ---------------- */
const WL = Object.assign(COURSE(["Start", "Windward", "Leeward", "Windward", "Finish"]), { route: "wl" });
const NAVR = (progress, index) => ({ available: true, route: "wl", progress: progress,
  next_mark: { index: index, name: "Windward", distance_nm: 0.4, bearing_deg: 10 } });
test("#246 no controls where the engine can't change marks (cloud: progress null)", () => {
  const T = load();
  T.D.App.courseMap = WL;
  T.D.App.nav = NAVR(null, 1);
  ok(T.D.navControlsHtml() === "", "controls shown without progress");
});
test("#246 Start, Undo and the mark picker on the navigator card; lap 2 is its own mark", () => {
  const T = load();
  T.D.App.courseMap = WL;
  T.D.App.nav = NAVR({ can_undo: false }, 3);
  const h = T.D.navControlsHtml();
  ok(/>Start</.test(h), "no Start: " + h);
  ok(/data-nav="undo" disabled>Undo last mark</.test(h), "Undo not disabled with nothing to undo: " + h);
  ok(/<option value="3" selected>4\. Windward<\/option>/.test(h), "lap 2's Windward not picked: " + h);
  T.D.App.nav = NAVR({ can_undo: true }, 1);
  ok(/data-nav="undo">Undo last mark</.test(T.D.navControlsHtml()), "Undo disabled with something to undo");
});
test("#246 Start takes two taps; Undo and a pick post to the engine", async () => {
  const T = load();
  T.D.App.courseMap = WL;
  T.D.App.nav = NAVR({ can_undo: true }, 1);
  T.ctx.fetchImpl = (url) => json(url.indexOf("/navigator/") >= 0 ? { ok: true, index: 0, next_mark: "Start" } : NAVR({ can_undo: true }, 0));
  await T.D.navAction("start");
  ok(!T.ctx.fetchLog.some((f) => /navigator\/start/.test(f.url)), "one tap started a new run");
  ok(/Tap again to start/.test(T.D.navControlsHtml()), "first tap doesn't ask for the second");
  await T.D.navAction("start");
  ok(T.ctx.fetchLog.some((f) => /\/navigator\/start$/.test(f.url) && f.opts.method === "POST"), "second tap didn't post Start");
  ok(/Next mark: Start/.test(T.D.navControlsHtml()), "no confirmation line");
  await T.D.navAction("undo");
  ok(T.ctx.fetchLog.some((f) => /\/navigator\/undo$/.test(f.url) && f.opts.method === "POST"), "Undo didn't post");
  await T.D.navAction("mark", 3);
  const m = T.ctx.fetchLog.find((f) => /\/navigator\/mark$/.test(f.url));
  ok(m && JSON.parse(m.opts.body).index === 3, "pick didn't post index 3");
});

/* ---------------- #40 settling ---------------- */
const SETTLING = Object.assign({}, SEL, { settling: { to_value: "Switch to Plan B", to_status: "act",
  to_action: "switch", why: "Lasting right shift.", held_s: 45, need_s: 120 } });
test("#40 the selector banner and PLAYBOOK tile show a settling change", () => {
  const T = load();
  T.D.App.selector = SETTLING;
  const html = T.D.selectorHtml();
  ok(/May switch\. Confirming: 0:45 of 2:00\./.test(html), "banner: " + html);
  ok(/Switch to Plan B/.test(html), "banner doesn't say what it moves to");
  ok(/HOLD/.test(html), "the call on screen must stay the held one");
  const tile = T.D.BUILD.playbook({});
  ok(/may switch/.test(tile.sub), "tile sub: " + tile.sub);
  ok(tile.status === "ok", "tile status must stay the held call's: " + tile.status);
  T.D.App.selector = SEL;
  ok(!/sr-settle/.test(T.D.selectorHtml()) && !/may /.test(T.D.BUILD.playbook({}).sub), "settling shown with no settle");
});

/* ---------------- #95 ---------------- */
test("#95 G14/G52 provenance table: lat/lon labels, rounded per unit, kts", () => {
  const T = load();
  T.D.App.health = { available: true, provenance: { channels: {
    lat: { value: 45.123456, unit: "°", device: "GPS24" }, lon: { value: -82.654321, unit: "°", device: "GPS24" },
    tws: { value: 12.123, unit: "kn", device: "Orca" }, bank_voltage: { value: 12.6789, unit: "V", device: "BMV" },
    twd: { value: 213.456, unit: "°", device: "Orca" } } } };
  const h = T.D.provenanceTableHtml();
  ok(/>lat</.test(h) && />lon</.test(h), "lat/lon both labelled position");
  ok(/45\.1235°/.test(h) && /-82\.6543°/.test(h), "position not rounded: " + h);
  ok(/12\.1 kts/.test(h) && !/ kn</.test(h), "speed not in kts: " + h);
  ok(/12\.68 V/.test(h) && /213°/.test(h), "volts/degrees not rounded: " + h);
  ok(/prov-tab/.test(h), "no fixed-column class");
});
test("#95 G53 the trend chart's 'now' label is anchored at its end", () => {
  const T = load();
  const s = T.now() / 1000;
  const hist = []; for (let i = 0; i <= 210; i += 5) hist.push({ t: s - i * 60, tws: 10 + (i % 3), twd: 200 });
  hist.reverse();
  const html = T.D.twsTrendTile(hist);
  ok(/text-anchor="end">now</.test(html), "now label: " + (html.match(/<text[^>]*>now</) || ["missing"])[0]);
});
test("#95 G55 a hidden coach line is hidden", () => {
  const css = fs.readFileSync(path.join(DIR, "dashboard.css"), "utf8");
  ok(/\.coach-line\[hidden\][^{}]*\{\s*display:\s*none/.test(css),
    "no .coach-line[hidden] { display:none } rule");
});
test("#95 G56 crew energy reads as a percent", () => {
  const T = load();
  const t = T.D.BUILD.charge({ fatigue: { available: true, index: 23, level: "fresh" }, watch: null });
  ok(/^77%/.test(t.value), "value: " + t.value);
});

/* ---------------- #185-4 N2K address drift in the DATA tile ---------------- */
test("#185-4 a sensor missing from the bus (its address re-claimed) shows on the DATA tile", () => {
  const reason = "GPS24xd-NMEA2000 not found, n2k-socketcan.3 is now GST10";
  const t = dataTile({ available: true, status: "warn", flags: [reason], notes: [], reason: reason,
    provenance: { checks: { n2k_addresses: { status: "warn", reason: reason } }, channels: {} } });
  ok(t.status === "watch", "status " + t.status);
  ok(/sensor missing/.test(t.sub), "sub: " + t.sub);
  ok(t.why.indexOf(reason) >= 0, "why: " + t.why);
});
test("#185-4 a sensor that moved and was followed is a note, not a warning", () => {
  const note = "GPS24xd-NMEA2000 moved to n2k-socketcan.20, still used";
  const t = dataTile({ available: true, status: "ok", flags: [], notes: [note], reason: "instruments agree",
    provenance: { checks: { n2k_addresses: { status: "ok", note: note } }, channels: {} } });
  ok(t.status === "ok" && !/sensor missing/.test(t.sub), "status " + t.status + " sub " + t.sub);
  ok(t.why.indexOf(note) >= 0, "why: " + t.why);
});

/* ---------------- FLEET: no lead claimed without a ranking ---------------- */
function fleetTile(fl) { return load().D.BUILD.ais({ fleet: Object.assign({ available: true }, fl) }); }
test("FLEET: us unranked (no own rating, #271) shows the gap note, never 'Leading'", () => {
  const t = fleetTile({ gaps: ["own rating missing, no corrected gaps"],
    own_rank: { overall: null, of: 0, division: null, division_of: 1, unranked: 2 },
    standings: [{ boat: "Rival", dtf_nm: 4 }, { boat: "US", source: "own", dtf_nm: 5 }] });
  ok(!/Leading/.test(t.consider), "consider: " + t.consider);
  ok(/own rating missing/.test(t.consider) && /own rating missing/.test(t.sub), "consider: " + t.consider + " / sub: " + t.sub);
  ok(t.value === "—", "value: " + t.value);
});
test("FLEET: only us ranked (no SOG, no rival gaps) is not a lead and not '1st of 1'", () => {
  const t = fleetTile({ own_rank: { overall: 1, of: 1, division: 1, division_of: 1, unranked: 3 },
    standings: [{ boat: "US", source: "own", rank: 1, corrected_delta_s: 0 }, { boat: "Rival", dtf_nm: 4 }] });
  ok(!/Leading/.test(t.consider), "consider: " + t.consider);
  ok(t.value === "—", "value: " + t.value);
});
test("FLEET: a real lead still says so", () => {
  const t = fleetTile({ own_rank: { overall: 1, of: 2, division: 1, division_of: 2, unranked: 0 },
    standings: [{ boat: "US", source: "own", rank: 1, corrected_delta_s: 0 },
                { boat: "Rival", rank: 2, corrected_delta_s: 600 }] });
  ok(/Leading on corrected/.test(t.consider), "consider: " + t.consider);
});

/* ---------------- #247 per-channel stale limits ---------------- */
test("#247 a dead depth with every other sensor fresh turns the DATA tile, with a chip", () => {
  const g = [{ group: "depth", label: "Depth", channels: ["depth"], age_s: 20, severity: "warn" }];
  const t = dataTile({ available: true, status: "ok", flags: [], notes: [], reason: "ok" },
    { conditions: { available: true, dead_groups: g } });
  ok(t.status === "watch", "status " + t.status);
  ok(/depth 20s old/.test(t.sub), "sub: " + t.sub);
  ok(/Depth: no reading for 20s/.test(t.why) && /Don't trust depth/.test(t.consider), t.why + " | " + t.consider);
  const d = dataTile({ available: true, status: "ok", flags: [], notes: [], reason: "ok" },
    { conditions: { available: true, dead_groups: [Object.assign({}, g[0], { age_s: 90, severity: "danger" })] } });
  ok(d.status === "act", "past 60 s: " + d.status);
});
test("#247 dead wind: the last number stays with a red age, no trend call", () => {
  const T = load();
  const t = T.D.BUILD.wind({ conditions: { available: true, tws: null, twd: null,
    raw: { tws: { value: 12, age_s: 20, state: "dead" }, twd: { value: 220, age_s: 20, state: "dead" } } } });
  ok(t.status === "watch" && /age-dead/.test(t.value) && /20s old/.test(t.value), JSON.stringify(t));
  ok(/12 kts/.test(t.value), "the last number is gone: " + t.value);
  ok(/no fresh wind/.test(t.sub), "sub: " + t.sub);
});
test("#247 wind silent over 30 min (gone from raw): no number, no 'Steady' advice", () => {
  const T = load();
  const t = T.D.BUILD.wind({ conditions: { available: true, tws: null, twd: null, raw: { depth: { value: 9, age_s: 1, state: "ok" } } } });
  ok(t.status === "na" && !/Steady/.test(t.consider || "") && !/kts/.test(t.value || ""), JSON.stringify(t));
});
test("#247 the provenance table keeps a reading the 5-min health read dropped, with its red age", () => {
  const T = load();
  T.D.App.health = { available: true, provenance: { channels: {
    sog: { value: 6.1, unit: "kn", age_s: 1, device: "GPS24xd" } } } };
  T.D.App.cond = { available: true, raw: { sog: { value: 6.1, age_s: 1, state: "ok" },
    depth: { value: 12.4, age_s: 400, state: "dead" } } };
  const h = T.D.provenanceTableHtml();
  ok(/12.4 m/.test(h) && /age-dead">400s/.test(h), h);
});
test("#247 a stale bank: grey, last volts with a red age, no verdict", () => {
  const T = load();
  T.D.App.power = { available: true, status: "unknown", stale: true, volts_last: 12.31, age_s: 90,
    volts: 12.3, trend_v_per_h: -0.1, thresholds: { floor_v: 11, danger_v: 11.3 } };
  const t = T.D.BUILD.bank({});
  ok(t.status === "na" && /12.31 V/.test(t.value) && /age-dead/.test(t.value), JSON.stringify(t));
  ok(!/healthy/i.test(t.consider), "consider: " + t.consider);
});

/* ---------------- #291 consumers on the per-channel table ---------------- */
test("#291 DATA tile: a source unseen past its own limit (5 s GPS aboard) is 'one stale'", () => {
  const h = { available: true, status: "ok", flags: [], notes: [], reason: "ok" };
  const t = dataTile(h, { sources: { count: 4, sources: [{ last_seen_s: 8, stale_after_s: 5 }] } });
  ok(t.status === "watch" && /one stale/.test(t.why), t.status + " | " + t.why);
  const f = dataTile(h, { sources: { count: 4, sources: [{ last_seen_s: 3, stale_after_s: 5 }] } });
  ok(f.status === "ok" && !/one stale/.test(f.why), f.status + " | " + f.why);
  const old = dataTile(h, { sources: { count: 4, sources: [{ last_seen_s: 30 }] } });
  ok(old.status === "ok", "an engine without stale_after_s keeps 45 s: " + old.status);
});
test("#291 FORECAST 'Now': no cached wind once /conditions wind is gone", () => {
  const T = load();
  const now = T.now() / 1000;
  T.D.App.windHist = [{ t: now - 25, tws: 11, twd: 230 }];
  T.D.App.seriesHist = [{ t: now - 25, tws: 11, twd: 230 }];
  const fc = { available: true, hours: [{ in_h: 1, tws: 12, twd: 235 }, { in_h: 2, tws: 13, twd: 240 }] };
  const t = T.D.BUILD.forecast({ forecast: fc, conditions: { available: true, tws: null, twd: null } });
  ok(/no wind reading/.test(t.rows[0].cols[0]), "Now row: " + JSON.stringify(t.rows[0]));
  const l = T.D.BUILD.forecast({ forecast: fc, conditions: { available: true, tws: 9, twd: 200 } });
  ok(/9/.test(l.rows[0].cols[0]), "live Now row: " + JSON.stringify(l.rows[0]));
});

/* ---------------- #295 the Orin's load shed ---------------- */
test("#295 shedding: one plain line under the top bar; the Re-route offer sees the level", async () => {
  const T = load();
  T.ctx.fetchImpl = (url) => (url === "/copilot/health"
    ? json({ status: "ok", shed: { level: 3, guard: "on", cause: "memory", paused: ["narration", "plume", "reroute"] } })
    : Promise.reject(new Error("down")));
  await T.D.fetchCopilotHealth();
  const el = T.ctx.document.getElementById("shedWarn");
  ok(el.hidden === false && el.textContent === "Orin low on memory. AI, plume and re-route paused.",
    "line: " + el.hidden + " " + el.textContent);
  T.D.App.rtPress = { phase: "paused", engine: "orin", at: T.now() - 1000, armAt: 0, until: T.now() + 60000,
    detail: "Re-route paused: Orin low on memory." };
  T.D.App.cpHealthAt = T.now();
  ok(T.D.rerouteView().button.engine === "pi", "the Pi offer stays while level 3");
  T.ctx.fetchImpl = (url) => (url === "/copilot/health"
    ? json({ status: "ok", shed: { level: 0, guard: "on", cause: null, paused: [] } })
    : Promise.reject(new Error("down")));
  T.tick(1000);
  await T.D.fetchCopilotHealth();
  ok(el.hidden === true && el.textContent === "", "level 0 hides it");
  ok(T.D.rerouteView().button.engine === "orin", "back to the Orin once the shed clears");
});
test("#295 a health read asked before the press doesn't end the Pi offer", async () => {
  const T = load();
  let answer;
  T.ctx.fetchImpl = (url) => (url === "/copilot/health"
    ? new Promise((res) => { answer = () => res({ ok: true, status: 200, json: () => Promise.resolve(
      { shed: { level: 0, guard: "on" } }) }); })
    : Promise.reject(new Error("down")));
  const p = T.D.fetchCopilotHealth();
  T.tick(500);
  T.D.App.rtPress = { phase: "paused", engine: "orin", at: T.now(), armAt: 0, until: T.now() + 60000,
    detail: "Re-route paused: Orin low on memory." };
  T.tick(500);
  answer(); await p;
  ok(T.D.rerouteView().button.engine === "pi", "an older read ended the offer");
});
test("#295 overlapping health polls: an older one finishing late doesn't hide the line", async () => {
  const T = load();
  let failOld;
  T.ctx.fetchImpl = () => new Promise((res, rej) => { failOld = () => rej(new Error("timeout")); });
  const a = T.D.fetchCopilotHealth();
  T.tick(15000);
  T.ctx.fetchImpl = () => json({ shed: { level: 3, guard: "on", cause: "heat" } });
  await T.D.fetchCopilotHealth();
  failOld(); await a;
  const el = T.ctx.document.getElementById("shedWarn");
  ok(el.hidden === false && el.textContent === "Orin hot. AI, plume and re-route paused.", "line: " + el.textContent);
});
test("#295 no Orin / no copilot (any boat): no line, no error", async () => {
  const T = load();
  for (const impl of [() => Promise.reject(new Error("no route to host")),
    () => Promise.resolve({ ok: false, status: 502, json: () => Promise.resolve({}) })]) {
    T.ctx.fetchImpl = impl;
    await T.D.fetchCopilotHealth();
    const el = T.ctx.document.getElementById("shedWarn");
    ok(el.hidden === true && el.textContent === "", "a line with no copilot: " + el.textContent);
    ok(T.D.App.cpHealth === null, "held a health read with no copilot");
    ok(T.D.rerouteView().button.label === "Re-route now", "button changed with no copilot");
  }
});

/* ---------------- #312 ---------------- */
test("#312 auto day/night follows the live fix, else the course's first mark — no fixed venue", () => {
  const T = load();
  T.set(Date.UTC(2027, 5, 21, 12, 0));              // 12:00 UTC: London noon, Sydney 22:00
  const A = T.D.App;
  A.theme = "auto";
  ok(T.D.sunElevation(51.5, -0.1, new Date(T.now())) > 55, "London noon sun too low");
  A.nav = { position: { lat: -33.86, lon: 151.2 } };
  ok(T.D.resolveTheme() === "night", "Sydney at 22:00 was day");
  A.nav = { position: { lat: 51.5, lon: -0.1 } };
  ok(T.D.resolveTheme() === "day", "London at noon was night");
  A.nav = { position: { lat: null, lon: null } };    // no fix
  A.courseFirst = { lat: -33.86, lon: 151.2 };
  ok(T.D.resolveTheme() === "night", "fallback to the first mark not used");
  A.courseFirst = null;
  const r = T.D.resolveTheme();
  ok(r === "day" || r === "night", "no position at all: " + r);
});

/* ---------------- #305: the sails bar is this boat's ---------------- */
test("#305 the sails bar lists the boat's sails; an explicit [] clears another boat's", async () => {
  const T = load();
  pollAnswers(T, { conditions: COND, sail: { available: false, sails: [{ name: "G1" }, { name: "A2" }] } });
  await T.D.poll();
  ok(T.D.sailsInv().join() === "G1,A2", "first boat's sails: " + T.D.sailsInv().join());
  pollAnswers(T, { conditions: COND, sail: { available: false, sails: [] } });
  await T.D.poll();
  ok(T.D.sailsInv().length === 0, "a boat with no sails kept the last boat's: " + T.D.sailsInv().join());
  pollAnswers(T, { conditions: COND, sail: { available: false, sails: [{ name: "J1" }] } });
  await T.D.poll();
  pollAnswers(T, { conditions: COND, sail: { available: false, note: "Sail advice withheld" } });
  await T.D.poll();
  ok(T.D.sailsInv().join() === "J1", "an answer with no sails field must keep the list: " + T.D.sailsInv().join());
});
test("#305 any sail name the boat uses gets a button (spaces, long names), built as DOM", async () => {
  const T = load();
  const long = "HEAVY WEATHER JIB NUMBER FOUR";
  pollAnswers(T, { conditions: COND, sail: { available: false, sails: [{ name: "BIG RED" }, { name: long }, { name: "<b>X'" }] } });
  await T.D.poll();
  ok(T.D.sailsInv().join("|") === "BIG RED|" + long + "|<b>X'", "names dropped: " + T.D.sailsInv().join("|"));
  let tapped = null;
  const b = T.D.sailButton("sb-chip", "BIG RED", "Tap = hoist the BIG RED", () => { tapped = "BIG RED"; });
  ok(b.textContent === "BIG RED" && b.className === "sb-chip", "the button carries the name as text");
  const src = require("node:fs").readFileSync(require("node:path").join(DIR, "dashboard.js"), "utf8");
  ok(!/onclick="toggle(Flying|Gear)\(/.test(src), "sail buttons must not be inline-HTML onclick strings");
  ok(tapped === null, "no tap yet");
});

(async () => {
  let fails = 0;
  for (const [name, fn] of cases) {
    try { await fn(); console.log("[OK ] " + name); }
    catch (e) { fails++; console.log("[FAIL] " + name + ": " + e.message); }
  }
  console.log(cases.length - fails + "/" + cases.length + " passed");
  process.exit(fails ? 1 : 0);
})();
