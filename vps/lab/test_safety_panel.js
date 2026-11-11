// The route safety panel with a chart override in it. It used to call a date helper that only exists
// inside another function, so a route with any "Chart wrong here?" override threw and the Gameplan
// showed nothing after a finished run.
// Run: node vps/lab/test_safety_panel.js  (CI: tools/ci/run.sh node_area)
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "web", "app.js"), "utf8");
function fnSource(name) {
  const i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`no function ${name}`);
  let depth = 0, j = src.indexOf("{", i);
  for (; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) break;
  }
  return src.slice(i, j + 1);
}

let ok = true;
function check(name, cond) {
  ok = ok && !!cond;
  console.log(`  [${cond ? "OK " : "FAIL"}] ${name}`);
}

const ctx = vm.createContext({});
// a stand-in esc (the real one is a one-line HTML escaper at the top of app.js)
vm.runInContext('const esc = (s) => String(s == null ? "" : s).replace(/[<>&"]/g, "");\n' +
  fnSource("safetyPanel") + "\nthis.safetyPanel = safetyPanel;", ctx);
const s = { ok: true, problems: [], notes: [],
  overrides: [{ mark: "Finish", by: "Cole", at: 1794412311, why: "Finish is at Harbor Mouth" }] };
let html = "", err = null;
try { html = ctx.safetyPanel(s, "route", { race: "r", course: "c" }); } catch (e) { err = e; }
console.log("Safety panel with a chart override");
check("renders without throwing" + (err ? ` (${err.message})` : ""), !err);
check("names the mark, who and why", /Finish/.test(html) && /Cole/.test(html) && /Harbor Mouth/.test(html));
check("shows when, in UTC", /2026-11-11 \d\d:\d\dZ/.test(html));

process.exit(ok ? 0 : 1);
