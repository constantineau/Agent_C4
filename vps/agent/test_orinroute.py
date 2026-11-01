"""Full re-route on the Orin — the Pi engine's side (vps/agent/app/orinroute.py, issue #124).

Pins:
  * POST /reroute/run refuses, in a short sentence, when the Orin can't be reached (a real
    connection to a closed port), when there's no position, and when there's no course;
  * the run request carries the position, the next mark, the deployed definition (when it is the
    course the navigator holds), the plan's track and who asked;
  * busy passes through with the Orin's progress; started → 202;
  * GET /reroute fetches a finished route once, stores it (label "Off the playbook"), and still
    serves it with the Orin off;
  * the real engine endpoints wire it (status codes included), and /course/load keeps the
    definition.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_orinroute.py
"""
import json
import os
import socket
import sys
import tempfile

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "engine"))

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")

from app import orinroute, deviation  # noqa: E402
from app import navigator as NAV  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


DEF = {"race_id": "r1", "name": "Race", "requirements": [{"x": 1}], "fleet": [{"y": 2}],
       "courses": [{"id": "c", "start": {"lat": 43.0, "lon": -82.4},
                    "marks": [{"name": "Gate", "type": "gate", "lat": 45.3, "lon": -81.9,
                               "lat2": 45.3, "lon2": -81.8, "rounding": "gate"},
                              {"name": "Isle", "type": "island", "lat": 45.6, "lon": -82.9,
                               "rounding": "starboard"}],
                    "finish": {"points": [{"lat": 45.84, "lon": -84.62}]}}],
       "zones": []}
MARKS = [{"seq": 1, "name": "Start", "lat": 43.0, "lon": -82.4},
         {"seq": 2, "name": "Gate", "lat": 45.3, "lon": -81.85},
         {"seq": 3, "name": "Finish", "lat": 45.84, "lon": -84.62}]
STATE = {"nav": {"available": True, "route": "r1", "next_mark": {"name": "Gate", "index": 1}},
         "latest": {"lat": 44.4, "lon": -81.9}}
NAV.get_navigator = lambda route=None: STATE["nav"]
NAV._marks = lambda route: MARKS
NAV._latest = lambda: STATE["latest"]
BUNDLE = {"recommended": "left", "variants": [{"id": "left", "name": "Left side", "route": {
    "path": [{"lat": 43.0, "lon": -82.4, "t": 100}, {"lat": 45.84, "lon": -84.62, "t": 9000}]}}]}
deviation._load_playbook = lambda: BUNDLE


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


print("the Orin can't be reached:")
os.environ["ORIN_REROUTE_URL"] = f"http://127.0.0.1:{closed_port()}"
os.environ["ORIN_REROUTE_TIMEOUT_S"] = "2"
code, body = orinroute.run("iPad 1")
check("503, not started", code == 503 and body["ok"] is False and body["orin"] == "unreachable")
check("short plain message", body["detail"] == "Can't reach the Orin. Re-route not started."
      and len(body["detail"].split()) <= 12)
g = orinroute.get()
check("GET /reroute: Orin unreachable, no route yet", g["orin"]["reachable"] is False
      and g["result"] is None and g["note"] == "Can't reach the Orin.")

print("nothing to route:")
STATE["latest"] = {"lat": None, "lon": None}
code, body = orinroute.run("iPad 1")
check("no position → 409, plain", code == 409 and body["detail"] == "No boat position yet.")
STATE["latest"] = {"lat": 44.4, "lon": -81.9}
STATE["nav"] = {"available": False, "note": "No course loaded for 'default'."}
check("no course → 409, plain", orinroute.run()[1]["detail"] == "No course loaded.")
STATE["nav"] = {"available": True, "route": "r1", "next_mark": {"name": "Gate", "index": 1}}

print("the request (with the deployed definition aboard):")
check("save_definition keeps the course, drops the rest",
      orinroute.save_definition(DEF, "c", "r1"))
saved = json.load(open(os.path.join(_tmp, "engine", "course-definition.json")))
check("trimmed: no requirements/fleet", "requirements" not in saved["definition"]
      and saved["definition"]["courses"][0]["id"] == "c" and saved["route"] == "r1")
SENT = []


def orin(responses):
    def call(method, path, body=None):
        SENT.append((method, path, body))
        return responses[(method, path)]
    return call


code, body = orinroute.run(" iPad 1\n", call=orin({("POST", "/run"): (202, {"ok": True, "job_id": "j1"})}))
req = SENT[-1][2]
check("202 started, job id passed back", code == 202 and body == {"ok": True, "job_id": "j1",
                                                                  "state": "running"})
check("position + next mark", req["position"]["lat"] == 44.4 and req["next_index"] == 1)
check("who asked (cleaned)", req["device"] == "iPad 1")
check("the definition rides along (same course as the navigator)",
      (req["definition"] or {}).get("race_id") == "r1" and req["course_id"] == "c")
check("the plan's track", req["plan"]["variant"] == "left" and len(req["plan"]["points"]) == 2)
NAV._marks = lambda route: MARKS[:2]
orinroute.run("x", call=orin({("POST", "/run"): (202, {"ok": True, "job_id": "j2"})}))
check("a different course aboard → no definition, marks only",
      SENT[-1][2]["definition"] is None and len(SENT[-1][2]["marks"]) == 2)
NAV._marks = lambda route: MARKS

print("busy:")
PROG = {"phase": "routing", "step": "Finding the fastest route", "elapsed_s": 30, "pct": 40}
code, body = orinroute.run("iPad 2", call=orin({("POST", "/run"): (
    409, {"ok": False, "busy": True, "job_id": "j1", "progress": PROG,
          "detail": "A re-route is already running."})}))
check("409 busy + the Orin's progress", code == 409 and body["busy"] and body["progress"] == PROG
      and body["detail"] == "A re-route is already running.")

print("done → stored:")
RESULT = {"schema": "c4.reroute/v1", "job_id": "j1", "available": True, "label": "x",
          "finished_at": 1_000, "points": [{"lat": 44.4, "lon": -81.9, "t": 1}],
          "weather": {"models": [{"model": "gfs"}]}}
st = {"state": "done", "job_id": "j1", "progress": None, "result_job_id": "j1",
      "last": {"job_id": "j1", "state": "done", "run_s": 70}}
SENT.clear()
g = orinroute.get(call=orin({("GET", "/status"): (200, st), ("GET", "/result"): (200, RESULT)}))
check("fetched the route once and serves it", g["orin"]["reachable"] and g["state"] == "done"
      and g["result"]["job_id"] == "j1" and ("GET", "/result", None) in SENT)
check("labelled off the playbook, from the Orin", g["result"]["label"] == "Off the playbook"
      and g["result"]["off_playbook"] is True and g["result"]["source"] == "orin")
SENT.clear()
orinroute.get(call=orin({("GET", "/status"): (200, st)}))
check("not fetched again for the same job", [p for _m, p, _b in SENT] == ["/status"])
bad = dict(RESULT, job_id="j9", schema="other")
orinroute.get(call=orin({("GET", "/status"): (200, dict(st, result_job_id="j9",
                                                        last=dict(st["last"], job_id="j9"))),
                         ("GET", "/result"): (200, bad)}))
check("a bad result is not stored", orinroute.stored()["job_id"] == "j1")
g = orinroute.get(call=orin({("GET", "/status"): (None, None)}))
check("Orin off: the stored route is still served", g["orin"]["reachable"] is False
      and g["result"]["job_id"] == "j1" and g["result"]["age_s"] is not None)
g = orinroute.get(call=orin({("GET", "/status"): (200, {"state": "error", "result_job_id": "j1",
                                                       "last": {"state": "error", "job_id": "j3",
                                                                "detail": "Re-route took too long. Try again."}})}))
check("a failed run says why, short", g["note"] == "Re-route took too long. Try again.")
SENT.clear()
g = orinroute.get(call=orin({
    ("GET", "/status"): (200, {"state": "error", "result_job_id": "j2",
                               "last": {"state": "error", "job_id": "j3", "detail": "x"}}),
    ("GET", "/result"): (200, dict(RESULT, job_id="j2"))}))
check("a route that finished unpolled before a failed run is still fetched",
      g["result"]["job_id"] == "j2" and ("GET", "/result", None) in SENT)

print("the real engine endpoints:")
from fastapi.testclient import TestClient  # noqa: E402

import engine_app  # noqa: E402

client = TestClient(engine_app.app)
os.environ["ORIN_REROUTE_URL"] = f"http://127.0.0.1:{closed_port()}"
r = client.post("/reroute/run", json={"device": "iPad 1"})
check("POST /reroute/run with the Orin off → 503 + message", r.status_code == 503
      and r.json()["detail"] == "Can't reach the Orin. Re-route not started.")
r = client.get("/reroute")
check("GET /reroute → stored route, Orin unreachable", r.status_code == 200
      and r.json()["result"]["job_id"] == "j2" and r.json()["orin"]["reachable"] is False)


class _DS:
    def __init__(self):
        self.fail = False
        self.saved = []

    def save_course(self, route, marks):
        if self.fail:
            raise RuntimeError("disk full")
        self.saved.append((route, marks))


DS = _DS()
engine_app.datasource.active = lambda: DS
engine_app.navigator.set_active = lambda route: None
DEFP = os.path.join(_tmp, "engine", "course-definition.json")
r = client.post("/course/load", json={"definition": DEF, "course_id": "c"})
check("/course/load keeps the definition for the Orin", r.json()["loaded"]
      and json.load(open(DEFP))["definition"]["race_id"] == "r1")
check("…bound to these marks", orinroute._definition_for("r1", MARKS)[0] is not None)

print("a definition never pairs with other marks:")
moved = [dict(m) for m in MARKS]
moved[1]["lat"] = 45.0                                   # same names, the gate moved
check("a moved mark → no definition", orinroute._definition_for("r1", moved) == (None, None))
legacy = json.load(open(DEFP))
legacy.pop("marks_hash")
json.dump(legacy, open(DEFP, "w"))
check("a stored definition without the hash → not used", orinroute._definition_for("r1", MARKS)
      == (None, None))

print("a course load that fails half way:")
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
DS.fail = True
DEF2 = json.loads(json.dumps(DEF))
DEF2["zones"] = [{"name": "No-go", "type": "exclusion", "geometry": {}}]
r = client.post("/course/load", json={"definition": DEF2, "course_id": "c"})
check("marks write fails → loaded false, short", r.json() == {
    "loaded": False, "detail": "Couldn't save the course. Try again."})
check("…and the old definition is gone", not os.path.exists(DEFP))
check("…no temp files left", not [n for n in os.listdir(os.path.dirname(DEFP)) if n.endswith(".part")])
DS.fail = False
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
_orig_commit = orinroute.commit_definition


def broken_commit(tmp):
    raise OSError("read-only")


orinroute.commit_definition = broken_commit
r = client.post("/course/load", json={"definition": DEF2, "course_id": "c"})
orinroute.commit_definition = _orig_commit
check("definition write fails → loaded false, old one gone", r.json()["loaded"] is False
      and not os.path.exists(DEFP))
r = client.post("/course/load", json={"definition": DEF, "course_id": "c"})
check("a good load afterwards works again", r.json()["loaded"] and os.path.exists(DEFP))

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
