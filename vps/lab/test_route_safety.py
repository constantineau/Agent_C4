"""#242 route acceptance + the optimizer findings it was blocked on (#52, #65, #256, #257, #258,
#44.1b, #221-3). No network: steady test winds, an injected polar, stub shorelines.

Pins:
  1) #52 a port/starboard mark is rounded the legal way: in at the inbound standoff, out at the
     exit standoff. The repro: 8.5 kn from N, start = finish 3 nm south of a windward mark. Before,
     the route went up the mark's legal side and straight back down it, never north of the mark;
  2) the string test (`_wrong_side`) on hand-made paths: right/wrong side, port/starboard, a
     rounding mark (touched) and an island (side only);
  3) #257 a beat whose mark sits just inside the beat angle no longer strands at 0 kn;
  4) #65 the run layline commit fires: a mark off to one side of dead downwind is sailed in one
     gybe;
  5) #256 the overstand gate only near the mark: a route that must go past the mark along the
     wind axis, far off, to get round an island finishes;
  6) #258 outside the obstacle map's box is never open water;
  7) #242 a mark the chart blocks stops the route at once and names the mark; an ENC failure is
     "depths not checked"; land off is "land not checked";
  8) #44.1b an override pocket moves with its mark (the cache key has it);
  9) #221-3 a closing board on borrowed wind counts in `wind_filled`;
 10) #242 the bundle verdict and the refusal text Freeze, Deploy and GPX use.
"""
import math

from app import optimizer as O
from app import routesafety as RS
from app.geo import coastline
from app.geo import enc
from app.geo import obstacles as OB

ok = True


def check(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


class Steady:
    def __init__(self, tws, twd):
        self.tws, self.twd = tws, twd
        self.loaded = True
        self.series, self.meta = {}, []

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


# SR33-ish polar at 8.5 kn: beat VMG ~42, run VMG ~150
ROWS = {30: 2.8, 40: 4.2, 42: 4.4, 50: 4.9, 60: 5.3, 75: 5.7, 90: 5.9,
        110: 5.8, 135: 5.2, 150: 4.6, 165: 4.0, 180: 3.2}
P = [(8.5, a, s) for a, s in ROWS.items()]
LAT0, LON0 = 44.0, -82.3
DLON = 1.0 / (60.0 * math.cos(math.radians(LAT0)))          # deg of longitude per nm


def xy(lat, lon):            # nm east, north of (LAT0, LON0)
    return (lon - LON0) / DLON, (lat - LAT0) * 60.0


def course(side, marks=None, overrides=None):
    c = {"id": "c", "start": {"lat": LAT0, "lon": LON0},
         "marks": marks if marks is not None else
         [{"name": "W", "type": "mark", "rounding": side, "lat": LAT0 + 3 / 60.0, "lon": LON0}],
         "finish": {"points": [{"lat": LAT0, "lon": LON0 + 0.0001}]}}
    if overrides:
        c["chart_overrides"] = overrides
    return {"race_id": "t242", "courses": [c], "zones": []}


print("1) #52 the windward mark is rounded the legal way")
for side in ("port", "starboard"):
    r = O.optimize_course(course(side), "c", 0, Steady(8.5, 0.0), polar=P, avoid=False,
                          emit_exploration=False, time_budget_s=60)
    path = [xy(p["lat"], p["lon"]) for p in r["path"]]
    mark = (0.0, 3.0)
    north = max(y for _x, y in path)
    # the path's first segment that starts at or after the mark's latitude, going back south
    k = r["leg_end_idx"][0] if r.get("leg_end_idx") else 0
    a, b = path[k], path[k + 1]
    cross = (b[0] - a[0]) * (mark[1] - a[1]) - (b[1] - a[1]) * (mark[0] - a[0])  # >0: mark on the left
    want_left = side == "port"
    check(f"{side}: complete", r.get("complete"), r.get("incomplete"))
    check(f"{side}: goes north of the mark", north > mark[1], f"max north {north:.2f} nm")
    check(f"{side}: leaves with the mark on the {'left' if want_left else 'right'}",
          (cross > 0) == want_left, f"cross {cross:.3f}")
    check(f"{side}: no wrong-side problem in the verdict",
          not any(p["code"] == "side" for p in (r.get("safety") or {}).get("problems", [{"code": "side"}])))

class NoWind(Steady):
    def wind_at(self, lat, lon, t):
        return None


up = O._corner_speed(Steady(8.5, 0.0), P, LAT0, LON0, 0, 340.0)     # 20° off: inside the no-go
reach = O._corner_speed(Steady(8.5, 0.0), P, LAT0, LON0, 0, 270.0)
check("a corner step into the wind is tacked through at the beat VMG (slower than a reach)",
      up is not None and reach is not None and 0.3 <= up < reach, f"{up} vs {reach}")
check("a corner step with no wind anywhere near: no speed",
      O._corner_speed(NoWind(8.5, 0.0), P, LAT0, LON0, 0, 270.0) is None)

print("2) the string test on hand-made paths")
M = (0.0, 0.0)


def ll(pts):
    return [(LAT0 + y / 60.0, LON0 + x * DLON) for x, y in pts]


up_east_back = ll([(0, -3), (0.1, 0), (0, -3)])                         # the old bug
round_top = ll([(0, -3), (0.1, 0), (0, 0.1), (-0.1, 0), (0, -3)])       # port, CCW
round_top_cw = ll([(0, -3), (-0.1, 0), (0, 0.1), (0.1, 0), (0, -3)])    # starboard, CW
mla, mlo = LAT0, LON0
check("port hairpin rounded CCW: right side", not O._wrong_side(round_top, mla, mlo, "port"))
check("port hairpin up and back down the east side: wrong side",
      O._wrong_side(up_east_back, mla, mlo, "port"))
check("starboard hairpin rounded CW: right side", not O._wrong_side(round_top_cw, mla, mlo, "starboard"))
check("starboard rounded CCW: wrong side", O._wrong_side(round_top, mla, mlo, "starboard"))
check("port mark passed straight with it on the left: right side",
      not O._wrong_side(ll([(0.1, -3), (0.1, 3)]), mla, mlo, "port"))
check("port mark passed straight with it on the right: wrong side",
      O._wrong_side(ll([(-0.1, -3), (-0.1, 3)]), mla, mlo, "port"))
far_east = ll([(5, -10), (5, 10)])          # an island 5 nm off, passed on its east side going N
check("port island 5 nm to the left: right side",
      not O._wrong_side(far_east, mla, mlo, "port", touch=False))
check("starboard island 5 nm to the left: wrong side",
      O._wrong_side(far_east, mla, mlo, "starboard", touch=False))

print("3) #257 a beat with the mark just inside the beat angle doesn't strand")
# 5 kn: nothing below 44° TWA, best VMG right at 44°, mark 42.5° off the wind, 5 nm away
P5 = [(5.0, a, s) for a, s in {44: 3.0, 60: 3.2, 90: 3.6, 135: 3.3, 180: 2.6}.items()]
half = O._vmg_twa(P5, 5.0, "beat")
b = 42.5
mlat5, mlon5 = O._advance(LAT0, LON0, b, 5.0)
leg = O.route_leg(Steady(5.0, 0.0), P5, LAT0, LON0, 0, mlat5, mlon5, deadline=None)
check("beat VMG angle is the 44° no-go edge", half is not None and abs(half - 44) < 1, f"{half}")
check("the leg reaches the mark", leg["reached"], f"short {leg['short_nm']} nm")

print("4) #65 the run layline commit fires")
# 8.5 kn from N: run VMG ~150° TWA, so the run layline is ~30° off dead downwind. A mark at 145°
# (35° off) is past it: only the gybe that lays it stays. Before, the run's 150° TWA was compared
# with the 35° off dead downwind, and it never fired.
fan = list(range(0, 360, 12))
kept = O._layline_fan(P, 8.5, 0.0, "run", 145.0, fan)
check("run past the layline: only the laying gybe's headings kept",
      kept and all(O._wrap180(h - 180.0) < 0 for h in kept), f"{kept}")
check("...the other gybe dropped", 216 not in kept and len(kept) < len(fan))
check("run inside the layline (20° off): both gybes stay",
      O._layline_fan(P, 8.5, 0.0, "run", 160.0, fan) == fan)
mlat_r, mlon_r = O._advance(LAT0, LON0, 145.0, 6.0)
run = O.route_leg(Steady(8.5, 0.0), P, LAT0, LON0, 0, mlat_r, mlon_r)
check("a run leg past the layline still reaches the mark", run["reached"])

print("5) #256 a route that must pass the mark's wind line to get round an island")
# wind from N, the finish at (0, 0), start 6 nm E, 10 nm S. A wall south of the finish from far
# west to x = 3.5 nm, and an island east of it reaching 1.5 nm north of the finish: the only way
# in is north round the island (past the finish's wind line, 3.5+ nm off) and back west. Before,
# the overstand gate refused every point more than 0.5 nm past that line, however far off.
BBOX = (LAT0 + 0.5, LAT0 - 0.5, LON0 - 0.7, LON0 + 0.9)


def rect(x0, x1, y0, y1):
    return {"name": "z", "type": "exclusion", "geometry": {
        "north": LAT0 + y1 / 60.0, "south": LAT0 + y0 / 60.0,
        "west": LON0 + x0 * DLON, "east": LON0 + x1 * DLON}}


zones = [rect(3.5, 12, -6, 1.5), rect(-45, 3.5, -6, -1)]
s5 = (LAT0 - 10 / 60.0, LON0 + 6 * DLON)
d5 = {"race_id": "t256", "zones": zones, "courses": [{"id": "c", "marks": [],
      "start": {"lat": s5[0], "lon": s5[1]}, "finish": {"points": [{"lat": LAT0, "lon": LON0}]}}]}
f5 = OB.build_for_course(d5, "c", BBOX, coastline_on=False, use_cache=False)
leg5 = O.route_leg(Steady(8.5, 0.0), P, s5[0], s5[1], 0, LAT0, LON0, obstacles=f5)
check("the leg finishes round the island", leg5["reached"], f"short {leg5['short_nm']} nm")

print("6) #258 outside the obstacle map's box is never open water")
check("a point outside the box is blocked", f5.blocked(LAT0 + 0.6, LON0))
check("a step leaving the box is blocked", f5.crosses(LAT0 + 0.45, LON0, LAT0 + 0.55, LON0))
check("a step inside the box in open water is clear", not f5.crosses(LAT0 + 0.1, LON0, LAT0 + 0.2, LON0))

print("7) #242 what the route says when it can't be proven safe")
inzone = course("port")
inzone["zones"] = [rect(-1, 1, 2, 4)]          # the windward mark sits inside a zone
fz = OB.build_for_course(inzone, "c", BBOX, coastline_on=False, use_cache=False)
rz = O.optimize_course(inzone, "c", 0, Steady(8.5, 0.0), polar=P, obstacles=fz,
                       emit_exploration=False, time_budget_s=60)
probs = {p["code"]: p for p in (rz.get("safety") or {}).get("problems", [])}
check("a mark inside a zone: stopped, the mark named",
      (rz.get("incomplete") or {}).get("blocked_mark") == "W", rz.get("incomplete"))
check("...and the verdict says so", "blocked" in probs and probs["blocked"]["mark"] == "W",
      list(probs))
check("...not ok", (rz.get("safety") or {}).get("ok") is False)
check("zones only, no shoreline: land not checked", "land" in probs)
check("no depth chart: depths not checked", probs.get("depth", {}).get("text") == O.SAFE_DEPTH_NONE)
ro = O.optimize_course(course("port"), "c", 0, Steady(8.5, 0.0), polar=P, avoid=False,
                       emit_exploration=False, time_budget_s=60)
check("avoid off: land not checked",
      any(p["code"] == "land" for p in (ro.get("safety") or {}).get("problems", [])))

LAND = [[(LON0 - 0.7, LAT0 - 0.5), (LON0 + 0.9, LAT0 - 0.5), (LON0 + 0.9, LAT0 - 0.45),
         (LON0 - 0.7, LAT0 - 0.45)]]
_orig = (coastline.ensure_global, coastline.layers_in_bbox, coastline.active_source, enc.ensure_bbox)
coastline.ensure_global = lambda cache_dir=None: None
coastline.layers_in_bbox = lambda bbox, cache_dir=None: {"land": [LAND], "lakes": [], "islands": []}
coastline.active_source = lambda: "stub"


def _enc_down(bbox, cache_dir=None):
    raise OSError("ENC server down")


enc.ensure_bbox = _enc_down
try:
    fe = OB.build_for_course(course("port"), "c", BBOX, use_cache=False, source="enc")
    check("ENC failure is recorded on the field", getattr(fe, "depth", None) == "failed")
    re_ = O.optimize_course(course("port"), "c", 0, Steady(8.5, 0.0), polar=P, obstacles=fe,
                            emit_exploration=False, time_budget_s=60)
    check("ENC failure: depths not checked",
          any(p["text"] == O.SAFE_DEPTH_FAILED for p in (re_.get("safety") or {}).get("problems", [])))
    # ENC loaded land but no depth areas: depths were not checked (Codex on #287)
    _orig_l = enc.layers_in_bbox
    enc.ensure_bbox = lambda bbox, cache_dir=None: None
    enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {"land": [LAND], "shoal": []}
    try:
        fl = OB.build_for_course(course("port"), "c", BBOX, use_cache=False, source="enc")
        check("ENC land without depth areas: depth not ok", fl.depth == "none", fl.depth)
        enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {"land": [LAND], "shoal": [],
                                                                       "depth_areas": 12}
        fd = OB.build_for_course(course("port"), "c", BBOX, use_cache=False, source="enc")
        check("ENC depth areas, all deep: depth ok", fd.depth == "ok", fd.depth)
        enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {"land": [], "shoal": [],
                                                                       "depth_areas": 5}
        fw = OB.build_for_course(course("port"), "c", BBOX, use_cache=False, source="enc")
        check("ENC open deep water, no land in the box: depth ok", fw.depth == "ok", fw.depth)
        enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {"land": [LAND], "shoal": [LAND]}
        fs = OB.build_for_course(course("port"), "c", BBOX, use_cache=False, source="enc")
        check("ENC with depth areas: depth ok", fs.depth == "ok", fs.depth)
    finally:
        enc.layers_in_bbox = _orig_l

    print("8) #44.1b an override pocket moves with its mark")
    # the start sits on the stub land strip; an override opens it. Move the start (and its
    # override): the cached mask must not keep the old pocket.
    def on_land(lat):
        d = course("port", overrides={"Start": {"by": "t", "at": 1, "why": "chart artefact"}})
        d["courses"][0]["start"] = {"lat": lat, "lon": LON0}
        return d
    OB._FIELD_CACHE.clear()
    a1 = OB.build_for_course(on_land(LAT0 - 0.48), "c", BBOX, source="natural_earth")
    a2 = OB.build_for_course(on_land(LAT0 - 0.46), "c", BBOX, source="natural_earth")
    check("first start is open", not a1.blocked(LAT0 - 0.48, LON0))
    check("moved start is open too (not the cached mask)", not a2.blocked(LAT0 - 0.46, LON0))
    nov = course("port")
    nov["courses"][0]["start"] = {"lat": LAT0 - 0.47, "lon": LON0}
    a3 = OB.build_for_course(nov, "c", BBOX, source="natural_earth")
    check("no override: the start on land stays land (#242)", a3.blocked(LAT0 - 0.47, LON0))
finally:
    coastline.ensure_global, coastline.layers_in_bbox, coastline.active_source, enc.ensure_bbox = _orig

print("9) #221-3 a closing board on borrowed wind counts")


class Hole(Steady):
    """Forecast everywhere but within 0.1 nm of the mark: no path node lands in the hole, only
    the closing board's last sample (the mark) does."""
    def __init__(self, mlat, mlon):
        super().__init__(8.5, 0.0)
        self.m = (mlat, mlon)

    def wind_at(self, lat, lon, t):
        return None if O._hav_nm(lat, lon, *self.m) < 0.1 else (self.tws, self.twd)


mlat9, mlon9 = O._advance(LAT0, LON0, 90.0, 3.0)      # a 3 nm beam reach
leg9 = O.route_leg(Hole(mlat9, mlon9), P, LAT0, LON0, 0, mlat9, mlon9)
check("reach reaches the mark", leg9["reached"])
check("the borrowed wind on the closing board is counted", leg9["wind_filled"] >= 1,
      f"wind_filled {leg9['wind_filled']}")

print("10) #242 the bundle verdict and the refusal")
good = {"ok": True, "problems": [], "overrides": []}
bad = {"ok": False, "problems": [{"code": "side", "mark": "W", "text": "Route leaves W on the wrong side."}]}
b_ok = {"consensus": {"safety": good}, "variants": [{"id": "left", "route": {"path": []}, "safety": good}],
        "plays": [{"id": "p", "response": {"route": {"path": []}}, "safety": good}]}
v = RS.bundle_verdict(b_ok, [{"mark": "Finish", "by": "Cole", "at": 1, "why": "chart"}])
check("all routes ok: ok, overrides kept", v["ok"] and v["overrides"][0]["by"] == "Cole")
b_bad = dict(b_ok, plays=[{"id": "p", "name": "Late", "response": {"route": {"path": []}}, "safety": bad}])
vb = RS.bundle_verdict(b_bad)
check("one play on the wrong side: not ok, named", not vb["ok"] and vb["problems"][0]["route"] == "Late")
b_none = dict(b_ok, variants=[{"id": "left", "route": {"path": []}}])
check("a route with no verdict: not ok", not RS.bundle_verdict(b_none)["ok"])
b_empty = dict(b_ok, variants=[{"id": "left", "route": {"path": []}, "safety": {}}],
               consensus={"safety": {"ok": False}})
check("verdicts that aren't ok, with no problems listed: not ok", not RS.bundle_verdict(b_empty)["ok"])
check("...and refused under a top-level ok", RS.refusal(dict(b_empty, safety=good)) is not None)
check("refusal: none when ok", RS.refusal(dict(b_ok, safety=v)) is None)
check("refusal: old bundle", RS.refusal({"variants": []}) == RS.OLD_BUNDLE)
check("refusal: says why", "wrong side" in (RS.refusal(dict(b_bad, safety=vb)) or ""))

d_ov = course("port")
d_ov, err = RS.set_override(d_ov, "c", "W", "Cole", "chart shows rock that isn't there", now=5)
check("override recorded with who/when/why",
      err is None and d_ov["courses"][0]["chart_overrides"]["W"] == {"by": "Cole", "at": 5,
                                                                    "why": "chart shows rock that isn't there"})
check("override needs a name", RS.set_override(course("port"), "c", "W", "", "x")[1] is not None)
check("override on an unknown mark refused", RS.set_override(course("port"), "c", "Nope", "C", "x")[1])
d_ov, _ = RS.set_override(d_ov, "c", "W", "", "")
check("empty why removes it", "chart_overrides" not in d_ov["courses"][0])

print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
