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
// rows carry the build's own cell bins (band, twa_bin); cells/race_cells/race_curves are the
// cells that passed the seconds gate — a dot is drawn only when its cell is one of them
const DATA = { grid: { tws_step_kn: 2 },
  cells: [
    { tws: 10, twa: 50, config: "J1" }, { tws: 10, twa: 90, config: "J1" },
    { tws: 10, twa: 140, config: "S1" }, { tws: 16, twa: 90, config: "J1" },
    { tws: 10, twa: 100, config: null }, { tws: 8, twa: 60, config: "J1" }],
  race_cells: [
    { tws: 10, twa: 50, config: "J1", recording: R1 }, { tws: 10, twa: 50, config: "J1", recording: R2 }],
  race_curves: [
    { tws: 10, twa: 50, recording: R1 }, { tws: 10, twa: 90, recording: R1 },
    { tws: 10, twa: 140, recording: R1 }, { tws: 10, twa: 50, recording: R2 },
    { tws: 10, twa: 100, recording: R2 }],
  scatter: {
  fields: ["t", "tws", "twa", "stw", "config", "recording", "band", "twa_bin"], kept: 6, total: 600,
  rows: [
    [1, 10.2, 45, 6.1, "J1", R1, 10, 50], [2, 9.8, 90, 7.0, "J1", R1, 10, 90],
    [3, 10.4, 140, 7.5, "S1", R1, 10, 140], [4, 10.0, 50, 6.3, "J1", R2, 10, 50],
    [5, 16.1, 90, 8.2, "J1", R2, 16, 90], [6, 10.1, 100, 30.0, null, R2, 10, 100],
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
  c.Pol.race = R1;              // R1's 90° and 140° cells are not in race_cells: refused for R1
  assert.deepEqual(circles(c.polScSvg(XY, 12)).map((p) => p.twa), [45]);
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

test("the band is the build's: 9 kn sits in the 8 kn cell, not Math.round's 10", () => {
  const c = load();
  c.Pol.data = { ...DATA, scatter: { ...DATA.scatter, rows: [[1, 9.0, 61, 6.0, "J1", R1, 8, 60]] } };
  c.Pol.tws = 8;
  assert.equal(circles(c.polScSvg(XY, 12)).length, 1);
  c.Pol.tws = 10;
  assert.equal(circles(c.polScSvg(XY, 12)).length, 0);
  // a saved test with only raw tws rounds half to even like the build (9 -> 8, 11 -> 12)
  assert.equal(c.polScBand({ tws: 9 }, 2), 8);
  assert.equal(c.polScBand({ tws: 11 }, 2), 12);
  assert.equal(c.polScBand({ tws: 9.2 }, 2), 10);
});

test("a dot whose cell failed the seconds gate is not drawn", () => {
  const c = load();
  c.Pol.data = { ...DATA, scatter: { ...DATA.scatter, rows: [[1, 10.0, 71, 6.0, "J1", R1, 10, 70]] } };
  assert.equal(circles(c.polScSvg(XY, 12)).length, 0);
  c.Pol.data.cells = [...DATA.cells, { tws: 10, twa: 70, config: "J1" }];
  assert.equal(circles(c.polScSvg(XY, 12)).length, 1);
});

test("pooled view draws an overlapping second once; each race's view keeps its copy", () => {
  const c = load();
  // the same second from two overlapping windows: R1's copy counted in the pooled polar, R2's not
  c.Pol.data = { ...DATA, scatter: { ...DATA.scatter, fields: [...DATA.scatter.fields, "pooled"],
    rows: [[1, 10.0, 50, 6.0, "J1", R1, 10, 50, 1], [1, 10.0, 50, 6.0, "J1", R2, 10, 50, 0]] } };
  assert.equal(circles(c.polScSvg(XY, 12)).length, 1);
  c.Pol.race = R2;
  assert.equal(circles(c.polScSvg(XY, 12)).length, 1);
  c.Pol.race = "all"; c.Pol.split = "race";
  assert.equal(circles(c.polScSvg(XY, 12)).length, 2);
});

test("a recording under 1 s off the cell's still finds its cell (same match as the race filter)", () => {
  const c = load();
  c.Pol.data = { ...DATA, race_cells: [{ tws: 10, twa: 50, config: "J1", recording: 100.49 }],
    race_curves: [{ tws: 10, twa: 50, recording: 100.49 }],
    scatter: { ...DATA.scatter, rows: [[1, 10.0, 50, 6.0, "J1", 100.51, 10, 50]] } };
  c.Pol.race = 100.49;
  assert.equal(circles(c.polScSvg(XY, 12)).length, 1);
  c.Pol.race = "all"; c.Pol.split = "race";
  assert.equal(circles(c.polScSvg(XY, 12)).length, 1);
});

test("no scatter block (an old build): nothing drawn, nothing thrown", () => {
  const c = load();
  c.Pol.data = { grid: {} };
  assert.equal(circles(c.polScSvg(XY, 12)).length, 0);
  assert.equal(c.polScLegend(), "");
});
