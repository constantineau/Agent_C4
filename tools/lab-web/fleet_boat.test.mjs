// One boat in two places: Gameplan's picker and Fleet's "Our boat" share one picker, and Fleet says
// so when its race's boat isn't the Lab's active one. Run: node --test tools/lab-web/fleet_boat.test.mjs
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
const BOATS = [{ boat_id: "hooligan", name: "Hooligan", sail_number: "USA 52521", draft_m: 3.2 },
               { boat_id: "other", name: "Other", sail_number: "", draft_m: 2 }];

test("same boat: sail numbers when both have one, spacing ignored; else names", () => {
  const f = run(["sameOwnBoat"], {});
  assert.ok(f.sameOwnBoat({ boat: "x", sail: "usa-52521" }, BOATS[0]));
  assert.ok(!f.sameOwnBoat({ boat: "Hooligan", sail: "USA 1" }, BOATS[0]));
  assert.ok(f.sameOwnBoat({ boat: "other", sail: "CAN 9" }, BOATS[1]));
  assert.ok(f.sameOwnBoat({ boat: "Hooligan" }, BOATS[0]));
  assert.ok(!f.sameOwnBoat({}, BOATS[0]));
});

test("mismatch: the active boat when the race has another (or none), else null", () => {
  const f = run(["sameOwnBoat", "ownMismatch"], {});
  assert.equal(f.ownMismatch({ boat: "Old", sail: "USA 1" }, BOATS, "hooligan").boat_id, "hooligan");
  assert.equal(f.ownMismatch({}, BOATS, "hooligan").boat_id, "hooligan");
  assert.equal(f.ownMismatch({ boat: "Hooligan", sail: "USA52521" }, BOATS, "hooligan"), null);
  assert.equal(f.ownMismatch({ boat: "Old" }, BOATS, null), null);
  assert.equal(f.ownMismatch({ boat: "Old" }, null, "hooligan"), null);
});

test("fill: the server's own replaces the race's; a bad reply keeps it", () => {
  const f = run(["ownFilled"], {});
  const own = { boat: "Old", division: "orc", rating: 1.01 };
  const filled = { boat: "Hooligan", sail: "USA 52521", division: "orc", orc_gph: 438.9, rating: 1.3889 };
  const out = f.ownFilled(own, filled);
  assert.deepEqual({ ...out }, filled);
  assert.notEqual(out, filled);
  assert.deepEqual({ ...f.ownFilled(own, null) }, own);
});

test("Fleet line: names the active boat with a Use it here button; nothing when they match", () => {
  const ctx = { esc, Opt: { boats: BOATS, activeBoat: "hooligan" } };
  const f = run(["sameOwnBoat", "ownMismatch", "fleetBoatLine"], ctx);
  const h = f.fleetBoatLine({ boat: "Old", sail: "USA 1" });
  assert.ok(h.includes("Gameplan uses <b>Hooligan</b>.") && h.includes("Use it here"));
  assert.equal(f.fleetBoatLine({ boat: "Hooligan", sail: "USA 52521" }), "");
  ctx.Opt.boats = [{ ...BOATS[0], name: "<i>x</i>" }];
  assert.ok(f.fleetBoatLine({}).includes("&lt;i&gt;x"));
});

test("one picker for both pages: same list, the active boat selected, New boat beside it", () => {
  const ctx = { esc, Opt: { boats: BOATS, activeBoat: "hooligan", depthUnit: "ft" } };
  const f = run(["depthUnit", "depthVal", "depthTxt", "boatPickerHtml"], ctx);
  const h = f.boatPickerHtml("fleetBoat");
  assert.ok(h.includes('id="fleetBoat"') && h.includes('onchange="pickBoat(this.value)"'));
  assert.ok(/value="hooligan" selected/.test(h) && !/value="other" selected/.test(h));
  assert.ok(h.includes("newBoatFromCert()"));
  assert.ok(src.includes('${boatPickerHtml("optBoat")}') && src.includes('${boatPickerHtml("fleetBoat")}'));
});
