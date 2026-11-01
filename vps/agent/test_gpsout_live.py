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
gpsout._post = lambda path, payload=None, **kw: (posts.append((path, payload)) or
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

print("the send uses the fresh fix, never the navigator's position:")
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "position": {"lat": PATH[5]["lat"], "lon": -82.0},
                                        "next_mark": None}
NAV._marks = lambda route: []
NAV._latest = lambda: {"lat": PATH[5]["lat"], "lon": -82.0}
gpsout.datasource.active = lambda: DS((44.003, -82.0, time.time()))
orinroute.set_load_id("race", "L1")
r = gpsout.show()
check("navigator says the boat is at the end, the fresh fix at the start → steer-to 1",
      r["shown"] and r["dest_index"] == 1)

print("course-mark waypoints are flagged for n2kout:")
COURSE = [{"name": "Start", "lat": PATH[0]["lat"], "lon": -82.0},
          {"name": "Top", "lat": PATH[3]["lat"], "lon": -82.0},
          {"name": "Finish", "lat": PATH[5]["lat"], "lon": -82.0}]
NAV._marks = lambda route: COURSE
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Top", "index": 1}}
r = gpsout.show()
marks = [i for i, w in enumerate(posts[-1][1]["waypoints"]) if w["mark"]]
check(f"marks at waypoints 0, 3, 5 [{marks}]", r["shown"] and marks == [0, 3, 5])
idxs = [w["mark_idx"] for w in posts[-1][1]["waypoints"] if w["mark"]]
check(f"…each with its position in the course [{idxs}]", idxs == [0, 1, 2])
gpsout._NEXT_MARK["at"] = None
check("the fix feed carries the navigator's next-mark index (Top = 1)",
      gpsout.fix_feed()["next_mark_index"] == 1)
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Finish", "index": 2}}
check("…cached for 5 s (n2kout polls every second)", gpsout.fix_feed()["next_mark_index"] == 1)
gpsout._NEXT_MARK["at"] = None
check("…then the navigator's new verdict (Finish = 2)",
      gpsout.fix_feed()["next_mark_index"] == 2)
NAV.get_navigator = lambda route=None: {"available": False}
gpsout._NEXT_MARK["at"] = None
check("no navigator → None", gpsout.fix_feed()["next_mark_index"] is None)

print("the next-mark cache never outlives a course:")
orinroute.set_load_id("race", "A1")
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Finish", "index": 2}}
gpsout._NEXT_MARK["at"] = None
tok_a, idx_a = gpsout.course_progress()
check("course A: index 2 cached", idx_a == 2 and tok_a.startswith("A1:"))
orinroute.set_load_id("race", "B1")                 # a new course, within the 5 s
NAV.get_navigator = lambda route=None: {"available": True, "route": "race",
                                        "next_mark": {"name": "Start", "index": 0}}
f = gpsout.fix_feed()
check("course B within 5 s: B's token with B's index (0), never A's 2",
      f["course"].startswith("B1:") and f["next_mark_index"] == 0)
held2, rel2 = threading.Event(), threading.Event()


def _hold2():
    with orinroute.COURSE_LOCK:
        held2.set()
        rel2.wait(5)


threading.Thread(target=_hold2, daemon=True).start()
held2.wait(2)
t0 = time.time()
tok, idx = gpsout.course_progress()
rel2.set()
check("lock busy (a load running) → no index this second, quickly",
      idx is None and time.time() - t0 < 1.0)

print("leg check — the engine approves each new leg n2kout wants:")
ISLAND = {"recommended": "a", "obstacles": {"islands": [{"lat": 44.02, "lon": -82.0,
                                                         "radius_nm": 0.1}]},
          "variants": [{"id": "a", "route": {"path": PATH}}]}
gpsout.deviation._load_playbook = lambda: ISLAND
NAV.get_navigator = lambda route=None: {"available": False}
tok = gpsout.course_token()
through = {"from": {"lat": 44.0, "lon": -82.0}, "to": {"lat": 44.04, "lon": -82.0}, "course": tok}
beside = {"from": {"lat": 44.0, "lon": -81.99}, "to": {"lat": 44.04, "lon": -81.99},
          "course": tok}
r = gpsout.leg_check(through)
check("line through the island → not clear (obstacle)", r == {"clear": False,
                                                              "reason": "obstacle"})
check("line beside it → clear", gpsout.leg_check(beside)["clear"] is True)
check("another course → not clear", gpsout.leg_check(dict(beside, course="old:race"))
      == {"clear": False, "reason": "course changed"})
check("garbage → not clear", gpsout.leg_check({"from": {"lat": "x"}})["clear"] is False
      and gpsout.leg_check({"from": {"lat": float("nan"), "lon": 1},
                            "to": {"lat": 1, "lon": 1}})["clear"] is False)
_real_obst = gpsout._obstacles
gpsout._obstacles = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("chart"))
check("a failing check → not clear", gpsout.leg_check(beside) == {"clear": False,
                                                                  "reason": "error"})
gpsout._obstacles = _real_obst
held = threading.Event()
done = threading.Event()


def _hold():
    with orinroute.COURSE_LOCK:
        held.set()
        done.wait(5)


threading.Thread(target=_hold, daemon=True).start()
held.wait(2)
t0 = time.time()
r = gpsout.leg_check(beside)
done.set()
check("course lock busy (a load running) → not clear within ~1.5 s",
      r == {"clear": False, "reason": "busy"} and time.time() - t0 < 2.5)

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
seen = {}
_stub = gpsout._post


def _post_probe(path, payload=None, **kw):
    if path == "/stop":
        free = []
        th = threading.Thread(target=lambda: free.append(
            orinroute.COURSE_LOCK.acquire(timeout=0) and not orinroute.COURSE_LOCK.release()))
        th.start()
        th.join()
        seen.update(lock_held=free == [False], payload=payload, timeout=kw.get("timeout"),
                    token=gpsout.course_token())
    return _stub(path, payload, **kw)


gpsout._post = _post_probe
r = client.post("/course/load", json={"definition": DEF, "course_id": "c"})
check("/course/load → loaded, and n2kout told to stop", r.json().get("loaded")
      and any(p == "/stop" for p, _ in posts))
check("…from INSIDE the course lock (no /gps/show can slip in before it)", seen.get("lock_held"))
check("…keeping only a route for the NEW course (a late stop can't end it)",
      seen["payload"] == {"keep_course": seen["token"]})
check("…with a short timeout (the lock is held)", seen["timeout"] is not None
      and seen["timeout"] <= 2)
gpsout._post = _stub
posts.clear()
r = client.post("/course/load", json={"definition": {"courses": []}})
check("a failed load leaves the plotter alone", not r.json().get("loaded") and posts == [])
gpsout.datasource.active = lambda: DS((44.0, -82.0, time.time()))
r = client.get("/gps/fix").json()
check("GET /gps/fix served", r["fix"]["lat"] == 44.0 and "course" in r)
r = client.post("/gps/leg-check", json=dict(through, course=gpsout.course_token())).json()
check("POST /gps/leg-check served (through the island → not clear)", r["clear"] is False)

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
