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
  assert.deepEqual({ ...ctx.manTarget({ available: true, race_id: "r1", recording: { start_ts: 1790000000 } }, {}) },
    { raceId: "r1", recording: 1790000000 });
  assert.deepEqual({ ...ctx.manTarget({ available: true, no_race: true, recording: { start_ts: 5 } }, {}) },
    { raceId: null, recording: 5 });
  assert.deepEqual({ ...ctx.manTarget(null, { raceId: "r1", track: { available: true } }) },
    { raceId: "r1", recording: null });
  assert.equal(ctx.manTarget(null, { raceId: "r1", track: { available: false } }), null);
});

test("loads once, fills the card, and announces the answer for other views", async () => {
  const { ctx, calls, events, el } = load();
  ctx.answer = ANSWER;
  const html = ctx.manCard({ available: true, race_id: "r 1", recording: { start_ts: 1790000000 } }, {});
  assert.match(html, /Looking for tacks/);
  el.dataset.key = "r 1|1790000000";
  ctx.manCard({ available: true, race_id: "r 1", recording: { start_ts: 1790000000 } }, {});
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
