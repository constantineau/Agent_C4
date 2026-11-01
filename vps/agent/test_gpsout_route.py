"""GPS-OUT route assembly — the plotter route keeps the course marks, never shortcuts across the
obstacle homework, and steers to a waypoint on the navigator's CURRENT leg.

Stubs the polar / wind / navigator / bundle / re-optimizer / n2kout POST (no DB, network, bus).
Invariants locked here (several by seeded property tests):
  - downsampling keeps endpoints + every mark vertex and splits any shortcut that cuts an island;
  - mark vertices come from the router's `leg_end_idx`; without them an ORDERED best fit (a near
    pass never beats the real rounding; Finish == Start takes the last vertex);
  - the boat→destination line is clear of the homework whenever an obstacle test exists (walk
    back within the current leg, else refuse the push);
  - the destination never passes the navigator's unrounded next mark;
  - the route, the navigator and the bundle are reconciled by OCCURRENCE (course position), so a
    lapped course steers on the right lap, a re-route trims passed laps, a stale one is refused.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_gpsout_route.py
"""
import sys

from app import reoptimize, routing, deviation, gpsout
from app import navigator as NAV
# these pin the path geometry; the course check (#125: a route for another course is refused)
# is pinned in test_reroute_button.py
gpsout._course_ok = lambda ro: True

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


routing._polar_speed = lambda tws, twa: 6.0 if twa >= 40 else 0.0
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 90.0)     # from the east: north/south = reach
wind, _ = routing.make_wind_fn(44.0, -82.0, (12.0, 90.0))

# ---- 1) downsample + dest ------------------------------------------------------------------
print("1) gpsout downsample never shortcuts across an island")
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
gpsout._course_context = lambda route=None: ([], BOAT, None, None)   # fix only, no course bounds
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
gpsout._course_context = lambda route=None: ([], BOAT5, None, None)
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

print("   re-route vs navigator from different moments (Codex round 7)")
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 90.0)
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}]}   # no homework
up7 = routing.route_leg(43.98, -82.0, 44.22, -82.0, wind, 0.0, 90.0)
dn7 = routing.route_leg(44.22, -82.0, 43.95, -82.05, wind, 0.0, 90.0)
RO_PATH = up7["path"] + dn7["path"][1:]
RO = {"available": True, "marks": ["Top", "Finish"], "path": RO_PATH,
      "leg_end_idx": [len(up7["path"]) - 1, len(RO_PATH) - 1]}
COURSE7 = [{"name": "Start", "lat": 43.98, "lon": -82.0}, {"name": "Top", "lat": 44.22, "lon": -82.0},
           {"name": "Finish", "lat": 43.95, "lon": -82.05}]
gpsout.reoptimize.get_reoptimize = lambda route=None: RO
NAV._marks = lambda route: COURSE7
past_top = dn7["path"][1]                     # the boat rounded Top while the re-route computed
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Finish"},
                                        "position": {"lat": past_top["lat"], "lon": past_top["lon"]}}
NAV._latest = lambda: {"lat": 43.5, "lon": -83.0}   # a DIFFERENT moment's fix — must not be used
sent.clear()
r = gpsout.show(source="reoptimize")
wp = sent["payload"]["waypoints"]
d = sent["payload"]["dest_index"]
print(f"     dest {d} of {len(wp)}; first waypoint ({wp[0]['lat']},{wp[0]['lon']})")
check("passed Top during compute: route trimmed to start at Top",
      NAV._hav_nm(wp[0]["lat"], wp[0]["lon"], 44.22, -82.0) < 0.01)
top_at = min(range(len(wp)), key=lambda i: NAV._hav_nm(wp[i]["lat"], wp[i]["lon"], 44.22, -82.0))
check("steer-to is on the Top→Finish leg (after the Top waypoint), never back toward Top",
      d > top_at and NAV._hav_nm(wp[d]["lat"], wp[d]["lon"], 44.22, -82.0) > 0.01
      and NAV._hav_nm(wp[d]["lat"], wp[d]["lon"], 43.95, -82.05)
      <= NAV._hav_nm(past_top["lat"], past_top["lon"], 43.95, -82.05) + 0.05)
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Top"},
                                        "position": {"lat": up7["path"][1]["lat"],
                                                     "lon": up7["path"][1]["lon"]}}
sent.clear()
gpsout.show(source="reoptimize")
d = sent["payload"]["dest_index"]
wp = sent["payload"]["waypoints"]
check("navigator agrees with the route (next Top): untrimmed, steer-to on the way up",
      NAV._hav_nm(wp[0]["lat"], wp[0]["lon"], 43.98, -82.0) < 0.01 and 1 <= d
      <= min(range(len(wp)), key=lambda i: NAV._hav_nm(wp[i]["lat"], wp[i]["lon"], 44.22, -82.0)))
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Gate"}, "position": {"lat": 44.0, "lon": -82.0}}
sent.clear()
r = gpsout.show(source="reoptimize")
check("next mark not on the re-route → refused as stale, by name, nothing sent",
      r["shown"] is False and "stale" in r["note"] and "Gate" in r["note"] and "payload" not in sent)
NAV.get_navigator = lambda route=None: {"available": False}
sent.clear()
gpsout.show(source="reoptimize")
check("no navigator: the re-route's own frame (dest 1 from its start), not refused",
      sent.get("payload", {}).get("dest_index") is not None)
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Finish"}}     # no position reported
NAV._latest = lambda: {"lat": past_top["lat"], "lon": past_top["lon"]}
check("navigator without a position → falls back to the latest fix",
      gpsout._course_context()[1] == (past_top["lat"], past_top["lon"]))
# a playbook variant that names its own legs: the navigator's next mark is located BY NAME
VAR = {"race_id": "u", "recommended": "m",
       "variants": [{"id": "m", "route": {"path": RO_PATH, "leg_end_idx": RO["leg_end_idx"],
                                          "legs": [{"to": "Top"}, {"to": "Finish"}]}}]}
deviation._load_playbook = lambda: VAR
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Finish"},
                                        "position": {"lat": past_top["lat"], "lon": past_top["lon"]}}
sent.clear()
gpsout.show()
wp, d = sent["payload"]["waypoints"], sent["payload"]["dest_index"]
check("variant: next mark Finish located by its legs' names → steer-to past Top",
      NAV._hav_nm(wp[d]["lat"], wp[d]["lon"], 44.22, -82.0) > 0.01 and wp[d]["lat"] < 44.22)
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Start"},
                                        "position": {"lat": 43.97, "lon": -82.0}}
sent.clear()
gpsout.show()
check("variant: pre-start (next mark = Start) → steer-to 0", sent["payload"]["dest_index"] == 0)

print("   lapped course: marks reconciled by occurrence, not name (Codex round 8)")
POS = {"Start": (43.99, -82.01), "Top": (44.05, -82.0), "Bottom": (44.0, -82.0),
       "Gate": (44.02, -82.02), "Finish": (44.06, -82.01)}


def _lap_route(names, from_pt, k_per=5, t0=0):
    """A straight-segment route from `from_pt` through `names`, densified; unique t per point."""
    pts, ends, here = [], [], from_pt
    for nm in names:
        to = POS[nm]
        for j in range(k_per):
            f = j / k_per
            pts.append({"lat": here[0] + (to[0] - here[0]) * f, "lon": here[1] + (to[1] - here[1]) * f})
        here = to
        ends.append(len(pts))
    pts.append({"lat": here[0], "lon": here[1]})
    for i, p_ in enumerate(pts):
        p_["t"] = t0 + i
    return pts, ends


def _nav(course, k, boat):
    NAV._marks = lambda route: course
    NAV.get_navigator = lambda route=None: {
        "available": True, "route": "race", "position": {"lat": boat[0], "lon": boat[1]},
        "next_mark": {"name": course[k]["name"], "seq": course[k]["seq"], "index": k}}


def _on_leg(a, b, f):
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


LAP = ["Start", "Top", "Bottom", "Top", "Finish"]
LAPC = [{"name": n_, "seq": i + 1, "lat": POS[n_][0], "lon": POS[n_][1]} for i, n_ in enumerate(LAP)]
vpath, vends = _lap_route(LAP[1:], POS["Start"])
VLAP = {"race_id": "u", "recommended": "m",
        "variants": [{"id": "m", "route": {"path": vpath, "leg_end_idx": vends,
                                           "legs": [{"to": n_} for n_ in LAP[1:]]}}]}
deviation._load_playbook = lambda: VLAP
boat8 = _on_leg(POS["Bottom"], POS["Top"], 0.4)          # second beat, toward the SECOND Top
_nav(LAPC, 3, boat8)
sent.clear()
gpsout.show()
d_t = sent["payload"]["waypoints"][sent["payload"]["dest_index"]]["t"]
print(f"     variant: steer-to path point {d_t}; second beat spans {vends[1]}..{vends[2]}")
check("variant: steer-to is on the SECOND beat (Bottom → second Top), not the first",
      vends[1] <= d_t <= vends[2])
# the same course as a re-route computed on the first beat; the boat has since done a lap
rpath, rends = _lap_route(LAP[1:], _on_leg(POS["Start"], POS["Top"], 0.5))
gpsout.reoptimize.get_reoptimize = lambda route=None: {
    "available": True, "path": rpath, "leg_end_idx": rends, "marks": LAP[1:], "mark_idx": [1, 2, 3, 4]}
sent.clear()
r = gpsout.show(source="reoptimize")
wp = sent["payload"]["waypoints"]
d_t = wp[sent["payload"]["dest_index"]]["t"]
check("re-route: the passed lap is trimmed (route now starts at Bottom)",
      wp[0]["t"] == rends[1] and rends[1] <= d_t <= rends[2])
gpsout.reoptimize.get_reoptimize = lambda route=None: {
    "available": True, "path": rpath, "leg_end_idx": rends, "marks": LAP[1:]}   # no mark_idx
sent.clear()
r = gpsout.show(source="reoptimize")
check("re-route without course positions + a repeated next mark → refused (which lap?)",
      r["shown"] is False and "lap" in r["note"] and "payload" not in sent)
# the re-optimizer itself starts from the right occurrence
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Top", "seq": 4, "index": 3}}
rem = reoptimize._remaining_marks(NAV.get_navigator(), LAPC)
check("re-optimizer remaining marks start at the SECOND Top",
      [m["name"] for m in rem] == ["Top", "Finish"] and rem[0]["course_index"] == 3)
check("next-mark index: name mismatch → seq, then unique name, else None",
      reoptimize._next_course_index({"next_mark": {"name": "Top", "seq": 4, "index": 9}}, LAPC) == 3
      and reoptimize._next_course_index({"next_mark": {"name": "Bottom"}}, LAPC) == 2
      and reoptimize._next_course_index({"next_mark": {"name": "Top"}}, LAPC) is None)

print("   property: random lapped courses, navigator anywhere, variant + re-route")
rng8 = random.Random(8)
bad8, n8 = [], 0
for case in range(300):
    laps = rng8.randint(1, 3)
    body = []
    for _ in range(laps):
        body += ["Top", "Bottom"] if rng8.random() < 0.7 else ["Top", "Gate", "Bottom"]
    course_n = ["Start"] + body[:-1] + ["Top", "Finish"] if rng8.random() < 0.5 else ["Start"] + body + ["Finish"]
    course = [{"name": n_, "seq": i + 1, "lat": POS[n_][0], "lon": POS[n_][1]} for i, n_ in enumerate(course_n)]
    k = rng8.randint(1, len(course) - 1)                  # navigator's next mark (course index)
    boat_ = _on_leg(POS[course_n[k - 1]], POS[course_n[k]], rng8.uniform(0.1, 0.9))
    _nav(course, k, boat_)
    if rng8.random() < 0.5:
        vp, ve = _lap_route(course_n[1:], POS["Start"])
        deviation._load_playbook = lambda _v={"race_id": "u", "recommended": "m", "variants": [
            {"id": "m", "route": {"path": vp, "leg_end_idx": ve,
                                  "legs": [{"to": x} for x in course_n[1:]]}}]}: _v
        lo_t, hi_t, src = (ve[k - 2] if k >= 2 else 0), ve[k - 1], "playbook"
    else:
        k0 = rng8.randint(1, k)                           # the re-route was computed on leg k0
        rp, re_ = _lap_route(course_n[k0:], _on_leg(POS[course_n[k0 - 1]], POS[course_n[k0]], 0.05))
        gpsout.reoptimize.get_reoptimize = lambda route=None, _r={
            "available": True, "path": rp, "leg_end_idx": re_, "marks": course_n[k0:],
            "mark_idx": list(range(k0, len(course_n)))}: _r
        deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}]}
        j = k - k0                                        # the boat's leg in the re-route frame
        lo_t, hi_t, src = (re_[j - 1] if j >= 1 else 0), re_[j], "reoptimize"
    sent.clear()
    res = gpsout.show(source=src)
    n8 += 1
    if "payload" not in sent:
        bad8.append((case, src, "not shown", res.get("note")))
        continue
    t_ = sent["payload"]["waypoints"][sent["payload"]["dest_index"]]["t"]
    if not (lo_t <= t_ <= hi_t):
        bad8.append((case, src, course_n, k, t_, (lo_t, hi_t)))
print(f"     {n8} cases, {len(bad8)} violations {bad8[:2]}")
check("steer-to always lies on the navigator's CURRENT leg (right lap)", not bad8)

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
