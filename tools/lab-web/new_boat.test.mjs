// Lab "New boat" from an ORC certificate: the preview shows the cert's facts in the Lab's depth
// unit, escaped. Run: node --test tools/lab-web/new_boat.test.mjs
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
const P = { name: "Hooligan", sail_number: "USA 52521", boat_class: "TP 52", draft_m: 3.225,
            loa_m: 15.843, gph: 438.9, ref_no: "045600052L8", issue_date: "2026-11-12", polar_rows: 90 };

test("preview: the cert's facts, draft in feet", () => {
  const f = run(["depthVal", "depthTxt", "nbPreviewHtml"], { esc, depthUnit: () => "ft" });
  const h = f.nbPreviewHtml(P);
  for (const s of ["Hooligan", "USA 52521", "TP 52", "10.6 ft", "438.9", "045600052L8, issued 2026-11-12", "90 speeds"])
    assert.ok(h.includes(s), s);
});

test("preview: metres when the Lab shows metres; a name is escaped", () => {
  const f = run(["depthVal", "depthTxt", "nbPreviewHtml"], { esc, depthUnit: () => "m" });
  const h = f.nbPreviewHtml({ ...P, name: "<b>x</b>", polar_rows: 0 });
  assert.ok(h.includes("3.2 m") && h.includes("&lt;b&gt;x") && h.includes("none"));
});
