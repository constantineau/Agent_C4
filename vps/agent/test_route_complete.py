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
keep = gpsout._mark_indices(path, course)
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

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
