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

test("fill: only the fields a fill owns; division as on screen; MMSI only from the profile", () => {
  const f = run(["OWN_FILL_KEYS", "ownFilled"], {});
  const sent = { boat: "Old", division: "orc", rating: 1.01, rating_col: "manual", mmsi: "367000000" };
  const onScreen = { ...sent, division: "dh" };           // division typed while the fill ran
  const filled = { boat: "Hooligan", sail: "USA 52521", division: "orc", mmsi: "367000000",
                   orc_gph: 438.9, rating: 1.3889, rating_col: "TMF_Offshore" };
  const out = f.ownFilled(onScreen, filled, sent);
  assert.deepEqual({ ...out }, { boat: "Hooligan", sail: "USA 52521", division: "dh", mmsi: "367000000",
                                 orc_gph: 438.9, rating: 1.3889, rating_col: "TMF_Offshore" });
  // a number the reply leaves out is gone; a profile MMSI wins
  const out2 = f.ownFilled(sent, { boat: "New", sail: "", mmsi: "316000001" }, sent);
  assert.ok(!("rating" in out2) && !("rating_col" in out2) && out2.mmsi === "316000001");
  assert.deepEqual({ ...f.ownFilled(sent, null, sent) }, sent);
});

test("a fill reply is dropped after a later fill or typing", async () => {
  const d = { race_id: "r", own: { boat: "Old", division: "orc" } };
  let release;
  const ctx = { Lab: { editDef: d, ownRev: 0, ownFillGen: 0 }, Object,
    apiPost: () => new Promise((res) => { release = res; }) };
  const f = run(["OWN_FILL_KEYS", "ownFilled", "fillCurrent", "fillOwnFromBoat", "fleetOwnSet"],
                Object.assign(ctx, { _fleetOwnCols: () => ({ rating: null, tod: null }) }));
  const reply = { ok: true, json: async () => ({ own: { boat: "Hooligan", sail: "USA 52521", division: "orc" }, note: null }) };
  // typing while the fill runs
  let p = f.fillOwnFromBoat("hooligan");
  f.fleetOwnSet("division", "dh");
  release(reply); await p;
  assert.equal(d.own.boat, "Old"); assert.equal(d.own.division, "dh");
  // a second pick while the first is in flight: only the second lands
  p = f.fillOwnFromBoat("a"); const r1 = release;
  const p2 = f.fillOwnFromBoat("b"); const r2 = release;
  r2({ ok: true, json: async () => ({ own: { boat: "B", sail: "" }, note: null }) }); await p2;
  r1({ ok: true, json: async () => ({ own: { boat: "A", sail: "" }, note: null }) }); await p;
  assert.equal(d.own.boat, "B"); assert.equal(d.own.division, "dh");
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
