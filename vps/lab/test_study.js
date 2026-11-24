// #378 Gameplan study batch: the pure helpers in web/study.js, and its wiring as text.
// Run: node vps/lab/test_study.js  (CI: tools/ci/run.sh node_area)
"use strict";
const fs = require("fs");
const path = require("path");
const S = require(path.join(__dirname, "web", "study.js"));

let ok = true;
function check(name, cond) {
  ok = ok && !!cond;
  console.log(`  [${cond ? "OK " : "FAIL"}] ${name}`);
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

console.log("axes (same cleaning as the server)");
check("baseline dropped, junk and repeats gone", JSON.stringify(S.parseList("-30, 0 30 30 x", "start_min")) === "[-30,30]");
check("boat %: 100 is the baseline, 300 out of range", JSON.stringify(S.parseList("90 100 300", "polar_pct")) === "[90]");
check("empty box: no runs", S.parseList("", "twd_deg").length === 0);
const ax = S.axesOf({ models: true, members: false, start_min: "-30, 30", polar_pct: "90",
  tws_pct: "120", twd_deg: "-10 10" });

console.log("run count");
check("1 + 2 models + 2 + 1 + 1 + 2 = 9", S.runCount(ax, 2, 0) === 9);
check("one model: no 'alone' runs", S.runCount(ax, 1, 0) === 7);
check("ensemble runs count only when ticked", S.runCount(ax, 2, 5) === 9 &&
  S.runCount(Object.assign({}, ax, { members: true }), 2, 5) === 14);
check("estimate: weather once + runs", S.estimateMin(9, "fast") === Math.ceil((60 + 9 * 30) / 60));

console.log("table");
const r = { available: true, finished: 3, spread_min: 31.6, sides: { right: 2, left: 1 },
  tie_note: "Within about 5 min counts as a tie. Rough: the router's own noise isn't measured yet.",
  agreement_note: "Agreement, not accuracy: models can all be wrong together.",
  rows: [
    { id: "base", axis: "base", label: "Baseline", ok: true, finish_epoch: 1793560000, hours: 7.2, delta_min: 0, tie: true, side: "right" },
    { id: "model:gfs", axis: "model", label: "GFS alone", model: "gfs", cycle: "2026-11-01 12Z", weight_pct: 60, ok: true, finish_epoch: 1793560200, hours: 7.3, delta_min: 3.4, tie: true, side: "right" },
    { id: "twd_deg:-10", axis: "twd_deg", twd_deg: -10, label: "Wind 10° left", ok: true, finish_epoch: 1793561900, hours: 7.7, delta_min: 31.6, tie: false, side: "left" },
    { id: "polar_pct:90", axis: "polar_pct", label: "Boat 90%", ok: false, note: "No full route." }] };
const h = S.resultsHtml(r, esc, "UTC");
check("summary: finished, spread, sides", h.includes("3 of 4 runs finished") && h.includes("spread 32 min") && h.includes("2 right, 1 left"));
check("agreement and tie caveats shown", h.includes("Agreement, not accuracy") && h.includes("Rough"));
check("a model alone shows its cycle and weight", h.includes("2026-11-01 12Z · 60% of blend"));
check("tie marked (not on the baseline)", (h.match(/st-tie/g) || []).length === 1);
check("time vs baseline", h.includes("+3 min") && h.includes("+32 min"));
check("failed run says why", h.includes("No full route."));
check("not run: says why", S.resultsHtml({ available: false, note: "9 runs is over the limit of 5." }, esc).includes("over the limit"));
check("left shift blue, right shift red", S.colorOf({ axis: "twd_deg", twd_deg: -10 }) !== S.colorOf({ axis: "twd_deg", twd_deg: 10 }));

const rc = Object.assign({}, r, { windfield: { corrections: { text: ["GFS: 10° right"] } } });
rc.rows = [Object.assign({}, r.rows[1], { correction: "GFS: 10° right" })];
const hc = S.resultsHtml(rc, esc, "UTC");
check("corrections shown on the summary and the model row", hc.includes("Corrected: GFS: 10° right") &&
  hc.includes("corrected: GFS: 10° right"));

console.log("map fan");
const route = S.asRoute({ race_id: "t", start: {}, rows: r.rows,
  baseline: { available: true, path: [], candidate_paths: [{ model: "gfs", label: "GFS alone", path: [[1, 2]] },
    { model: "twd_deg", label: "Wind 10° left", path: [[1, 2]] }] } });
check("each line coloured like its row", route.candidate_paths[0].color === S.colorOf(r.rows[1]) &&
  route.candidate_paths[1].color === S.colorOf(r.rows[2]));
check("legend named for the study", route.fan_label === "Study runs" && route.race_id === "t");

console.log("wiring");
const web = path.join(__dirname, "web");
const app = fs.readFileSync(path.join(web, "app.js"), "utf8");
const idx = fs.readFileSync(path.join(web, "index.html"), "utf8");
const mv = fs.readFileSync(path.join(web, "mapview.js"), "utf8");
check("index loads study.js after app.js", idx.indexOf("/study.js") > idx.indexOf("/app.js"));
check("Gameplan mounts the study card", app.includes('<div id="studyCard"></div>') && app.includes('Study.mount("studyCard")'));
check("map draws a run's own colour and label", mv.includes("cp.color || modelColor(cp.model)") && mv.includes("cp.label ||"));
const src = fs.readFileSync(path.join(web, "study.js"), "utf8");
check("posts /api/study and polls the optimize job", src.includes('"/api/study"') && src.includes("/api/optimize/status?job_id="));

console.log(ok ? "ALL OK" : "SOME CHECKS FAILED");
process.exit(ok ? 0 : 1);
