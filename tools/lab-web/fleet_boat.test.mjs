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
  const f = run(["OWN_FILL_KEYS", "ownFilled", "fillToken", "fillCurrent", "fillOwnFromBoat", "fleetOwnSet"],
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

// pickBoat end to end with every await held open: whichever order the replies come back in, the
// last pick (or typing) wins.
function pickRig() {
  const d = { race_id: "r", own: { boat: "Old", sail: "USA 1", division: "orc" } };
  const held = [];                                    // [{what, id, res}]
  const hold = (what, id, value) => new Promise((res) => held.push({ what, id, res: () => res(value) }));
  const ctx = { Object, activeSec: "fleet", Lab: { editDef: d, ownRev: 0, ownFillGen: 0 },
    Opt: { activeBoat: "old", boatModel: null },
    boatGuard: () => Promise.resolve(true), repaints: 0, syncDirty: () => {},
    _fleetOwnCols: () => ({ rating: null, tod: null }) };
  ctx.repaintBoatPage = () => { ctx.repaints++; };
  ctx.apiPost = (path, body) => path === "/api/boats/active"
    ? hold("active", body.boat_id, {})
    : hold("fill", body.boat_id, { ok: true, json: async () => ({ note: null,
        own: Object.assign({}, body.own, { boat: body.boat_id.toUpperCase(), sail: "" }) }) });
  let reloadFor = null;
  ctx.reloadBoats = () => hold("reload", reloadFor, {});
  const f = run(["OWN_FILL_KEYS", "ownFilled", "fillToken", "fillCurrent", "fillOwnFromBoat", "fleetOwnSet", "pickBoat"], ctx);
  const tick = () => new Promise((r) => setTimeout(r, 0));
  async function release(what, id) {
    const i = held.findIndex((h) => h.what === what && (id == null || h.id === id));
    assert.ok(i >= 0, `${what} ${id} pending`);
    if (what === "active") reloadFor = held[i].id;    // that pick's reloadBoats comes next
    held.splice(i, 1)[0].res(); await tick(); await tick();
  }
  return { d, ctx, f, release, held, tick };
}

test("pick A then B: A's late reload can't start a fill over B", async () => {
  const { d, f, release, tick } = pickRig();
  const pa = f.pickBoat("a"); await tick();
  await release("active", "a");      // A now waits on reloadBoats
  const pb = f.pickBoat("b"); await tick();
  await release("active", "b");
  await release("reload", "b");
  await release("fill", "b"); await pb;
  assert.equal(d.own.boat, "B");
  await release("reload", "a"); await pa;             // A's reload finishes last
  assert.equal(d.own.boat, "B");
});

test("pick A then B: A's fill answering last is dropped", async () => {
  const { d, f, release, tick, held } = pickRig();
  const pa = f.pickBoat("a"); await tick();
  await release("active", "a"); await release("reload", "a");   // A's fill in flight
  const pb = f.pickBoat("b"); await tick();
  await release("active", "b"); await release("reload", "b");
  await release("fill", "b"); await pb;
  assert.equal(d.own.boat, "B");
  if (held.some((h) => h.what === "fill")) await release("fill", "a");
  await pa;
  assert.equal(d.own.boat, "B");
});

test("typing while the active-boat save is pending wins over the pick's fill", async () => {
  const { d, ctx, f, release, tick, held } = pickRig();
  const p = f.pickBoat("a"); await tick();
  f.fleetOwnSet("boat", "Typed");                    // before the active POST answers
  await release("active", "a"); await release("reload", "a");
  assert.ok(!held.some((h) => h.what === "fill"), "no fill sent over typing");
  await p;
  assert.equal(d.own.boat, "Typed");
  assert.equal(ctx.repaints, 1);
});
