// Race weather stations (#309): the Races "Weather stations" card, its saves, and the maps.
// Pulls the named top-level helpers out of vps/lab/web/app.js and runs them in a VM with a tiny
// fake DOM and a stubbed API. Lives under tools/ because the Lab image has no node.
// Run: node --test tools/lab-web/race_stations.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.join(here, "..", "..", "vps", "lab", "web");
const src = fs.readFileSync(path.join(web, "app.js"), "utf8");
const mapview = fs.readFileSync(path.join(web, "mapview.js"), "utf8");

function grab(name) {
  const m = new RegExp(`\\n((?:async )?function ${name}\\(|const ${name} |let ${name} )`).exec(src);
  assert.ok(m, `${name} found in app.js`);
  const rest = src.slice(m.index + 1);
  const end = /\n(?=(?:async )?function |const |let |var |window\.|document\.|\/\/|\/\*|\[)/.exec(rest.slice(1));
  return end ? rest.slice(0, end.index + 1) : rest;
}

const FOUND = {
  stations: [
    { id: "KFXE", kind: "metar", name: "Ft Lauderdale Exec", lat: 26.2, lon: -80.18, dist_nm: 6.9, source: "metar", auto: true },
    { id: "LKWF1", kind: "ndbc", name: "Lake <b>Worth</b>", lat: 26.6, lon: -80.03, dist_nm: 24.8, source: "ndbc", auto: true },
    { id: "KPBI", kind: "metar", name: "West Palm", lat: 26.7, lon: -80.1, dist_nm: 29.1, source: "metar", auto: false },
    { id: "62103", kind: "ukbuoy", name: "Lightship", lat: 49.9, lon: -2.9, dist_nm: 40, source: "ukbuoy", auto: false },
  ],
  notes: ["NDBC buoys: none near the course."],
};

function harness(def, opts = {}) {
  const els = { stCard: { innerHTML: "" }, stMap: {}, stAddId: { value: "" } };
  const posts = [], maps = [];
  const ctx = {
    console, Promise, JSON, Object, Set, String, syncDirty: () => {}, activeSec: "races",
    Lab: { editDef: def, isDraft: !!opts.draft, cleanDef: JSON.stringify(def) },
    window: {},
    document: { getElementById: (id) => els[id] || null },
    drawCourseMap: (id, course, stations, fit) => maps.push({ id, course, stations, fit }),
    jsonOrFriendly: async (res) => res.json(),
    apiPost: async (p, body) => {
      posts.push({ p, body: JSON.parse(JSON.stringify(body)) });
      if (opts.gate) await opts.gate(p);
      const reply = opts.reply ? opts.reply(p, body) : {};
      return { ok: reply.status ? reply.status < 400 : true, status: reply.status || 200, json: async () => reply.json || reply };
    },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  const names = ["esc", "attr", "saveChain", "queueDef", "withOverrides", "St", "stKey", "stationPicks", "stKind",
    "stReset", "detailStationsCard", "stCardHtml", "stPaint", "stDrawMap", "stBase", "stFind", "stTick", "stAdd",
    "stCheck", "stSave", "syncStations", "stDirty", "stDiscard", "stTickSig"];
  const code = names.map(grab).join("\n").replace(/^(const|let) /gm, "var ");
  vm.runInContext(code, ctx);
  // a top-level const, as in app.js: NOT a property of window (Codex on #346)
  ctx.__def = def;
  vm.runInContext("const Opt = { def: JSON.parse(JSON.stringify(__def)) };", ctx);
  return { ctx, els, posts, maps };
}

const RACE = () => ({ race_id: "fll", name: "FLL", courses: [{ id: "c1", marks: [{ name: "A", lat: 26.3, lon: -80 }] }] });

test("a draft can't pick stations yet", () => {
  const { ctx } = harness(RACE(), { draft: true });
  assert.match(ctx.detailStationsCard(ctx.Lab.editDef), /Save the race first\./);
});

test("automatic race: Find lists stations with the automatic picks ticked, map shows them", async () => {
  const { ctx, els, posts, maps } = harness(RACE(), { reply: () => Object.assign({ picked: null }, FOUND) });
  const html = ctx.detailStationsCard(ctx.Lab.editDef);
  assert.match(html, /Automatic: the stations near the course\./);
  assert.ok(!html.includes("Use automatic"));
  els.stCard.innerHTML = html;
  await ctx.stFind();
  assert.equal(posts[0].p, "/api/races/fll/stations/find");
  const h = els.stCard.innerHTML;
  assert.equal((h.match(/type="checkbox" checked/g) || []).length, 2, "the two auto picks ticked");
  assert.match(h, /Airport/); assert.match(h, /Buoy \/ coast/);
  assert.match(h, /ukbuoy/, "a new kind shows by name, no UI change");
  assert.ok(h.includes("Lake &lt;b&gt;Worth&lt;/b&gt;") && !h.includes("<b>Worth"), "names escaped");
  assert.match(h, /NDBC buoys: none near the course\./);
  assert.match(h, /Save picks<\/button>/);
  assert.match(h, /onclick="stSave\(false\)" disabled/, "nothing to save until a tick changes");
  const m = maps.at(-1);
  assert.equal(m.id, "stMap"); assert.equal(m.fit, true);
  assert.deepEqual(m.stations.map((s) => s.id + ":" + s.on), ["KFXE:true", "LKWF1:true", "KPBI:false", "62103:false"]);
});

test("tick, save: only the ticked rows go up, and the saved picks don't read as an unsaved edit", async () => {
  const def = RACE();
  const { ctx, els, posts } = harness(def, { reply: (p, body) => (p.endsWith("/find") ? Object.assign({ picked: null }, FOUND)
    : { saved: true, stations: { picked: body.picked } }) });
  els.stCard.innerHTML = ctx.detailStationsCard(def);
  await ctx.stFind();
  ctx.stTick(1, false);   // LKWF1 off
  ctx.stTick(2, true);    // KPBI on
  assert.match(els.stCard.innerHTML, /Not saved yet\./);
  assert.ok(!/onclick="stSave\(false\)" disabled/.test(els.stCard.innerHTML));
  await ctx.stSave(false);
  const sent = posts.at(-1);
  assert.equal(sent.p, "/api/races/fll/stations");
  assert.deepEqual(sent.body.picked.map((s) => s.id), ["KFXE", "KPBI"]);
  assert.deepEqual(Object.keys(sent.body.picked[0]).sort(), ["id", "kind", "lat", "lon", "name", "source"]);
  assert.deepEqual(def.stations.picked.map((s) => s.id), ["KFXE", "KPBI"]);
  assert.equal(JSON.parse(ctx.Lab.cleanDef).stations.picked.length, 2, "clean baseline has them too");
  assert.equal(vm.runInContext("Opt.def", ctx).stations.picked.length, 2);
  assert.match(els.stCard.innerHTML, /Saved\. Build the playbook again/);
  assert.match(els.stCard.innerHTML, /Your picks \(2\)\./);
  assert.match(els.stCard.innerHTML, /Use automatic/);
});

test("a race with picks: shown ticked before any search; Find keeps them; Use automatic clears", async () => {
  const def = Object.assign(RACE(), { stations: { picked: [
    { id: "KEYW", kind: "metar", lat: 24.5, lon: -81.7, name: "Key West" },
    { id: "KPBI", kind: "metar", lat: 26.7, lon: -80.1, name: "West Palm" }] } });
  const { ctx, els, posts } = harness(def, { reply: (p, body) => (p.endsWith("/find") ? Object.assign({ picked: def.stations.picked }, FOUND)
    : { saved: true, stations: body.picked === null ? null : { picked: body.picked } }) });
  els.stCard.innerHTML = ctx.detailStationsCard(def);
  assert.match(els.stCard.innerHTML, /Your picks \(2\)\./);
  assert.equal((els.stCard.innerHTML.match(/type="checkbox" checked/g) || []).length, 2);
  await ctx.stFind();
  const ids = ctx.St.rows.map((s) => s.id);
  assert.ok(ids.includes("KEYW"), "a saved pick the search didn't list is kept");
  assert.deepEqual(Object.keys(ctx.St.ticked).filter((k) => ctx.St.ticked[k]).sort(), ["metar:KEYW", "metar:KPBI"],
    "ticks = the saved picks, not the automatic ones");
  await ctx.stSave(true);
  assert.equal(posts.at(-1).body.picked, null);
  assert.ok(!("stations" in def));
  assert.ok(!("stations" in JSON.parse(ctx.Lab.cleanDef)));
  assert.match(els.stCard.innerHTML, /Back to automatic\./);
  assert.deepEqual(Object.keys(ctx.St.ticked).filter((k) => ctx.St.ticked[k]).sort(), ["metar:KFXE", "ndbc:LKWF1"]);
});

test("add by id: found → listed and ticked; unknown → says so", async () => {
  const { ctx, els } = harness(RACE(), { reply: (p, body) => (String(body.id).toUpperCase() === "KEYW"
    ? { station: { id: "KEYW", kind: "metar", lat: 24.5, lon: -81.7, name: "Key West", dist_nm: 129.3 } }
    : { status: 404, json: { detail: "No station with that id." } }) });
  els.stCard.innerHTML = ctx.detailStationsCard(ctx.Lab.editDef);
  els.stAddId.value = "keyw";
  await ctx.stAdd();
  assert.match(els.stCard.innerHTML, /Added KEYW \(129\.3 nm\)\. Not saved yet\./);
  assert.ok(ctx.St.ticked["metar:KEYW"] && ctx.St.changed);
  els.stAddId.value = "zzzz";
  await ctx.stAdd();
  assert.match(els.stCard.innerHTML, /No station with that id\./);
});

test("check live marks each row", async () => {
  const { ctx, els } = harness(RACE(), { reply: (p) => (p.endsWith("/find") ? Object.assign({ picked: null }, FOUND)
    : { live: { KFXE: true, LKWF1: false } }) });
  els.stCard.innerHTML = ctx.detailStationsCard(ctx.Lab.editDef);
  await ctx.stFind();
  await ctx.stCheck();
  assert.match(els.stCard.innerHTML, /wind now/);
  assert.match(els.stCard.innerHTML, /no wind/);
});

test("a save sent before the picks landed keeps the server's picks in its baseline", () => {
  const def = Object.assign(RACE(), { stations: { picked: [{ id: "KFXE", kind: "metar", lat: 1, lon: 1, name: "x" }] } });
  const { ctx } = harness(def);
  const old = JSON.stringify(RACE());
  assert.deepEqual(JSON.parse(ctx.withOverrides(old)).stations, def.stations);
});

test("maps: Course & Marks and the Gameplan route map draw the race's picks", () => {
  assert.match(src, /drawCourseMap\("map" \+ i, c, stationPicks\(d\)\)/);
  assert.match(src, /MapView\.render\("optMap", r, \{ stations: stationPicks\(stDef\) \|\| \[\] \}\)/);
  assert.match(grab("drawCourseMap"), /cm-station/);
  assert.match(mapview, /function buildStations\(\)/);
  assert.match(mapview, /className: "mv-station"/);
  assert.match(mapview, /chk\("stations", "Stations"\)/);
});

test("a tick changed while the save runs is not lost: controls are off, and a late change stays unsaved", async () => {
  const def = RACE();
  let release;
  const gate = (p) => (p.endsWith("/stations") ? new Promise((r) => { release = r; }) : null);
  const { ctx, els, posts } = harness(def, { gate, reply: (p, body) => (p.endsWith("/find") ? Object.assign({ picked: null }, FOUND)
    : { saved: true, stations: { picked: body.picked } }) });
  els.stCard.innerHTML = ctx.detailStationsCard(def);
  await ctx.stFind();
  ctx.stTick(2, true);                       // KPBI on
  const saving = ctx.stSave(false);
  await new Promise((r) => setTimeout(r, 0));
  assert.match(els.stCard.innerHTML, /type="checkbox" checked disabled|type="checkbox"  disabled/, "boxes disabled while saving");
  ctx.stTick(0, false);                      // ignored while busy
  assert.ok(ctx.St.ticked["metar:KFXE"]);
  // even if a change slips in (a stale handler), it stays unsaved
  ctx.St.ticked["metar:KFXE"] = false;
  release();
  assert.equal(await saving, true);
  assert.deepEqual(posts.at(-1).body.picked.map((s) => s.id), ["KFXE", "LKWF1", "KPBI"]);
  assert.equal(ctx.St.changed, true);
  assert.ok(ctx.stDirty());
  assert.match(els.stCard.innerHTML, /Changed while saving\. Not saved yet\./);
});

test("unsaved ticks are a dirty source on Races: in the Save bar, the leave prompt, Save and Discard", async () => {
  assert.match(grab("DIRTY_SOURCES"), /tabs: \["races"\], dirty: \(\) => stDirty\(\)[^]*save: \(\) => stSave\(false\), discard: \(\) => stDiscard\(\)/);
  const def = RACE();
  const { ctx, els } = harness(def, { reply: (p, body) => (p.endsWith("/find") ? Object.assign({ picked: null }, FOUND)
    : { saved: true, stations: { picked: body.picked } }) });
  els.stCard.innerHTML = ctx.detailStationsCard(def);
  await ctx.stFind();
  assert.equal(ctx.stDirty(), false, "a search alone is not an edit");
  ctx.stTick(1, false);
  assert.equal(ctx.stDirty(), true);
  ctx.stDiscard();
  assert.equal(ctx.stDirty(), false);
  assert.ok(ctx.St.ticked["ndbc:LKWF1"], "discard puts the automatic ticks back");
  ctx.stTick(1, false);
  assert.equal(await ctx.stSave(false), true, "the bar's Save reports success");
  assert.equal(ctx.stDirty(), false);
  ctx.Lab.editDef = Object.assign(RACE(), { race_id: "other" });
  ctx.St.changed = true;
  assert.equal(ctx.stDirty(), false, "only the open race's ticks count");
});
