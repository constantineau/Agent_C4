"""Onboard re-optimizer — unit test for route_leg + the multi-mark chain, the off-playbook flag,
and the divergence-vs-frozen comparison. Stubs the polar / wind / navigator so it runs standalone
(no DB / no network / no live GPS).

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_reoptimize.py
"""
from app import reoptimize, routing, deviation
from app import navigator as NAV

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

# --- stubs: 6 kn at any sailable TWA (>=40°), no-go below; constant wind FROM the north (twd 0) ---
routing._polar_speed = lambda tws, twa: 6.0 if twa >= 40 else 0.0
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 0.0)

# --- route_leg: a beam reach (wind from 090, sail due north = TWA 90) → near-direct, few tacks -----
print("route_leg (beam reach):")
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 90.0)
wind, use = routing.make_wind_fn(44.0, -82.0, (12.0, 90.0))
leg = routing.route_leg(44.0, -82.0, 44.1, -82.0, wind, 0.0, 90.0)
print("  sailed", round(leg["sailed_nm"], 2), "direct", round(leg["direct_nm"], 2), "eta_h", round(leg["reached_t"] / 3600, 2))
check("reach leg ~direct (sailed within 15% of direct)", leg["sailed_nm"] <= leg["direct_nm"] * 1.15)
check("reaches the mark (last point near 44.1)", abs(leg["path"][-1]["lat"] - 44.1) < 0.02)

# --- reoptimize: route through the remaining marks upwind (wind from north, marks due north) --------
print("reoptimize (upwind through 2 marks):")
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 0.0)   # dead upwind → must tack
MARKS = [{"seq": 1, "name": "Start", "lat": 44.0, "lon": -82.0},
         {"seq": 2, "name": "A", "lat": 44.1, "lon": -82.0},
         {"seq": 3, "name": "Finish", "lat": 44.2, "lon": -82.0}]
NAV.get_navigator = lambda route=None: {"available": True, "route": "race", "next_mark": {"name": "A"}}
NAV._marks = lambda route: MARKS
NAV._latest = lambda: {"lat": 44.05, "lon": -82.0, "tws": 12.0, "twd": 0.0, "cog": 0.0, "sog": 6.0}

# a frozen "plan" = a straight rhumb line north (so the tacking fresh route diverges from it)
FROZEN = [{"lat": 44.05 + i * 0.015, "lon": -82.0, "t": i} for i in range(11)]
deviation._load_playbook = lambda: {"race_id": "u", "recommended": "middle",
                                     "variants": [{"id": "middle", "route": {"path": FROZEN}}]}

r = reoptimize.get_reoptimize()
print("  ", r.get("marks"), "| eta_min", r.get("eta_min"), "| tacks", r.get("tacks"),
      "| sailed", r.get("sailed_nm"), "| vs", r.get("vs_playbook"))
check("available + off_playbook flagged", r.get("available") and r.get("off_playbook") is True)
check("routes the remaining marks A→Finish", r.get("marks") == ["A", "Finish"])
check("upwind → it tacks (>=1)", r.get("tacks") >= 1)
check("sailed > direct (tacking oversail)", r.get("sailed_nm") > 0.3 * 60 * (44.2 - 44.05) * 0.9)
check("reaches the finish (last point near 44.2)", abs(r["path"][-1]["lat"] - 44.2) < 0.03)
check("wind source is the forecast stub", r.get("wind_source") == "forecast")
check("note flags OFF THE PLAYBOOK", "OFF THE PLAYBOOK" in r.get("note", ""))
vs = r.get("vs_playbook") or {}
check("divergence vs the frozen plan computed", vs.get("available") and vs.get("max_divergence_nm") >= 0)
check("tacking route diverges from the straight plan (>0)", vs.get("max_divergence_nm") > 0.05)
check("each leg carries a sail slot + sail_plan is a list", "sail" in r["legs"][0] and isinstance(r.get("sail_plan"), list))

# --- obstacle avoidance: an island on the direct path forces a detour --------------------------
print("island avoidance:")
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 90.0)     # wind from east → sailing N is a reach
NAV.get_navigator = lambda route=None: {"available": True, "route": "race", "next_mark": {"name": "A"}}
NAV._marks = lambda route: [{"seq": 1, "name": "Start", "lat": 43.98, "lon": -82.0},
                            {"seq": 2, "name": "A", "lat": 44.22, "lon": -82.0}]
NAV._latest = lambda: {"lat": 43.98, "lon": -82.0, "tws": 12.0, "twd": 90.0, "cog": 0.0, "sog": 6.0}
ISLAND = (44.10, -82.0, 1.2)   # smack on the straight-north line from the boat to A

def _min_clear(path):
    return min(routing._pt_to_seg_nm(ISLAND[0], ISLAND[1], path[i]["lat"], path[i]["lon"],
                                     path[i + 1]["lat"], path[i + 1]["lon"]) for i in range(len(path) - 1))

# with NO obstacle homework → the reach route runs straight through the island's location
reoptimize._cache["key"] = None
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}]}   # no obstacles block
straight = reoptimize.get_reoptimize()
check("no obstacles → open-water note", "open-water" in straight.get("note", "") and straight.get("avoids_islands") == 0)
check("without avoidance the route passes through the island (< radius)", _min_clear(straight["path"]) < ISLAND[2])

# with the island frozen in the bundle → the route detours around it
reoptimize._cache["key"] = None
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}],
                                    "obstacles": {"islands": [{"name": "Rock", "lat": ISLAND[0],
                                                               "lon": ISLAND[1], "radius_nm": ISLAND[2]}]}}
avoided = reoptimize.get_reoptimize()
print("  avoids_islands", avoided.get("avoids_islands"), "| min clearance", round(_min_clear(avoided["path"]), 2),
      "nm (radius", ISLAND[2], ") | reaches", round(avoided["path"][-1]["lat"], 3))
check("obstacle count reported", avoided.get("avoids_islands") == 1)
check("route detours clear of the island (>= ~radius)", _min_clear(avoided["path"]) >= ISLAND[2] * 0.85)
check("still reaches the mark A (44.22)", abs(avoided["path"][-1]["lat"] - 44.22) < 0.05)
check("note names the avoided island", "1 charted island" in avoided.get("note", ""))

# --- polygon exclusion-zone avoidance -----------------------------------------------------------
print("exclusion-zone avoidance:")
# a no-go box straddling the straight-north line boat(43.98)→A(44.22) at lon -82.0
BOX = [[-82.02, 44.08], [-81.98, 44.08], [-81.98, 44.12], [-82.02, 44.12], [-82.02, 44.08]]  # (lon,lat)
def _in_box(path):
    return sum(1 for p in path if routing._pt_in_ring(p["lon"], p["lat"], BOX))

reoptimize._cache["key"] = None
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}]}   # no zones
thru = reoptimize.get_reoptimize()
check("without the zone, the route enters the box", _in_box(thru["path"]) > 0)

reoptimize._cache["key"] = None
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}],
    "obstacles": {"islands": [], "zones": [{"name": "NoGo", "type": "exclusion",
                                            "geometry": {"coordinates": [BOX]}}]}}
skirt = reoptimize.get_reoptimize()
print("  avoids_zones", skirt.get("avoids_zones"), "| points inside box", _in_box(skirt["path"]),
      "| reaches", round(skirt["path"][-1]["lat"], 3))
check("zone count reported", skirt.get("avoids_zones") == 1)
check("route stays OUT of the exclusion zone", _in_box(skirt["path"]) == 0)
check("still reaches the mark A", abs(skirt["path"][-1]["lat"] - 44.22) < 0.05)
check("note names the exclusion zone", "exclusion zone" in skirt.get("note", ""))

# a CIRCLE zone parses to a disk (folded into avoid)
reoptimize._cache["key"] = None
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}],
    "obstacles": {"islands": [], "zones": [{"name": "Ring", "type": "hazard",
                                            "geometry": {"center": [44.10, -82.0], "radius_nm": 1.0}}]}}
circ = reoptimize.get_reoptimize()
check("circle zone → disk avoidance (avoids_islands counts it)", circ.get("avoids_islands") == 1)

# --- na paths ------------------------------------------------------------------------------------
print("na paths:")
reoptimize._cache["key"] = None
NAV._latest = lambda: {"lat": None, "lon": None}
check("no fix → unavailable", reoptimize.get_reoptimize().get("available") is False)
reoptimize._cache["key"] = None
NAV.get_navigator = lambda route=None: {"available": False, "note": "no fix"}
check("no navigator → unavailable", reoptimize.get_reoptimize().get("available") is False)

# --- reoptimize with NO playbook aboard → still routes, vs_playbook unavailable -------------------
print("no playbook:")
reoptimize._cache["key"] = None
NAV.get_navigator = lambda route=None: {"available": True, "route": "race", "next_mark": {"name": "A"}}
NAV._latest = lambda: {"lat": 44.05, "lon": -82.0, "tws": 12.0, "twd": 0.0}
deviation._load_playbook = lambda: {}
r = reoptimize.get_reoptimize()
check("routes even with no playbook", r.get("available") is True)
check("vs_playbook unavailable (nothing to compare)", (r.get("vs_playbook") or {}).get("available") is False)

print("\n", "ALL PASS" if ok else "FAILURES ABOVE")
raise SystemExit(0 if ok else 1)
