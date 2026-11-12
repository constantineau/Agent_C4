// #310: the Retro card's race picker. A slow /api/retro/setup answer for a race picked earlier must
// not overwrite the race picked now (its courses would be offered and the fleet routed on them).
// Run: node vps/lab/test_retro_setup_race.js  (CI: tools/ci/run.sh node_area)
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "web", "app.js"), "utf8");
function fnSource(name) {
  let i = src.indexOf(`async function ${name}(`);
  if (i < 0) i = src.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`no function ${name}`);
  let depth = 0, j = src.indexOf("{", i);
  for (; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) break;
  }
  return src.slice(i, j + 1);
}

let ok = true;
function check(name, cond) {
  ok = ok && !!cond;
  console.log(`  [${cond ? "OK " : "FAIL"}] ${name}`);
}

// apiGet answers each race when the test says so, in any order
const pending = {};
const SETUP = {
  A: { course_id: "a1", courses: [{ id: "a1" }, { id: "a2" }], yb_id: "ybA" },
  B: { course_id: "b1", courses: [{ id: "b1" }, { id: "b2" }], yb_id: "ybB" },
};
const ctx = vm.createContext({
  console,
  Retro: { ybId: "", defRace: null, courseId: null, setup: null },
  document: { getElementById: () => null },
  location: { hash: "#other" },
  paintDebrief: () => {},
  encodeURIComponent,
  apiGet: (url) => new Promise((resolve) => {
    const id = decodeURIComponent(url.split("def_race_id=")[1]);
    pending[id] = () => resolve({ ok: true, json: async () => SETUP[id] });
  }),
});
vm.runInContext(fnSource("retroReadYb") + "\n" + fnSource("retroSetup") + "\nthis.retroSetup = retroSetup;", ctx);

(async () => {
  console.log("A slow answer for an earlier pick is dropped");
  const a = ctx.retroSetup("A");
  const b = ctx.retroSetup("B");
  pending.B(); await b;
  pending.A(); await a;
  check("selection stays B", ctx.Retro.defRace === "B");
  check("setup is B's", ctx.Retro.setup === SETUP.B);
  check("course is B's", ctx.Retro.courseId === "b1");
  check("YB id is B's", ctx.Retro.ybId === "ybB");

  console.log("In order still works");
  ctx.Retro.ybId = "";
  const c = ctx.retroSetup("A");
  pending.A(); await c;
  check("setup and course are A's", ctx.Retro.setup === SETUP.A && ctx.Retro.courseId === "a1");

  console.log(ok ? "ALL OK" : "FAILED");
  process.exit(ok ? 0 : 1);
})();
