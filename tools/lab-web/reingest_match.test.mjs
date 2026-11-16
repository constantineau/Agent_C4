// Re-reading a race (the SIs landing in race week) saves over the race already saved, not as a second
// race without its fleet, boat or notes. The server names the likely race (`match`); the review ticks
// "Update <race>" by default and saves under its id. Helpers pulled out of vps/lab/web/app.js.
// Run: node --test tools/lab-web/reingest_match.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "app.js"), "utf8");

function grab(name) {
  const m = new RegExp(`\\n((?:async )?function ${name}\\(|const ${name} )`).exec(src);
  assert.ok(m, `${name} found in app.js`);
  const rest = src.slice(m.index + 1);
  const end = /\n(?=(?:async )?function |const |let |var |\/\/|\/\*)/.exec(rest.slice(1));
  return end ? rest.slice(0, end.index + 1) : rest;
}

function lab() {
  const ctx = { Lab: { isDraft: false, draft: null, editDef: null }, painted: 0,
    esc: (s) => String(s), paintDetail() { ctx.painted++; } };
  vm.createContext(ctx);
  vm.runInContext(["draftMatchSetup", "draftUseMatch", "detailMatch"].map(grab).join("\n")
    + "\nthis.__f = { draftMatchSetup, draftUseMatch, detailMatch };", ctx);
  return ctx;
}

const draft = (match) => ({ definition: { race_id: "2026-sorc-ocean-race", name: "SORC Ocean Race" }, match });
const SORC = { race_id: "sorc-ocean-race-2026", name: "SORC Ocean Race", same_id: false, sure: true };

function ready(ctx, resp) {          // what extractDraft does with the result
  ctx.__f.draftMatchSetup(resp);
  Object.assign(ctx.Lab, { draft: resp, editDef: resp.definition, isDraft: true });
}

test("a draft like a saved race saves under that race's id by default", () => {
  const ctx = lab(); const resp = draft(SORC);
  ready(ctx, resp);
  assert.equal(ctx.Lab.editDef.race_id, "sorc-ocean-race-2026");
  const html = ctx.__f.detailMatch();
  assert.match(html, /"checkbox" id="draftMatch" checked/);
  assert.match(html, /Update “SORC Ocean Race”, already saved\. Its fleet, boat, rating pick and notes stay\./);
});

test("unticking saves it as a new race under its own id, and ticking again goes back", () => {
  const ctx = lab(); ready(ctx, draft(SORC));
  ctx.__f.draftUseMatch(false);
  assert.equal(ctx.Lab.editDef.race_id, "2026-sorc-ocean-race");
  assert.match(ctx.__f.detailMatch(), /Unticked: Save makes a new race\./);
  assert.doesNotMatch(ctx.__f.detailMatch(), /id="draftMatch" checked/);
  ctx.__f.draftUseMatch(true);
  assert.equal(ctx.Lab.editDef.race_id, "sorc-ocean-race-2026");
  assert.equal(ctx.painted, 2);
});

test("not sure (a start date missing): offered unticked, saves as new unless ticked", () => {
  const ctx = lab(); ready(ctx, draft({ ...SORC, sure: false }));
  assert.equal(ctx.Lab.editDef.race_id, "2026-sorc-ocean-race");
  assert.match(ctx.__f.detailMatch(), /Unticked: Save makes a new race\. Tick it if this is the same race\./);
  ctx.__f.draftUseMatch(true);
  assert.equal(ctx.Lab.editDef.race_id, "sorc-ocean-race-2026");
});

test("same id already: just says it updates the race", () => {
  const ctx = lab();
  ready(ctx, { definition: { race_id: "sorc-ocean-race-2026" }, match: { ...SORC, same_id: true } });
  assert.equal(ctx.Lab.editDef.race_id, "sorc-ocean-race-2026");
  assert.match(ctx.__f.detailMatch(), /^<div class="banner ok">Saving updates “SORC Ocean Race”/);
});

test("no match, or a saved race open (not a draft): nothing shown, id untouched", () => {
  const ctx = lab(); ready(ctx, draft(null));
  assert.equal(ctx.Lab.editDef.race_id, "2026-sorc-ocean-race");
  assert.equal(ctx.__f.detailMatch(), "");
  const c2 = lab(); ready(c2, draft(SORC)); c2.Lab.isDraft = false;
  assert.equal(c2.__f.detailMatch(), "");
  c2.__f.draftUseMatch(false);
  assert.equal(c2.Lab.editDef.race_id, "sorc-ocean-race-2026");
});

test("wired in: the result goes through draftMatchSetup, the detail shows the choice", () => {
  assert.match(grab("extractDraft"), /draftMatchSetup\(resp\);\s*Lab\.draft = resp; Lab\.editDef = resp\.definition;/);
  assert.match(grab("paintDetail"), /detailActions\(d, v\) \+ detailMatch\(\) \+ detailBanner\(v\)/);
});
