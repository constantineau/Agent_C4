// Monitor fleet source label (#310): the fleet line names where the positions came from, as the
// backend's tracker provider labels it (YB, fleet file), never "public tracker" for a file.
// Run: node --test tools/lab-web/monitor_fleet.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "app.js"), "utf8");

function grab(name) {
  const m = new RegExp(`\\nfunction ${name}\\(`).exec(src);
  assert.ok(m, `${name} found in app.js`);
  const rest = src.slice(m.index + 1);
  const end = /\n(?=(?:async )?function |const |let |\/\/|\/\*)/.exec(rest.slice(1));
  return end ? rest.slice(0, end.index + 1) : rest;
}

const ctx = {};
vm.runInNewContext(grab("monSourceLabel") + "\nthis.monSourceLabel = monSourceLabel;", ctx);

test("label comes from the tracker provider", () => {
  assert.equal(ctx.monSourceLabel({ provider: "yb", label: "YB" }), "YB");
  assert.equal(ctx.monSourceLabel({ provider: "file", label: "fleet file" }), "fleet file");
  assert.equal(ctx.monSourceLabel({ provider: "tractrac", label: "tractrac" }), "tractrac");
  assert.equal(ctx.monSourceLabel({ provider: "sample" }), "demo");
  assert.equal(ctx.monSourceLabel({}), "tracker");
  assert.equal(ctx.monSourceLabel(null), "tracker");
});

test("the positions card no longer hard-codes the public tracker", () => {
  assert.ok(!src.includes("— from the public tracker</span>"));
  assert.ok(src.includes('file, not live</span>'));
});
