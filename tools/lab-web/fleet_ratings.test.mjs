// Lab Fleet tab rating columns (#311): applying a certificate candidate replaces both numbers and
// their column tags outright, and a save's server-side rating fixes land on screen.
// Pulls the named top-level helpers out of vps/lab/web/app.js and runs them in a VM.
// Run: node --test tools/lab-web/fleet_ratings.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "app.js"), "utf8");

function grab(name) {
  const m = new RegExp(`\\n((?:async )?function ${name}\\(|const ${name} |let ${name} )`).exec(src);
  assert.ok(m, `${name} found in app.js`);
  const rest = src.slice(m.index + 1);
  const end = /\n(?=(?:async )?function |const |let |var |window\.|document\.|\/\/|\/\*|\[)/.exec(rest.slice(1));
  return end ? rest.slice(0, end.index + 1) : rest;
}

function run(names, ctx) {
  vm.createContext(ctx);
  vm.runInContext(names.map(grab).join("\n") + "\nthis.__f = {" + names.join(",") + "};", ctx);
  return ctx.__f;
}

test("a candidate without the race's column clears the old cert's numbers and tags", () => {
  const e = { boat: "Il Mostro", rating: 1.1, rating_col: "COL_A", tod_allowance: 610, tod_col: "TOD_A",
              orc_gph: 500, source: "yb+orc" };
  const ctx = { Lab: { editDef: { fleet: [e] },
                       fleetCand: { i: 0, tot_col: "COL_B", tod_col: "TOD_B",
                                    list: [{ yacht: "Il Mostro", country: "USA", orc_gph: 505, rating: null,
                                             tod_allowance: null, cls: "VOR70", sail: "USA 60" }] } },
                paintFleet() {} };
  const f = run(["fleetApplyCand"], ctx);
  f.fleetApplyCand(0, 0);
  assert.equal(e.rating, null);
  assert.equal(e.tod_allowance, null);
  assert.equal(e.orc_gph, 505);
  assert.ok(!("rating_col" in e) && !("tod_col" in e), JSON.stringify(e));
});

test("a candidate with the column sets the numbers and tags them with the column", () => {
  const e = { boat: "Il Mostro" };
  const ctx = { Lab: { editDef: { fleet: [e] },
                       fleetCand: { i: 0, tot_col: "COL_B", tod_col: "TOD_B",
                                    list: [{ yacht: "x", country: "USA", orc_gph: 505, rating: 0.97,
                                             tod_allowance: 620 }] } },
                paintFleet() {} };
  run(["fleetApplyCand"], ctx).fleetApplyCand(0, 0);
  assert.deepEqual([e.rating, e.rating_col, e.tod_allowance, e.tod_col], [0.97, "COL_B", 620, "TOD_B"]);
});

function saveCtx(ed, fix) {
  const out = { clean: null };
  const ctx = { Lab: { editDef: ed, ratingFix: fix }, markClean(s) { out.clean = s; }, JSON, Object };
  vm.createContext(ctx);
  vm.runInContext(grab("_RATING_KEYS") + grab("_takeRatingFix") + grab("markSaved") + "\nthis.__f = markSaved;", ctx);
  return { markSaved: ctx.__f, out, ctx };
}

test("a save's rating fixes replace the roster on screen and become the saved baseline", () => {
  const ed = { race_id: "r", fleet: [{ boat: "A", rating: 1.1, rating_col: "COL_A" }], own: { rating: 1, rating_col: "COL_A" } };
  const snap = JSON.stringify(ed);
  const { markSaved, out, ctx } = saveCtx(ed, { fleet: [{ boat: "A", rating: null }], own: { rating: null } });
  markSaved(snap, ed);
  assert.equal(ed.fleet[0].rating, null);
  assert.ok(!("rating_col" in ed.fleet[0]));
  assert.equal(ed.own.rating, null);
  assert.equal(out.clean, JSON.stringify(ed));     // nothing else changed → clean
  assert.equal(ctx.Lab.ratingFix, null);
});

test("edits made while the save was in flight survive the fixes and stay unsaved", () => {
  const ed = { race_id: "r", name: "Old", fleet: [{ boat: "A", rating: 1.1, rating_col: "COL_A" },
                                                   { boat: "B", rating: 1.2, rating_col: "COL_A" }] };
  const snap = JSON.stringify(ed);
  // in flight: race renamed, boat A renamed, boat B's rating typed by hand
  ed.name = "New"; ed.fleet[0].owner = "Pat"; ed.fleet[1].rating = 0.95; ed.fleet[1].rating_col = "manual";
  const fix = { fleet: [{ boat: "A", rating: null }, { boat: "B", rating: null }], own: {} };
  const { markSaved, out } = saveCtx(ed, fix);
  markSaved(snap, ed);
  assert.equal(ed.name, "New");
  assert.equal(ed.fleet[0].owner, "Pat");
  assert.equal(ed.fleet[0].rating, null);                         // unchanged since send → fixed
  assert.deepEqual([ed.fleet[1].rating, ed.fleet[1].rating_col], [0.95, "manual"]);   // edited → kept
  const base = JSON.parse(out.clean);
  assert.equal(base.name, "Old");                                  // baseline = what was sent, fixed
  assert.equal(base.fleet[1].rating, null);
  assert.notEqual(out.clean, JSON.stringify(ed));                  // so the new edits show unsaved
});

test("a roster reordered in flight takes no fix on the wrong boat", () => {
  const ed = { race_id: "r", fleet: [{ boat: "A", rating: 1.1, rating_col: "COL_A" }, { boat: "B", rating: 1.2 }] };
  const snap = JSON.stringify(ed);
  ed.fleet.reverse();
  const { markSaved } = saveCtx(ed, { fleet: [{ boat: "A", rating: null }, { boat: "B", rating: 1.2 }], own: {} });
  markSaved(snap, ed);
  assert.deepEqual(ed.fleet.map((e) => [e.boat, e.rating]), [["B", 1.2], ["A", 1.1]]);
});

test("typing a number tags it manual", () => {
  const e = { boat: "A", rating: 1.1, rating_col: "COL_A" };
  const ctx = { Lab: { editDef: { fleet: [e] } } };
  vm.createContext(ctx);
  vm.runInContext(grab("_FLEET_TAG") + grab("fleetSet") + "\nthis.__f = fleetSet;", ctx);
  ctx.__f(0, "rating", "0.9");
  assert.equal(e.rating_col, "manual");
});
