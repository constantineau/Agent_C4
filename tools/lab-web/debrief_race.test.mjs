// The Lab Debrief tab must never show one race's debrief under another race's selector (#265
// Codex review). Loads the Debrief section of vps/lab/web/app.js in a VM with a fake fetch whose
// answers arrive in an order the test picks. Lives under tools/ because the Lab image has no node.
// Run: node --test tools/lab-web/debrief_race.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "..", "vps", "lab", "web", "app.js"), "utf8");
const a = src.indexOf("/* ---------- Debrief"), b = src.indexOf("function debTrackScore");
assert.ok(a > 0 && b > a, "Debrief section found in app.js");

function harness() {
  const pending = [];                       // [{url, resolve}]
  const view = { innerHTML: "" };
  const ctx = {
    console, setInterval: () => 1, clearInterval: () => {},
    document: { getElementById: () => view }, location: { hash: "#debrief" },
    esc: String, attr: String, qhelp: () => "", Lab: { races: [{ race_id: "A" }, { race_id: "B" }], sel: "A" },
    stale: () => false,
    apiGet: (url) => new Promise((resolve) => pending.push({ url, resolve })),
  };
  vm.createContext(ctx);
  vm.runInContext(src.slice(a, b).replace("const Deb =", "var Deb =").replace("const Retro =", "var Retro =")
    + "\nthis.Deb = Deb; this.renderDebrief = renderDebrief; this.debPickRace = debPickRace;"
    + "\nthis.debReport = (r) => 'REPORT ' + r.race_name; this.debTrackCard = () => ''; this.retroCard = () => '';", ctx);
  const answer = (pred, body) => {
    const i = pending.findIndex((p) => pred(p.url));
    assert.ok(i >= 0, "a request matching the test is waiting");
    const [p] = pending.splice(i, 1);
    p.resolve({ json: async () => body });
  };
  const flush = () => new Promise((r) => setTimeout(r, 0));
  return { ctx, pending, answer, flush };
}

const recs = (race, id) => ({ recordings: [{ recording: 1, debrief: { id, created_at: 100 } }], agent: null, race });
const reply = (url, race) => (url.startsWith("/api/playbooks") ? { playbooks: [] }
  : url.includes("/recordings") ? recs(race, race === "A" ? 11 : 22)
  : url.startsWith("/api/learning/debriefs/") ? { id: Number(url.split("/").pop()), race_name: url.endsWith("11") ? "A" : "B", report: {} }
  : url.startsWith("/api/retro/races") ? { races: [] } : { state: "idle" });

// Answer waiting requests that match `pick`, until none is left.
async function drain(h, pick, raceOf) {
  for (let n = 0; n < 60; n++) {
    const p = h.pending.find((q) => pick(q.url));
    if (!p) return;
    h.answer((u) => u === p.url, reply(p.url, raceOf(p.url)));
    await h.flush();
  }
}

for (const order of ["B first, A late", "B's list, then all of A, then the rest of B"]) {
  test(`a late answer for race A never lands under race B (${order})`, async () => {
    const h = harness();
    const isA = (u) => u.includes("race_id=A") || u.endsWith("/debriefs/11");
    const raceOf = (u) => (isA(u) ? "A" : "B");
    h.ctx.renderDebrief();                   // render for A starts
    await h.flush();
    // let A get as far as asking for its recordings list
    await drain(h, (u) => !u.includes("/recordings"), raceOf);
    assert.ok(h.pending.some((p) => p.url.includes("/recordings?race_id=A")), "A's list request is out");
    h.ctx.debPickRace("B");                  // the user picks B before A's answers arrive
    await h.flush();
    if (order.startsWith("B first")) {
      await drain(h, (u) => !isA(u), raceOf);
    } else {
      await drain(h, (u) => u.startsWith("/api/playbooks") || (u.includes("race_id=B")), raceOf);
    }
    await drain(h, isA, raceOf);
    await drain(h, () => true, raceOf);
    assert.equal(h.ctx.Deb.raceId, "B");
    assert.equal(h.ctx.Deb.recordings[0].debrief.id, 22, "B's recordings are kept");
    assert.notEqual(h.ctx.Deb.viewing, 11, "A's debrief is not the one on screen");
    assert.ok(!h.ctx.Deb.report || h.ctx.Deb.report.race_name === "B", "no report from A");
  });
}

test("a debrief with no recording left still opens (G65)", async () => {
  const h = harness();
  h.ctx.renderDebrief();
  for (let n = 0; n < 40; n++) {
    await h.flush();
    if (!h.pending.length) break;
    const p = h.pending[0];
    const body = p.url.includes("/recordings") ? { recordings: [], debriefs: [{ id: 11, created_at: 50 }] } : reply(p.url, "A");
    h.answer((u) => u === p.url, body);
  }
  assert.equal(h.ctx.Deb.viewing, 11);
});

test("the newest debrief opens on load (G65)", async () => {
  const h = harness();
  h.ctx.renderDebrief();
  // three debriefs, the newest in the middle of both lists
  const list = { recordings: [{ recording: 1, debrief: { id: 5, created_at: 100 } },
                              { recording: 2, debrief: { id: 11, created_at: 300 } },
                              { recording: 3, debrief: { id: 7, created_at: 200 } }],
                 debriefs: [{ id: 5, created_at: 100 }, { id: 11, created_at: 300 }, { id: 7, created_at: 200 }] };
  for (let n = 0; n < 40; n++) {
    await h.flush();
    if (!h.pending.length) break;
    const p = h.pending[0];
    h.answer((u) => u === p.url, p.url.includes("/recordings") ? list : reply(p.url, "A"));
  }
  assert.equal(h.ctx.Deb.viewing, 11);
  assert.equal(h.ctx.Deb.report && h.ctx.Deb.report.race_name, "A");
});
