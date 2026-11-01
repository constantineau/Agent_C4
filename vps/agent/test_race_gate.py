"""Frozen at the gun: one race-time gate on every load of shore homework (vps/agent/app/racegate.py).

Pins, through the REAL engine endpoints on a temp onboard DB with the REAL race log:
  * every homework load (course, fleet, playbook, checklist, a whole watch plan) is taken before
    the race, refused while a race session runs, and refused when the race
    log can't be read; a refused load changes nothing aboard (the plume and re-route assets use
    the same gate and keep their own suites: test_plume_cache.py, test_orinassets.py);
  * a practice session doesn't block;
  * crew inputs are never gated: checklist acks, watch quick edits (incl. the tap-a-block edit),
    sail state;
  * the playbook goes on to the Orin copilot from inside the gate (the session lock is held
    during the forward; nothing is forwarded while racing; an unreachable Orin is a plain answer);
  * the scoped session close (M14) never ends a newer session, on the real SQLite store;
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

FORWARDS = []
_REAL_COPILOT_LOAD = engine_app.copilot_load


def fake_copilot_load(bundle):
    """Stands in for the Orin: records whether the race log's lock was held during the forward."""
    FORWARDS.append((bundle.get("race_id"), racelog.SESSION_LOCK.locked()))
    return {"loaded": True, "race_id": bundle.get("race_id")}


engine_app.copilot_load = fake_copilot_load

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

print("a practice session doesn't block")
racelog.start(name="Practice", kind="practice")
for name, (url, body) in LOADS.items():
    check(f"{name} loads in practice", loaded(client.post(url, json=body)))

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

check("…and no playbook was forwarded to the Orin", "r2" not in [f[0] for f in FORWARDS])

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

print("the playbook goes on to the Orin from inside the gate")
FORWARDS.clear()
r = client.post("/playbook/load", json=BUNDLE).json()
check("forwarded once, while the session lock was held", FORWARDS == [("r1", True)])
check("the Orin's answer is in the response", r["copilot"] == {"loaded": True, "race_id": "r1"})
os.environ["COPILOT_URL"] = "http://127.0.0.1:9"          # nothing listens there
os.environ["COPILOT_STAGE_TIMEOUT_S"] = "1"
check("Orin unreachable → a plain answer, never raises",
      _REAL_COPILOT_LOAD(BUNDLE) == {"loaded": False, "detail": engine_app.NO_COPILOT})

print("the Pi's side of the two-step Orin load (stage, then a time-bounded commit)")
CALLS = []
ORIN = {"stage": None, "commit": None}


def fake_orin(path, body, timeout):
    CALLS.append((path, dict(body), timeout))
    step = ORIN[path.rsplit("/", 1)[-1]]
    return step(body) if callable(step) else step


_real_post = engine_app._copilot_post
engine_app._copilot_post = fake_orin
engine_app._sleep = lambda sec: None          # the post-commit wait is pinned further down
ORIN["stage"] = lambda b: {"staged": True, "nonce": b["nonce"], "ttl_s": 3}
ORIN["commit"] = {"loaded": True, "race_id": "r1"}
r = _REAL_COPILOT_LOAD(BUNDLE)
check("stage carries the bundle and a fresh nonce; commit carries only that nonce",
      [c[0] for c in CALLS] == ["/playbook/stage", "/playbook/commit"]
      and CALLS[0][1]["bundle"] == BUNDLE and CALLS[1][1] == {"nonce": CALLS[0][1]["nonce"]}
      and len(CALLS[0][1]["nonce"]) >= 16 and r == {"loaded": True, "race_id": "r1"})
check("the Pi waits for the commit at least the Orin's TTL + 1 s", CALLS[1][2] >= 3 + 1)
n1 = CALLS[0][1]["nonce"]
CALLS.clear()
_REAL_COPILOT_LOAD(BUNDLE)
check("a new nonce every load", CALLS[0][1]["nonce"] != n1)
ORIN["stage"] = lambda b: {"staged": True, "nonce": b["nonce"], "ttl_s": 7}
CALLS.clear()
_REAL_COPILOT_LOAD(BUNDLE)
check("a longer Orin TTL makes the Pi wait longer (never less than the TTL)", CALLS[1][2] >= 8)
for label, stage in (("stage refused", {"loaded": False, "detail": "Load the playbook from the Pi. Not loaded."}),
                     ("stage unreachable", {"loaded": False, "detail": engine_app.NO_COPILOT}),
                     ("stage echoes another nonce", {"staged": True, "nonce": "x" * 24, "ttl_s": 3}),
                     ("TTL missing", lambda b: {"staged": True, "nonce": b["nonce"]}),
                     ("TTL too long", lambda b: {"staged": True, "nonce": b["nonce"], "ttl_s": 60})):
    ORIN["stage"] = stage
    CALLS.clear()
    r = _REAL_COPILOT_LOAD(BUNDLE)
    check(f"{label} → no commit sent, not loaded", [c[0] for c in CALLS] == ["/playbook/stage"]
          and r["loaded"] is False)
ORIN["stage"] = lambda b: {"staged": True, "nonce": b["nonce"], "ttl_s": 3}
ORIN["commit"] = {"loaded": False, "detail": engine_app.NO_COPILOT}      # the commit timed out
check("commit unanswered → 'didn't confirm', plain",
      _REAL_COPILOT_LOAD(BUNDLE) == {"loaded": False, "detail": engine_app.NO_CONFIRM})
ORIN["commit"] = {"loaded": False, "detail": "Too late. Not loaded."}
check("the Orin's 'too late' is passed on", _REAL_COPILOT_LOAD(BUNDLE)["detail"] == "Too late. Not loaded.")

print("a commit the Orin doesn't confirm (connection reset): the Pi keeps the lock past the window")
engine_app.copilot_load = _REAL_COPILOT_LOAD             # the real two-step, through the endpoint
SLEPT = []
engine_app._sleep = lambda sec: SLEPT.append((sec, racelog.SESSION_LOCK.locked()))
ORIN["commit"] = {"loaded": True, "race_id": "r1"}
r = client.post("/playbook/load", json=BUNDLE).json()
h = engine_app.bundle_hash(BUNDLE)
check("confirmed: approved, no extra wait", r["copilot"]["loaded"] and SLEPT == []
      and client.get("/playbook/copilot").json() == {"hash": h})


def orin_reset(path, body, timeout):
    """The commit is sent, then the connection resets (the Orin may have taken it). That is what
    _copilot_post answers for a dropped connection (pinned against a real socket below)."""
    if path.endswith("/commit"):
        CALLS.append((path, dict(body), timeout))
        return {"loaded": False, "detail": engine_app.NO_COPILOT}
    return fake_orin(path, body, timeout)


engine_app._copilot_post = orin_reset
r = client.post("/playbook/load", json=dict(BUNDLE, race_id="r3")).json()
check("reset: not loaded, plain", r["copilot"] == {"loaded": False, "detail": engine_app.NO_CONFIRM})
check("reset: the Pi waited out the Orin's window (TTL 3 s + 1 s) still holding the lock",
      len(SLEPT) == 1 and 3.9 < SLEPT[0][0] <= 4.0 and SLEPT[0][1] is True)
check("reset: NO playbook approved for the copilot (not the old one, not the new one)",
      client.get("/playbook/copilot").json() == {"hash": None})
SLEPT.clear()
ORIN["commit"] = {"loaded": False, "detail": "Too late. Not loaded."}
client.post("/playbook/load", json=BUNDLE)
check("an Orin refusal also waits out the window", len(SLEPT) == 1 and SLEPT[0][1] is True)
engine_app._sleep = time.sleep
engine_app.copilot_load = fake_copilot_load

print("the reset is what _copilot_post really makes of a dropped connection")
import http.server  # noqa: E402


class Drop(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.connection.shutdown(2)                  # accepted, then the connection drops

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), Drop)
threading.Thread(target=srv.handle_request, daemon=True).start()
os.environ["COPILOT_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
check("a dropped connection → the 'can't reach' answer the Pi then waits on",
      _real_post("/playbook/commit", {"nonce": "x"}, 2) == {"loaded": False, "detail": engine_app.NO_COPILOT})
srv.server_close()

print("one hash on both boxes")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "orin"))
from copilot import playbook as orin_pb  # noqa: E402

for b in (BUNDLE, {"race_id": "é", "variants": [{"id": 1.5}], "signature": {"value": "ab"}}):
    check("engine_app.bundle_hash == copilot playbook.bundle_hash", engine_app.bundle_hash(b)
          == orin_pb.bundle_hash(b))
engine_app._copilot_post = _real_post

print("M14: the scoped close never ends a newer session (real store)")
ds = datasource.active()
racelog.start(name="Yesterday", kind="practice")
old = ds.session_current()
racelog.start(name="Today", kind="race")               # auto-ends yesterday
ds.session_end(old["start_ts"] + 1, session_id=old["id"])
cur = ds.session_current()
check("closing yesterday's id leaves today's race open", cur and cur["name"] == "Today")
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
