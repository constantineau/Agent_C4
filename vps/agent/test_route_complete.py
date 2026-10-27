"""#31 (onboard) — a re-route whose leg never reaches its mark must not report a finished route,
and the chartplotter downsample must never shortcut across the obstacle homework.

Stubs the polar / wind / navigator / bundle so it runs standalone (no DB, no network, no bus).
  1) routing.route_leg flags `reached` (+ short_nm) — True on a clear leg, False when the mark sits
     inside an island disk (every approach blocked) and when the leg outruns the search horizon;
  2) reoptimize refuses (available False) by name instead of chaining the next leg from the mark;
  3) get_route refuses the same way;
  4) gpsout downsample keeps the mark vertices, never cuts the island, and the plotter's dest is
     the first waypoint AHEAD of the boat (projection within the current leg), not index 1.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_route_complete.py
"""
import sys

from app import reoptimize, routing, deviation, gpsout
from app import navigator as NAV

_REAL_REOPT = reoptimize.get_reoptimize      # later sections stub it on the shared module

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


routing._polar_speed = lambda tws, twa: 6.0 if twa >= 40 else 0.0
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 90.0)     # from the east: north/south = reach
wind, _ = routing.make_wind_fn(44.0, -82.0, (12.0, 90.0))

# ---- 1) route_leg's reached flag ----------------------------------------------------------------
print("1) routing.route_leg reached flag")
leg = routing.route_leg(44.0, -82.0, 44.2, -82.0, wind, 0.0, 90.0)
check("clear leg: reached, short 0", leg["reached"] is True and leg["short_nm"] == 0.0)
MARK_ISLAND = [(44.2, -82.0, 1.0)]          # the mark sits inside an island disk → never layable
leg = routing.route_leg(44.0, -82.0, 44.2, -82.0, wind, 0.0, 90.0, avoid=MARK_ISLAND)
print(f"     blocked: reached={leg['reached']} short_nm={leg['short_nm']}")
check("blocked leg: reached=False, ends ≥1 nm short", leg["reached"] is False and leg["short_nm"] >= 1.0)
far = routing.route_leg(44.0, -82.0, 48.0, -82.0, wind, 0.0, 90.0)   # 240 nm > 160×0.2 h × 6 kn
print(f"     beyond the horizon: reached={far['reached']} short_nm={far['short_nm']}")
check("leg beyond the search horizon: reached=False", far["reached"] is False and far["short_nm"] > 20)

# ---- 2) reoptimize refuses by name --------------------------------------------------------------
print("2) reoptimize never chains past a missed mark")
MARKS = [{"seq": 1, "name": "Start", "lat": 44.0, "lon": -82.0},
         {"seq": 2, "name": "Rock", "lat": 44.2, "lon": -82.0},
         {"seq": 3, "name": "Finish", "lat": 44.4, "lon": -82.0}]
NAV.get_navigator = lambda route=None: {"available": True, "route": "race", "next_mark": {"name": "Rock"}}
NAV._marks = lambda route: MARKS
NAV._latest = lambda: {"lat": 44.0, "lon": -82.0, "tws": 12.0, "twd": 90.0, "cog": 0.0, "sog": 6.0}
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}],
                                    "obstacles": {"islands": [{"lat": 44.2, "lon": -82.0,
                                                               "radius_nm": 1.0}]}}
reoptimize._cache.update(ctx=None, t=0, val=None)
r = reoptimize.get_reoptimize()
print(f"     {r.get('note')}")
check("unavailable", r.get("available") is False)
check("incomplete names the leg (Rock) and how far short",
      (r.get("incomplete") or {}).get("to") == "Rock" and (r["incomplete"].get("short_nm") or 0) >= 1)
check("no path / ETA offered", "path" not in r and "eta_min" not in r)
check("note says INCOMPLETE and names the mark", "INCOMPLETE" in r.get("note", "") and "Rock" in r["note"])
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}]}
reoptimize._cache.update(ctx=None, t=0, val=None)
r = reoptimize.get_reoptimize()
check("(control) same course with no island: available + reaches the finish",
      r.get("available") and abs(r["path"][-1]["lat"] - 44.4) < 0.02)

# ---- 3) get_route ---------------------------------------------------------------------------------
print("3) get_route refuses a partial route")
routing._cache.update(key=None, t=0, val=None)
_orig_leg = routing.route_leg
routing.route_leg = lambda *a, **k: {"path": [{"lat": 44.0, "lon": -82.0}], "legs": [],
                                     "reached_t": 0.0, "reached": False, "short_nm": 12.0,
                                     "sailed_nm": 0.0, "direct_nm": 12.0}
try:
    g = routing.get_route()
finally:
    routing.route_leg = _orig_leg
check("get_route: unavailable, names the target", g.get("available") is False and "Rock" in g.get("note", ""))

# ---- 4) gpsout downsample + dest ------------------------------------------------------------------
print("4) gpsout downsample never shortcuts across an island")
ISL = (44.10, -82.0, 1.2)
bundle = {"race_id": "u", "recommended": "m",
          "obstacles": {"islands": [{"lat": ISL[0], "lon": ISL[1], "radius_nm": ISL[2]}]},
          "variants": [{"id": "m"}]}
deviation._load_playbook = lambda: bundle
avoid = [ISL]
# a real detour around the island (start south, mark north of it), densified to many points
leg = routing.route_leg(43.98, -82.0, 44.22, -82.0, wind, 0.0, 90.0, avoid=avoid)
check("router's own path is clear of the island", not any(
    routing._seg_blocked(a["lat"], a["lon"], b["lat"], b["lon"], avoid)
    for a, b in zip(leg["path"], leg["path"][1:])))
back = routing.route_leg(44.22, -82.0, 43.95, -82.05, wind, 0.0, 90.0, avoid=avoid)
raw = leg["path"] + back["path"][1:]
path = []
for a, b in zip(raw, raw[1:]):
    for k in range(6):
        f = k / 6.0
        path.append({"lat": a["lat"] + (b["lat"] - a["lat"]) * f,
                     "lon": a["lon"] + (b["lon"] - a["lon"]) * f})
path.append(raw[-1])
course = [{"name": "Start", "lat": 43.98, "lon": -82.0}, {"name": "Top", "lat": 44.22, "lon": -82.0},
          {"name": "Finish", "lat": 43.95, "lon": -82.05}]
keep = set(gpsout._mark_vertices(path, course).values())
top_i = min(range(len(path)), key=lambda i: NAV._hav_nm(path[i]["lat"], path[i]["lon"], 44.22, -82.0))
print(f"     {len(path)} points, marks at {sorted(keep)}")
check("the Top mark vertex is found", top_i in keep)
blocked = gpsout._blocked_fn(bundle)
for n in (24, 8, 4):
    idx = gpsout._downsample_idx(path, n, keep, blocked)
    pts = [path[i] for i in idx]
    cut = sum(routing._seg_blocked(a["lat"], a["lon"], b["lat"], b["lon"], avoid)
              for a, b in zip(pts, pts[1:]))
    check(f"max {n}: {len(pts)} pts, 0 segments cut the island", cut == 0)
    check(f"max {n}: endpoints + Top mark kept", idx[0] == 0 and idx[-1] == len(path) - 1 and top_i in idx)
naive = gpsout._downsample_idx(path, 4, keep, None)
check("(control) without the check a 4-point thin DOES cut the island", any(
    routing._seg_blocked(path[a]["lat"], path[a]["lon"], path[b]["lat"], path[b]["lon"], avoid)
    for a, b in zip(naive, naive[1:])))

print("   dest index by projecting the boat onto the path")
sent = {}
gpsout._post = lambda p, payload=None: (sent.update(payload=payload) or
                                        {"broadcasting": True, "route": payload["name"],
                                         "n_waypoints": len(payload["waypoints"])})
bundle["variants"] = [{"id": "m", "route": {"path": path}}]
# boat on the RETURN leg, near the bottom — the out-and-back passes close by on the outbound leg too
NAV._marks = lambda route: course
NAV.get_navigator = lambda route=None: {"available": True, "route": "race", "next_mark": {"name": "Finish"}}
boat = back["path"][len(back["path"]) * 3 // 4]
NAV._latest = lambda: {"lat": boat["lat"], "lon": boat["lon"]}
res = gpsout.show()
wp = sent["payload"]["waypoints"]
d = sent["payload"]["dest_index"]
print(f"     dest_index={d} of {len(wp)} (boat {boat['lat']:.3f},{boat['lon']:.3f})")
top_wp = min(range(len(wp)), key=lambda i: NAV._hav_nm(wp[i]["lat"], wp[i]["lon"], 44.22, -82.0))
check("shown", res.get("shown"))
check("dest is past the Top mark (on the return leg), not index 1", d > top_wp and d != 1)
check("dest waypoint lies ahead: closer to the finish than the boat",
      NAV._hav_nm(wp[d]["lat"], wp[d]["lon"], 43.95, -82.05)
      <= NAV._hav_nm(boat["lat"], boat["lon"], 43.95, -82.05) + 0.05)
NAV._latest = lambda: {"lat": None, "lon": None}
gpsout.show()
check("no fix → dest falls back to 1", sent["payload"]["dest_index"] == 1)

print("   practice course, Finish == Start: the return leg keeps its own bounds (Codex repro)")
# windward/leeward like make_practice_course: Start and Finish at the SAME spot, Top 2 nm upwind.
# Wind from the north → beat up, run back; the run passes close to the outbound tacks.
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 0.0)
wind_n, _ = routing.make_wind_fn(44.0, -82.0, (12.0, 0.0))
SL = (44.0, -82.0)
TOP = (44.0333, -82.0)
up = routing.route_leg(SL[0], SL[1], TOP[0], TOP[1], wind_n, 0.0, 0.0)
dn = routing.route_leg(TOP[0], TOP[1], SL[0], SL[1], wind_n, 0.0, 0.0)

def _dense(pts, k=6):
    out = []
    for a, b in zip(pts, pts[1:]):
        out += [{"lat": a["lat"] + (b["lat"] - a["lat"]) * j / k,
                 "lon": a["lon"] + (b["lon"] - a["lon"]) * j / k} for j in range(k)]
    return out + [pts[-1]]


ppath = _dense(up["path"] + dn["path"][1:])
run_from = len(_dense(up["path"])) - 1              # the Top vertex: the run home starts here
practice = [{"name": "Start", "lat": SL[0], "lon": SL[1]}, {"name": "Top", "lat": TOP[0], "lon": TOP[1]},
            {"name": "Finish", "lat": SL[0], "lon": SL[1]}]
v = gpsout._mark_vertices(ppath, practice)
print(f"     {len(ppath)} points, mark vertices {v}")
check("Finish maps to the LAST vertex, not the Start's (index 0)", v.get(2) == len(ppath) - 1
      and v.get(0) == 0 and 0 < v.get(1, 0) < len(ppath) - 1)
bundle.pop("obstacles", None)
bundle["variants"] = [{"id": "m", "route": {"path": ppath}}]
NAV._marks = lambda route: practice
NAV.get_navigator = lambda route=None: {"available": True, "route": "practice",
                                        "next_mark": {"name": "Finish"}}
pb_i = run_from + (len(ppath) - run_from) * 2 // 3
pb = ppath[pb_i]                                     # on the run home, approaching the Finish
NAV._latest = lambda: {"lat": pb["lat"], "lon": pb["lon"]}
gpsout.show()
wp = sent["payload"]["waypoints"]
d = sent["payload"]["dest_index"]
top_wp = min(range(len(wp)), key=lambda i: NAV._hav_nm(wp[i]["lat"], wp[i]["lon"], *TOP))
print(f"     dest_index={d} of {len(wp)}, Top at {top_wp}")
check("dest is on the return leg (after Top), never steering back toward Top", d > top_wp)
check("dest waypoint is no farther from the Finish than the boat",
      NAV._hav_nm(wp[d]["lat"], wp[d]["lon"], *SL) <= NAV._hav_nm(pb["lat"], pb["lon"], *SL) + 0.02)
# the same practice course on a RE-ROUTE (path starts at the boat): Start is astern and must not
# grab the Finish vertex that shares its spot
gpsout.reoptimize.get_reoptimize = lambda route=None: {"available": True,
                                                       "path": ppath[pb_i:]}
gpsout.show(source="reoptimize")
rp = sent["payload"]
print("     re-route payload:", rp["dest_index"], [(round(w["lat"],4), round(w["lon"],4)) for w in rp["waypoints"]])
check("re-route: dest is the first waypoint ahead (1), course end kept",
      rp["dest_index"] == 1 and abs(rp["waypoints"][-1]["lat"] - SL[0]) < 1e-6)

print("   near-pass: a tack 0.4 nm from Top before the real rounding (Codex round 2)")
# up the course: a board that comes within 0.4 nm of Top, sails away, then the actual rounding
NP = [{"lat": 44.0, "lon": -82.0}, {"lat": 44.02, "lon": -82.01},
      {"lat": 44.0333 - 0.4 / 60.0, "lon": -82.0},     # 0.4 nm short of Top — NOT the rounding
      {"lat": 44.03, "lon": -82.03}, {"lat": 44.0333, "lon": -82.0},   # the rounding
      {"lat": 44.02, "lon": -81.99}, {"lat": 44.0, "lon": -82.0}]
TOPM = {"name": "Top", "lat": 44.0333, "lon": -82.0}
course_np = [{"name": "Start", "lat": 44.0, "lon": -82.0}, TOPM,
             {"name": "Finish", "lat": 44.0, "lon": -82.0}]
v = gpsout._mark_vertices(NP, course_np)
print(f"     fallback (no leg ends): {v}")
check("fallback picks the real rounding (index 4), not the 0.4 nm pass (2)", v.get(1) == 4)
check("fallback: Start 0, Finish last", v.get(0) == 0 and v.get(2) == 6)
v = gpsout._mark_vertices(NP, course_np, leg_end_idx=[4, 6])
check("router's leg ends win: {0:0, 1:4, 2:6}", v == {0: 0, 1: 4, 2: 6})
v = gpsout._mark_vertices(NP, course_np, leg_end_idx=[2, 6])
check("leg ends are trusted over proximity (they ARE the rounding)", v.get(1) == 2)
check("bad leg ends (not increasing / out of range) fall back to the fit",
      gpsout._mark_vertices(NP, course_np, leg_end_idx=[6, 4]).get(1) == 4
      and gpsout._mark_vertices(NP, course_np, leg_end_idx=[4, 99]).get(1) == 4)
check("re-route numbering: leg ends map from the next mark",
      gpsout._mark_vertices(NP[3:], course_np, leg_end_idx=[1, 3], first_leg_to=1) == {0: 0, 1: 1, 2: 3}
      and gpsout._mark_vertices(NP[5:], course_np, leg_end_idx=[1], first_leg_to=2) == {2: 1})
# show() uses the variant's leg ends: return-leg dest never lands on the outbound board
bundle["variants"] = [{"id": "m", "route": {"path": NP, "leg_end_idx": [4, 6]}}]
NAV._marks = lambda route: course_np
NAV.get_navigator = lambda route=None: {"available": True, "route": "race", "next_mark": {"name": "Finish"}}
NAV._latest = lambda: {"lat": 44.012, "lon": -81.993}    # on the run home, beside the outbound board
gpsout.show()
print(f"     dest_index={sent['payload']['dest_index']}")
check("return-leg dest stays on the return leg (5 or 6)", sent["payload"]["dest_index"] in (5, 6))

print("   boat exactly on an interior waypoint")
LINE = [{"lat": 44.0 + 0.01 * i, "lon": -82.0} for i in range(4)]
CLEAR = lambda a, b: False      # noqa: E731 — an obstacle test that finds open water everywhere
for at in (1, 2):
    d = gpsout._dest_index(LINE, (LINE[at]["lat"], LINE[at]["lon"]), blocked=CLEAR)
    check(f"boat at waypoint {at} → dest {at + 1} (not {at})", d == at + 1)
d = gpsout._dest_index(LINE, (LINE[2]["lat"] + 0.0003, LINE[2]["lon"]), blocked=CLEAR)  # ~0.02 nm past
check("just past a waypoint (within arrival) → the next one", d == 3)
d = gpsout._dest_index(LINE, (LINE[1]["lat"] + 0.005, LINE[1]["lon"]), blocked=CLEAR)  # mid 1→2
check("mid-segment → the segment's end", d == 2)
check("at the last waypoint → stays on it (no overrun)",
      gpsout._dest_index(LINE, (LINE[3]["lat"], LINE[3]["lon"]), blocked=CLEAR) == 3)
check("no obstacle test available → keep the attained waypoint (conservative)",
      gpsout._dest_index(LINE, (LINE[2]["lat"], LINE[2]["lon"])) == 2)

print("   arrival skip never shortcuts an avoidance vertex (Codex round 4)")
DOG = [{"lat": 44.0, "lon": -82.002}, {"lat": 44.001, "lon": -82.0}, {"lat": 44.0, "lon": -81.998}]
ROCK = [(44.0, -82.0, 0.04)]
rock = lambda a, b: routing._seg_blocked(a["lat"], a["lon"], b["lat"], b["lon"], ROCK)  # noqa: E731
BOAT = (44.0006, -82.0008)
check("(setup) both route segments clear the rock", not rock(DOG[0], DOG[1]) and not rock(DOG[1], DOG[2]))
check("(setup) boat is within arrival range of waypoint 1",
      NAV._hav_nm(BOAT[0], BOAT[1], DOG[1]["lat"], DOG[1]["lon"]) <= gpsout.ARRIVE_NM)
check("(setup) boat → waypoint 2 crosses the rock", rock({"lat": BOAT[0], "lon": BOAT[1]}, DOG[2]))
check("rock in the way → keeps steering to waypoint 1", gpsout._dest_index(DOG, BOAT, blocked=rock) == 1)
check("same geometry in clear water → advances to waypoint 2",
      gpsout._dest_index(DOG, BOAT, blocked=CLEAR) == 2)
check("no obstacle test → keeps waypoint 1", gpsout._dest_index(DOG, BOAT) == 1)
# the same through show(): the bundle's island homework is the obstacle test
bundle_rock = {"race_id": "u", "recommended": "m",
               "obstacles": {"islands": [{"lat": 44.0, "lon": -82.0, "radius_nm": 0.04}]},
               "variants": [{"id": "m", "route": {"path": DOG}}]}
deviation._load_playbook = lambda: bundle_rock
_orig_ctx = gpsout._course_context
gpsout._course_context = lambda route=None: ([], BOAT, None)     # fix only, no course bounds
gpsout.show()
check("show(): island homework keeps the steer-to on waypoint 1", sent["payload"]["dest_index"] == 1)
bundle_rock["obstacles"]["islands"][0]["lat"] = 45.0          # move the rock far away
gpsout.show()
check("show(): rock elsewhere → advances to waypoint 2", sent["payload"]["dest_index"] == 2)
gpsout._course_context = _orig_ctx

print("   one invariant: the boat→destination line is clear (Codex round 5)")
BOAT5 = (44.0007, -81.9999)
check("(setup) boat → waypoint 2 crosses the rock, → waypoint 1 is clear",
      rock({"lat": BOAT5[0], "lon": BOAT5[1]}, DOG[2]) and not rock({"lat": BOAT5[0], "lon": BOAT5[1]}, DOG[1]))
check("projection's dest 2 runs through the rock → walks back to waypoint 1",
      gpsout._dest_index(DOG, BOAT5, blocked=rock) == 1)
check("(control) clear water: the projection's dest 2 stands",
      gpsout._dest_index(DOG, BOAT5, blocked=CLEAR) == 2)
ALL = lambda a, b: True    # noqa: E731 — every line blocked
check("nothing reachable → None (refuse)", gpsout._dest_index(DOG, BOAT5, blocked=ALL) is None)
gpsout._course_context = lambda route=None: ([], BOAT5, None)
bundle_rock["obstacles"]["islands"][0]["lat"] = 44.0
gpsout.show()
check("show(): island homework → steer-to waypoint 1", sent["payload"]["dest_index"] == 1)
_orig_blk = gpsout._blocked_fn
gpsout._blocked_fn = lambda b: ALL
sent.clear()
r = gpsout.show()
gpsout._blocked_fn = _orig_blk
gpsout._course_context = _orig_ctx
check("show(): no clear waypoint → not pushed, named reason, nothing sent",
      r["shown"] is False and "hazard" in r["note"] and "payload" not in sent)

print("   arrival never skips the next mark's own vertex (Codex round 6)")
RND = [{"lat": 44.0, "lon": -82.0}, {"lat": 44.01, "lon": -82.0023}, {"lat": 44.0, "lon": -82.004}]
check("boat 0.02 nm from unrounded mark 1 → still steers to it",
      gpsout._dest_index(RND, (44.0096, -82.0023), {0: 0, 1: 1, 2: 2}, 1, CLEAR) == 1)
check("navigator advanced to mark 2 → steers on to it",
      gpsout._dest_index(RND, (44.0096, -82.0023), {0: 0, 1: 1, 2: 2}, 2, CLEAR) == 2)
check("exactly on the mark (tie) with the mark unpassed → stays",
      gpsout._dest_index(RND, (44.01, -82.0023), {0: 0, 1: 1, 2: 2}, 1, CLEAR) == 1)

print("   property: seeded random paths around random rocks")
import math   # noqa: E402
import random   # noqa: E402
rng = random.Random(31)
cases = walked = refused = 0
bad = []
while cases < 400:
    clat, clon = 44.0 + rng.uniform(-0.05, 0.05), -82.0 + rng.uniform(-0.05, 0.05)
    r_nm = rng.uniform(0.02, 0.3)
    disk = [(clat, clon, r_nm)]
    blk = lambda a, b, _d=disk: routing._seg_blocked(a["lat"], a["lon"], b["lat"], b["lon"], _d)  # noqa: E731
    coslat = math.cos(math.radians(clat))
    # a detour arc around the rock (what a router produces): 3–7 vertices on a ring 1.1–4 r out
    R, a0 = r_nm * rng.uniform(1.1, 4.0), rng.uniform(0, 2 * math.pi)
    n = rng.randint(3, 7)
    span = rng.uniform(0.3, 1.6) * math.pi
    pts = [{"lat": clat + R * math.cos(a0 + span * k / (n - 1)) / 60.0,
            "lon": clon + R * math.sin(a0 + span * k / (n - 1)) / (60.0 * coslat)} for k in range(n)]
    if any(blk(a, b) for a, b in zip(pts, pts[1:])):
        continue                     # a router never returns a path through the rock
    seg = rng.randrange(n - 1)
    f = rng.random()
    off = rng.uniform(-0.4, 0.4) * R
    bl = pts[seg]["lat"] + (pts[seg + 1]["lat"] - pts[seg]["lat"]) * f + off / 60.0 * rng.choice((-1, 1)) * rng.random()
    bo = pts[seg]["lon"] + (pts[seg + 1]["lon"] - pts[seg]["lon"]) * f + off / (60.0 * coslat) * rng.random()
    if rng.random() < 0.5:           # half: anywhere around the rock, incl. its far side
        ba, bd = rng.uniform(0, 2 * math.pi), rng.uniform(1.02, 2.0) * r_nm
        bl, bo = clat + bd * math.cos(ba) / 60.0, clon + bd * math.sin(ba) / (60.0 * coslat)
    if NAV._hav_nm(bl, bo, clat, clon) <= r_nm:
        continue                     # the boat isn't ON the rock
    cases += 1
    here = {"lat": bl, "lon": bo}
    if rng.random() < 0.6:           # random mark vertices + a random next mark
        inner = sorted(rng.sample(range(1, n - 1), rng.randint(0, n - 2)))
        marks = {k: v for k, v in enumerate([0] + inner + [n - 1])}
        nk = rng.choice(list(marks))
    else:
        marks, nk = None, None
    d = gpsout._dest_index(pts, (bl, bo), marks, nk, blk)
    raw, _lo = gpsout._dest_candidate(pts, (bl, bo), marks or {}, nk, blk)
    if d is None:
        refused += 1
        if any(not blk(here, pts[j]) for j in range(_lo, raw + 1)):
            bad.append(("refused but a waypoint in the leg, at/behind the candidate, was clear", cases))
        continue
    if not (0 <= d < n):
        bad.append(("out of range", cases, d))
    elif blk(here, pts[d]):
        bad.append(("boat→dest crosses the rock", cases, d))
    if marks and nk is not None and d > marks[nk]:
        bad.append(("past the unrounded next mark", cases, d, marks[nk]))
    if d != raw:
        walked += 1
        if d > raw:
            bad.append(("walked forward", cases, d, raw))
    if gpsout._dest_index(pts, (bl, bo), marks, nk, CLEAR) != gpsout._dest_candidate(
            pts, (bl, bo), marks or {}, nk, CLEAR)[0]:
        bad.append(("clear water changed the projection", cases))
print(f"     {cases} cases: {walked} walked back, {refused} refused, {len(bad)} violations {bad[:3]}")
check("every returned destination is clear of the rock and never past the next mark", not bad)
check("the property actually exercised the walk-back and the refusal", walked > 0 and refused > 0)

check("next mark is the path start (pre-start) → 0",
      gpsout._dest_index(LINE, (43.99, -82.0), {0: 0, 1: 3}, 0) == 0)

print("   re-optimizer carries its leg ends")
reoptimize.get_reoptimize = _REAL_REOPT
NAV._marks = lambda route: MARKS
NAV.get_navigator = lambda route=None: {"available": True, "route": "race", "next_mark": {"name": "Rock"}}
NAV._latest = lambda: {"lat": 44.0, "lon": -82.0, "tws": 12.0, "twd": 90.0, "cog": 0.0, "sog": 6.0}
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 90.0)
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}]}
reoptimize._cache.update(ctx=None, t=0, val=None)
r = reoptimize.get_reoptimize()
le = r.get("leg_end_idx") or []
check("one leg end per remaining mark, last = path end",
      len(le) == 2 and le[-1] == len(r["path"]) - 1)
check("each leg end IS its mark", all(
    NAV._hav_nm(r["path"][i]["lat"], r["path"][i]["lon"], m["lat"], m["lon"]) < 0.01
    for i, m in zip(le, MARKS[1:])))

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
