"""#242: the Lab refuses a route it can't prove safe and complete. No freeze, no lock-in, no Deploy
push, no GPX; a reviewed chart override is recorded on the course (who, when, why).

Pins (no network, temp dirs):
  * freeze takes the server's own synthesis result (by job id), never a bundle sent back by the
    browser, and refuses one whose route checks failed or that was built before the checks;
  * lock-in refuses such a frozen bundle; the Deploy check (`bundle_problem`, used by Push and by
    the readiness banner) refuses it;
  * the GPX export refuses it;
  * a bundle that passed is frozen, locked in and exported as before;
  * the chart-override route records who/when/why on the course's nav mark, refuses an unknown
    mark or no name, and an empty why removes it.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLAYBOOK_DIR"] = os.path.join(_tmp, "playbooks")
os.environ["LAB_AUTH_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app import deploy as D, labstate, main as M, pbstore, store, synthesis  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")
pbstore.PLAYBOOK_DIR = os.environ["PLAYBOOK_DIR"]
client = TestClient(M.app)

ok = True


def check(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


RACE = {"race_id": "r1", "name": "Race", "courses": [{"id": "c",
        "start": {"lat": 45.0, "lon": -83.0},
        "marks": [{"name": "A", "type": "mark", "rounding": "port", "lat": 45.2, "lon": -83.0}],
        "finish": {"points": [{"lat": 45.6, "lon": -83.6}]}}]}
store.get_race = lambda rid: RACE if rid == "r1" else None
M.store.get_race = store.get_race
written = []
M._write_race = lambda d, draft=False: written.append(d) or d.get("race_id")

GOOD = {"ok": True, "problems": [], "overrides": []}
BAD = {"ok": False, "problems": [{"code": "blocked", "mark": "Finish", "route": "Main route",
                                  "text": "Finish is on land, in shallow water or in a no-go zone on the chart."}],
       "overrides": []}


def bundle(safety, start=100):
    b = {"race_id": "r1", "course_id": "c", "start_epoch": start, "variants": [{"id": "a"}],
         "plays": []}
    if safety is not None:
        b["safety"] = safety
        b["consensus"] = {"safety": safety}
    return b


print("freeze:")
import time  # noqa: E402

from app import jobs  # noqa: E402


def built(jid, b):
    """A finished synthesis run on the server, as the job store keeps it."""
    jobs._DONE.setdefault("synthesis", {})[jid] = {"state": "done", "job_id": jid, "bundle": b,
                                                 "finished_at": time.time()}


built("j-bad", bundle(BAD))
built("j-old", bundle(None))
built("j-good", bundle(GOOD))
built("j-forged", dict(bundle(GOOD, start=150), consensus={"safety": BAD}))
r = client.post("/api/playbook/freeze", json={"job_id": "j-bad"})
check("a failed route check: refused", r.status_code == 409 and not r.json().get("frozen"), r.text[:120])
check("...and says why", "shallow water" in r.json().get("detail", ""))
r = client.post("/api/playbook/freeze", json={"job_id": "j-old"})
check("a bundle built before the checks: refused", r.status_code == 409, r.text[:120])
r = client.post("/api/playbook/freeze", json={"job_id": "j-forged"})
check("a top-level ok over a failed route: refused", r.status_code == 409, r.text[:120])
r = client.post("/api/playbook/freeze", json={"bundle": bundle(GOOD)})
check("a bundle sent by the browser: refused (freeze what the server built)",
      r.status_code == 409, r.text[:120])
r = client.post("/api/playbook/freeze", json={"job_id": "nope"})
check("an unknown run: refused", r.status_code == 409, r.text[:120])
r = client.post("/api/playbook/freeze", json={"job_id": "j-good"})
check("a bundle that passed: frozen", r.status_code == 200 and r.json().get("frozen"), r.text[:120])
good_id = r.json().get("id")

print("lock-in, Deploy, GPX:")
bad_frozen = synthesis.sign_bundle(bundle(BAD, start=200))    # e.g. frozen before this check
bad_id = pbstore.save(bad_frozen)
old_id = pbstore.save(synthesis.sign_bundle(bundle(None, start=300)))
r = client.post("/api/deploy/lock-in", json={"race_id": "r1", "playbook_id": bad_id})
check("lock-in of a failed route: refused", r.status_code == 409, r.text[:120])
r = client.post("/api/deploy/lock-in", json={"race_id": "r1", "playbook_id": old_id})
check("lock-in of a bundle from before the checks: refused", r.status_code == 409, r.text[:120])
check("Deploy check refuses a failed route (Push and the banner)",
      D.bundle_problem("r1", bad_frozen, RACE) is not None)
r = client.post("/api/deploy/lock-in", json={"race_id": "r1", "playbook_id": good_id})
check("lock-in of a route that passed: locked", r.status_code == 200 and r.json().get("locked_in"))
check("Deploy check passes it", D.bundle_problem("r1", pbstore.get(good_id), RACE) is None)
r = client.get(f"/api/playbooks/{bad_id}/gpx")
check("GPX of a failed route: refused", r.status_code == 409, r.text[:120])
r = client.get(f"/api/playbooks/{good_id}/gpx")
check("GPX of a route that passed: sent", r.status_code == 200 and "<gpx" in r.text)

import copy  # noqa: E402

from shared import race_def  # noqa: E402

built("j-hash", dict(bundle(GOOD, start=400), course_hash=race_def.course_hash(RACE, "c")))
hid = client.post("/api/playbook/freeze", json={"job_id": "j-hash"}).json().get("id")
r = client.get(f"/api/playbooks/{hid}/gpx")
check("GPX of a playbook routed on today's course: sent", r.status_code == 200, r.text[:120])
_saved = copy.deepcopy(RACE)
RACE["courses"][0]["chart_overrides"] = {"Finish": {"by": "C", "at": 1, "why": "x"}}
r = client.get(f"/api/playbooks/{hid}/gpx")
check("GPX after the course (an override) changed: refused", r.status_code == 409, r.text[:120])
RACE.clear(); RACE.update(_saved)

print("chart override:")
r = client.post("/api/races/r1/chart-override",
                json={"course_id": "c", "mark": "Finish", "by": "Cole", "why": "harbour charted as land"})
ov = (RACE["courses"][0].get("chart_overrides") or {}).get("Finish") or {}
check("recorded on the course: who, when, why", r.status_code == 200 and ov.get("by") == "Cole"
      and ov.get("why") == "harbour charted as land" and isinstance(ov.get("at"), int), r.text[:120])
check("saved", written and written[-1]["race_id"] == "r1")
r = client.post("/api/races/r1/chart-override", json={"course_id": "c", "mark": "Nowhere", "by": "C", "why": "x"})
check("unknown mark: refused", r.status_code == 400)
r = client.post("/api/races/r1/chart-override", json={"course_id": "c", "mark": "A", "by": "", "why": "x"})
check("no name: refused", r.status_code == 400)
r = client.post("/api/races/r1/chart-override", json={"course_id": "c", "mark": "Finish", "by": "", "why": ""})
check("empty why removes it", r.status_code == 200 and "chart_overrides" not in RACE["courses"][0])

print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
