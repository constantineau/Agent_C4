// Lab Races, Course and Rules grill fixes (#96 G19 G60, #104 G44 G93 G94 G96, #105 G46 G47 G50).
// Pulls the named top-level helpers out of vps/lab/web/app.js and runs them in a VM with a tiny
// fake DOM; markup-only fixes are checked as text. Lives under tools/ because the Lab image has no node.
// Run: node --test tools/lab-web/races_rules.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.join(here, "..", "..", "vps", "lab", "web");
const src = fs.readFileSync(path.join(web, "app.js"), "utf8");
const css = fs.readFileSync(path.join(web, "styles.css"), "utf8");

// One top-level declaration, up to the next one.
function grab(name) {
  const m = new RegExp(`\\n((?:async )?function ${name}\\(|const ${name} |let ${name} )`).exec(src);
  assert.ok(m, `${name} found in app.js`);
  const rest = src.slice(m.index + 1);
  const end = /\n(?=(?:async )?function |const |let |var |window\.|document\.|\/\/|\/\*|\[)/.exec(rest.slice(1));
  return end ? rest.slice(0, end.index + 1) : rest;
}

function fakeEl(tag) {
  const el = {
    tag, id: "", className: "", hidden: false, innerHTML: "", style: {}, attrs: {}, onclick: null,
    setAttribute(k, v) { this.attrs[k] = v; },
    querySelector(sel) { return sel === "button" ? (this._btn = this._btn || fakeEl("button")) : null; },
  };
  return el;
}

function harness(extra) {
  const byId = {}, listeners = {}, timers = [];
  const ctx = {
    console, Lab: { races: [], sel: null, editDef: null }, activeSec: "",
    setTimeout: (f) => { timers.push(f); return timers.length; }, clearTimeout: () => {},
    requestAnimationFrame: (f) => f(),
    window: { addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); } },
    document: {
      body: { appendChild(el) { byId[el.id] = el; } },
      createElement: (t) => fakeEl(t),
      getElementById: (id) => byId[id] || null,
      querySelectorAll: () => [],
    },
    paints: 0,
  };
  vm.createContext(ctx);
  const names = ["esc", "attr", "ein", "etxt", "growTa", "growAllTa", "undoTimer", "undoToast", "noRaceYet",
    "raceDetailEmpty", "PYS", "pyUnq", "CHECK_WORDS", "CHECK_TAB", "plainCheck", "removeMark",
    "modRemove", "reqAdd", "reqRemove", "rulesUndoHeld", "rulesRemove", ...(extra || [])];
  const code = names.map(grab).join("\n").replace(/^(const|let) /gm, "var ");
  vm.runInContext(code + "\nvar paintCourse = () => { paints++; }, paintRules = () => { paints++; };", ctx);
  return { ctx, byId, listeners, timers };
}

test("noRaceYet links to Races, and replaces the placeholder on Course, Deploy, Fleet and Rules (G60)", () => {
  const { ctx } = harness();
  assert.match(ctx.noRaceYet(), /<a href="#races">/);
  for (const fn of ["renderCourse", "renderDeploy", "renderFleet", "renderRules"]) {
    assert.match(grab(fn), /noRaceYet\(\)/, `${fn} uses noRaceYet()`);
  }
});

test("Races detail: an empty library doesn't say 'Select a race.' (G94)", () => {
  const { ctx } = harness();
  assert.ok(!src.includes("Select a race."));
  ctx.Lab.races = [];
  assert.match(ctx.raceDetailEmpty(), /Add a race/);
  ctx.Lab.races = [{ race_id: "a" }];
  assert.match(ctx.raceDetailEmpty(), /Pick a race/);
});

test("validator lines read as plain words with a link to the fixing tab (G96)", () => {
  const { ctx } = harness();
  const d = { requirements: [{ id: "monitor-vhf-16-71", text: "Monitor VHF 16" }], courses: [{ id: "c1", name: "Shore course" }] };
  const say = (m) => ctx.plainCheck(m, d);
  let h = say("rules_profile.tracker_permitted unset — confirm per-race (SI)");
  assert.ok(!h.includes("rules_profile") && h.includes('href="#rules"'), h);
  h = say("requirement 'monitor-vhf-16-71' is delivered to the iPad but has no trigger — when should it surface?");
  assert.ok(h.includes("Monitor VHF 16") && !h.includes("monitor-vhf") && h.includes('href="#rules"'), h);
  h = say(`mark "Bob's Shoal" (gate) has no coordinates — needs review`);
  assert.ok(h.includes("Bob&#39;s Shoal") || h.includes("Bob's Shoal"), h);
  assert.ok(h.includes('href="#course"') && !h.includes("(gate)"), h);
  h = say("course 'c1': finish coords need review");
  assert.ok(h.includes("Shore course") && h.includes('href="#course"'), h);
  h = say("requirement 'x': bad trigger_type 'soon'");
  assert.ok(h.includes("unknown trigger &quot;soon&quot;"), h);
  h = say("tracker provider 'yb' not fetchable — set `race` (e.g. 'bayviewmack2026') — verify the live endpoint");
  assert.ok(h.includes('href="#fleet"') && !h.includes("bayviewmack"), h);
  assert.equal(say("Ingest: link failed <x>"), "Ingest: link failed &lt;x&gt;");   // unknown: shown as is, escaped
  assert.match(grab("detailBanner"), /plainCheck\(/);
});

test("removing a mark offers Undo that puts it back in place (G19)", () => {
  const { ctx, byId } = harness();
  const marks = [{ seq: 1, name: "A" }, { seq: 2, name: "B" }, { seq: 3, name: "C" }];
  ctx.Lab.editDef = { courses: [{ marks }] }; ctx.activeSec = "course";
  ctx.removeMark(0, 1);
  assert.deepEqual(marks.map((m) => m.name + m.seq), ["A1", "C2"]);
  const t = byId.undoToast;
  assert.ok(t && !t.hidden && t.innerHTML.includes("Removed mark B") && t.innerHTML.includes("Undo"));
  t._btn.onclick();
  assert.deepEqual(marks.map((m) => m.name + m.seq), ["A1", "B2", "C3"]);
  assert.ok(t.hidden);
});

test("Undo does nothing once another race is open (G19)", () => {
  const { ctx, byId } = harness();
  const marks = [{ seq: 1, name: "A" }];
  ctx.Lab.editDef = { courses: [{ marks }] }; ctx.activeSec = "course";
  ctx.removeMark(0, 0);
  ctx.Lab.editDef = { courses: [{ marks: [] }] };
  byId.undoToast._btn.onclick();
  assert.equal(marks.length, 0);
});

test("removing a rule change or checklist item offers Undo (G50)", () => {
  const { ctx, byId, timers } = harness();
  const mods = [{ ref: "1" }, { ref: "2" }], reqs = [{ id: "a" }, { id: "b" }];
  ctx.Lab.editDef = { rules_profile: { modifications: mods }, requirements: reqs }; ctx.activeSec = "rules";
  ctx.modRemove(0);
  assert.deepEqual(mods.map((m) => m.ref), ["2"]);
  byId.undoToast._btn.onclick();
  assert.deepEqual(mods.map((m) => m.ref), ["1", "2"]);
  ctx.reqRemove(1);
  assert.match(byId.undoToast.innerHTML, /checklist item/);
  byId.undoToast._btn.onclick();
  assert.deepEqual(reqs.map((r) => r.id), ["a", "b"]);
  ctx.reqRemove(0);
  timers.at(-1)();                       // 8 s pass
  assert.ok(byId.undoToast.hidden);
  assert.match(css, /\.undo-toast \{/);
});

test("Add item after a removal doesn't reuse the id Undo would bring back (G50)", () => {
  const { ctx, byId } = harness();
  const reqs = [{ id: "req-1" }, { id: "req-2" }];
  ctx.Lab.editDef = { rules_profile: { modifications: [] }, requirements: reqs }; ctx.activeSec = "rules";
  ctx.reqRemove(1);
  ctx.reqAdd();
  byId.undoToast._btn.onclick();
  const ids = reqs.map((r) => r.id);
  assert.equal(new Set(ids).size, ids.length, ids.join(","));
  assert.ok(ids.includes("req-2"));
});

test("Rules tab: checklist card comes before the rules profile (G47)", () => {
  const p = grab("paintRules");
  const c = p.indexOf("<h3>Checklist"), r = p.indexOf("<h3>Race rules");
  assert.ok(c > 0 && r > 0 && c < r);
});

test("review textareas grow to fit their text (G46)", () => {
  const { ctx } = harness();
  assert.match(ctx.etxt("x", "hi", ""), /growTa\(this\)/);
  const el = { style: {}, scrollHeight: 90, offsetHeight: 40, clientHeight: 38 };
  ctx.growTa(el);
  assert.equal(el.style.height, "92px");
  const hidden = { style: { height: "50px" }, scrollHeight: 0, offsetHeight: 0, clientHeight: 0 };
  ctx.growTa(hidden);
  assert.notEqual(hidden.style.height, "0px");
  assert.match(grab("paintRules"), /growAllTa\(\)/);
  assert.match(grab("paintDetail"), /growAllTa\(\)/);
});

test("long header, method and mark name fields don't clip (G44)", () => {
  const { ctx } = harness();
  assert.match(ctx.ein("a", "v", "", "wide"), /class="ein wide"/);
  assert.match(ctx.ein("a", "v", ""), /class="ein"/);
  assert.match(css, /\.ein\.wide \{[^}]*min-width/);
  assert.match(grab("detailHead"), /"organizer", "wide"/);
  assert.match(grab("paintRules"), /"Single-Number ToT", "wide"/);
  const card = grab("courseEditCard");
  assert.ok(!card.includes('style="width:140px"') && card.includes("size="));
});

test("watch-plan date boxes use the dark field style (G93)", () => {
  const w = grab("detailWatchCard");
  const dt = w.match(/<input[^>]*type="datetime-local"[^>]*>/g) || [];
  assert.equal(dt.length, 3);
  for (const i of dt) assert.match(i, /class="ein"/);
});
