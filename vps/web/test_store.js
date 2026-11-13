// The iPad web app's browser storage moved from the old "sr33.*" keys to "c4.*" (#314).
// Pins: a value under the old key is carried over (the iPad keeps its login across the deploy);
// the new key wins when both exist; writes go to the new key only and leave the old one for a
// rollback; a sign-out clears BOTH keys, so a reload stays signed out instead of the old key
// bringing the token back. store.js and app.js are browser scripts: they run here in a vm with
// Map-backed storage, and a "reload" is a fresh vm over the same storage.
// Run: node vps/web/test_store.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PUB = path.join(__dirname, "public");

function storage(init) {
  const m = new Map(Object.entries(init || {}));
  return {
    m: m,
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)); },
    removeItem: (k) => { m.delete(k); },
  };
}

function el() { return { style: {}, textContent: "", value: "", dataset: {} }; }

/* One page load: store.js then app.js, as index.html orders them. */
function page(local, session, extra) {
  const els = {};
  const ctx = Object.assign({
    console, JSON, Promise, Set, Map,
    localStorage: local, sessionStorage: session,
    setInterval: () => 0, clearInterval: () => {},
    document: { getElementById: (id) => els[id] || (els[id] = el()), body: { dataset: {} } },
  }, extra || {});
  ctx.window = { addEventListener() {}, C4_ONBOARD: false };
  vm.createContext(ctx);
  for (const f of ["store.js", "app.js"])
    vm.runInContext(fs.readFileSync(path.join(PUB, f), "utf8"), ctx, { filename: f });
  vm.runInContext("start = function () {};", ctx);   // the live app is not under test here
  return { ctx: ctx, els: els, App: vm.runInContext("App", ctx) };
}

let n = 0;
function test(name, fn) {
  return Promise.resolve().then(fn).then(
    () => { n++; console.log("  ok  " + name); },
    (e) => { console.error("  FAIL " + name); throw e; });
}

(async () => {
  await test("an old key is carried over to the new one, and the old one stays", () => {
    const local = storage({ "sr33.theme": "night", "sr33.mode": "race" });
    const session = storage({ "sr33.token": "tok-old" });
    const { App } = page(local, session);
    assert.equal(App.token, "tok-old", "the iPad stays signed in across the deploy");
    assert.equal(App.theme, "night");
    assert.equal(App.mode, "race");
    assert.equal(session.m.get("c4.token"), "tok-old");
    assert.equal(local.m.get("c4.theme"), "night");
    assert.equal(session.m.get("sr33.token"), "tok-old", "old key left for a rollback");
  });

  await test("the new key wins when both exist", () => {
    const { App } = page(storage({ "sr33.theme": "night", "c4.theme": "day" }),
      storage({ "sr33.token": "old", "c4.token": "new" }));
    assert.equal(App.token, "new");
    assert.equal(App.theme, "day");
  });

  await test("nothing stored reads as the defaults", () => {
    const { App } = page(storage(), storage());
    assert.equal(App.token, null);
    assert.equal(App.theme, "auto");
    assert.equal(App.mode, "practice");
  });

  await test("writes go to the new key only", async () => {
    const local = storage({ "sr33.theme": "night" });
    const session = storage();
    const p = page(local, session, {
      fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ token: "tok-new" }) }),
    });
    p.els.pw = Object.assign(el(), { value: "pw" });
    await vm.runInContext("unlock()", p.ctx);
    assert.equal(session.m.get("c4.token"), "tok-new");
    assert.ok(!session.m.has("sr33.token"));
    vm.runInContext("applyTheme = function () {}; cycleTheme();", p.ctx);
    assert.equal(local.m.get("c4.theme"), "auto", "night → auto, on the new key");
    assert.equal(local.m.get("sr33.theme"), "night", "the old key is never written");
  });

  await test("a sign-out clears both keys and stays signed out on reload", () => {
    const local = storage();
    const session = storage({ "sr33.token": "tok-old" });
    const p = page(local, session);
    assert.equal(p.App.token, "tok-old");
    vm.runInContext("relock('Session expired')", p.ctx);
    assert.equal(p.App.token, null);
    assert.ok(!session.m.has("c4.token") && !session.m.has("sr33.token"), "both keys gone");
    assert.equal(page(local, session).App.token, null, "the reload does not bring the token back");
  });

  await test("storage that throws (private mode) reads as nothing and drops writes", () => {
    const bad = { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); },
      removeItem() { throw new Error("denied"); } };
    const p = page(bad, bad);
    assert.equal(p.App.token, null);
    assert.equal(p.App.theme, "auto");
    vm.runInContext("C4Store.local.set('x', '1'); C4Store.session.remove('token');", p.ctx);
  });

  await test("only store.js names the old prefix; index.html loads it before the app", () => {
    for (const f of ["app.js", "sail.js", "plot.js"])
      assert.ok(!/sr33/i.test(fs.readFileSync(path.join(PUB, f), "utf8")), f);
    const html = fs.readFileSync(path.join(PUB, "index.html"), "utf8");
    const i = html.indexOf('src="/store.js"');
    assert.ok(i > 0 && i < html.indexOf('src="/app.js"') && i < html.indexOf('src="/sail.js"')
      && i < html.indexOf('src="/plot.js"'));
  });

  console.log(n + " passed");
})().catch((e) => { console.error(e); process.exit(1); });
