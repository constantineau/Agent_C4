"""#31 (onboard) — a route whose leg never reaches its mark must not report a finished route.

Stubs the polar / wind / navigator / bundle so it runs standalone (no DB, no network).
  1) routing.route_leg flags `reached` (+ short_nm) — True on a clear leg, False when the mark sits
     inside an island disk (every approach blocked) and when the leg outruns the search horizon;
  2) reoptimize refuses (available False) by name instead of chaining the next leg from the mark;
  3) get_route refuses the same way;
  4) the re-optimizer records where each leg ends (`leg_end_idx`) — each one AT its mark.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_route_complete.py
"""
import sys

from app import reoptimize, routing, deviation
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

# ---- 4) leg ends ---------------------------------------------------------------------------------
print("4) re-optimizer carries its leg ends")
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
