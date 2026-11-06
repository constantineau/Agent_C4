// Lab Monitor UI fixes from the grill (#102 G37 G84 G85 G86, #97 G66 Monitor half, #106 G51 G99).
// Pure helpers in web/monitor-geo.js are run directly; the markup fixes are checked as text.
// Run: node vps/lab/test_monitor_geo.js  (CI: tools/ci/run.sh node_area)
"use strict";
const fs = require("fs");
const path = require("path");
const G = require(path.join(__dirname, "web", "monitor-geo.js"));

let ok = true;
function check(name, cond) {
  ok = ok && !!cond;
  console.log(`  [${cond ? "OK " : "FAIL"}] ${name}`);
}

console.log("G85 fleet age: hours and days, not raw minutes");
check("146980 min reads as days", G.fmtAge(146980 * 60) === "102 days");
check("12 min stays minutes", G.fmtAge(12 * 60) === "12 min");
check("3 h reads as hours", G.fmtAge(3 * 3600) === "3.0 h");
check("unknown time is blank", G.fmtAge(null) === "");

console.log("G84 / #240 own boat symbol");
const base = { available: true, lat: 44, lon: -82.5, sog: 6.2, cog: 350, cog_age_s: 2, age_s: 2 };
let s = G.ownSymbol(Object.assign({}, base, { heading: 339, heading_ref: "true", heading_age_s: 3 }));
check("fresh true heading -> hull along heading", s.hull === 339);
check("COG is its own vector", s.cog === 350 && s.cogDim === false);
s = G.ownSymbol(Object.assign({}, base, { heading: 339, heading_ref: "magnetic", heading_age_s: 3 }));
check("magnetic heading -> no hull (map is true north, no variation)", s.hull === null && s.headingFresh);
s = G.ownSymbol(Object.assign({}, base, { heading: 339, heading_ref: null, heading_age_s: 3 }));
check("unknown reference -> no hull", s.hull === null);
s = G.ownSymbol(Object.assign({}, base, { heading: 339, heading_ref: "true", heading_age_s: 120 }));
check("old heading -> no hull", s.hull === null && !s.headingFresh);
s = G.ownSymbol(Object.assign({}, base, { heading: null }));
check("no heading -> neutral", s.hull === null);
s = G.ownSymbol(Object.assign({}, base, { sog: 0.2 }));
check("COG dimmed below 0.5 kn", s.cog === 350 && s.cogDim === true);
s = G.ownSymbol(Object.assign({}, base, { sog: null }));
check("COG dimmed when SOG unknown", s.cogDim === true);
s = G.ownSymbol(Object.assign({}, base, { cog_age_s: 90 }));
check("old COG not drawn", s.cog === null);
check("heading text names the reference", G.headingText({ heading: 339.4, heading_ref: "magnetic", heading_age_s: 1 }) === "HDG 339°M");
check("heading text flags an old heading", G.headingText({ heading: 10, heading_ref: "true", heading_age_s: 99 }) === "HDG 10°T (old)");

console.log("G86 map fit ignores stray inland fixes");
// Bayview Mack-like: Port Huron start, up Lake Huron, finish at Mackinac (course index 0)
const marks = [{ lat: 43.05, lon: -82.42, course: 0 }, { lat: 44.0, lon: -82.6, course: 0 },
  { lat: 45.4, lon: -83.6, course: 0 }, { lat: 45.85, lon: -84.62, course: 0 }];
const fleet = [
  { lat: 44.2, lon: -82.6 }, { lat: 44.3, lon: -82.7 }, { lat: 44.25, lon: -82.65 },
  { lat: 42.29, lon: -85.59 },   // Kalamazoo
  { lat: 42.33, lon: -83.05 },   // Detroit
];
let v = G.fitView(marks, fleet, { available: true, lat: 44.1, lon: -82.5 });
check("Kalamazoo is far", v.far.has(3));
check("racing boats are not far", !v.far.has(0) && !v.far.has(1) && !v.far.has(2));
check("fit has marks + near fixes + our boat, no far fixes",
  v.pts.length === 4 + 3 + 1 && v.far.has(4) && !v.pts.some((p) => p[1] === -85.59 || p[1] === -83.05));
check("legs only join marks of the same course",
  G.fitView([{ lat: 44, lon: -82, course: 0 }, { lat: 44, lon: -84, course: 1 }], [{ lat: 44, lon: -83 }], {}).far.has(0));
v = G.fitView([], fleet, {});
check("no marks: inland outlier dropped around the fleet middle", v.far.has(3) && !v.far.has(0));
v = G.fitView([], [], {});
check("nothing at all -> empty fit", v.pts.length === 0);

console.log("markup");
const app = fs.readFileSync(path.join(__dirname, "web", "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "web", "index.html"), "utf8");
const mon = app.slice(app.indexOf("async function renderMonitor"), app.indexOf("/* ---------- Debrief"));
check("G85 no raw 'N min' age in the table", !/ageMin\(f\.time\) \+ " min"/.test(mon) && /fmtAge\(fixAge\(f\.time\)\)/.test(mon));
check("G85 old rows are tinted", /" old"/.test(mon));
check("G84 fleet COG header says true", mon.includes("<th>COG °T</th>"));
check("G37 note names the race rule", mon.includes("race rules: tracker data allowed aboard")
  && !mon.includes("shore only (not allowed on board)"));
check("G66 empty Monitor links to Races", /No race yet\. <a href="#races">/.test(mon));
check("G86 map fits to MonGeo.fitView, not every fix", mon.includes("MonGeo.fitView(") && !/fitBounds\(pts,/.test(mon));
check("monitor-geo.js loads before app.js", html.indexOf("/monitor-geo.js") > 0 && html.indexOf("/monitor-geo.js") < html.indexOf("/app.js"));
check("G51 sign-in is a form that submits on Return", /<form class="gatebox" onsubmit="[^"]*unlock\(\)/.test(html)
  && /<button type="submit">Enter<\/button>/.test(html));
check("G99 show-password toggle", /id="pwShow"[^>]*onclick="pwToggle\(\)"/.test(html) && /function pwToggle\(\)/.test(app));
check("sign-in stays per tab (sessionStorage, #240)", !/localStorage\.setItem\("c4lab\.token"/.test(app));

console.log("\n" + (ok ? "PASS" : "FAIL"));
process.exit(ok ? 0 : 1);
