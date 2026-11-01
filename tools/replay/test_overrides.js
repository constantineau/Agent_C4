// Race Rewind "what if" overrides (tools/replay/web/overrides.js): the maths that bends a real
// replay frame before the console sees it. The rules under test: no override = the frame comes
// back untouched; a wind/heading change moves TWA/AWA the right way round; the sail read follows
// the sr33 jib bands; the URL hash round-trips a scenario.
// Run: node tools/replay/test_overrides.js (no deps; tools/ci/run.sh runs it in node:22-alpine).
"use strict";
const assert = require("node:assert/strict");
const path = require("node:path");
const O = require(path.join(__dirname, "web", "overrides.js"));

// Trimmed from the Jul 18 full-race timeline, frame 500 (21:13Z, port beat in 27 kts).
function frame() {
  return { t: "2026-07-18T21:13:31Z", data: {
    "/conditions": { available: true, stw: 6.22, sog: 5.268, tws: 27.194, twa: -37.608, twd: 350.175,
                     aws: 38.255, awa: -30.4, heading: 27.376, cog: 28.338 },
    "/navigator": { available: true, wind: { twd: 350.2, tws: 27.2, beat_twa: 36.8, run_twa: 144.1 },
                    next_mark: { name: "Cove Island Virtual Gate", eta_min: 1041.9 } },
    "/tactics": { available: true, wind: { now: 350.2, mean_12min: 351.8 } },
    "/sail": { available: true, tws_used: 24.0, twa: -37.6, twa_abs: 37.6, tack: "port",
      zones: [{ sail: "J1", twa_min: 0.0, twa_max: 100.0 }, { sail: "A3", twa_min: 100.0, twa_max: 139.6 },
              { sail: "S1", twa_min: 139.6, twa_max: 180.0 }],
      overlaps: [{ sails: ["J1", "A3"], twa_min: 100.0, twa_max: 115.0 }],
      optimal_sail: "J1", crew_sail: "J1", fit: "overpowered", change_to: "J3", hoisted_sail: "J1" },
    "/sails/state": { out_of_service: [], flying: ["J1", "J3"], hoisted: "J1", ts: 1784403926 },
  } };
}
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= (tol || 0.05), `${msg}: ${a} vs ${b}`);

// 1. no override, or a link-only override: the very same object back (nothing recomputed)
{
  const f = frame();
  assert.equal(O.apply(f, {}), f);
  assert.equal(O.apply(f, { link: "lost", fail: ["/fleet"] }), f);
  assert.equal(O.isActive({}), false);
  assert.equal(O.isActive({ link: "slow" }), true);
}

// 2. a do-nothing heading override (same heading) leaves AWA/AWS at the instrument values
{
  const c = O.apply(frame(), { hdg: 27.376 }).data["/conditions"];
  near(c.awa, -30.4, 0.05, "awa unchanged"); near(c.aws, 38.255, 0.01, "aws unchanged");
  near(c.twa, -37.608, 0.01, "twa unchanged");
}

// 3. TWS +8: true wind and the navigator/tactics copies move; AWS grows, AWA opens a little
{
  const input = frame(), before = JSON.parse(JSON.stringify(input));
  const f = O.apply(input, { tws: 8 }), c = f.data["/conditions"];
  near(c.tws, 35.194, 0.001, "tws"); near(f.data["/navigator"].wind.tws, 35.2, 0.05, "nav tws");
  assert.ok(c.aws > 38.255, "aws grows with tws");
  assert.ok(Math.abs(c.awa) > 30.4, "awa opens (apparent wind swings aft) with more true wind");
  // the jib read follows the sr33 bands: J1 above 14 kts is overpowered -> J3 above 20
  assert.equal(f.data["/sail"].fit, "overpowered"); assert.equal(f.data["/sail"].change_to, "J3");
  // the input frame is never mutated
  assert.deepEqual(input, before);
}

// 4. TWD 30° right on port tack: the wind comes round toward the bow -> |TWA| shrinks by 30
{
  const f = O.apply(frame(), { twd: 30 }), c = f.data["/conditions"];
  near(c.twd, 20.175, 0.001, "twd wraps past 360"); near(c.twa, -7.608, 0.001, "twa");
  near(f.data["/tactics"].wind.now, 20.2, 0.05, "tactics wind");
  assert.ok(Math.abs(c.awa) < 30.4, "awa closes");
  assert.equal(f.data["/sail"].tack, "port");
}

// 5. heading 90° more to the right: TWA goes 90° more to port; COG follows the heading
{
  const c = O.apply(frame(), { hdg: 117.376 }).data["/conditions"];
  near(c.twa, -127.608, 0.001, "twa"); near(c.cog, 118.338, 0.001, "cog follows");
  assert.ok(c.awa < -30.4 && c.awa > -180, "awa swings aft on port");
}

// 6. bear away onto a reach: the sail read goes A3 and the J1 is now the wrong sail
{
  const s = O.apply(frame(), { hdg: 137.376 }).data["/sail"];   // twa -147.6 -> S1 zone
  assert.equal(s.optimal_sail, "S1"); assert.equal(s.fit, "wrong_angle"); assert.equal(s.change_to, "S1");
  assert.equal(s.wrong_sail, true);
}

// 7. sail override: J2 at 27 kts is overpowered (band 14–20) -> J3; J3 is in range; sails bar follows
{
  let f = O.apply(frame(), { sail: "J2" });
  assert.equal(f.data["/sail"].crew_sail, "J2"); assert.equal(f.data["/sail"].hoisted_sail, "J1");
  assert.equal(f.data["/sail"].fit, "overpowered"); assert.equal(f.data["/sail"].change_to, "J3");
  assert.deepEqual(f.data["/sails/state"].flying, ["J2"]); assert.equal(f.data["/sails/state"].hoisted, "J2");
  f = O.apply(frame(), { sail: "J3" });
  assert.equal(f.data["/sail"].fit, "in_range"); assert.equal(f.data["/sail"].change_to, null);
  f = O.apply(frame(), { sail: "A3" });                         // kite on a beat
  assert.equal(f.data["/sail"].fit, "wrong_angle"); assert.equal(f.data["/sail"].change_to, "J1");
  f = O.apply(frame(), { sail: "J1", tws: -16 });               // 11 kts: J1 is right again
  assert.equal(f.data["/sail"].fit, "in_range");
}

// 8. forecast: not archived, so a forecast override makes a flat one at the (bent) current wind
{
  const fc = O.apply(frame(), { fsh: 20, fsc: 150, tws: 2 }).data["/forecast"];
  assert.equal(fc.available, true); assert.equal(fc.synthetic, true); assert.equal(fc.hours.length, 6);
  near(fc.hours[0].tws, 29.194 * 1.5, 0.06, "scaled"); near(fc.hours[0].twd, 10.2, 0.06, "shifted");
  assert.equal(O.apply(frame(), { tws: 2 }).data["/forecast"], undefined, "no forecast override, none made up");
}

// 9. hash round trip, and junk in a shared link is ignored rather than trusted
{
  const ov = { tws: 8, twd: -30, hdg: 45, sail: "J2", fsc: 120, link: "slow", delay: 6,
               fail: ["/fleet", "/conditions/full"] };
  const h = O.toHash(ov, 512);
  const back = O.fromHash(h);
  assert.equal(back.f, 512);
  assert.deepEqual(back.ov, O.normalize(ov));
  assert.equal(O.toHash({}, 3), "#f=3");
  const junk = O.fromHash("#f=-4&tws=abc&sail=<script>&link=nuke&fail=etc/passwd,fleet");
  assert.equal(junk.f, 0); assert.equal(junk.ov.tws, 0); assert.equal(junk.ov.sail, null);
  assert.equal(junk.ov.link, "normal"); assert.deepEqual(junk.ov.fail, ["/fleet"]);
}

// 9b. a malformed percent-escape ("/#sail=%") is skipped, not thrown, and the good entries survive
{
  const bad = O.fromHash("#sail=%&tws=5&%E0%A4%A=1&f=7");
  assert.equal(bad.ov.sail, null); assert.equal(bad.ov.tws, 5); assert.equal(bad.f, 7);
  assert.doesNotThrow(() => O.fromHash("#%"));
}

// 10. the banner words
{
  assert.deepEqual(O.summary({ tws: 8, twd: 30, link: "lost" }), ["TWS +8 kts", "TWD 30° right", "link LOST"]);
  assert.deepEqual(O.linkOf({ link: "slow", delay: 6, fail: ["/sail"] }), { mode: "slow", delayS: 6, fail: ["/sail"] });
}

console.log("test_overrides: all passed");
