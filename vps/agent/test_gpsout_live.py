"""GPS-OUT — the engine's side of the live plotter leg (the n2kout side: pi/n2kout/test_n2kout_app.py).

Pins:
  * the own-ship fix for the plotter is lat AND lon from ONE source, live cache only, and a fix
    older than FIX_MAX_AGE_S is no fix;
  * no fresh fix → /gps/show refuses ("No GPS fix. Not sent to GPS."), nothing posted;
  * the route posted to n2kout carries the course token, and GET /gps/fix serves the fix + the
    token, which changes on every course change (so n2kout ends a route for an old course);
  * /course/load stops the broadcast (POST /stop to n2kout).

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_gpsout_live.py
"""
import os
import sys
import tempfile
import threading
import time

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "engine"))
_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")
os.environ["ONBOARD_LIVE_WS"] = "false"

from app import datasource_onboard, gpsout, orinroute  # noqa: E402
from app import navigator as NAV  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


print("own-ship fix: one source, live only:")
src = object.__new__(datasource_onboard.OnboardSource)        # no archive, no websocket
src._live, src._live_lock = {}, threading.Lock()
now = time.time()
LAT, LON = "navigation.position.latitude", "navigation.position.longitude"
check("empty cache → None", src.latest_position() is None)
src._live[(LAT, "gpsA")] = (now - 1, 44.1)
check("lat without a lon from the same source → None", src.latest_position() is None)
src._live[(LON, "gpsB")] = (now - 1, -82.9)
check("lat from A + lon from B is never paired", src.latest_position() is None)
src._live[(LON, "gpsA")] = (now - 2, -82.1)
p = src.latest_position()
check("A's lat with A's lon; time = the older of the two", p == (44.1, -82.1, now - 2))
src._live[(LAT, "gpsA")] = (now - 60, 44.1)                   # past the live cache TTL
src._live[(LON, "gpsA")] = (now - 60, -82.1)
check("only stale entries → None (no archive fallback)", src.latest_position() is None)


class DS:
    def __init__(self, fix):
        self.fix = fix

    def latest_position(self):
        return self.fix


gpsout.datasource.active = lambda: DS((44.0, -82.0, now - 3))
check("3 s old → a fix", gpsout.own_fix(now) == (44.0, -82.0, now - 3))
gpsout.datasource.active = lambda: DS((44.0, -82.0, now - gpsout.FIX_MAX_AGE_S - 1))
check("older than FIX_MAX_AGE_S → no fix", gpsout.own_fix(now) is None)
gpsout.datasource.active = lambda: object()                   # the cloud: no live position
check("a source without live positions → no fix", gpsout.own_fix(now) is None)

print("no fix → refuse:")
PATH = [{"lat": 44.0 + i * 0.01, "lon": -82.0} for i in range(6)]
gpsout.deviation._load_playbook = lambda: {"recommended": "a",
                                           "variants": [{"id": "a", "route": {"path": PATH}}]}
gpsout._course_ok = lambda ro: True
NAV.get_navigator = lambda route=None: {"available": False}
NAV._latest = lambda: {"lat": 44.003, "lon": -82.0}            # the navigator's (maybe old) read
posts = []
gpsout._post = lambda path, payload=None: (posts.append((path, payload)) or
                                           {"broadcasting": True, "route": "C4 a",
                                            "n_waypoints": 6})
gpsout.datasource.active = lambda: DS(None)
r = gpsout.show()
check("no fresh fix → 'No GPS fix. Not sent to GPS.'", r == {
    "shown": False, "dest_index": None, "note": "No GPS fix. Not sent to GPS."})
check("…and nothing posted to n2kout", posts == [])
check("_dest_index with no boat → None, never waypoint 2",
      gpsout._dest_index(PATH, None) is None and gpsout._dest_index(PATH, (None, None)) is None)

print("fresh fix → sent, with the course token:")
gpsout.datasource.active = lambda: DS((44.003, -82.0, time.time()))
orinroute.set_load_id("race", "L1")
NAV.active_route = lambda: "race"
r = gpsout.show()
check("shown, steer-to the first waypoint ahead", r["shown"] and r["dest_index"] == 1)
check("the route carries the course token", posts[-1][1]["course"] == "L1:race")
check("no land chart → not 'shore checked'", r["shore_checked"] is False and r["warning"])
f = gpsout.fix_feed()
check("GET /gps/fix shape: fix {lat, lon, t} + course", f["fix"]["lat"] == 44.003
      and abs(f["fix"]["t"] - time.time()) < 5 and f["course"] == "L1:race")
orinroute.bump_load_id("race")                                 # a practice course / switch
check("a course change changes the token", gpsout.fix_feed()["course"] not in (None, "L1:race"))
gpsout.datasource.active = lambda: DS(None)
check("no fix → fix null, token still there", gpsout.fix_feed()["fix"] is None
      and gpsout.fix_feed()["course"])

print("the engine endpoints:")
from fastapi.testclient import TestClient  # noqa: E402

import engine_app  # noqa: E402


class _DS:
    def save_course(self, route, marks):
        pass


engine_app.datasource.active = lambda: _DS()
engine_app.navigator.set_active = lambda route: None
client = TestClient(engine_app.app)
DEF = {"race_id": "r1", "courses": [{"id": "c", "start": {"lat": 43.0, "lon": -82.4},
                                     "marks": [], "finish": {"points": [{"lat": 45.8,
                                                                         "lon": -84.6}]}}]}
posts.clear()
r = client.post("/course/load", json={"definition": DEF, "course_id": "c"})
check("/course/load → loaded, and n2kout told to stop", r.json().get("loaded")
      and ("/stop", None) in posts)
posts.clear()
r = client.post("/course/load", json={"definition": {"courses": []}})
check("a failed load leaves the plotter alone", not r.json().get("loaded") and posts == [])
gpsout.datasource.active = lambda: DS((44.0, -82.0, time.time()))
r = client.get("/gps/fix").json()
check("GET /gps/fix served", r["fix"]["lat"] == 44.0 and "course" in r)

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
