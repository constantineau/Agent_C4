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
  const ctx = { Lab: { editDef: d, ownRev: 0, ownFillGen: 0 }, Object, activeSec: "fleet",
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
    ? (ctx.posts.push(body.boat_id), hold("active", body.boat_id, ctx.failActive ? { ok: false } : { ok: true }))
    : hold("fill", body.boat_id, { ok: true, json: async () => ({ note: null,
        own: Object.assign({}, body.own, { boat: body.boat_id.toUpperCase(), sail: "" }) }) });
  let reloadFor = null;
  // the REAL reloadBoats: its GET is held, and answers with the active boat as of that pick
  ctx.apiGet = (path) => {
    const id = reloadFor;
    return path === "/api/boats"
      ? hold("reload", id, { json: async () => ({ boats: BOATS, active: id, chart_source: "enc" }) })
      : hold("model", id, { json: async () => ({ boat: id }) });
  };
  ctx.bmLoaded = (m) => { ctx.model = m.boat; };
  ctx.posts = [];
  const f = run(["OWN_FILL_KEYS", "ownFilled", "fillToken", "fillCurrent", "boatWrite", "fillOwnFromBoat",
                 "fleetOwnSet", "reloadBoats", "pickBoat"], ctx);
  const tick = () => new Promise((r) => setTimeout(r, 0));
  async function release(what, id) {
    const i = held.findIndex((h) => h.what === what && (id == null || h.id === id));
    assert.ok(i >= 0, `${what} ${id} pending`);
    if (what === "active") reloadFor = held[i].id;   // that pick's GETs come next
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

test("pick A then B: A's late boat list and model never replace B's", async () => {
  const { ctx, f, release, tick } = pickRig();
  ctx.Opt.boatModel = { boat: "old" }; ctx.Opt.showBoatModel = true;
  const pa = f.pickBoat("a"); await tick();
  await release("active", "a");                       // A's GET /api/boats now held (says active: a)
  const pb = f.pickBoat("b"); await tick();
  await release("active", "b"); await release("reload", "b"); await release("model", "b");
  await release("fill", "b"); await pb;
  assert.equal(ctx.Opt.activeBoat, "b"); assert.equal(ctx.model, "b");
  await release("reload", "a"); await pa;             // A's list answers last, saying active: a
  assert.equal(ctx.Opt.activeBoat, "b");
});

test("pick A then B: A's late model reply never replaces B's", async () => {
  const { ctx, f, release, tick } = pickRig();
  ctx.Opt.boatModel = { boat: "old" }; ctx.Opt.showBoatModel = true;
  const pa = f.pickBoat("a"); await tick();
  await release("active", "a"); await release("reload", "a");   // A's /api/crossovers held
  const pb = f.pickBoat("b"); await tick();
  await release("active", "b"); await release("reload", "b"); await release("model", "b");
  await release("fill", "b"); await pb;
  await release("model", "a"); await pa;
  assert.equal(ctx.model, "b"); assert.equal(ctx.Opt.activeBoat, "b");
});

test("active-boat writes go in pick order; an overtaken pick skips its write", async () => {
  const { ctx, f, release, tick } = pickRig();
  const pa = f.pickBoat("a"); await tick();
  const pb = f.pickBoat("b"), pc = f.pickBoat("c"); await tick();
  assert.deepEqual([...ctx.posts], ["a"]);            // B and C wait for A's write
  await release("active", "a"); await tick();
  assert.deepEqual([...ctx.posts], ["a", "c"]);       // B was overtaken by C before its turn
  await release("active", "c"); await release("reload", "c"); await release("fill", "c");
  await Promise.all([pa, pb, pc]);
  assert.equal(ctx.Opt.activeBoat, "c");
});

test("left the tab mid-pick: no repaint of the old tab, no fill of the race", async () => {
  const { d, ctx, f, release, tick, held } = pickRig();
  const p = f.pickBoat("a"); await tick();
  await release("active", "a");
  ctx.activeSec = "races";                            // navigated away while the list loads
  await release("reload", "a"); await p;
  assert.ok(!held.some((h) => h.what === "fill"), "no fill sent");
  assert.equal(d.own.boat, "Old"); assert.equal(ctx.repaints, 0);
  // and a fill already in flight is dropped when its tab is left
  ctx.activeSec = "fleet";
  const p2 = f.pickBoat("b"); await tick();
  await release("active", "b"); await release("reload", "b");
  ctx.activeSec = "races";
  await release("fill", "b"); await p2;
  assert.equal(d.own.boat, "Old"); assert.equal(ctx.repaints, 0);
});

test("a Gameplan pick repaints Gameplan only while it is still open", async () => {
  const { ctx, f, release, tick } = pickRig();
  ctx.activeSec = "gameplan";
  let p = f.pickBoat("a"); await tick();
  await release("active", "a"); await release("reload", "a"); await p;
  assert.equal(ctx.repaints, 1); assert.equal(ctx.Lab.ownFill, "a");
  p = f.pickBoat("b"); await tick();
  await release("active", "b"); ctx.activeSec = "races";
  await release("reload", "b"); await p;
  assert.equal(ctx.repaints, 1);
});

test("the server refuses the switch: no fill, the race keeps its boat, and it says so", async () => {
  const { d, ctx, f, release, tick, held } = pickRig();
  ctx.failActive = true; ctx.alerts = []; ctx.alert = (m) => ctx.alerts.push(m);
  const p = f.pickBoat("a"); await tick();
  await release("active", "a");
  await release("reload", "a");
  await p;
  assert.ok(!held.some((h) => h.what === "fill"), "no fill after a refused switch");
  assert.equal(d.own.boat, "Old");
  assert.equal(ctx.alerts.length, 1);
});
