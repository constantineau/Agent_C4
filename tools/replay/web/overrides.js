/* Race Rewind — "what if" overrides, as pure functions (no DOM, no fetch).
 *
 * The review page scrubs to a real moment of the race, then bends that moment's inputs (more
 * wind, a shift, another sail, a slow or dead link) and feeds the bent frame to the REAL
 * console, so a screen can be judged against a situation the race never produced.
 *
 * Loaded by review.html (window.ReplayOverrides) and required by tools/replay/test_overrides.js
 * under node, so the maths is tested without a browser.
 *
 * WHAT IS RECOMPUTED (cheap, and consistent with the inputs):
 *   /conditions   tws, twd, heading, cog (moves with heading), sog, stw, twa, awa, aws
 *                 (AWA/AWS move by the CHANGE in the computed apparent wind, so with no override
 *                 the frame's own instrument values come back unchanged)
 *   /navigator    wind.tws, wind.twd
 *   /tactics      wind.now
 *   /sail         twa, twa_abs, tack, optimal_sail, wrong_sail, in_range, crew_sail,
 *                 hoisted_sail, fit, change_to, next_crossover, recommendation — a port of
 *                 vps/agent/app/sails.py using the frame's OWN zones/overlaps (its TWS bucket
 *                 is not re-picked) and the sr33 jib bands from vps/lab/boats/sr33.json
 *   /sails/state  hoisted, flying (the chosen sail only)
 *   /forecast     never archived: a forecast override synthesizes a flat 6 h forecast at the
 *                 (overridden) current wind, then shifts/scales it
 *
 * WHAT IS NOT RECOMPUTED (engine outputs built from history; they keep the frame's values):
 *   /navigator ETA, laylines, layline_call, beat/run TWA, next_rounding · /tactics shift/trend/
 *   favoured side · /trend · /plays (predicates still see the real TWS/sail) · /selector ·
 *   /strategy · /deviation · /fatigue · /checklist · /conditions/full per-source readings ·
 *   /sail tws_used bucket, zones, targets · kite over/underpowered (needs every TWS bucket, so a
 *   wrong kite reads "wrong_angle").
 */
(function (root) {
  "use strict";

  /* ---------------- the override record ---------------- */
  // null = off. tws/twd/fsh are deltas; hdg/sog/stw are absolute; fsc is a percent.
  const DEFAULTS = { tws: 0, twd: 0, hdg: null, sog: null, stw: null, sail: null,
                     fsh: 0, fsc: 100, link: "normal", delay: 6, fail: [] };

  // The boat's inventory as the console's SAILS bar knows it (SS flies WITH another sail and
  // has no solo rating, so it is not offered as "the" sail).
  const SAILS = ["J1", "J2", "J3", "C0", "A3", "S1"];

  // Every /api endpoint the console asks for that a timeline can answer, plus /forecast.
  const ENDPOINTS = ["/conditions", "/sail", "/navigator", "/tactics", "/fatigue", "/forecast",
    "/sources", "/fleet", "/watch", "/course", "/deviation", "/selector", "/strategy", "/plays",
    "/checklist", "/trend", "/session", "/sails/state", "/playbook/track", "/gps/status",
    "/power", "/health/sensors", "/conditions/full"];

  const PRESETS = [
    { id: "build8", label: "Wind builds +8 kts", set: { tws: 8 } },
    { id: "right30", label: "30° right shift", set: { twd: 30 } },
    { id: "lost", label: "Link lost", set: { link: "lost" } },
    { id: "slow6", label: "Engine slow (6 s)", set: { link: "slow", delay: 6 } },
  ];

  function normalize(ov) {
    const o = Object.assign({}, DEFAULTS, ov || {});
    const num = (v, d) => (v === null || v === undefined || v === "" || !isFinite(Number(v)) ? d : Number(v));
    o.tws = num(o.tws, 0); o.twd = num(o.twd, 0); o.fsh = num(o.fsh, 0); o.fsc = num(o.fsc, 100);
    o.hdg = num(o.hdg, null); o.sog = num(o.sog, null); o.stw = num(o.stw, null);
    o.delay = Math.max(1, Math.min(30, num(o.delay, 6)));
    if (o.hdg !== null) o.hdg = wrap360(o.hdg);
    if (SAILS.indexOf(o.sail) < 0) o.sail = null;
    if (["normal", "slow", "lost"].indexOf(o.link) < 0) o.link = "normal";
    o.fail = (Array.isArray(o.fail) ? o.fail : []).filter((e) => ENDPOINTS.indexOf(e) >= 0);
    return o;
  }

  const windActive = (o) => o.tws !== 0 || o.twd !== 0 || o.hdg !== null || o.sog !== null || o.stw !== null;
  const fcstActive = (o) => o.fsh !== 0 || o.fsc !== 100;
  function isActive(ov) {
    const o = normalize(ov);
    return windActive(o) || fcstActive(o) || o.sail !== null || o.link !== "normal" || o.fail.length > 0;
  }

  /* ---------------- angles ---------------- */
  function wrap360(d) { return ((d % 360) + 360) % 360; }
  function wrap180(d) { const w = wrap360(d); return w > 180 ? w - 360 : w; }
  const D2R = Math.PI / 180, R2D = 180 / Math.PI;
  const r1 = (v) => Math.round(v * 10) / 10;
  const r3 = (v) => Math.round(v * 1000) / 1000;

  /* apparent wind from true wind (tws, signed twa off the bow) and boat speed v */
  function apparent(tws, twa, v) {
    const x = tws * Math.cos(twa * D2R) + v, y = tws * Math.sin(twa * D2R);
    return { aws: Math.hypot(x, y), awa: Math.atan2(y, x) * R2D };
  }

  /* ---------------- sail fit (port of vps/agent/app/sails.py) ---------------- */
  const LABEL = { J1: "Jib (J1)", A3: "Asym A3", S1: "Kite (S1)" };
  const CERT_EQUIV = { J1: "J1", J2: "J1", J3: "J1", A3: "A3", S1: "S1" };
  const JIB_BANDS = [{ sail: "J1", tws_max: 14 }, { sail: "J2", tws_min: 14, tws_max: 20 },
                     { sail: "J3", tws_min: 20 }];            // vps/lab/boats/sr33.json
  const CODE0 = { twa_min: 55, twa_max: 110, tws_max: 9 };

  function sailRead(s, crew, twaSigned, tws) {
    const lbl = (x) => LABEL[x] || x;
    const zones = s.zones || [], a = Math.abs(twaSigned);
    const cur = zones.find((z) => z.twa_min <= a && a <= z.twa_max);
    let optimal = cur ? cur.sail : s.optimal_sail;
    const down = zones.filter((z) => z.twa_min > a).sort((p, q) => p.twa_min - q.twa_min)[0];
    const xo = down ? { at_twa: down.twa_min, to_sail: down.sail, deg_away: r1(down.twa_min - a),
                        direction: "bear away" } : null;
    const coopt = new Set();
    (s.overlaps || []).forEach((o) => { if (o.twa_min <= a && a <= o.twa_max) (o.sails || []).forEach((x) => coopt.add(x)); });
    const tossup = coopt.has(optimal) ? [...coopt].filter((x) => x !== optimal).sort() : [];
    const fam = crew ? CERT_EQUIV[crew] || null : null;
    const tossOk = !!(fam && fam !== optimal && coopt.has(optimal) && coopt.has(fam));
    const wrong = !!(fam && optimal && fam !== optimal && !tossOk);

    let rec;
    if (wrong) rec = "Optimal sail is " + lbl(optimal) + " but " + lbl(fam) + " is up — peel/change to " + lbl(optimal) + ".";
    else if (tossOk) rec = lbl(fam) + " is fine — a toss-up with " + lbl(optimal) + " here (within ~1.5% of target); no need to peel.";
    else if (xo && xo.deg_away <= 8) rec = lbl(optimal) + " is right; " + xo.to_sail + " peel coming up at " + xo.at_twa + "° TWA (" + xo.deg_away + "° away).";
    else if (tossup.length) rec = lbl(optimal) + " — but a toss-up with " + tossup.map(lbl).join(", ") + " here; carry either.";
    else rec = lbl(optimal) + " is the right sail for " + Math.round(a) + "° TWA / " + Math.round(tws) + " kts.";

    let fit = null, change = null;
    if (crew === "SS") fit = "no_model";
    else if (crew === "C0") {
      if (!(CODE0.twa_min <= a && a <= CODE0.twa_max)) { fit = "wrong_angle"; change = optimal; }
      else if (tws > CODE0.tws_max) { fit = "overpowered"; change = optimal; }
      else fit = "in_range";
    } else if (crew === "J1" || crew === "J2" || crew === "J3") {
      if (wrong) { fit = "wrong_angle"; change = optimal; }
      else {
        const band = JIB_BANDS.find((b) => b.sail === crew);
        const right = (JIB_BANDS.find((b) => (b.tws_min || 0) <= tws && tws <= (b.tws_max == null ? 99 : b.tws_max)) || {}).sail;
        if (band.tws_max != null && tws > band.tws_max) { fit = "overpowered"; change = right || optimal; }
        else if (band.tws_min != null && tws < band.tws_min) { fit = "underpowered"; change = right || optimal; }
        else fit = "in_range";
      }
    } else if (crew) {
      if (wrong) { fit = "wrong_angle"; change = optimal; } else fit = "in_range";
    }
    return { twa: r1(twaSigned), twa_abs: r1(a), tack: twaSigned < 0 ? "port" : "stbd",
             optimal_sail: optimal, tossup_with: tossup, crew_sail: crew || null, hoisted_sail: fam,
             wrong_sail: wrong, in_range: cur ? !wrong : false, fit: fit, change_to: change,
             next_crossover: xo, recommendation: rec };
  }

  /* ---------------- apply ---------------- */
  function apply(frame, ov) {
    const o = normalize(ov);
    if (!frame || !frame.data) return frame;
    if (!windActive(o) && !fcstActive(o) && o.sail === null) return frame;   // link-only: data untouched
    const f = JSON.parse(JSON.stringify(frame));
    const d = f.data;
    const c = d["/conditions"];
    let tws = null, twd = null, twa = null;

    if (c && c.available !== false && c.tws != null && c.twd != null) {
      const h0 = c.heading, twa0 = c.twa, v0 = c.stw != null ? c.stw : c.sog;
      tws = Math.max(0, c.tws + o.tws);
      twd = wrap360(c.twd + o.twd);
      const dh = o.hdg !== null && h0 != null ? wrap180(o.hdg - h0) : 0;
      if (o.hdg !== null) {
        c.heading = o.hdg;
        if (c.cog != null) c.cog = r3(wrap360(c.cog + dh));
      }
      if (o.sog !== null) c.sog = o.sog;
      if (o.stw !== null) c.stw = o.stw;
      if (twa0 != null) {
        twa = wrap180(twa0 + o.twd - dh);
        const v1 = c.stw != null ? c.stw : c.sog;
        if (c.awa != null && c.aws != null && v0 != null && v1 != null) {
          const before = apparent(c.tws, twa0, v0), after = apparent(tws, twa, v1);
          c.awa = r1(wrap180(c.awa + (after.awa - before.awa)));
          c.aws = r3(before.aws > 0.1 ? c.aws * after.aws / before.aws : after.aws);
        }
        c.twa = r3(twa);
      }
      c.tws = r3(tws); c.twd = r3(twd);
    }
    const nav = d["/navigator"];
    if (nav && nav.wind && tws !== null) { nav.wind.tws = r1(tws); nav.wind.twd = r1(twd); }
    const tac = d["/tactics"];
    if (tac && tac.wind && twd !== null) tac.wind.now = r1(twd);

    const s = d["/sail"];
    if (s && s.available !== false && (windActive(o) || o.sail !== null)) {
      const tw = twa !== null ? twa : s.twa;
      const sp = tws !== null ? tws : (c && c.tws) || s.tws_used;
      if (tw != null && sp != null) Object.assign(s, sailRead(s, o.sail || s.crew_sail, tw, sp));
    }
    if (o.sail !== null) {
      const st = d["/sails/state"] || {};
      d["/sails/state"] = Object.assign({}, st, { hoisted: o.sail, flying: [o.sail] });
    }

    if (fcstActive(o)) {
      let fc = d["/forecast"];
      if (!fc || !fc.available || !fc.hours || !fc.hours.length) {
        const bt = tws !== null ? tws : null, bd = twd !== null ? twd : null;
        fc = bt === null ? null : { available: true, synthetic: true,
          note: "replay: /forecast was not archived — flat forecast at the current wind, then overridden",
          hours: [1, 2, 3, 4, 5, 6].map((h) => ({ in_h: h, tws: bt, twd: bd })) };
      }
      if (fc) {
        fc.hours = fc.hours.map((h) => Object.assign({}, h, {
          tws: r1(Math.max(0, h.tws * o.fsc / 100)), twd: r1(wrap360(h.twd + o.fsh)) }));
        d["/forecast"] = fc;
      }
    }
    f.overridden = true;
    return f;
  }

  /* what the shim needs to fake the link */
  function linkOf(ov) {
    const o = normalize(ov);
    return { mode: o.link, delayS: o.delay, fail: o.fail.slice() };
  }

  /* ---------------- words for the ON banner ---------------- */
  const sgn = (v) => (v > 0 ? "+" : "") + v;
  function summary(ov) {
    const o = normalize(ov), out = [];
    if (o.tws) out.push("TWS " + sgn(o.tws) + " kts");
    if (o.twd) out.push("TWD " + Math.abs(o.twd) + "° " + (o.twd > 0 ? "right" : "left"));
    if (o.hdg !== null) out.push("heading " + Math.round(o.hdg) + "°");
    if (o.sog !== null) out.push("SOG " + o.sog + " kts");
    if (o.stw !== null) out.push("STW " + o.stw + " kts");
    if (o.sail) out.push("sail " + o.sail);
    if (o.fsh) out.push("forecast " + Math.abs(o.fsh) + "° " + (o.fsh > 0 ? "right" : "left"));
    if (o.fsc !== 100) out.push("forecast ×" + (o.fsc / 100).toFixed(2));
    if (o.link === "slow") out.push("link slow " + o.delay + " s");
    if (o.link === "lost") out.push("link LOST");
    if (o.fail.length) out.push("failing " + o.fail.join(" "));
    return out;
  }

  /* ---------------- URL hash ---------------- */
  function toHash(ov, frameIdx) {
    const o = normalize(ov), p = [];
    if (frameIdx != null) p.push("f=" + frameIdx);
    ["tws", "twd", "fsh"].forEach((k) => { if (o[k]) p.push(k + "=" + o[k]); });
    if (o.fsc !== 100) p.push("fsc=" + o.fsc);
    ["hdg", "sog", "stw"].forEach((k) => { if (o[k] !== null) p.push(k + "=" + o[k]); });
    if (o.sail) p.push("sail=" + o.sail);
    if (o.link !== "normal") p.push("link=" + o.link);
    if (o.link === "slow") p.push("delay=" + o.delay);
    if (o.fail.length) p.push("fail=" + o.fail.map((e) => e.slice(1)).join(","));
    return p.length ? "#" + p.join("&") : "";
  }
  function fromHash(h) {
    const q = {};
    String(h || "").replace(/^#/, "").split("&").forEach((kv) => {
      if (!kv) return;
      const i = kv.indexOf("=");
      const k = decodeURIComponent(i < 0 ? kv : kv.slice(0, i));
      q[k] = i < 0 ? "" : decodeURIComponent(kv.slice(i + 1));
    });
    const ov = {};
    ["tws", "twd", "fsh", "fsc", "hdg", "sog", "stw", "delay", "sail", "link"].forEach((k) => { if (k in q) ov[k] = q[k]; });
    if (q.fail) ov.fail = q.fail.split(",").filter(Boolean).map((e) => "/" + e);
    const f = q.f !== undefined && q.f !== "" && isFinite(Number(q.f)) ? Math.max(0, Math.floor(Number(q.f))) : null;
    return { ov: normalize(ov), f: f };
  }

  const api = { DEFAULTS, SAILS, ENDPOINTS, PRESETS, normalize, isActive, apply, linkOf, summary,
                toHash, fromHash, sailRead, apparent, wrap180, wrap360 };
  root.ReplayOverrides = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
