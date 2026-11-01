"""Re-routing is button-only (#125).

Pins:
  * NOTHING computes a route on its own: GET /reoptimize, GET /reroute, /strategy going off-book and
    the GPS output only read what a crew press made (a trip-wire on the isochrone proves it);
  * the Pi fallback's button flow: 202 started → a second press is refused (busy) → done, stored
    on disk with who asked; a failed run says why and keeps the last good route; nothing to route
    is refused in a short sentence;
  * the Orin's side: an unreachable Orin is refused with 503 and the Pi fallback is then one more
    press; GET /reroute carries both engines and says which route is newest;
  * every press and every finished route is logged once (time, device, weather age, models);
  * "show on GPS" sends the stored route (Orin or Pi), never a fresh one;
  * the real engine endpoints wire all of it.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_reroute_button.py
"""
import json
import os
import socket
import sys
import tempfile
import time

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "engine"))

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")

from app import deviation, gpsout, orinroute, reoptimize, reroutelog, routing  # noqa: E402
from app import navigator as NAV  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


os.environ["ORIN_REROUTE_URL"] = f"http://127.0.0.1:{closed_port()}"
os.environ["ORIN_REROUTE_TIMEOUT_S"] = "2"

# a small fast course: 6 kn everywhere sailable, wind from the east, marks due north
routing._polar_speed = lambda tws, twa: 6.0 if twa >= 40 else 0.0
routing.weather.wind_at = lambda lat, lon, epoch: (12.0, 90.0)
MARKS = [{"seq": 1, "name": "Start", "lat": 44.0, "lon": -82.0},
         {"seq": 2, "name": "A", "lat": 44.1, "lon": -82.0},
         {"seq": 3, "name": "Finish", "lat": 44.2, "lon": -82.0}]
STATE = {"nav": {"available": True, "route": "race", "next_mark": {"name": "A", "index": 1}},
         "latest": {"lat": 44.02, "lon": -82.0, "tws": 12.0, "twd": 90.0}}
NAV.get_navigator = lambda route=None: STATE["nav"]
NAV._marks = lambda route: MARKS
NAV._latest = lambda: STATE["latest"]
deviation._load_playbook = lambda: {"race_id": "u", "variants": [{"id": "m"}]}

# trip-wire: count every isochrone computation; `allowed` is set only around a crew press
_real_compute = reoptimize._compute
CALLS = {"n": 0, "allowed": False}


def counted(*a, **k):
    CALLS["n"] += 1
    if not CALLS["allowed"]:
        raise AssertionError("a route was computed without the button")
    return _real_compute(*a, **k)


reoptimize._compute = counted
SENT = []
gpsout._post = lambda path, payload=None: SENT.append((path, payload)) or {
    "broadcasting": True, "route": (payload or {}).get("name"),
    "n_waypoints": len((payload or {}).get("waypoints") or [])}


def wait_done(timeout=30):
    t = reoptimize._thread
    if t is not None:
        t.join(timeout)


print("nothing runs on its own:")
r = reoptimize.get_reoptimize()
check("GET /reoptimize before any press: no route, nothing computed",
      r["available"] is False and r["note"] == "No Pi route yet." and CALLS["n"] == 0)
g = orinroute.tile(compact=True)
check("GET /reroute: Orin unreachable, Pi idle, no route, nothing computed",
      g["orin"]["reachable"] is False and g["pi"]["state"] == "idle" and g["latest"] is None
      and CALLS["n"] == 0)
s = gpsout.show(source="reroute")
check("show on GPS with no route made: refused, nothing computed, nothing sent",
      s["shown"] is False and s["note"] == "No new route yet. Press Re-route now."
      and CALLS["n"] == 0 and not SENT)
s = gpsout.show(source="reoptimize")
check("…the Pi source too", s["shown"] is False and CALLS["n"] == 0 and not SENT)

print("press on the Orin, which is off:")
code, body = orinroute.run("iPad 1")
check("503: Orin unreachable, not started", code == 503 and body["orin"] == "unreachable")
check("nothing computed on the Pi by itself", CALLS["n"] == 0 and reoptimize._job["state"] == "idle")

print("the Pi fallback (a second, explicit press):")
STATE["latest"] = {"lat": None, "lon": None}
code, body = reoptimize.run(device="iPad 1")
check("no position → 409, plain", code == 409 and body["detail"] == "No position yet.")
STATE["latest"] = {"lat": 44.02, "lon": -82.0, "tws": 12.0, "twd": 90.0}
CALLS["allowed"] = True
gate = __import__("threading").Event()
_slow = reoptimize._compute


def slow(*a, **k):
    gate.wait(10)
    return _slow(*a, **k)


reoptimize._compute = slow
code, body = reoptimize.run(device=" iPad 1\n")
check("202 started on the Pi", code == 202 and body["ok"] and body["engine"] == "pi")
st = reoptimize.status()
check("status: running, with progress, who asked",
      st["state"] == "running" and st["asked_by"] == "iPad 1" and "pct" in st["progress"])
code2, body2 = reoptimize.run(device="iPad 2")
check("a second press while it runs → 409 busy", code2 == 409 and body2["busy"] is True
      and body2["detail"] == "A re-route is already running.")
g = orinroute.tile(compact=True)
check("GET /reroute shows the Pi running", g["pi"]["state"] == "running")
gate.set()
wait_done()
reoptimize._compute = counted
st = reoptimize.status()
check("done: state done, route stored", st["state"] == "done" and st["result"]["available"]
      and st["result"]["asked_by"] == "iPad 1")
check("compact status has no track", "path" not in st["result"])
full = reoptimize.get_reoptimize()
check("GET /reoptimize returns it (track included), labelled", full["available"]
      and len(full["path"]) > 2 and full["label"] == "Off the playbook" and full["source"] == "pi"
      and full["age_s"] is not None)
check("stored on disk", os.path.exists(os.path.join(_tmp, "engine", "reroute-pi.json")))
n_after = CALLS["n"]
check("exactly one computation for one press", n_after == 1)
CALLS["allowed"] = False
reoptimize.get_reoptimize()
orinroute.tile()
check("reading it again computes nothing", CALLS["n"] == n_after)

print("a failed Pi run keeps the last route:")
CALLS["allowed"] = True
reoptimize._compute = lambda *a, **k: {"available": False, "note": "No new route. Couldn't reach A (3 nm short)."}
code, _b = reoptimize.run(device="iPad 2")
wait_done()
st = reoptimize.status()
check("error with its reason", code == 202 and st["state"] == "error"
      and st["detail"].startswith("No new route."))
check("the last good route is still there", st["result"] and st["result"]["asked_by"] == "iPad 1")


def boom(*a, **k):
    raise RuntimeError("bad polar")


reoptimize._compute = boom
reoptimize.run(device="iPad 2")
wait_done()
check("a crash says 'Re-route failed. Try again.'", reoptimize.status()["detail"] == "Re-route failed. Try again.")
reoptimize._compute = counted
CALLS["allowed"] = False

print("show on GPS sends the stored route:")
s = gpsout.show(source="reroute")
check("newest route (the Pi's) sent, not recomputed", s["shown"] and SENT and SENT[-1][0] == "/route"
      and SENT[-1][1]["name"] == "C4 REROUTE" and CALLS["n"] == n_after)

print("the Orin's route, and which is newest:")
t_new = int(time.time()) + 5
RESULT = {"schema": "c4.reroute/v1", "job_id": "o1", "available": True, "finished_at": t_new,
          "asked_by": "iPad 3", "asked_at": t_new - 60, "marks": ["A", "Finish"],
          "points": [{"lat": 44.02, "lon": -82.0, "t": 1}, {"lat": 44.1, "lon": -82.0, "t": 2},
                     {"lat": 44.2, "lon": -82.0, "t": 3}], "leg_end_idx": [1, 2],
          "vs_plan": {"available": True, "gain_min": 12},
          "weather": {"models": [{"model": "gfs", "cycle": "2026110106", "age_h": 3.2},
                                 {"model": "hrrr", "cycle": "2026110109", "age_h": 0.8}],
                      "newest_age_h": 0.8, "oldest_age_h": 3.2, "online": False},
          "warnings": ["No chart aboard. Route doesn't check the shore."]}


def orin(responses):
    def call(method, path, body=None):
        return responses[(method, path)]
    return call


code, body = orinroute.run("iPad 3", call=orin({("POST", "/run"): (202, {"ok": True, "job_id": "o1"})}))
check("Orin press → 202", code == 202)
st_done = {"state": "done", "job_id": "o1", "result_job_id": "o1", "last": {"job_id": "o1", "state": "done"}}
call = orin({("GET", "/status"): (200, st_done), ("GET", "/result"): (200, RESULT)})
g = orinroute.tile(call=call, compact=True)
check("GET /reroute: Orin route fetched, newest = orin", g["latest"] == "orin"
      and g["result"]["job_id"] == "o1" and g["pi"]["state"] == "error")
check("compact: no track", "points" not in g["result"])
stored = orinroute.stored()
check("course positions kept from the request (laps)", stored.get("mark_idx") == [1, 2])
s = gpsout.show(source="reroute")
check("show on GPS sends the Orin's route", s["shown"] and SENT[-1][1]["name"] == "C4 REROUTE"
      and len(SENT[-1][1]["waypoints"]) >= 2 and CALLS["n"] == n_after)
s = gpsout.show(source="orin")
check("…or by name", s["shown"])

print("every run is logged once:")
orinroute.tile(call=call)                                     # seen again: not logged again
h = reroutelog.history(100)
ev = [(r["event"], r["engine"], r.get("outcome")) for r in reversed(h)]
check("Orin unreachable press logged", ("pressed", "orin", "unreachable") in ev)
check("Pi presses logged: refused, started, busy",
      ("pressed", "pi", "refused") in ev and ("pressed", "pi", "started") in ev
      and ("pressed", "pi", "busy") in ev)
pi_done = [r for r in h if r["event"] == "done" and r["engine"] == "pi"]
check("Pi route logged with who and when", len(pi_done) == 1 and pi_done[0]["asked_by"] == "iPad 1"
      and pi_done[0]["finished_at"] and pi_done[0]["weather"]["models"][0]["model"] == "open-meteo")
check("Pi failures logged with the reason",
      sum(1 for r in h if r["event"] == "failed" and r["engine"] == "pi") == 2)
o_done = [r for r in h if r["event"] == "done" and r["engine"] == "orin"]
check("Orin route logged once: device, weather age, models, gain",
      len(o_done) == 1 and o_done[0]["asked_by"] == "iPad 3"
      and o_done[0]["weather"]["newest_age_h"] == 0.8
      and [m["model"] for m in o_done[0]["weather"]["models"]] == ["gfs", "hrrr"]
      and o_done[0]["gain_min"] == 12)
g = orinroute.tile(call=orin({("GET", "/status"): (200, {
    "state": "error", "result_job_id": "o1",
    "last": {"state": "error", "job_id": "o2", "asked_by": "iPad 3", "detail": "Re-route took too long. Try again."}})}))
orinroute.tile(call=orin({("GET", "/status"): (200, {
    "state": "error", "result_job_id": "o1",
    "last": {"state": "error", "job_id": "o2", "detail": "Re-route took too long. Try again."}})}))
o_fail = [r for r in reroutelog.history(100) if r["event"] == "failed" and r["engine"] == "orin"]
check("an Orin failure logged once, with why", len(o_fail) == 1 and o_fail[0]["job_id"] == "o2")

print("a route keeps the course it was made for (#143 review):")
pi_r = reoptimize.get_reoptimize()
check("Pi route: course hash, its own departure + arrival",
      pi_r["course_hash"] == orinroute.course_hash("race", MARKS)
      and abs((pi_r["finish_eta"] - pi_r["depart_epoch"]) - pi_r["eta_min"] * 60) <= 1
      and pi_r["depart_epoch"] <= pi_r["asked_at"] + 1)
check("Orin route: course hash from its request", orinroute.stored()["course_hash"]
      == orinroute.course_hash("race", MARKS))
g = orinroute.tile(call=call, compact=True)
check("same course → both current on the tile", g["result"]["old_course"] is False
      and g["pi"]["result"]["old_course"] is False)
n_sent = len(SENT)
moved = [dict(m) for m in MARKS]
moved[1]["lat"] = 44.12                          # mark A moved, same names, course reloaded
NAV._marks = lambda route: moved
s = gpsout.show(source="reroute")
check("a moved mark → refused, old coordinates never broadcast",
      s["shown"] is False and s["note"] == "Route is for an old course. Press Re-route now."
      and len(SENT) == n_sent)
check("…by name too", gpsout.show(source="orin")["shown"] is False
      and gpsout.show(source="reoptimize")["note"] == "Route is for an old course. Press Re-route now.")
g = orinroute.tile(call=call, compact=True)
check("the tile flags both routes as for an old course (kept, not deleted)",
      g["result"]["old_course"] is True and g["pi"]["result"]["old_course"] is True
      and g["result"]["job_id"] == "o1")
NAV._marks = lambda route: MARKS
STATE["nav"] = dict(STATE["nav"], route="race2")    # another course, same marks
check("another course name → old course", gpsout.show(source="reroute")["shown"] is False)
STATE["nav"] = dict(STATE["nav"], route="race")
check("back on the course it was made for → sent again", gpsout.show(source="reroute")["shown"])
NAV.get_navigator = lambda route=None: {"available": False, "note": "No course loaded."}
check("no course aboard → refused", gpsout.show(source="reroute")["shown"] is False)
NAV.get_navigator = lambda route=None: STATE["nav"]
legacy = dict(orinroute.stored())
legacy.pop("course_hash")
reoptimize_ok = gpsout.show(source="reroute")["shown"]
_orig_stored = orinroute.stored
orinroute.stored = lambda: legacy
check("a stored route from before the hash → old course", gpsout.show(source="orin")["note"]
      == "Route is for an old course. Press Re-route now." and reoptimize_ok)
orinroute.stored = _orig_stored

print("an incomplete Orin route is never sent to the GPS:")
orinroute.run("iPad 3", call=orin({("POST", "/run"): (202, {"ok": True, "job_id": "o3"})}))
STALL = dict(RESULT, job_id="o3", finished_at=t_new + 10, complete=False, finish_eta=None,
             vs_plan={"available": False, "note": "Route incomplete."},
             warnings=["Route incomplete."])
st3 = {"state": "done", "job_id": "o3", "result_job_id": "o3", "last": {"job_id": "o3", "state": "done"}}
g = orinroute.tile(call=orin({("GET", "/status"): (200, st3), ("GET", "/result"): (200, STALL)}))
check("stored and newest", g["latest"] == "orin" and g["result"]["complete"] is False)
n_sent = len(SENT)
s = gpsout.show(source="reroute")
check("refused: 'Route incomplete. Not sent to GPS.'", s["shown"] is False
      and s["note"] == "Route incomplete. Not sent to GPS." and len(SENT) == n_sent)
check("…by name too", gpsout.show(source="orin")["note"] == "Route incomplete. Not sent to GPS.")

print("a course reload can't land mid-read (#143 review, races):")
import threading  # noqa: E402

COURSE_A = [dict(m) for m in MARKS]
COURSE_B = [dict(m) for m in MARKS]
COURSE_B[1]["lat"] = 44.12                      # the same course reloaded with mark A moved
holder = {"marks": COURSE_A}
NAV._marks = lambda route: holder["marks"]
in_read, go = threading.Event(), threading.Event()
_nav = NAV.get_navigator


def slow_nav(route=None):
    out = _nav(route)
    in_read.set()                               # the run has read the next mark...
    go.wait(5)                                  # ...and stalls before reading the marks
    return out


def reload_b():
    with orinroute.COURSE_LOCK:                 # what /course/load holds for its whole load
        holder["marks"] = COURSE_B
        res["loaded_at"] = time.time()


res = {}
NAV.get_navigator = slow_nav
reoptimize._compute = lambda *a, **k: {"available": True, "marks": ["A", "Finish"],
                                       "path": [{"lat": 44.02, "lon": -82.0}] * 2}
CALLS["allowed"] = True
t_run = threading.Thread(target=lambda: res.setdefault("run", reoptimize.run(device="iPad 5")))
t_run.start()
in_read.wait(5)
t_load = threading.Thread(target=reload_b)
t_load.start()
time.sleep(0.3)
check("Pi run: the reload waits while the run reads the course", "loaded_at" not in res)
go.set()
t_run.join(5)
t_load.join(5)
wait_done()
NAV.get_navigator = _nav
reoptimize._compute = counted
CALLS["allowed"] = False
pi_r = reoptimize.get_reoptimize()
check("…the run's hash is the course it read (A), not the reload's",
      res["run"][0] == 202 and pi_r["course_hash"] == orinroute.course_hash("race", COURSE_A))
check("…so after the reload it's an old course and the GPS refuses it",
      gpsout.show(source="reoptimize")["note"] == "Route is for an old course. Press Re-route now.")

print("the GPS check and the broadcast are one step:")
holder["marks"] = COURSE_A
orinroute.stored = lambda: None                 # only the Pi route (made on course A)
posting, release = threading.Event(), threading.Event()
_post_orig = gpsout._post
sent_marks = {}


def slow_post(path, payload=None):
    if path == "/route":
        sent_marks["course"] = holder["marks"]
        posting.set()
        release.wait(5)
    return {"broadcasting": True, "route": (payload or {}).get("name"), "n_waypoints": 2}


gpsout._post = slow_post
res = {}
t_show = threading.Thread(target=lambda: res.setdefault("show", gpsout.show(source="reroute")))
t_show.start()
posting.wait(5)
t_load = threading.Thread(target=reload_b)
t_load.start()
time.sleep(0.3)
check("the reload waits until the broadcast is done", "loaded_at" not in res)
release.set()
t_show.join(5)
t_load.join(5)
check("…the route went out on the course it was checked against",
      res["show"]["shown"] and sent_marks["course"] is COURSE_A and "loaded_at" in res)
check("…and the next show sees the reload and refuses",
      gpsout.show(source="reroute")["note"] == "Route is for an old course. Press Re-route now.")
gpsout._post = _post_orig
orinroute.stored = _orig_stored
NAV._marks = lambda route: MARKS
check("the broadcast that holds the lock is bounded (a few seconds)", gpsout.POST_TIMEOUT_S <= 3)

print("a route is bound to the course LOAD, not just its marks (#143 review):")
OLD = "Route is for an old course. Press Re-route now."
reoptimize._compute = counted
check("start: the Pi route is current", gpsout.show(source="reoptimize")["shown"])
# /course/load of the very same marks (say a new exclusion zone or a reversed rounding side):
# it writes a new load id under the lock; nothing about the marks changes
with orinroute.COURSE_LOCK:
    orinroute.set_load_id("race", orinroute.new_load_id())
check("same marks, new load → old course, refused", gpsout.show(source="reoptimize")["note"] == OLD)
g = orinroute.tile(compact=True)
check("…and flagged on the tile", g["pi"]["result"]["old_course"] is True)


class _DS:
    def __init__(self):
        self.saved = []

    def save_practice_course(self, marks):
        self.saved.append(marks)

    def save_active_route(self, route):
        pass


DS = _DS()
NAV.datasource.active = lambda: DS
CALLS["allowed"] = True
reoptimize.run(device="iPad 6")
wait_done()
CALLS["allowed"] = False
check("a fresh press on the new load is current again", gpsout.show(source="reoptimize")["shown"])
before = json.load(open(os.path.join(_tmp, "engine", "course-load.json")))["load_id"]
r = NAV.make_practice_course(1.0)
after = json.load(open(os.path.join(_tmp, "engine", "course-load.json")))
check("a practice course bumps the load id", r["available"] and after["load_id"] != before
      and after["route"] == "practice")
check("…so the route made before it is an old course",
      gpsout.show(source="reoptimize")["note"] == OLD)
lock_seen = {}
DS.save_practice_course = lambda marks: lock_seen.setdefault(
    "held", orinroute.COURSE_LOCK._is_owned())
NAV.make_practice_course(1.0)
check("…and writes its marks under the course lock", lock_seen.get("held") is True)
NAV.set_active("race")                                  # back to the race course
CALLS["allowed"] = True
reoptimize.run(device="iPad 6")
wait_done()
CALLS["allowed"] = False
check("route on 'race' current", gpsout.show(source="reoptimize")["shown"])
NAV.set_active("race")
check("re-selecting the active course changes nothing", gpsout.show(source="reoptimize")["shown"])
NAV.set_active("other")
NAV.set_active("race")
check("a switch away and back → old course (the course may have changed meanwhile)",
      gpsout.show(source="reoptimize")["note"] == OLD)

print("the real engine endpoints:")
from fastapi.testclient import TestClient  # noqa: E402

import engine_app  # noqa: E402

client = TestClient(engine_app.app)
n0 = CALLS["n"]
r = client.get("/reoptimize")
check("GET /reoptimize → the stored route, nothing computed", r.status_code == 200
      and r.json()["available"] and CALLS["n"] == n0)
r = client.get("/reroute?compact=1")
check("GET /reroute?compact=1 → both engines, no tracks", r.status_code == 200
      and "pi" in r.json() and r.json()["latest"] in ("orin", "pi")
      and "points" not in (r.json()["result"] or {}) and CALLS["n"] == n0)
r = client.post("/reroute/run", json={"device": "iPad 4"})
check("POST /reroute/run, Orin off → 503", r.status_code == 503)
CALLS["allowed"] = True
r = client.post("/reoptimize/run", json={"device": "iPad 4"})
check("POST /reoptimize/run → 202", r.status_code == 202 and r.json()["engine"] == "pi")
wait_done()
CALLS["allowed"] = False
check("…one computation", CALLS["n"] == n0 + 1)
lid = json.load(open(os.path.join(_tmp, "engine", "course-load.json")))["load_id"]
r = client.post("/course/practice")
check("POST /course/practice bumps the course load id", r.status_code == 200
      and json.load(open(os.path.join(_tmp, "engine", "course-load.json")))["load_id"] != lid)
r = client.get("/reroute/history?limit=5")
check("GET /reroute/history → newest first", r.status_code == 200
      and r.json()["runs"][0]["event"] == "done" and r.json()["runs"][0]["asked_by"] == "iPad 4")

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
