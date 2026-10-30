// What gets captured and reviewed. A screen is one Lab tab or the race console; each has
// states (empty / loaded / filtered for the Lab, race moments for the console) and the
// front-end source the reviewer reads alongside the screenshots. Sources are cut by marker
// text, not line numbers, so they survive edits to the files.
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

// iPad sizes in CSS pixels. 1024x1366 is the 12.9" Pro, 820x1180 the 10.9" Air.
export const SIZES = {
  "1024x1366": { width: 1024, height: 1366 },
  "1366x1024": { width: 1366, height: 1024 },
  "820x1180": { width: 820, height: 1180 },
};

const LAB_JS = "vps/lab/web/app.js";
const LAB_CSS = { file: "vps/lab/web/styles.css" };
const LAB_HTML = { file: "vps/lab/web/index.html" };
const sec = (from, to) => ({ file: LAB_JS, from, to });

// An empty race library, answered by the browser (no write to the Lab): the "nothing yet" view.
const NO_RACES = { "/api/races": { races: [] } };

const labTab = (id, title, hash, source, extra = []) => ({
  id: `lab-${id}`,
  app: "lab",
  title: `Lab: ${title}`,
  hash,
  states: [{ id: "loaded" }, ...extra],
  sources: [LAB_HTML, LAB_CSS, source],
});

export const SCREENS = [
  {
    id: "lab-signin",
    app: "lab",
    title: "Lab: sign-in gate",
    hash: "",
    signedOut: true,
    states: [{ id: "empty" }],
    sources: [LAB_HTML, LAB_CSS, sec("/* ---------- auth + api", "/* ---------- router")],
  },
  labTab("races", "Races (library + ingest + review)", "#races", sec("/* ---------- Races", "/* ---------- Course & Marks"), [
    { id: "empty", mock: NO_RACES },
    { id: "filtered", note: "the second race in the library selected", steps: [{ click: ".raceitem >> nth=1" }] },
  ]),
  labTab("course", "Course & Marks", "#course", sec("/* ---------- Course & Marks", "/* ---------- placeholders"), [{ id: "empty", mock: NO_RACES }]),
  labTab("rules", "Rules, Safety & Checklists", "#rules", sec("/* ---------- Rules, Safety", "/* ---------- Monitor")),
  labTab("fleet", "Fleet", "#fleet", sec("/* ---------- Fleet", "/* ---------- Rules, Safety")),
  labTab("learnings", "Learnings", "#learnings", sec("/* ---------- Learnings", "/* ---------- Polar")),
  labTab("gameplan", "Gameplan (optimizer)", "#gameplan", sec("/* ---------- Gameplan", "/* ---------- Lock-in")),
  labTab("deploy", "Lock-in & Deploy", "#deploy", sec("/* ---------- Lock-in", "/* ---------- Fleet")),
  labTab("monitor", "Monitor (shore-side live view)", "#monitor", sec("/* ---------- Monitor", "/* ---------- Debrief")),
  labTab("debrief", "Debrief", "#debrief", sec("/* ---------- Debrief", "/* ---------- Learnings"), [{ id: "empty", mock: NO_RACES }]),
  labTab("polar", "Polar (observed polar)", "#polar", { file: LAB_JS, from: "/* ---------- Polar", to: null }, [
    // one race instance instead of all races, split by race
    {
      id: "filtered",
      note: "split by race instance, then one race instance picked",
      steps: [{ check: 'input[name="polsplit"][value="race"]' }, { selectIndex: 'select:has(option:text("all races together"))', index: 1 }],
    },
  ]),
  {
    id: "console",
    app: "console",
    title: "Race console (iPad on deck), Jul 18 Race Rewind replay",
    states: [
      { id: "early-race", at: "2026-07-18T17:45:00Z", note: "early race, everything healthy" },
      { id: "heading-watch", at: "2026-07-18T22:08:31Z", note: "the heading row first says WATCH" },
      { id: "gps-kick", at: "2026-07-18T22:59:01Z", note: "the GPS/heading kick: 24xd reads 133° while the Reactor reads 35°; attitude goes ACT" },
      { id: "compass-out", at: "2026-07-18T23:56:10Z", note: "the compass steps out for good" },
      { id: "off-gps-course", at: "2026-07-19T00:25:00Z", note: "the heading row reads about -90° off GPS course (vs Orca Core)" },
      { id: "off-gps-course-data-detail", at: "2026-07-19T00:25:00Z", steps: [{ click: '.tile[data-tile="data"]' }], note: "same moment, the DATA tile's detail slide-over opened" },
    ],
    sources: [
      { file: "pi/console/dashboard/index.html" },
      { file: "pi/console/dashboard/dashboard.css" },
      { file: "pi/console/dashboard/dashboard.js", from: "/* ============================ helpers", to: "/* ============ COURSE MAP" },
      { file: "pi/console/dashboard/dashboard.js", from: "/* ============================ tile builders", to: "/* ============ the STRATEGY STACK" },
      { file: "pi/console/dashboard/dashboard.js", from: "/* ============================ state", to: "/* ============ the COACH WINDOW" },
      { file: "pi/console/dashboard/dashboard.js", from: "/* ============ INSTRUMENT HEALTH", to: "/* ============ RACE CHECKLIST" },
    ],
  },
];

/** The screens whose id contains any of the comma-separated filters (all when none). */
export function pickScreens(only, screens = SCREENS) {
  if (!only) return screens;
  const want = only.split(",").map((s) => s.trim()).filter(Boolean);
  const out = screens.filter((s) => want.some((w) => s.id.includes(w)));
  if (!out.length) throw new Error(`--only ${only}: no screen matches (have: ${screens.map((s) => s.id).join(", ")})`);
  return out;
}

/**
 * One source spec -> { file, start, end, text } with every line numbered as it is in the file,
 * so the reviewer can cite file:line. `from`/`to` are marker substrings (the slice starts at
 * the line holding `from` and stops before the line holding `to`); a missing marker falls back
 * to the whole file with a note, so a renamed header degrades instead of failing.
 */
export function sliceSource(repoDir, { file, from, to }) {
  const path = join(repoDir, file);
  if (!existsSync(path)) return { file, start: 0, end: 0, text: "", note: "file not found" };
  const lines = readFileSync(path, "utf8").split("\n");
  let start = 0;
  let end = lines.length;
  let note = "";
  if (from) {
    const i = lines.findIndex((l) => l.includes(from));
    if (i < 0) note = `marker "${from}" not found: whole file`;
    else {
      start = i;
      if (to) {
        const j = lines.findIndex((l, k) => k > i && l.includes(to));
        if (j > 0) end = j;
        else note = `end marker "${to}" not found: to the end of the file`;
      }
    }
  }
  const width = String(end).length;
  const text = lines.slice(start, end).map((l, k) => `${String(start + k + 1).padStart(width)}| ${l}`).join("\n");
  return { file, start: start + 1, end, text, note };
}

/** All of a screen's sources as one text block, cut at `maxChars` (earlier sources win). */
export function sourceBlock(repoDir, screen, maxChars) {
  const parts = [];
  let used = 0;
  for (const spec of screen.sources) {
    const s = sliceSource(repoDir, spec);
    if (!s.text) continue;
    const head = `===== ${s.file} lines ${s.start}-${s.end}${s.note ? ` (${s.note})` : ""} =====\n`;
    const room = maxChars - used - head.length;
    if (room < 500) {
      parts.push(`===== ${s.file} lines ${s.start}-${s.end}: left out (source budget) =====`);
      continue;
    }
    const body = s.text.length > room ? `${s.text.slice(0, room)}\n… (cut: source budget)` : s.text;
    parts.push(head + body);
    used += head.length + body.length;
  }
  return parts.join("\n\n");
}
