// The iPad sail dial's "Up:" buttons (vps/web/public/sail.js) are the boat's own sails (#305).
// An explicit `sails: []` (a boat that lists none) clears another boat's buttons; an answer with
// no `sails` field (race-gated, older server) keeps them. sail.js is a browser IIFE: it runs here
// in a vm with a small fake DOM and a scripted /api/sail.
// Run: node vps/web/test_sail_dial.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function el(tag) {
  return { tag: tag, dataset: {}, textContent: "", className: "", children: [], parent: null,
    classList: { toggle() {} }, addEventListener() {},
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); },
    querySelectorAll(sel) { return sel === "button" ? this.children.filter((c) => c.tag === "button") : []; } };
}

const sel = el("div");
const none = el("button"); none.dataset.sail = ""; sel.appendChild(none);   // the page's "—"
const els = { sailSel: sel, sailRec: el("div") };
const handlers = {};
let answer = null;
const asked = [];
const ctx = {
  console, JSON, Promise,
  localStorage: { getItem: () => "A2", setItem() {} },
  document: { documentElement: { getAttribute: () => "day" }, body: {},
    getElementById: (id) => els[id] || null, createElement: (t) => el(t),
    querySelectorAll: (q) => (q === "#sailSel button" ? sel.querySelectorAll("button") : []) },
  getComputedStyle: () => ({ getPropertyValue: () => "" }),
  MutationObserver: function () { this.observe = () => {}; },
  App: {},
  apiFetch: (url) => { asked.push(url); return Promise.resolve({ json: () => Promise.resolve(answer) }); },
};
ctx.window = { addEventListener: (k, fn) => { handlers[k] = fn; } };
vm.createContext(ctx);
// the storage helper rides at the top of app.js, which index.html loads before sail.js
const appSrc = fs.readFileSync(path.join(__dirname, "public", "app.js"), "utf8");
vm.runInContext(appSrc.slice(appSrc.indexOf("/* c4-store:begin */"), appSrc.indexOf("/* c4-store:end */")), ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "public", "sail.js"), "utf8"), ctx);

const names = () => sel.children.filter((c) => c.tag === "button").map((b) => b.dataset.sail).join(",");
const flush = () => new Promise((r) => setImmediate(r));
let failed = 0;
const check = (name, cond) => { if (!cond) failed++; console.log(`  [${cond ? "OK " : "FAIL"}] ${name}`); };

(async () => {
  const cond = { tws: 10, twa: 90 };
  // a fresh page, no wind yet: the dial still asks (no wind params) and lists the boat's sails
  answer = { available: false, note: "Need live wind speed and angle.",
             sails: [{ name: "G1", family: "genoa" }, { name: "BIG RED", family: "asym" }] };
  handlers.DOMContentLoaded(); await flush(); await flush();
  check("asked with no wind params", asked.length === 1 && !/tws=|twa=/.test(asked[0]));
  check("the boat's sails before any wind, a spaced name too", names() === "G1,BIG RED,");
  check("says no wind yet", els.sailRec.textContent === "No true wind yet.");
  answer = { available: false, note: "x", sails: [{ name: "G1", family: "genoa" }, { name: "A2", family: "asym" }] };
  handlers["c4:conditions"]({ detail: cond }); await flush(); await flush();
  check("the boat's sails become the buttons (+ none)", names() === "G1,A2,");
  answer = { available: false, note: "Sail advice withheld" };
  handlers["c4:conditions"]({ detail: cond }); await flush(); await flush();
  check("an answer with no sails field keeps them", names() === "G1,A2,");
  answer = { available: false, note: "No polar for this boat.", sails: [] };
  handlers["c4:conditions"]({ detail: cond }); await flush(); await flush();
  check("an explicit [] clears the other boat's buttons", names() === "");
  console.log(failed ? "FAIL" : "PASS");
  process.exit(failed ? 1 : 0);
})();
