// One race for every Lab tab, kept across a reload. Run: node --test tools/lab-web/race_pick.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "app.js"), "utf8");

function grab(name) {
  const m = new RegExp(`\\n((?:async )?function ${name}\\(|const ${name} )`).exec(src);
  assert.ok(m, `${name} found in app.js`);
  const rest = src.slice(m.index + 1);
  const end = /\n(?=(?:async )?function |const |let |var |\/\/|\/\*)/.exec(rest.slice(1));
  return end ? rest.slice(0, end.index + 1) : rest;
}

function lab(stored) {
  const store = new Map(stored ? [["c4lab.race", stored]] : []);
  const ctx = { Lab: { sel: null }, localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) } };
  vm.createContext(ctx);
  vm.runInContext(["RACE_KEY", "rememberRace", "pickRace"].map(grab).join("\n")
    + "\nthis.__f = { rememberRace, pickRace };", ctx);
  return { ...ctx.__f, ctx, store };
}
const RACES = [{ race_id: "bayview" }, { race_id: "sorc" }, { race_id: "nas" }];

test("a reload comes back to the remembered race, not the first", () => {
  const { pickRace, ctx } = lab("sorc");
  assert.equal(pickRace(RACES, null), "sorc");
  assert.equal(ctx.Lab.sel, "sorc");
});

test("a race picked on one tab is the race on every other tab", () => {
  const { pickRace, rememberRace, store } = lab(null);
  rememberRace("sorc");                       // picked on Races
  assert.equal(store.get("c4lab.race"), "sorc");
  assert.equal(pickRace(RACES, "bayview"), "sorc");   // Gameplan had Bayview: follows
});

test("a remembered race that was deleted falls back to the tab's own, then the first", () => {
  const { pickRace } = lab("gone");
  assert.equal(pickRace(RACES, "nas"), "nas");
  assert.equal(lab("gone").pickRace(RACES, null), "bayview");
  assert.equal(lab(null).pickRace([], null), null);
});

test("no storage (private window): still works for the visit", () => {
  const ctx = { Lab: { sel: null }, localStorage: {
    getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } } };
  vm.createContext(ctx);
  vm.runInContext(["RACE_KEY", "rememberRace", "pickRace"].map(grab).join("\n")
    + "\nthis.__f = { rememberRace, pickRace };", ctx);
  ctx.__f.rememberRace("sorc");
  assert.equal(ctx.__f.pickRace(RACES, null), "sorc");
});
