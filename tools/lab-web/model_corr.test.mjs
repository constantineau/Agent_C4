// Model corrections (#384): what the Gameplan sends, kept per race, and the words it shows.
// Run: node --test tools/lab-web/model_corr.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "modelcorr.js"), "utf8");

function lab(raceId = "r1", chosen = ["gfs", "hrrr"]) {
  const store = new Map();
  const ctx = {
    Opt: { raceId, chosen }, esc: (s) => String(s), qhelp: () => "",
    document: { getElementById: () => null },
    localStorage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) },
  };
  vm.createContext(ctx);
  vm.runInContext(src + "\nthis.__f = { mcSet, mcFor, mcRequest, mcText, mcLines, mcResultNote, mcPlumeNote, mcClear };", ctx);
  return { ...ctx.__f, ctx };
}

test("nothing set → nothing sent (raw forecasts)", () => {
  const { mcRequest } = lab();
  assert.equal(mcRequest("r1", ["gfs"], ""), null);
});

test("a correction is kept per race and sent for the chosen models only", () => {
  const { mcSet, mcRequest, ctx } = lab();
  mcSet("gfs", "rotate_deg", "10");
  mcSet("gfs", "scale_pct", "80");
  mcSet("hrrr", "shift_h", "1");
  assert.deepEqual(JSON.parse(JSON.stringify(mcRequest("r1", ["gfs"], ""))),
    { gfs: { rotate_deg: 10, scale_pct: 80, shift_h: 0 } });
  assert.equal(mcRequest("r2", ["gfs", "hrrr"], ""), null);      // another race: its own (none)
  ctx.Opt.raceId = "r2";
  mcSet("gfs", "rotate_deg", "-5");
  assert.equal(mcRequest("r1", ["gfs", "hrrr"], "").hrrr.shift_h, 1);
  assert.equal(mcRequest("r2", ["gfs"], "").gfs.rotate_deg, -5);
});

test("a fade sends its minutes and its start time (UTC without a race zone)", () => {
  const { mcSet, mcRequest } = lab();
  mcSet("gfs", "rotate_deg", "10");
  mcSet("gfs", "fade_min", "90");
  mcSet("gfs", "fade_at", "2027-06-01T12:00");
  const r = mcRequest("r1", ["gfs"], "").gfs;
  assert.equal(r.fade_min, 90);
  assert.equal(r.fade_from, Date.UTC(2027, 5, 1, 12, 0) / 1000);
  mcSet("gfs", "fade_at", "");
  assert.equal(mcRequest("r1", ["gfs"], "").gfs.fade_from, null);   // blank = the route start
});

test("a no-op correction (0°, 100 %, 0 h) is not sent", () => {
  const { mcSet, mcRequest } = lab();
  mcSet("gfs", "rotate_deg", "0");
  mcSet("gfs", "scale_pct", "100");
  assert.equal(mcRequest("r1", ["gfs"], ""), null);
});

test("plain words match the server's", () => {
  const { mcText } = lab();
  assert.equal(mcText("gfs", { rotate_deg: -10, scale_pct: 80, shift_h: 1.5 }), "GFS: 10° left, speed 80%, 1.5 h later");
  assert.equal(mcText("hrrr", { rotate_deg: 10, fade_min: 60 }), "HRRR: 10° right, back to raw over 60 min from start");
});

test("the route says it used a correction; the plume says its lines are raw", () => {
  const { mcResultNote, mcPlumeNote, mcSet } = lab();
  assert.equal(mcResultNote({ corrections: null }), "");
  const html = mcResultNote({ corrections: { applied: true, text: ["GFS: 10° right"],
    items: [{ model: "gfs" }, { model: "icon", unused: true }] } });
  assert.match(html, /Corrected forecast: GFS: 10° right\./);
  assert.match(html, /ICON gave no wind/);
  assert.equal(mcPlumeNote("r1"), "");
  mcSet("gfs", "rotate_deg", "10");
  assert.match(mcPlumeNote("r1"), /Raw forecasts, without your corrections \(GFS: 10° right\)/);
});
