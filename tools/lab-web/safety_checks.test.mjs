// Route checks pills (#405, vps/lab/web/safetychecks.js): every check shown with its own state,
// and a route never reads as all checked unless every check ran. Loads the module in a VM.
// Run: node --test tools/lab-web/safety_checks.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "safetychecks.js"), "utf8");
const ctx = { esc: (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])) };
vm.createContext(ctx);
vm.runInContext(src + "\nthis.safetyChecksHtml = safetyChecksHtml;", ctx);
const html = ctx.safetyChecksHtml;

const ck = (land, depth, extra = {}) => [
  { id: "land", label: "Land", state: land, texts: land === "checked" ? [] : ["Land not checked."] },
  { id: "depth", label: "Depth", state: depth, texts: [] },
  { id: "zones", label: "Race zones", state: extra.zones || "checked", texts: [] },
  { id: "marks", label: "Marks and sides", state: extra.marks || "checked", texts: [] },
  { id: "chart", label: "Chart covers route", state: "checked", texts: [] },
];

test("all checked: five green pills, no caveat", () => {
  const h = html(ck("checked", "checked"));
  assert.match(h, /data-checks="all"/);
  assert.equal((h.match(/pill ok/g) || []).length, 5);
  assert.doesNotMatch(h, /Not every check/);
});

test("land not checked: amber pill that says so, with its reason as a tip", () => {
  const h = html(ck("not_checked", "checked"));
  assert.match(h, /data-checks="some"/);
  assert.match(h, /pill warn" title="Land not checked\.">Land: not checked/);
  assert.match(h, /Not every check ran\./);
});

test("a failed check is red; the red panel carries the words", () => {
  const h = html(ck("checked", "checked", { marks: "failed" }));
  assert.match(h, /pill bad">Marks and sides: failed/);
  assert.doesNotMatch(h, /Not every check/);
});

test("a verdict from before the checks never reads as checked", () => {
  assert.match(html(undefined), /Checks not listed/);
  assert.doesNotMatch(html(undefined), /pill ok/);
});

test("an unknown state counts as not checked", () => {
  const c = ck("checked", "checked"); c[1].state = "maybe";
  assert.match(html(c), /Depth: not checked/);
});
