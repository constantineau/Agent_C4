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
     "depths not checked"; land off is "land not checked"; water no ENC depth area covers
     (Canada) passes but the verdict names those legs ("No depth data: check the paper chart");
  8) #44.1b an override pocket moves with its mark (the cache key has it);
  9) #221-3 a closing board on borrowed wind counts in `wind_filled`;
 10) #242 the bundle verdict and the refusal text Freeze, Deploy and GPX use;
 11) #325 one-click override: Override is offered only where a chart override opens the mark
     (not a race zone, an island disk, off the chart, nor a mark the router just didn't reach),
     and every refusal says what to click.
"""
import math
import os

# the stub charts here are the only depth source: no real depth grid (#292) over the test water
os.environ["DEPTH_GRIDS"] = "none"

from app import optimizer as O  # noqa: E402
from app import routesafety as RS  # noqa: E402
from app.geo import coastline  # noqa: E402
from app.geo import enc  # noqa: E402
from app.geo import obstacles as OB  # noqa: E402

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

# Codex on #287: thinning a play's route keeps the way round each mark (the verdict it carries
# is the whole route's)
from app import synthesis as SY  # noqa: E402
# a long beat up the mark's east side, the corner over its top, a long run back down the west
WLAT, R_ = LAT0 + 3 / 60.0, 0.1
_up = [(LAT0 + (WLAT - LAT0) * k / 496, LON0 + R_ * DLON) for k in range(497)]
_corner = [(WLAT + R_ / 60.0, LON0), (WLAT, LON0 - R_ * DLON)]           # top, then exit standoff
_down = [(WLAT - (WLAT - LAT0) * k / 500, LON0 - R_ * DLON) for k in range(1, 501)]
_pts = _up + _corner + _down
_path = [{"lat": la, "lon": lo, "t": 60 * k} for k, (la, lo) in enumerate(_pts)]
_lend = [len(_up) + len(_corner) - 1, len(_pts) - 1]
check("(the whole route leaves the mark on its side)",
      not O._wrong_side(_pts, WLAT, LON0, "port", touch=True))
thin = SY._thin_route({"path": _path, "leg_end_idx": _lend}, max_pts=200)
tp = [(p["lat"], p["lon"]) for p in thin["path"]]
check("a thinned play route still leaves the mark on its side",
      not O._wrong_side(tp, WLAT, LON0, "port", touch=True), f"{len(_pts)} → {len(tp)}")


class NoWind(Steady):
    def wind_at(self, lat, lon, t):
        return None


up = O._corner_speed(Steady(8.5, 0.0), P, LAT0, LON0, 0, 340.0)     # 20° off: inside the no-go
reach = O._corner_speed(Steady(8.5, 0.0), P, LAT0, LON0, 0, 270.0)
check("a corner step into the wind is tacked through at the beat VMG (slower than a reach)",
      up is not None and reach is not None and 0.3 <= up < reach, f"{up} vs {reach}")
b8 = O._vmg_twa(P, 8.5, "beat")
want = O._polar_speed(P, 8.5, b8) * math.cos(math.radians(b8)) / math.cos(math.radians(20.0))
check("...at the two-tack speed made good", up is not None and abs(up - want) < 0.01, f"{up} vs {want}")
check("a corner step with no wind anywhere near: no speed",
      O._corner_speed(NoWind(8.5, 0.0), P, LAT0, LON0, 0, 270.0) is None)
# Codex on #287: the boards a tacked corner step assumes are obstacle-checked, not just the chord
a_ = (LAT0, LON0)
b_ = (LAT0 + 0.1 / 60.0, LON0)                                   # 0.1 nm dead upwind
tk = O._corner_tacks(Steady(8.5, 0.0), P, a_, b_, 0)
check("a step dead upwind: two ways to tack it", tk is not None and len(tk) == 2)
if tk:
    c1, c2 = tk[0][1], tk[1][1]
    check("...one tack point each side of the chord",
          (c1[1] - LON0) * (c2[1] - LON0) < 0 and abs(abs(c1[1] - LON0) - abs(c2[1] - LON0)) < 1e-9)
    check("...on the beat angle from the start",
          abs(abs(O._wrap180(O._bearing(*a_, *c1))) - b8) < 0.5
          and abs(O._wrap180(O._bearing(*c1, *b_)) + O._wrap180(O._bearing(*a_, *c1))) < 0.5,
          f"{O._bearing(*a_, *c1):.1f} then {O._bearing(*c1, *b_):.1f}, beat {b8}")
check("a reaching step: sailed straight, no tacks",
      O._corner_tacks(Steady(8.5, 0.0), P, a_, (LAT0, LON0 + 0.1 * DLON), 0) is None)
# rock both sides of the chord, the chord itself clear: both boards hit, so the corner is refused
fine = OB.ObstacleField((LAT0 + 0.01, LAT0 - 0.01, LON0 - 0.01, LON0 + 0.01), res_deg=0.0002)
for x0, x1 in ((-0.002, -0.0004), (0.0004, 0.002)):
    fine._fill_polygon([[(LON0 + x0, LAT0), (LON0 + x1, LAT0), (LON0 + x1, LAT0 + 0.002),
                         (LON0 + x0, LAT0 + 0.002)]], 1, "coastline")
# end to end: wind from 330 makes a step round the port windward mark point into the wind. The
# path must sail it as two boards, never a heading inside the no-go zone (Codex on #287)
rw = O.optimize_course(course("port"), "c", 0, Steady(8.5, 330.0), polar=P, avoid=False,
                       emit_exploration=False, time_budget_s=60)
pw = [(p["lat"], p["lon"]) for p in rw["path"]]
near = [(a, b) for a, b in zip(pw, pw[1:])
        if min(O._hav_nm(*a, LAT0 + 3 / 60.0, LON0), O._hav_nm(*b, LAT0 + 3 / 60.0, LON0)) < 0.3
        and O._hav_nm(*a, *b) > 1e-4]
worst = min((abs(O._wrap180(O._bearing(*a, *b) - 330.0)) for a, b in near), default=None)
check("round the mark every step is a heading the boat can sail",
      rw.get("complete") and worst is not None and worst >= b8 - 1.0, f"closest to the wind {worst}°")
check("(the chord alone is clear)", not fine.crosses(*a_, *b_))
check("...but every way of tacking it is blocked",
      tk and all(fine.crosses(*p, *c) or fine.crosses(*c, *q) for p, c, q in tk))

# #293: each tack the rounding puts in costs the boat's tack time, like a tack anywhere else.
# Two steps dead upwind (wind N): each is split into two boards, so two tacks are added.
_c3 = (LAT0 + 0.2 / 60.0, LON0)
_leg0 = {"path": [{"lat": LAT0, "lon": LON0, "t": 0}], "eta": 1000.0, "sailed_nm": 1.0, "tacks": 3}
_saved = O.TACK_COST_S
O.TACK_COST_S = 0.0
_free = O._corner_leg(Steady(8.5, 0.0), P, _leg0, [a_, b_, _c3], _c3[0], _c3[1], None, 1.0)
O.TACK_COST_S = _saved
_cost = O._corner_leg(Steady(8.5, 0.0), P, _leg0, [a_, b_, _c3], _c3[0], _c3[1], None, 1.0)
check("a rounding that forces two tacks: two tacks added",
      _cost is not None and _cost["tacks"] == 5, f"{_cost and _cost['tacks']}")
check("...and its ETA carries two tack costs",
      _free is not None and _cost is not None
      and abs((_cost["eta"] - _free["eta"]) - 2 * O.TACK_COST_S) < 1e-6 and O.TACK_COST_S > 0,
      f"{_cost and _free and _cost['eta'] - _free['eta']:.1f} s vs 2 × {O.TACK_COST_S}")
_reach = O._corner_leg(Steady(8.5, 90.0), P, _leg0, [a_, b_, _c3], _c3[0], _c3[1], None, 1.0)
check("...a rounding sailed straight adds no tack time",
      _reach is not None and _reach["tacks"] == 3 and len(_reach["path"]) == 3)

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
check("...a zone isn't opened by a chart override: none offered, move the mark (#325)",
      probs["blocked"].get("override") is False and probs["blocked"]["fix"] == O.FIX_MOVE
      and probs["blocked"]["text"] == O.SAFE_BLOCKED_FIXED.format(mark="W"), probs["blocked"])
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
    OB._FIELD_CACHE.clear()
    OB.build_for_course(course("port"), "c", BBOX, source="enc")
    check("an ENC failure is never cached (retried next run)", not OB._FIELD_CACHE)
    re_ = O.optimize_course(course("port"), "c", 0, Steady(8.5, 0.0), polar=P, obstacles=fe,
                            emit_exploration=False, time_budget_s=60)
    check("ENC failure: depths not checked",
          any(p["text"] == O.SAFE_DEPTH_FAILED for p in (re_.get("safety") or {}).get("problems", [])))
    # Cole's decision on #242: depths are checked wherever ENC charts them. Water no ENC depth
    # area covers (Canada: NOAA ENC is US-only) never fails the route; the verdict names the legs
    # ("No depth data: check the paper chart"). Before, any depth area anywhere in the box made
    # the whole route "depths ok" (Codex on #287).
    _orig_l = enc.layers_in_bbox
    enc.ensure_bbox = lambda bbox, cache_dir=None: None

    def box(s_nm, n_nm):                      # a depth area over the whole box's width
        return [[(LON0 - 0.7, LAT0 + s_nm / 60.0), (LON0 + 0.9, LAT0 + s_nm / 60.0),
                 (LON0 + 0.9, LAT0 + n_nm / 60.0), (LON0 - 0.7, LAT0 + n_nm / 60.0)]]
    try:
        enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {"land": [LAND], "shoal": []}
        fl = OB.build_for_course(course("port"), "c", BBOX, use_cache=False, source="enc")
        check("ENC loaded, no depth areas: loaded, but every step is 'no depth data'",
              fl.depth == "ok" and fl.no_depth(LAT0, LON0, LAT0 + 0.01, LON0), fl.depth)
        check("...ENC land counts as charted", not fl.no_depth(LAT0 - 0.48, LON0, LAT0 - 0.47, LON0))
        half = box(-30, 1.5)                  # charted up to 1.5 nm north of the start only
        enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {
            "land": [LAND], "shoal": [], "depth_cover": [half]}
        fh = OB.build_for_course(course("port"), "c", BBOX, use_cache=False, source="enc")
        check("a step inside the depth area: depths checked",
              not fh.no_depth(LAT0, LON0, LAT0 + 1 / 60.0, LON0))
        check("a step past it: no depth data", fh.no_depth(LAT0, LON0, LAT0 + 2 / 60.0, LON0))
        check("a step leaving the box: no depth data",
              fh.no_depth(LAT0 + 0.45, LON0, LAT0 + 0.55, LON0))
        rh = O.optimize_course(course("port"), "c", 0, Steady(8.5, 0.0), polar=P, obstacles=fh,
                               emit_exploration=False, time_budget_s=60)
        sh = rh.get("safety") or {}
        check("route half outside ENC depths: passes", sh.get("ok") is True, sh.get("problems"))
        check("...and is marked: both legs named",
              [n.get("code") for n in sh.get("notes", [])] == ["no_depth"]
              and sh["notes"][0]["text"] == O.SAFE_NO_DEPTH
              and sh["notes"][0]["legs"] == ["Start → W", "W → Finish"], sh.get("notes"))
        enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {
            "land": [LAND], "shoal": [], "depth_cover": [box(-30, 30)]}
        ff = OB.build_for_course(course("port"), "c", BBOX, use_cache=False, source="enc")
        rf = O.optimize_course(course("port"), "c", 0, Steady(8.5, 0.0), polar=P, obstacles=ff,
                               emit_exploration=False, time_budget_s=60)
        sf = rf.get("safety") or {}
        check("route all inside ENC depths: passes, no note", sf.get("ok") is True
              and not sf.get("notes"), sf)
        enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {
            "land": [LAND], "shoal": [], "depth_cover": [box(-30, 30)], "missing": ["US4MI99M"]}
        fm = OB.build_for_course(course("port"), "c", BBOX, use_cache=False, source="enc")
        check("an ENC cell that didn't download: depths failed, not 'no data'",
              fm.depth == "failed" and "US4MI99M" in (fm.depth_error or ""), fm.depth_error)
        rn = O.optimize_course(course("port"), "c", 0, Steady(8.5, 0.0), polar=P, obstacles=fl,
                               emit_exploration=False, time_budget_s=60)
        check("no ENC depth anywhere on the route: passes, marked",
              (rn.get("safety") or {}).get("ok") is True and (rn["safety"].get("notes") or [{}])[0]
              .get("code") == "no_depth", rn.get("safety"))
        rg = O.optimize_course(course("port"), "c", 0, Steady(8.5, 0.0), polar=P,
                               obstacles=OB.build_for_course(course("port"), "c", BBOX,
                                                             use_cache=False,
                                                             source="natural_earth"),
                               emit_exploration=False, time_budget_s=60)
        check("ENC not asked for at all: still refused (no depth chart)",
              any(p["text"] == O.SAFE_DEPTH_NONE for p in (rg.get("safety") or {}).get("problems", [])))
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

print("8b) #242 ENC depth cover from the chart files: deep and dredged areas, missing cells")
import json as _json
import os as _os
import tempfile as _tf

with _tf.TemporaryDirectory() as cd:
    def _gj(path, feats):
        with open(path, "w") as fh:
            _json.dump({"type": "FeatureCollection", "features": feats}, fh)

    def _sq(x0, y0, x1, y1, **props):
        return {"type": "Feature", "properties": props, "geometry": {
            "type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}}
    with open(_os.path.join(cd, "cell_index.json"), "w") as fh:
        _json.dump({f"US4AA0{k}M": {"bbox": [45, 43, -83, -81], "url": "x", "band": "4"}
                    for k in (1, 2, 3, 4, 5)}, fh)
    _os.makedirs(_os.path.join(enc._extract_dir(cd)))
    for k in (1, 3, 4):                       # downloaded; 2 never was
        _os.makedirs(_os.path.join(cd, "cells", "ENC_ROOT", f"US4AA0{k}M"))
        open(_os.path.join(cd, "cells", "ENC_ROOT", f"US4AA0{k}M", f"US4AA0{k}M.000"), "w").close()
        for lyr in enc.LAYERS:                # the S-57 driver's empty file: a layer it hasn't
            open(_os.path.join(enc._extract_dir(cd), f"US4AA0{k}M_{lyr}.json"), "w").close()
    _os.remove(_os.path.join(enc._extract_dir(cd), "US4AA03M_DEPARE.json"))      # extract failed
    with open(_os.path.join(enc._extract_dir(cd), "US4AA04M_DEPARE.json"), "w") as fh:
        fh.write('{"type": "FeatureColl')                                  # cut short
    _os.makedirs(_os.path.join(cd, "cells", "ENC_ROOT", "US4AA05M"))
    open(_os.path.join(cd, "cells", "ENC_ROOT", "US4AA05M", "US4AA05M.000"), "w").close()
    for lyr in enc.LAYERS:
        open(_os.path.join(enc._extract_dir(cd), f"US4AA05M_{lyr}.json"), "w").close()
    with open(_os.path.join(enc._extract_dir(cd), "US4AA05M_UWTROC.json"), "w") as fh:
        fh.write('{"type": "Feat')                                         # rocks cut short
    _gj(_os.path.join(enc._extract_dir(cd), "US4AA01M_DEPARE.json"),
        [_sq(-82.5, 43.5, -82.4, 43.6, DRVAL1=10.0), _sq(-82.4, 43.5, -82.3, 43.6, DRVAL1=1.0)])
    _gj(_os.path.join(enc._extract_dir(cd), "US4AA01M_DRGARE.json"),
        [_sq(-82.3, 43.5, -82.2, 43.6, DRVAL1=8.0)])
    # a failed ogr2ogr run that still wrote part of a file: never kept (Codex on #287)
    import subprocess as _sp
    _run = enc.subprocess.run

    def _gdal(rc, body):
        def run(cmd, **k):
            with open(cmd[4], "w") as fh:                  # ogr2ogr -f GeoJSON -skipfailures OUT
                fh.write(body)
            return _sp.CompletedProcess(cmd, rc)
        return run
    try:
        out_p = _os.path.join(enc._extract_dir(cd), "US4AA09M_UWTROC.json")
        enc.subprocess.run = _gdal(1, '{"type": "FeatureCollection", "features": []}')
        check("a failed extract (non-zero exit, part of a file) is not kept",
              enc._extract_layer("x.000", "UWTROC", out_p) is False and not _os.path.exists(out_p))
        enc.subprocess.run = _gdal(0, "")
        check("a layer the cell doesn't have (exit 0, empty file) is kept",
              enc._extract_layer("x.000", "UWTROC", out_p) is True and _os.path.getsize(out_p) == 0)
    finally:
        enc.subprocess.run = _run
        if _os.path.exists(out_p):
            _os.remove(out_p)
    _cfb, asked = enc.cells_for_bbox, []
    enc.cells_for_bbox = lambda *a, **k: asked.append(k.get("max_cells", "cap")) or _cfb(*a, **k)
    try:
        L = enc.layers_in_bbox((44.5, 43.2, -82.8, -82.0), cd, 2.6)
    finally:
        enc.cells_for_bbox = _cfb
    check("every cell over the box is looked at, past the download cap (Codex on #287)",
          asked == [None] and len(_cfb((44.5, 43.2, -82.8, -82.0), cd, max_cells=2)) == 2, asked)
    check("deep, shallow and dredged areas all count as depth cover", len(L["depth_cover"]) == 3,
          len(L["depth_cover"]))
    check("only the shallow one is a shoal", len(L["shoal"]) == 1, len(L["shoal"]))
    check("a cell that never downloaded, one whose depths didn't extract, one whose depth file "
          "won't read and one whose rock file won't read are all named",
          L["missing"] == ["US4AA02M", "US4AA03M", "US4AA04M", "US4AA05M"], L["missing"])

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
nd = dict(good, notes=[{"code": "no_depth", "text": O.SAFE_NO_DEPTH, "legs": ["A → B"]}])
nd2 = dict(good, notes=[{"code": "no_depth", "text": O.SAFE_NO_DEPTH, "legs": ["A → B", "B → C"]}])
b_nd = dict(b_ok, consensus={"safety": nd}, variants=[{"id": "left", "route": {"path": []}, "safety": nd2}])
vn = RS.bundle_verdict(b_nd)
check("no depth data on some legs: bundle passes, the legs listed once",
      vn["ok"] and vn["notes"] == [{"code": "no_depth", "text": O.SAFE_NO_DEPTH,
                                    "legs": ["A → B", "B → C"]}], vn)
check("...and isn't refused", RS.refusal(dict(b_nd, safety=vn)) is None)

d_ov = course("port")
d_ov, err = RS.set_override(d_ov, "c", "W", "Cole", "chart shows rock that isn't there", now=5)
check("override recorded with who/when/why",
      err is None and d_ov["courses"][0]["chart_overrides"]["W"] == {"by": "Cole", "at": 5,
                                                                    "why": "chart shows rock that isn't there"})
d_one, err1 = RS.set_override(course("port"), "c", "W", now=7)
check("one click (#325): no name, no reason: by Lab, why blank",
      err1 is None and d_one["courses"][0]["chart_overrides"]["W"] == {"by": "Lab", "at": 7, "why": ""},
      d_one["courses"][0].get("chart_overrides"))
d_one, _ = RS.set_override(d_one, "c", "W", "", "", remove=True)
check("remove takes it off", "chart_overrides" not in d_one["courses"][0])
check("override on an unknown mark refused", RS.set_override(course("port"), "c", "Nope", "C", "x")[1])
d_two = course("port")
d_two["courses"].append(dict(d_two["courses"][0], id="other"))
d_two, err2 = RS.set_override(d_two, "deleted-course", "Finish", "C", "x")
check("override on a course that isn't in the race refused, nothing recorded (Codex on #287)",
      err2 == "No such course." and not any(c.get("chart_overrides") for c in d_two["courses"]))
check("no course named: the race's first course, as the router picks",
      RS.set_override(course("port"), None, "W", "C", "x")[1] is None)
d_ov, _ = RS.set_override(d_ov, "c", "W", "", "", remove=True)
check("remove removes it", "chart_overrides" not in d_ov["courses"][0])

print("11) #325 Override only where it helps; every refusal says what to click")


class Field:                 # a checked chart: land + depths ok, a box round the test water
    land, depth, source = "ok", "ok", "enc"

    def inside(self, lat, lon):
        return abs(lat - LAT0) < 0.5 and abs(lon - LON0) < 0.5


def verdict(d, incomplete, fields=None):
    c = d["courses"][0]
    mk = [(0, "Start", c["start"]["lat"], c["start"]["lon"])] + \
         [(i + 1, m["name"], m["lat"], m["lon"]) for i, m in enumerate(c["marks"])] + \
         [(len(c["marks"]) + 1, "Finish", LAT0, LON0 + 0.0001)]
    f = fields if fields is not None else Field()
    s = O.route_safety(d, "c", mk, [{"lat": LAT0, "lon": LON0}], [], incomplete, f, None, False)
    return {p["code"]: p for p in s["problems"]}


blk = {"leg": 0, "to": "W", "short_nm": 3.0, "blocked_mark": "W"}
pb = verdict(course("port"), blk)
check("a mark the chart blocks: Override offered", pb["blocked"].get("override") is True
      and pb["blocked"]["fix"] == O.FIX_OVERRIDE, pb)
pb = verdict(course("port", overrides={"W": {"by": "Lab", "at": 1, "why": ""}}), blk)
check("...still blocked with an override on it: not offered again, move it",
      pb["blocked"].get("override") is False and pb["blocked"]["fix"] == O.FIX_MOVE, pb)
far = course("port", marks=[{"name": "W", "type": "mark", "rounding": "port", "lat": LAT0 + 0.9,
                             "lon": LON0}])
pb = verdict(far, blk)
check("...off the chart: not offered", pb["blocked"].get("override") is False, pb)
isl = course("port", marks=[{"name": "W", "type": "mark", "rounding": "port", "lat": LAT0 + 3 / 60.0,
                             "lon": LON0},
                            {"name": "Rock", "type": "island", "rounding": "port", "lat": LAT0 + 3 / 60.0,
                             "lon": LON0 + 0.2 * DLON, "radius_nm": 0.5}])
backstop = Field()
backstop.source = "gshhg"
check("...inside an island disk on the coastline backstop: not offered",
      verdict(isl, blk, backstop)["blocked"].get("override") is False)
check("...the same island under ENC charts (its land is shoreline): offered",
      verdict(isl, blk)["blocked"].get("override") is True)
pu = verdict(course("port"), {"leg": 0, "to": "W", "short_nm": 0.8, "reason": "time budget"})
check("a mark the router just didn't reach: no Override (not the chart), run it again",
      pu["unreached"].get("override") is False and pu["unreached"]["fix"] == O.FIX_RERUN, pu)

b_blk = dict(b_ok, consensus={"safety": {"ok": False, "problems": [
    {"code": "blocked", "mark": "W", "text": "W is on land.", "override": True}]}})
why = RS.refusal(dict(b_blk, safety=RS.bundle_verdict(b_blk))) or ""
check("refusal on a chart block says click Override, then build", "Click Override" in why
      and "W" in why and "build the playbook again" in why, why)
b_old = dict(b_ok, consensus={"safety": {"ok": False, "problems": [
    {"code": "blocked", "mark": "W", "text": "W is on land."}]}})
check("...a verdict from before #325 (no `override`): the same",
      "Click Override" in (RS.refusal(dict(b_old, safety=RS.bundle_verdict(b_old))) or ""))
b_mv = dict(b_ok, consensus={"safety": {"ok": False, "problems": [
    {"code": "blocked", "mark": "W", "text": "W is in a race zone.", "override": False}]}})
why = RS.refusal(dict(b_mv, safety=RS.bundle_verdict(b_mv))) or ""
check("refusal on a zone block says move it", "Move W in Course & Marks" in why
      and "Override" not in why, why)
why = RS.refusal(dict(b_bad, safety=vb)) or ""
check("any other refusal says build again", why.endswith(RS.FIX_REBUILD), why)

print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
