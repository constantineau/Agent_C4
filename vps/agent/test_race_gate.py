"""Frozen at the gun: one race-time gate on every load of shore homework (vps/agent/app/racegate.py).

Pins, through the REAL engine endpoints on a temp onboard DB with the REAL race log:
  * every homework load (course, fleet, playbook, checklist, a whole watch plan) is taken before
    the race, refused while a race session runs, and refused when the race
    log can't be read; a refused load changes nothing aboard (the plume and re-route assets use
    the same gate and keep their own suites: test_plume_cache.py, test_orinassets.py);
  * a practice session doesn't block;
  * crew inputs are never gated: checklist acks, watch quick edits (incl. the tap-a-block edit),
    sail state;
  * GET /racing answers what the Orin copilot asks (racing, practice, unreadable log);
  * the gate holds the race log's session lock, so a session can't start between check and write;
  * the cloud agent's /course/load and whole-plan /watch refuse in race mode.

Run in-container (make ci): PYTHONPATH=vps/agent:.:pi/archiver python3 vps/agent/test_race_gate.py
"""
import os
import sys
import tempfile
import threading
import time

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "engine"))

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")

from fastapi.testclient import TestClient  # noqa: E402

import engine_app  # noqa: E402
from app import datasource, racegate, racelog  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


client = TestClient(engine_app.app)
NOW = time.time()
DEF = {"race_id": "r1", "name": "Race",
       "courses": [{"id": "c", "start": {"lat": 43.0, "lon": -82.4},
                    "marks": [{"name": "Gate", "type": "buoy", "lat": 44.5, "lon": -82.2}],
                    "finish": {"points": [{"lat": 45.0, "lon": -82.3}]}}],
       "fleet": [{"name": "Rival", "sail_no": "USA 1"}]}
BUNDLE = {"race_id": "r1", "variants": [{"id": "a", "route": {"path": []}}], "plays": []}
ITEMS = {"items": [{"id": "lights", "text": "Nav lights on", "trigger": "manual"}], "race_id": "r1"}
PLAN = {"plan": {"teams": {"A": {"name": "Port"}, "B": {"name": "Stbd"}},
                 "blocks": [{"start": NOW - 3600, "end": NOW + 3600, "on": "A"},
                            {"start": NOW + 3600, "end": NOW + 7200, "on": "B"}]}}
LOADS = {
    "course": ("/course/load", {"definition": DEF, "course_id": "c"}),
    "fleet": ("/fleet/load", {"definition": DEF}),
    "playbook": ("/playbook/load", BUNDLE),
    "checklist": ("/checklist/load", ITEMS),
    "watch plan": ("/watch", PLAN),
}


def aboard():
    """What the boat holds, to prove a refusal changed nothing."""
    ds = datasource.active()
    return {"course": engine_app.navigator.get_course(None).get("marks"),
            "fleet": ds.get_fleet(), "playbook": ds.get_playbook(),
            "checklist": ds.get_checklist(), "watch": ds.get_watch_plan()}


def loaded(r):
    return r.status_code == 200 and r.json().get("loaded") is not False


def refused(r):
    return r.status_code == 200 and r.json() == {"loaded": False, "detail": "Racing now. Not loaded."}


print("before the race (no session): every load is taken")
for name, (url, body) in LOADS.items():
    check(f"{name} loads", loaded(client.post(url, json=body)))
check("GET /racing → not racing", client.get("/racing").json() == {"racing": False})

print("a practice session doesn't block")
racelog.start(name="Practice", kind="practice")
for name, (url, body) in LOADS.items():
    check(f"{name} loads in practice", loaded(client.post(url, json=body)))
check("GET /racing → not racing in practice", client.get("/racing").json() == {"racing": False})

print("a race session running: every load refused, nothing changes")
racelog.start(name="Race", kind="race", race_id="r1")
before = aboard()
other = dict(DEF, race_id="r2", courses=[dict(DEF["courses"][0], marks=[
    {"name": "New", "type": "buoy", "lat": 44.0, "lon": -82.0}])], fleet=[{"name": "Other"}])
RACE_LOADS = {
    "course": ("/course/load", {"definition": other, "course_id": "c"}),
    "fleet": ("/fleet/load", {"fleet": [{"name": "Other", "sail_no": "X"}]}),
    "playbook": ("/playbook/load", dict(BUNDLE, race_id="r2")),
    "checklist": ("/checklist/load", {"items": [{"id": "x", "text": "New item"}]}),
    "watch plan": ("/watch", {"plan": dict(PLAN["plan"], blocks=[])}),
}
for name, (url, body) in RACE_LOADS.items():
    check(f"{name} refused", refused(client.post(url, json=body)))
check("…and nothing aboard changed", aboard() == before)
check("GET /racing → racing", client.get("/racing").json() == {"racing": True})

print("crew inputs while racing are never gated")
r = client.post("/checklist/ack", json={"id": "lights"})
check("checklist ack taken", r.status_code == 200 and r.json().get("loaded") is not False)
r = client.post("/watch", json={"action": "swap"})
check("watch quick edit (swap) taken", r.status_code == 200 and r.json().get("plan_set"))
start_b = [b for b in r.json()["blocks"] if b["start"] > NOW][0]
r = client.post("/watch", json={"action": "set_on", "start": start_b["start"], "on": "ALL"})
check("tap-a-block edit (set_on) taken while racing", r.status_code == 200
      and [b["on"] for b in r.json()["blocks"] if b["start"] == start_b["start"]] == ["ALL"])
r = client.post("/sails/state", json={"hoisted": "J1"})
check("sail state taken", r.status_code == 200)

print("race session ended → loads taken again")
racelog.end()
check("playbook loads after the finish", loaded(client.post("/playbook/load", json=BUNDLE)))

print("race log unreadable → treated as racing (fail closed)")
saved_src = racelog._src
racelog._src = lambda: (_ for _ in ()).throw(OSError("db locked"))
for name, (url, body) in LOADS.items():
    check(f"{name} refused", refused(client.post(url, json=body)))
check("GET /racing → racing", client.get("/racing").json() == {"racing": True})
racelog._src = saved_src

print("the gate holds the session lock (no session can start between check and write)")
held = {}
with racegate.hold() as racing:
    held["lock"] = racelog.SESSION_LOCK.locked()
    t = threading.Thread(target=lambda: held.setdefault("started", racelog.start(kind="race")))
    t.start()
    t.join(0.3)
    held["blocked"] = t.is_alive()
t.join(5)
check("lock held inside the gate", held["lock"] and not racing)
check("a race start waits for the load to finish", held["blocked"])
check("…then starts, and the next load is refused",
      refused(client.post("/playbook/load", json=BUNDLE)))
racelog.end()

print("the cloud agent (race mode, no race log)")
from app import main as cloud, race_mode  # noqa: E402

race_mode.racing = lambda: True
race_mode.audit_refusal = lambda *a, **k: None
r = cloud.course_load({"definition": DEF, "course_id": "c"})
check("/course/load refused in race mode (409)", getattr(r, "status_code", None) == 409
      and b"Racing now. Not loaded." in r.body)
r = cloud.watch_set(PLAN)
check("/watch whole plan refused in race mode", getattr(r, "status_code", None) == 409)
cloud.watches.set_watch = lambda body: {"edited": body}
check("/watch quick edit still taken in race mode",
      cloud.watch_set({"action": "swap"}) == {"edited": {"action": "swap"}})
race_mode.racing = lambda: False
check("/watch whole plan taken in practice mode", cloud.watch_set(PLAN) == {"edited": PLAN})

print("short text:")
check("the refusal is ≤ 6 words", len(racegate.RACING.split()) <= 6)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
