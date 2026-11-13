// #314 transition: a browser can pair cached older scripts with new ones. Check both pairings
// still talk: new app.js -> an old listener on "sr33:conditions"; old app.js (no C4Store, old
// events, old keys) -> new sail.js / plot.js follow it.
// Run: node vps/web/test_mixed_cache.js (no deps).
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");
const PUB = path.join(__dirname, "public");
const src = (f) => fs.readFileSync(path.join(PUB, f), "utf8");

function mem(init) {
  const m = Object.assign({}, init || {});
  return { m, getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); },
           removeItem: (k) => { delete m[k]; } };
}

// 1) new app.js emits each event under both names
{
  const got = [];
  const ctx = { console, CustomEvent: function (t, o) { this.type = t; this.detail = o.detail; } };
  ctx.window = { dispatchEvent: (e) => got.push(e.type) };
  vm.createContext(ctx);
  const app = src("app.js");
  const i = app.indexOf("/* Page events under");
  vm.runInContext(app.slice(i, app.indexOf("\n}\n", i) + 3), ctx);
  vm.runInContext("emit('conditions', {tws: 10})", ctx);
  assert.deepStrictEqual(got, ["c4:conditions", "sr33:conditions"]);
  console.log("  ok  new app.js: an old cached listener still hears the event");
}

// 2) old app.js (no C4Store): new sail.js / plot.js use the old keys and event names
for (const f of ["sail.js", "plot.js"]) {
  const local = mem({ "sr33.hoisted": "J1", "sr33.route": "alt", "sr33.orient": "course" });
  const listeners = [];
  const node = () => ({ style: {}, dataset: {}, classList: { toggle() {}, add() {}, remove() {} },
    appendChild() {}, addEventListener() {}, querySelectorAll: () => [], getContext: () => null,
    setAttribute() {}, children: [] });
  const ctx = { console, localStorage: local, setInterval: () => 0, setTimeout: () => 0,
    document: { getElementById: node, createElement: node, createElementNS: node,
                querySelectorAll: () => [], body: { dataset: {} } },
    getComputedStyle: () => ({ getPropertyValue: () => "" }),
    MutationObserver: function () { this.observe = () => {}; },
    App: {}, apiFetch: () => new Promise(() => {}), requestAnimationFrame: () => 0 };
  ctx.window = { addEventListener: (k) => listeners.push(k), devicePixelRatio: 1 };
  vm.createContext(ctx);
  vm.runInContext(src(f), ctx, { filename: f });
  assert.ok(listeners.includes("sr33:conditions"), f + " listens on the old name: " + listeners);
  assert.ok(!listeners.some((k) => k.startsWith("c4:")), f + ": " + listeners);
  console.log("  ok  old app.js + new " + f + ": old event names");
}
// 3) the onboard flag: the Pi's /config.js sets both names; the new app accepts either
{
  const conf = fs.readFileSync(path.join(__dirname, "..", "..", "pi", "console", "default.conf.template"), "utf8");
  const body = conf.match(/return 200 "([^"]*ONBOARD[^"]*)"/)[1];
  for (const name of ["C4_ONBOARD", "SR33_ONBOARD"]) {     // what a new / an old app.js reads
    const w = {};
    vm.runInNewContext(body, { window: w });
    assert.strictEqual(w[name], true, name);
  }
  const app = src("app.js");
  const line = app.split("\n").find((l) => /onboard: !!/.test(l));
  for (const w of [{ C4_ONBOARD: true }, { SR33_ONBOARD: true }])
    assert.strictEqual(vm.runInNewContext("(" + line.trim().replace(/^onboard: /, "").replace(/,\s*\/\/.*$/, "") + ")", { window: w }), true);
  console.log("  ok  onboard flag works for old and new app.js, from old and new config.js");
}
console.log("PASS");
