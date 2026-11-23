// Log points behind the observed polar (#376): vps/lab/web/polarscatter.js in a VM.
// The scatter must follow the same race and sail filters as the polar lines, colour by wind band
// with one colour per band whatever the filter, never rescale the chart, and take extra point
// sets (saved tests, #374) through the hook without needing them.
// Run: node --test tools/lab-web/polar_scatter.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "polarscatter.js"), "utf8");

const R1 = 1784155457.9, R2 = 1784394211.4;
const DATA = { grid: { tws_step_kn: 2 }, scatter: {
  fields: ["t", "tws", "twa", "stw", "config", "recording"], kept: 6, total: 600,
  rows: [
    [1, 10.2, 45, 6.1, "J1", R1], [2, 9.8, 90, 7.0, "J1", R1], [3, 10.4, 140, 7.5, "S1", R1],
    [4, 10.0, 50, 6.3, "J1", R2], [5, 16.1, 90, 8.2, "J1", R2], [6, 10.1, 100, 30.0, null, R2],
  ] } };

function load() {
  const ctx = { console, esc: String, Pol: { data: DATA, tws: 10, race: "all", config: "all", split: "config" } };
  vm.createContext(ctx);
  vm.runInContext(src.replace("const PolSc =", "var PolSc =") + "\nthis.PolSc = PolSc;", ctx);
  return ctx;
}
const XY = (twa, v) => [twa, v];
const circles = (svg) => [...svg.matchAll(/<circle cx="([\d.]+)" cy="([\d.]+)" r="1.8" fill="([^"]+)"/g)]
  .map((m) => ({ twa: Number(m[1]), stw: Number(m[2]), fill: m[3] }));

test("this wind: only the selected band, every race and sail", () => {
  const c = load();
  const pts = circles(c.polScSvg(XY, 12));
  assert.deepEqual(pts.map((p) => p.twa).sort((a, b) => a - b), [45, 50, 90, 140]);
  assert.equal(c.PolSc.last.off, 1, "a point past the outer ring is counted, not drawn");
});

test("race and sail filters match the lines", () => {
  const c = load();
  c.Pol.race = R2;
  assert.deepEqual(circles(c.polScSvg(XY, 12)).map((p) => p.twa), [50]);
  c.Pol.race = "all"; c.Pol.config = "S1";
  assert.deepEqual(circles(c.polScSvg(XY, 12)).map((p) => p.twa), [140]);
  c.Pol.split = "race";                       // lines by race: the sail filter is off
  assert.equal(circles(c.polScSvg(XY, 12)).length, 4);
  c.Pol.config = "—"; c.Pol.split = "config"; // unknown sails
  assert.equal(circles(c.polScSvg(XY, 40)).length, 1);
});

test("all winds: one colour per band, the same colour in both modes", () => {
  const c = load();
  c.PolSc.mode = "all";
  const pts = circles(c.polScSvg(XY, 12));
  assert.equal(pts.length, 5);
  const c10 = pts.find((p) => p.twa === 45).fill, c16 = pts.find((p) => p.stw === 8.2).fill;
  assert.notEqual(c10, c16);
  c.PolSc.mode = "tws";
  assert.equal(circles(c.polScSvg(XY, 12))[0].fill, c10);
  assert.match(c.polScLegend(), /600/, "the legend says it is a sample of the full set");
});

test("off draws nothing; the hook adds saved tests under the same filters", () => {
  const c = load();
  c.PolSc.mode = "off";
  assert.equal(circles(c.polScSvg(XY, 12)).length, 0);
  assert.equal(c.polScLegend(), "");
  c.polRegisterPointSource("tests", () => [
    { tws: 10.3, twa: 60, stw: 6.6, label: "J1 upwind test" },
    { tws: 18.0, twa: 60, stw: 8.0, label: "other wind" }]);
  const svg = c.polScSvg(XY, 12);
  assert.equal((svg.match(/<title>/g) || []).length, 1);
  assert.match(svg, /J1 upwind test/);
  c.polRegisterPointSource("tests", () => { throw new Error("broken source"); });
  assert.doesNotThrow(() => c.polScSvg(XY, 12));
  assert.equal(c.PolSc.sources.length, 1, "re-registering replaces");
});

test("no scatter block (an old build): nothing drawn, nothing thrown", () => {
  const c = load();
  c.Pol.data = { grid: {} };
  assert.equal(circles(c.polScSvg(XY, 12)).length, 0);
  assert.equal(c.polScLegend(), "");
});
