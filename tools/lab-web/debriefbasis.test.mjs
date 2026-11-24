// The Debrief tactics card's weather line and split time lost (#404, vps/lab/web/debriefbasis.js).
// Run: node --test tools/lab-web/debriefbasis.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "debriefbasis.js"), "utf8");
const app = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "app.js"), "utf8");
const html = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "index.html"), "utf8");

function load() {
  const ctx = {
    esc: (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])),
  };
  vm.createContext(ctx);
  vm.runInContext(src + "\nthis.debBasisRows = debBasisRows;", ctx);
  return ctx;
}

const WEATHER = {
  kind: "archive", summary: "Forecast from the start: GFS Jul 18 12Z (3–9 h old).",
  note: "The forecast out at the start, not the wind that blew.",
  models: [{ model: "gfs" }],
  hourly: [{ h: 0, age_h: { gfs: 3 } }, { h: 1, age_h: { gfs: null } }],
};

test("names the weather and its age by hour", () => {
  const out = load().debBasisRows({ oracle_weather: WEATHER, regret: {}, playbook: {} }, "OLD");
  assert.match(out, /Weather used/);
  assert.match(out, /GFS Jul 18 12Z \(3–9 h old\)/);
  assert.match(out, /3 h old/);
  assert.match(out, /no data/);
});

test("same wind: route choice and forecast are separate rows", () => {
  const r = { oracle_weather: WEATHER, playbook: { predicted_hours: 10 },
    regret: { kind: "same_wind", minutes: 12, forecast_error_minutes: 30, plan_on_actual_hours: 10.5 } };
  const out = load().debBasisRows(r, "OLD");
  assert.match(out, /Route choice<\/b> 12 min slower than the best route, same wind/);
  assert.match(out, /The plan's route took 30 min longer than forecast/);
  assert.match(out, /said 10\.0 h, took 10\.5 h/);
  assert.doesNotMatch(out, /OLD/);
});

test("faster than forecast reads as less", () => {
  const r = { playbook: {}, regret: { kind: "same_wind", minutes: 0, forecast_error_minutes: -15 } };
  const out = load().debBasisRows(r, "OLD");
  assert.match(out, /same time as the best route/);
  assert.match(out, /took 15 min less than forecast/);
});

test("an older report keeps the old row; a failed split says why", () => {
  const ctx = load();
  assert.equal(ctx.debBasisRows({ regret: { minutes: 15 }, playbook: {} }, "OLD"), "OLD");
  const out = ctx.debBasisRows({ playbook: {}, regret: { kind: "vs_forecast", split_note: "Time lost includes forecast error: no saved route to re-time." } }, "OLD");
  assert.match(out, /^OLD/);
  assert.match(out, /includes forecast error/);
});

test("app.js uses it and index.html loads it", () => {
  assert.match(app, /debBasisRows\(r, lostRow\)/);
  assert.match(html, /<script src="\/debriefbasis\.js"><\/script>/);
});
