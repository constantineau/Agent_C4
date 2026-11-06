// iPad console (pi/console/dashboard/dashboard.js): the display fixes of 2026-11-06.
// Pins: #45.5a "Now" has a distance bound; #45.5b/c the PLAYBOOK tile ages with the selector,
// synthesis and plays; #45.7 held slow reads say how old they are; #46.1 "unknown" health is grey
// "not checked", never "All checks pass" / "Safe to use", and never hides a known warning; #56 two
// quick sail taps keep both sails; #184-4 a reading is stamped when asked for, a wake poll is never
// swallowed, a held wind is not logged again; #184-5 the course map follows a new course and
// survives a stale mark index; #40 the selector's `settling` is on screen; #95 G14/G52/G53/G55/G56.
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
  for (const f of ["crewwatch.js", "collisionwatch.js", "briefguard.js", "plume.js", "reroute.js", "reclog.js"])
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

(async () => {
  let fails = 0;
  for (const [name, fn] of cases) {
    try { await fn(); console.log("[OK ] " + name); }
    catch (e) { fails++; console.log("[FAIL] " + name + ": " + e.message); }
  }
  console.log(cases.length - fails + "/" + cases.length + " passed");
  process.exit(fails ? 1 : 0);
})();
