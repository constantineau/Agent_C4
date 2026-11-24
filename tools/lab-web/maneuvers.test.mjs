// The Debrief "Tacks and gybes" card (#375, vps/lab/web/maneuvers.js): which stored track it asks
// for, and what it shows. Loads the module in a VM with stubbed app.js helpers.
// Run: node --test tools/lab-web/maneuvers.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "maneuvers.js"), "utf8");

function load() {
  const calls = [];
  const events = [];
  const el = { dataset: {}, innerHTML: "" };
  const ctx = {
    console,
    esc: (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])),
    qhelp: () => "",
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    document: { getElementById: () => el, dispatchEvent: (e) => events.push(e) },
    apiGet: async (url) => { calls.push(url); return { json: async () => ctx.answer }; },
    answer: null,
  };
  vm.createContext(ctx);
  vm.runInContext(src.replace("const ManData =", "var ManData =")
    + "\nthis.manCard = manCard; this.manTarget = manTarget; this.manBody = manBody; this.ManData = ManData;", ctx);
  ctx.keyOf = (html) => /data-key="([^"]*)"/.exec(html)[1];
  return { ctx, calls, events, el };
}

const M1 = { id: 0, type: "tack", t: 1790000304, from_side: "starboard", to_side: "port", loss_s: 6, loss_m: 15,
  entry_kn: 6.5, min_kn: 3.6, exit_kn: 3.6, recover_s: 24, config: "J1+M", counted: true, flags: [] };
const M2 = { ...M1, id: 1, from_side: "port", to_side: "starboard", loss_s: null, loss_m: null, counted: false,
  flags: ["gap in the log"] };
const ANSWER = { available: true, basis: "instruments", confidence: "high", note: null, maneuvers: [M1, M2],
  summary: { found: 2, counted: 1, tack: { n: 1, loss_s: 6, loss_m: 15, recover_s: 24, entry_kn: 6.5, min_kn: 3.6, exit_kn: 3.6 },
    gybe: { n: 0 }, groups: [{ type: "tack", by: "side", key: "port", n: 1, loss_s: 6, loss_m: 15 },
      { type: "tack", by: "config", key: "J1+M", n: 1, loss_s: 6, loss_m: 15 }] } };

test("asks for the open debrief's recording, a practice sail's, or the loaded GPX track", () => {
  const { ctx } = load();
  const pick = (tg) => ({ raceId: tg.raceId, recording: tg.recording });
  assert.deepEqual(pick(ctx.manTarget({ available: true, race_id: "r1", recording: { start_ts: 1790000000 } }, {})),
    { raceId: "r1", recording: 1790000000 });
  assert.deepEqual(pick(ctx.manTarget({ available: true, no_race: true, recording: { start_ts: 5 } }, {})),
    { raceId: null, recording: 5 });
  assert.deepEqual(pick(ctx.manTarget(null, { raceId: "r1", track: { available: true } })),
    { raceId: "r1", recording: null });
  assert.equal(ctx.manTarget(null, { raceId: "r1", track: { available: false } }), null);
});

test("loads once, fills the card, and announces the answer for other views", async () => {
  const { ctx, calls, events, el } = load();
  ctx.answer = ANSWER;
  const rep = { available: true, race_id: "r 1", recording: { start_ts: 1790000000 } };
  const html = ctx.manCard(rep, {});
  assert.match(html, /Looking for tacks/);
  el.dataset.key = ctx.keyOf(html);
  ctx.manCard(rep, {});
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.length, 1);
  assert.equal(calls[0], "/api/debrief/maneuvers?race_id=r%201&recording=1790000000");
  assert.equal(events.length, 1);
  assert.equal(events[0].type, "c4:maneuvers");
  assert.equal(events[0].detail.data.maneuvers.length, 2);
  assert.match(el.innerHTML, /Tacks/);
  assert.match(el.innerHTML, /onto port/);
  assert.match(el.innerHTML, /J1\+M/);
  assert.match(el.innerHTML, /1 of 2 not in the averages/);
  assert.match(el.innerHTML, /gap in the log/);
  assert.match(el.innerHTML, /S→P/);
});

test("GPS-only answers carry the warning; empty and failed answers say so", () => {
  const { ctx } = load();
  const gps = ctx.manBody({ ...ANSWER, basis: "gps", note: "No wind instruments: wind direction guessed from the track." });
  assert.match(gps, /banner warn/);
  assert.match(gps, /guessed from the track/);
  assert.match(ctx.manBody({ available: true, maneuvers: [], summary: {} }), /No tacks or gybes found/);
  assert.match(ctx.manBody({ available: false, note: "Fixes are 120 s apart." }), /120 s apart/);
});

test("a replaced track or a new debrief asks again; a failure is not kept", async () => {
  const { ctx, calls } = load();
  ctx.answer = ANSWER;
  const deb = { raceId: "r1", track: { available: true, source: "gpx", n: 100 } };
  ctx.manCard(null, deb);
  await new Promise((r) => setTimeout(r, 0));
  ctx.manCard(null, deb);                                  // same track: cached
  assert.equal(calls.length, 1);
  deb.track = { available: true, source: "gpx", n: 250 };  // a new GPX uploaded
  ctx.manCard(null, deb);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.length, 2);
  deb.track = { available: false };                        // removed: nothing to show
  assert.equal(ctx.manCard(null, deb), "");
  const rec = { start_ts: 1790000000 };
  ctx.manCard({ available: true, race_id: "r1", recording: rec }, deb);
  await new Promise((r) => setTimeout(r, 0));
  ctx.manCard({ available: true, race_id: "r1", recording: rec }, deb);   // the debrief re-run
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.length, 4);
  // a failed load shows a message and is retried on the next paint
  ctx.apiGet = async (url) => { calls.push(url); throw new Error("down"); };
  const rep = { available: true, race_id: "r2", recording: rec };
  ctx.manCard(rep, deb);
  await new Promise((r) => setTimeout(r, 0));
  assert.match(ctx.manCard(rep, deb), /Looking for tacks/);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.length, 6);
});

test("a GPX replaced by another with the same fix count still asks again", async () => {
  const { ctx, calls } = load();
  ctx.answer = ANSWER;
  const deb = { raceId: "r1", track: { available: true, source: "gpx", n: 3, fixes: [[26.1, -80.0], [26.2, -80.0], [26.3, -80.0]] } };
  ctx.manCard(null, deb);
  await new Promise((r) => setTimeout(r, 0));
  deb.track = { ...deb.track, fixes: [[26.1, -80.0], [26.2, -80.0], [26.3, -80.0]] };   // same content, new object
  ctx.manCard(null, deb);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.length, 1);
  deb.track = { ...deb.track, fixes: [[44.1, -82.0], [44.2, -82.1], [44.3, -82.2]] };   // a different GPX, same n
  ctx.manCard(null, deb);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.length, 2);
});

test("the server's track revision (times, instruments) keys the cache; manForget clears it", async () => {
  const { ctx, calls } = load();
  vm.runInContext("this.manForget = manForget;", ctx);
  ctx.answer = ANSWER;
  const pts = [[26.1, -80.0], [26.2, -80.0]];
  const deb = { raceId: "r1", track: { available: true, source: "gpx", n: 2, fixes: pts, revision: "aaa" } };
  ctx.manCard(null, deb);
  await new Promise((r) => setTimeout(r, 0));
  deb.track = { ...deb.track, revision: "bbb" };            // same positions, new times
  ctx.manCard(null, deb);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.length, 2);
  ctx.manCard(null, deb);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.length, 2);
  ctx.manForget();                                         // after an upload/fetch/clear
  ctx.manCard(null, deb);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(calls.length, 3);
});

test("the Debrief tab clears the card's cache after a track upload, fetch or removal", () => {
  const app = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "app.js"), "utf8");
  for (const fn of ["debUploadGpx", "debFetchYb", "debClearTrack"]) {
    const a = app.indexOf("async function " + fn + "(");
    const body = app.slice(a, app.indexOf("\n}\n", a));
    assert.match(body, /manForget\(\)/, fn);
  }
});

test("measured but unchecked turns say why none are averaged", () => {
  const { ctx } = load();
  const m = { ...M1, counted: false, trusted: false, flags: ["instruments not checked"] };
  const html = ctx.manBody({ ...ANSWER, trust_checked: false, maneuvers: [m],
    summary: { found: 1, counted: 0, tack: { n: 0 }, gybe: { n: 0 }, groups: [] } });
  assert.doesNotMatch(html, /could be measured/);
  assert.match(html, /instrument check didn't run/);
  const none = ctx.manBody({ ...ANSWER, maneuvers: [M2], summary: { found: 1, counted: 0, tack: { n: 0 }, gybe: { n: 0 }, groups: [] } });
  assert.match(none, /None of these turns could be measured/);
});
