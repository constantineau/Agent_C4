// #325: the chart override is one click, offered only where it helps; a result that fails to draw
// says so; an open tab notices a new Lab build.
// Run: node vps/lab/test_override_oneclick.js  (CI: tools/ci/run.sh node_area)
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

const ctx = vm.createContext({ console });
vm.runInContext('const esc = (s) => String(s == null ? "" : s).replace(/[<>&"]/g, "");\n' +
  ["safetyPanel", "chartOverride", "showResult", "webVersionStep"].map(fnSource).join("\n") +
  "\nthis.safetyPanel = safetyPanel; this.chartOverride = chartOverride; this.showResult = showResult;" +
  " this.webVersionStep = webVersionStep;", ctx);

console.log("Override offered only where it helps");
const ctxRC = { race: "r", course: "c" };
const panel = (problems, overrides) => ctx.safetyPanel({ ok: false, problems, overrides: overrides || [], notes: [] }, "route", ctxRC);
let h = panel([{ code: "blocked", mark: "W", text: "W is on land.", fix: "Chart wrong? Click Override.", override: true }]);
check("a chart block: one Override button", (h.match(/>Override</g) || []).length === 1);
check("...that saves at once (remove=false), for this result's race/course and panel",
  /chartOverride\(this\.dataset\.mark, false,[^"]*this\.dataset\.what, this\)/.test(h) &&
  /data-race="r"/.test(h) && /data-course="c"/.test(h) && /data-what="route"/.test(h));
check("...says what to click", /Chart wrong\? Click Override\./.test(h));
check("no old 'Chart wrong here?' button", !/Chart wrong here\?/.test(h));
h = panel([{ code: "blocked", mark: "W", text: "W is on land." }]);
check("a verdict from before #325 (no `override`): still offered", />Override</.test(h));
h = panel([{ code: "unreached", mark: "W", text: "Route doesn't reach W.", fix: "Run it again.", override: false }]);
check("a mark the router just didn't reach: no Override", !/>Override</.test(h) && /Run it again\./.test(h));
h = panel([{ code: "unreached", mark: "W", text: "Route doesn't reach W." }]);
check("...nor on an older verdict", !/>Override</.test(h));
h = panel([{ code: "blocked", mark: "W", text: "W is in a race zone.", fix: "Move it in Course & Marks.", override: false }]);
check("a race zone / island / off the chart: no Override, says move it", !/>Override</.test(h) && /Move it in Course/.test(h));
h = panel([{ code: "blocked", mark: "W", text: "W is on land." }], [{ mark: "W", by: "Lab", at: 1794412311, why: "" }]);
check("already overridden: no second Override, a Remove", !/>Override</.test(h) && />Remove</.test(h) &&
  /chartOverride\(this\.dataset\.mark, true,/.test(h));
check("...the record says overridden and when, no 'by Lab', no blank reason",
  /W: chart overridden, 2026-11-11 \d\d:\d\dZ\./.test(h) && !/by Lab/.test(h));

console.log("One click: no prompts, no confirm, no alert");
const calls = { prompt: 0, confirm: 0, alert: 0, posts: [], runs: 0, synced: null };
function makeBtn() {
  const b = { disabled: false, textContent: "Override", replaced: null };
  b.replaceWith = (el) => { b.replaced = el; };
  return b;
}
Object.assign(ctx, {
  prompt: () => { calls.prompt++; return "x"; }, confirm: () => { calls.confirm++; return true; },
  alert: () => { calls.alert++; },
  queueDef: (fn) => fn(),
  apiPost: async (url, body) => { calls.posts.push({ url, body });
    return { ok: true, json: async () => ({ saved: true, course_id: "c", chart_overrides: body.remove ? {} : { W: { by: "Lab" } } }) }; },
  syncOverrides: (rid, cid, ov) => { calls.synced = { rid, cid, ov }; },
  runOptimize: () => { calls.runs++; },
  Opt: { running: false, raceId: "r", courseId: "c" },
  document: { createElement: () => ({ style: {}, className: "", textContent: "" }),
    getElementById: (id) => (id === "optRun" ? {} : null) },
});

(async () => {
  let b = makeBtn();
  await ctx.chartOverride("W", false, "r", "c", "route", b);
  check("posts at once: the mark, not a removal, no name/why asked",
    calls.posts.length === 1 && calls.posts[0].url === "/api/races/r/chart-override" &&
    calls.posts[0].body.mark === "W" && calls.posts[0].body.remove === false && calls.posts[0].body.course_id === "c");
  check("no prompt, confirm or alert", calls.prompt + calls.confirm + calls.alert === 0);
  check("the Lab's copies of the race get the override", calls.synced && calls.synced.rid === "r" && calls.synced.ov.W);
  check("the route runs again by itself", calls.runs === 1);

  b = makeBtn();
  await ctx.chartOverride("W", true, "r", "c", "route", b);
  check("Remove: one click, no confirm, and the route runs again",
    calls.posts[1].body.remove === true && calls.confirm === 0 && calls.runs === 2);

  b = makeBtn();
  await ctx.chartOverride("W", false, "r", "c", "playbook", b);
  check("on a playbook: not rebuilt for you, says so in place",
    calls.runs === 2 && b.replaced && b.replaced.textContent === "Overridden. Build again." && calls.alert === 0);

  b = makeBtn();
  ctx.Opt.courseId = "other";
  await ctx.chartOverride("W", false, "r", "c", "route", b);
  check("a route for another course than the one picked now: not re-run, says find route again",
    calls.runs === 2 && b.replaced && b.replaced.textContent === "Overridden. Find route again.");
  ctx.Opt.courseId = "c";

  b = makeBtn();
  ctx.Opt.running = true;
  await ctx.chartOverride("W", false, "r", "c", "route", b);
  check("a route already running isn't interrupted", calls.runs === 2 && /Find route again/.test(b.replaced.textContent));
  ctx.Opt.running = false;

  b = makeBtn();
  ctx.apiPost = async () => ({ ok: false, status: 400, json: async () => ({ detail: "No such mark." }) });
  await ctx.chartOverride("W", false, "r", "c", "route", b);
  check("a refused save says why in place, no alert", b.replaced && b.replaced.textContent === "Not saved: No such mark." &&
    calls.alert === 0 && calls.runs === 2);

  console.log("A result that fails to draw says so");
  const out = { innerHTML: "" };
  ctx.document.getElementById = (id) => (id === "optOut" ? out : null);
  ctx.console = { error: () => {} };
  ctx.showResult(() => { throw new Error("fmtDate is not defined"); }, {}, "optOut");
  check("'Couldn't show this route: <error>' in the output box",
    /Couldn't show this route: fmtDate is not defined/.test(out.innerHTML));
  let drew = false;
  ctx.showResult(() => { drew = true; }, {}, "optOut");
  check("a good result just draws", drew);

  console.log("New version bar");
  const v = ctx.webVersionStep;
  check("first look: notes the build, no bar", JSON.stringify(v(null, "a", false)) === JSON.stringify({ loaded: "a", show: false }));
  check("same build: no bar", v("a", "a", false).show === false);
  check("new build: bar", v("a", "b", false).show === true);
  check("new build while a route/playbook runs: waits", v("a", "b", true).show === false && v("a", "b", true).loaded === "a");
  check("health unreachable: nothing changes", v("a", null, false).show === false && v("a", null, false).loaded === "a");

  process.exit(ok ? 0 : 1);
})().catch((e) => { console.log("  [FAIL] threw: " + e.message); process.exit(1); });
