"""Monitor backend for the grill fixes (#102 G37 G84 G86).

G84 / #240: the map may point our hull along heading only when the heading is fresh and its
reference (true / magnetic) is known. /conditions folds both compasses into one bare `heading`,
so own() now reads /conditions/full and reports heading_ref + heading_age_s + cog_age_s.
G86: the snapshot carries the course marks (in order, per course) so the map can fit to the
course instead of to every tracker fix. G37: the tracker rule is tri-state (unset is not "no").

Run in-container (make ci does this): see test_monitor_freshness.py.
"""
from app import monitor as M

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


STRIP = {"available": True, "lat": 44.0, "lon": -82.5, "sog": 6.0, "cog": 349.0, "heading": 339.0,
         "data_age_seconds": 2, "stale": False}


def chan(value, age):
    return {"preferred": {"source": "x", "value": value, "age_s": age}, "freshest_age_s": age}


def own_with(full):
    real_http, real_pw, real_tok = M._http_json, M._BOAT_PASSWORD, M._agent_token

    def http(url, **kw):
        if url.endswith("/conditions/full"):
            if isinstance(full, Exception):
                raise full
            return full
        return STRIP
    M._http_json, M._BOAT_PASSWORD, M._agent_token = http, "x", (lambda: "tok")
    try:
        return M.own()
    finally:
        M._http_json, M._BOAT_PASSWORD, M._agent_token = real_http, real_pw, real_tok


print("heading reference and ages")
o = own_with({"available": True, "channels": {"heading_true": chan(338.6, 3), "cog": chan(350.1, 1)}})
check("true heading named true", o["heading_ref"] == "true" and o["heading"] == 338.6)
check("heading age reported", o["heading_age_s"] == 3)
check("cog age reported", o["cog_age_s"] == 1 and o["cog"] == 350.1)
o = own_with({"available": True, "channels": {"heading_mag": chan(330.0, 4)}})
check("magnetic heading named magnetic", o["heading_ref"] == "magnetic" and o["heading"] == 330.0)
o = own_with({"available": True, "channels": {"heading_true": chan(10.0, 2), "heading_mag": chan(20.0, 2)}})
check("true wins over magnetic (same order as the strip)", o["heading_ref"] == "true" and o["heading"] == 10.0)
o = own_with(RuntimeError("down"))
check("full view down: reference unknown, strip values kept",
      o["available"] and o["heading_ref"] is None and o["heading_age_s"] is None and o["heading"] == 339.0)

print("course marks for the map fit")
DEF = {"courses": [
    {"id": "shore", "start": {"lat": 43.05, "lon": -82.42},
     "marks": [{"name": "Cove", "lat": 44.0, "lon": -82.6}, {"name": "Isl", "type": "island", "lat": 1, "lon": 1}],
     "finish": {"points": [{"lat": 45.8, "lon": -84.6}, {"lat": 45.9, "lon": -84.64}]}},
    {"id": "cove", "start": {"lat": 43.05, "lon": -82.42}, "marks": [{"name": "Far", "lat": 45.0, "lon": -81.6}]}]}
mk = M.course_marks(DEF)
check("every positioned mark of every course, islands left out",
      [(m["name"], m["course"]) for m in mk] == [("Start", 0), ("Cove", 0), ("Finish", 0), ("Start", 1), ("Far", 1)])
check("no courses -> no marks", M.course_marks({}) == [])
real_fleet, real_own = M.fleet, M.own
M.fleet, M.own = (lambda d, demo=False: {"fixes": []}), (lambda: {"available": False})
try:
    snap = M.snapshot(DEF)
finally:
    M.fleet, M.own = real_fleet, real_own
check("snapshot carries the marks", len(snap["marks"]) == 5)

print("tracker rule is tri-state")
for val, want in ((True, True), (False, False), (None, None)):
    f = M._fleet({"rules_profile": {"tracker_permitted": val}, "tracker": {"provider": "none"}})
    check(f"tracker_permitted={val} -> {want}", f["onboard_permitted"] is want)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
