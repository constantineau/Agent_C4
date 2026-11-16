// The ORC column picker (Rules race-wide, Rules per course, Fleet): one pick sets the ToT column
// and its ToD partner, a saved column the list doesn't hold is kept, and Other pairs X_TOT / X_TOD
// from the certificates the Lab has read. Run: node --test tools/lab-web/orc_columns.test.mjs
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

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const attr = (s) => esc(s).replace(/'/g, "&#39;");
const PICKER = ["ORC_COLS", "ORC_US_COLS", "orcOtherCols", "orcColSplit", "orcColRaw", "orcColSelect"];
const opts = (html) => [...html.matchAll(/<option value="([^"]*)"( selected)?>([^<]*)</g)]
  .map((m) => ({ v: m[1], sel: !!m[2], text: m[3] }));

test("standard options pair each ToT column with its ToD partner", () => {
  const f = run(PICKER, { Lab: {}, esc, attr });
  const by = Object.fromEntries(f.ORC_COLS.map((o) => [o.tot || o.tod, o.tod]));
  assert.equal(by.TMF_Offshore, "OSN");
  assert.equal(by.TMF_Inshore, "ILCWA");
  assert.equal(by.TN_Offshore_Low, "TND_Offshore_Low");
  assert.equal(by.TN_Inshore_High, "TND_Inshore_High");
  assert.equal(by.APHT, "APHD");
  assert.equal(by.GPH, "GPH");
  const o = opts(f.orcColSelect("", "", "x("));
  assert.equal(o[0].v, "|");
  assert.ok(o[0].sel && o[0].text === "Not set");
  assert.ok(o.some((x) => x.v === "TMF_Offshore|OSN" && x.text.includes("Offshore, single number")));
});

test("a saved pair shows selected; an unknown saved pair is kept as its own option", () => {
  const f = run(PICKER, { Lab: {}, esc, attr });
  let o = opts(f.orcColSelect("TMF_Inshore", "ILCWA", "x("));
  assert.deepEqual(o.filter((x) => x.sel).map((x) => x.v), ["TMF_Inshore|ILCWA"]);
  o = opts(f.orcColSelect("TMF_Offshore", "", "x("));   // an older race named only the ToT column
  assert.deepEqual(o.filter((x) => x.sel).map((x) => x.v), ["TMF_Offshore|"]);
  o = opts(f.orcColSelect("XX_ODD_COL", "", "x("));
  assert.deepEqual(o.filter((x) => x.sel).map((x) => x.v), ["XX_ODD_COL|"]);
});

test("Other: X_TOT / X_TOD pairs from the certs read, else the common US pairs", () => {
  const f = run(PICKER, { Lab: {}, esc, attr });
  const seen = ["GPH", "TMF_Offshore", "OSN", "US_PREDRC_TOT", "US_PREDRC_TOD", "IRL_TOT", "IRL_TOD",
                "LOA", "FIN_X_TOT"];
  assert.deepEqual(JSON.parse(JSON.stringify(f.orcOtherCols(seen).map((o) => [o.label, o.tot, o.tod]))),
    [["FIN_X", "FIN_X_TOT", ""], ["IRL", "IRL_TOT", "IRL_TOD"], ["US_PREDRC", "US_PREDRC_TOT", "US_PREDRC_TOD"]]);
  const us = f.orcOtherCols([]);
  assert.ok(us.some((o) => o.tot === "US_BAYMAC_CV_TOT" && o.tod === "US_BAYMAC_CV_TOD"));
  // a pair from the Lab's certs is a known option, not a "Saved" extra
  const g = run(PICKER, { Lab: { fleetCols: seen }, esc, attr });
  const o = opts(g.orcColSelect("IRL_TOT", "IRL_TOD", "x("));
  assert.equal(o.filter((x) => x.v === "IRL_TOT|IRL_TOD").length, 1);
});

test("Rules: one pick sets both columns; Not set clears them; course rows likewise", () => {
  const sc = { rating_column: "TMF_Offshore", rating_column_tod: "", course_columns: [{ course: "b", column: "Q", column_tod: "" }] };
  const ctx = { Lab: { editDef: { rules_profile: { scoring: sc } } }, paintRules() {} };
  const f = run(["orcColSplit", "rulesColPick", "rulesCourseCol"], ctx);
  f.rulesColPick("TN_Offshore_Low|TND_Offshore_Low");
  assert.equal(sc.rating_column, "TN_Offshore_Low");
  assert.equal(sc.rating_column_tod, "TND_Offshore_Low");
  f.rulesCourseCol("a", "TMF_Inshore|ILCWA");
  f.rulesCourseCol("b", "|");
  assert.deepEqual(JSON.parse(JSON.stringify(sc.course_columns)), [{ course: "a", column: "TMF_Inshore", column_tod: "ILCWA" }]);
  f.rulesColPick("|");
  assert.equal(sc.rating_column, "");
  assert.equal(sc.rating_column_tod, "");
});

test("Fleet: a pick sets both columns on the draft and repaints", () => {
  let painted = 0;
  const ctx = { Lab: { editDef: {} }, paintFleet() { painted++; } };
  const f = run(["orcColSplit", "_fleetScoring", "fleetColSet"], ctx);
  f.fleetColSet("US_PREDRC_TOT|US_PREDRC_TOD");
  const sc = ctx.Lab.editDef.rules_profile.scoring;
  assert.equal(sc.rating_column, "US_PREDRC_TOT");
  assert.equal(sc.rating_column_tod, "US_PREDRC_TOD");
  assert.equal(painted, 1);
});

test("the picker's onchange calls the given prefix with the picked value", () => {
  const f = run(PICKER, { Lab: {}, esc, attr });
  assert.ok(f.orcColSelect("", "", "rulesCourseCol('c1', ").includes(`onchange="rulesCourseCol('c1', this.value)"`));
});
