// #379 time-loss band: the pure helpers in web/criticality.js, and the map/run wiring as text.
// Run: node vps/lab/test_criticality.js  (CI: tools/ci/run.sh node_area)
"use strict";
const fs = require("fs");
const path = require("path");
const C = require(path.join(__dirname, "web", "criticality.js"));

let ok = true;
function check(name, cond) {
  ok = ok && !!cond;
  console.log(`  [${cond ? "OK " : "FAIL"}] ${name}`);
}

console.log("shades: 5 / 10 / 20 min");
check("0 min is the darkest", C.shadeOf(0) === 0);
check("5 min is still the darkest", C.shadeOf(5) === 0);
check("7 min is the middle", C.shadeOf(7) === 1);
check("20 min is the lightest", C.shadeOf(20) === 2);
check("past 20 min: none", C.shadeOf(20.1) === -1);
check("null: none", C.shadeOf(null) === -1);

console.log("cells");
const leg = { leg: 0, to: "Top", lat0: 44, lon0: -82, dlat: 0.1, dlon: 0.2, nx: 3, ny: 2,
  loss_min: [null, 0, 12, 30, 4, null], width_nm: { "5": 1, "10": 1.5, "20": 2 } };
const cs = C.cells(leg);
check("only cells within 20 min", cs.length === 3);
check("row-major from the south-west", cs[0].lat === 44 && Math.abs(cs[0].lon - -81.8) < 1e-9 && cs[0].shade === 0);
check("second row", cs[2].lat === 44.1 && Math.abs(cs[2].lon - -81.8) < 1e-9);
const R = { criticality: { legs: [leg] } };
check("has a band", C.has(R) && !C.has({}) && !C.has({ criticality: { legs: [] } }));
check("width line", C.widths(R)[0].nm === 1.5 && /Top 1.5 nm/.test(C.legendHtml(R)));
check("names are escaped", !/<b>/.test(C.legendHtml({ criticality: { legs: [Object.assign({}, leg, { to: "<b>x" })] } })));

console.log("rough legs and the caveat");
const rough = { criticality: { legs: [Object.assign({}, leg, { rough: true })] } };
check("rough leg says so", /Top 1.5 nm <b[^>]*>rough<\/b>/.test(C.legendHtml(rough)));
check("steady leg doesn't", !/rough<\/b>/.test(C.legendHtml(R)));
check("legend says it's approximate in fast-changing wind", /Approximate when the wind changes fast/.test(C.legendHtml(R)));

console.log("draw");
const rects = [];
const ctx = { beginPath() {}, fill() { rects.push("fill"); }, rect(x, y, w, h) { rects.push([x, y, w, h]); } };
C.draw(ctx, (lat, lon) => ({ x: (lon + 82) * 100, y: (45 - lat) * 100 }), R);
check("one rect per shaded cell", rects.filter((r) => Array.isArray(r)).length === 3);
check("alpha reset", ctx.globalAlpha === 1);

console.log("wiring");
const web = (f) => fs.readFileSync(path.join(__dirname, "web", f), "utf8");
check("index.html loads criticality.js", /<script src="\/criticality.js"><\/script>/.test(web("index.html")));
check("map draws it in its own layer", /Criticality\.draw\(ctx, project, R\)/.test(web("mapview.js")));
check("run sends criticality only when ticked", /critEl && critEl\.checked\) body\.criticality = true/.test(web("app.js")));

console.log(ok ? "ALL OK" : "SOME FAILED");
process.exit(ok ? 0 : 1);
