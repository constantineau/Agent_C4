"""#148 — a waypoint's pocket opens land only, never a zone or an island disk.

Since #242 the Lab opens a 0.5 nm pocket of water only at a waypoint with a reviewed chart
override (the course's `chart_overrides`), never by default: land is never opened for a route. Before, the pocket was cut after the race's zones
and island disks were drawn and cleared every layer, so a start or mark on a zone's edge opened
a 0.5 nm hole into the zone. Pinned here (no network: the shoreline is a stub polygon):
  1) the issue's repro: a zone cell inside the start's pocket stays blocked;
  2) an island disk cell inside a mark's pocket stays blocked;
  3) the pocket opens the shoreline only with an override: a start inside land stays land
     without one (#242), is water with one, and stays land with carve_waypoints=False;
  4) a zone drawn over that land stays blocked inside the pocket;
  5) the Orin's build (carve_waypoints=False) is unchanged.
"""
from app.geo import coastline
from app.geo import obstacles as OB

ok = True


def check(name, cond, detail=""):
    global ok
    print(f"[{'OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    ok = ok and bool(cond)


BBOX = (45.5, 44.5, -84.5, -83.5)


OVERRIDE = {"Start": {"by": "Cole", "at": 1762700000, "why": "chart artefact"},
            "M1": {"by": "Cole", "at": 1762700000, "why": "chart artefact"}}


def race(zones=(), marks=(), start=(45.0, -84.0), overrides=OVERRIDE):
    c = {"id": "c", "start": {"lat": start[0], "lon": start[1]}, "marks": list(marks),
         "finish": {"points": [{"lat": 45.3, "lon": -84.0}]}}
    if overrides:
        c["chart_overrides"] = dict(overrides)
    return {"race_id": "t148", "zones": list(zones), "courses": [c]}


ZONE = {"name": "box", "type": "exclusion",
        "geometry": {"north": 45.02, "south": 45.00, "west": -84.02, "east": -83.98}}

# 1) the issue's repro: the start sits on the zone's south edge
f = OB.build_for_course(race([ZONE]), "c", BBOX, coastline_on=False, use_cache=False)
check("zone cell inside the start's pocket is blocked", f.blocked(45.01, -84.0))
check("zone cell outside the pocket is blocked", f.blocked(45.01, -83.985))
check("open water inside the pocket is clear", not f.blocked(44.995, -84.0))

# 2) a mark next to a hazard island: the disk is not opened by the mark's pocket
isl = {"name": "Rock", "type": "island", "rounding": "none", "lat": 45.15, "lon": -84.0,
       "radius_nm": 1.0}
mark = {"name": "M1", "type": "mark", "rounding": "port", "lat": 45.15 + 1.2 / 60, "lon": -84.0}
f = OB.build_for_course(race(marks=[isl, mark]), "c", BBOX, coastline_on=False, use_cache=False)
check("island disk cell inside the mark's pocket is blocked", f.blocked(45.15 + 0.8 / 60, -84.0))
check("the mark itself is clear", not f.blocked(mark["lat"], mark["lon"]))

# 3) and 4) a stub shoreline: land from 44.9 to 45.05 N across the box, so the start is on land
LAND = [[(-84.5, 44.9), (-83.5, 44.9), (-83.5, 45.05), (-84.5, 45.05)]]
_orig = (coastline.ensure_global, coastline.layers_in_bbox, coastline.active_source)
coastline.ensure_global = lambda cache_dir=None: None
coastline.layers_in_bbox = lambda bbox, cache_dir=None: {"land": [LAND], "lakes": [], "islands": []}
coastline.active_source = lambda: "stub"
try:
    f = OB.build_for_course(race(), "c", BBOX, use_cache=False, source="natural_earth")
    check("shoreline loaded", f.land == "ok" and f.layers["coastline"] > 0, f.land)
    check("the override's pocket opens land at the start", not f.blocked(45.0, -84.0))
    check("land outside the pocket stays blocked", f.blocked(45.0, -83.9))
    f0 = OB.build_for_course(race(), "c", BBOX, use_cache=False, source="natural_earth",
                             carve_waypoints=False)
    check("no pocket: the start is land", f0.blocked(45.0, -84.0))
    f1 = OB.build_for_course(race(overrides=None), "c", BBOX, use_cache=False,
                             source="natural_earth")
    check("#242: no override, no pocket: the start stays land", f1.blocked(45.0, -84.0))

    f = OB.build_for_course(race([ZONE]), "c", BBOX, use_cache=False, source="natural_earth")
    check("zone over land inside the pocket stays blocked", f.blocked(45.01, -84.0))
    check("land in the pocket south of the zone is open", not f.blocked(44.995, -84.0))
finally:
    coastline.ensure_global, coastline.layers_in_bbox, coastline.active_source = _orig

# 5) the Orin's race layer: no pocket at all
f = OB.build_for_course(race([ZONE], marks=[isl, mark]), "c", BBOX, coastline_on=False,
                        use_cache=False, carve_waypoints=False)
check("Orin build: zone and disk blocked", f.blocked(45.01, -84.0) and f.blocked(45.15, -84.0))
check("Orin build: open water clear", not f.blocked(44.995, -84.0))

print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
