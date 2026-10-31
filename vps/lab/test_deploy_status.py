"""Deploy tab "what's on the boat" — the last push is persisted and the live boat read is honest (#82).

The Deploy tab used to keep the push result in browser memory only: after a reload, or after
re-locking a new playbook, nothing said what the boat actually held. Pushes are now recorded
server-side (when, race, playbook, per-step ok — boat-wide, since the boat holds whatever went
last), and a read-only live check reports what the Pi engine and Orin copilot have loaded, with
`reachable: false` when the boat is off the line. This pins both, without touching any network.

Run in-container (make ci does this): python3 /repo/vps/lab/test_deploy_status.py (workdir /srv).
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi:8200"
os.environ["DEPLOY_COPILOT_URL"] = "http://pi:8091/copilot"

from app import deploy as D, labstate  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---- push is persisted ---------------------------------------------------------------------
print("push is recorded:")
check("nothing on record at first", D.last_push() == {"last": None, "last_ok": None})

BUNDLE = {"race_id": "r1", "start_epoch": 100, "variants": [{"id": "a"}], "plays": [],
          "signature": {"value": "ab" * 32}}
posted = []


def fake_post(url, body, timeout=25):
    posted.append(url)
    if url.endswith("/playbook/load"):
        return True, {"loaded": True, "race_id": "r1", "variants": 1, "plays": 0, "signed": True,
                      "echo": {"huge": "nested"}}
    return True, {"loaded": True, "marks": 3, "skipped": []}


D.package = lambda rid, course_id=None: {"race_name": "Race One", "course_load": {}, "fleet_load": {},
                                         "checklist_load": {"items": []}, "watch_load": None}
D.pbstore.get = lambda pid: BUNDLE if pid == "r1__100" else None
labstate.set("deploy:r1", {"playbook_id": "r1__100", "signature": "ab" * 8, "locked_at": 50})
D._post_json = fake_post

res = D.push("r1")
lp = D.last_push()
check("push still returns per-step results", res["ok"] and len(res["steps"]) == 4)
check("last push recorded with race, name, playbook, signature",
      lp["last"] and lp["last"]["race_id"] == "r1" and lp["last"]["race_name"] == "Race One"
      and lp["last"]["playbook_id"] == "r1__100" and lp["last"]["signature"] == "ab" * 8)
check("per-step ok kept", [s["ok"] for s in lp["last"]["steps"]] == [True] * 4
      and [s["step"] for s in lp["last"]["steps"]] == ["course", "fleet", "playbook", "playbook"])
check("stored detail is trimmed to scalars (no echoed objects)",
      "echo" not in lp["last"]["steps"][2]["detail"] and lp["last"]["steps"][2]["detail"]["variants"] == 1)
check("a full success is also the last complete push", lp["last_ok"] == lp["last"])
# a failure in the SAME second as the success must still not hide it
same = D.record_push({"race_id": "r1", "playbook_id": "r1__100", "pushed_playbook": True, "ok": False,
                      "steps": []}, now=lp["last"]["at"])
lp2 = D.last_push()
check("same-second failure keeps the success as last_ok", lp2["last"]["ok"] is False and lp2["last_ok"]["ok"])
first_at = lp["last"]["at"]

# a later push that fails part-way (boat off the line) must not erase the last good one
rec = D.record_push({"race_id": "r1", "playbook_id": "r1__100", "pushed_playbook": True, "ok": False,
                     "steps": [{"target": "pi-engine", "step": "course", "ok": False,
                                "detail": {"error": "timed out"}}]},
                    race_name="Race One", signature="ab" * 8, now=first_at + 600)
lp = D.last_push()
check("failed attempt becomes `last`", lp["last"]["ok"] is False and lp["last"]["at"] == first_at + 600)
check("last complete push survives the failure", lp["last_ok"]["ok"] and lp["last_ok"]["at"] == first_at)
check("failed step keeps its error", lp["last"]["steps"][0]["detail"]["error"] == "timed out")

# a push with no playbook in it never claims one went aboard
rec = D.record_push({"race_id": "r2", "playbook_id": "r2__1", "pushed_playbook": False, "ok": True,
                     "steps": [{"target": "pi-engine", "step": "course", "ok": True, "detail": {}}]})
check("no bundle pushed -> playbook_id None", rec["playbook_id"] is None and rec["signature"] == "")

# concurrent writers (push records from a worker thread, lock-in from the loop) never lose a key
import threading  # noqa: E402
ths = [threading.Thread(target=lambda i=i: [labstate.set(f"k{i}_{j}", j) for j in range(20)]) for i in range(6)]
[t.start() for t in ths]
[t.join() for t in ths]
st = labstate.all()
check("concurrent labstate writes all land", all(st.get(f"k{i}_{j}") == j for i in range(6) for j in range(20)))

# ---- live boat read ------------------------------------------------------------------------
print("live boat read:")


def fake_get(table):
    calls = []

    def get(url, timeout=4):
        calls.append(url)
        return table.get(url, (False, {"error": "connection refused"}))
    get.calls = calls
    return get


COURSE = (True, {"available": True, "route": "r1", "marks": [{"name": "Start"}, {"name": "Finish"}]})
HEALTH = (True, {"mode": "llm", "playbook": {"loaded": True, "race_id": "r1", "variants": 2,
                                             "signed": True, "signature_ok": True,
                                             "sail_inventory": ["J1"]}})
g = fake_get({
    "http://pi:8200/playbook": (True, {"loaded": True, "race_id": "r1", "id": "r1__100",
                                       "variants": 2, "plays": 5, "signed": True,
                                       "signature": "ab" * 8}),
    "http://pi:8200/course": COURSE,
    "http://pi:8091/copilot/health": HEALTH,
})
st = D.boat_status(get=g)
check("reachable when both answer", st["reachable"] and st["engine"]["reachable"] and st["copilot"]["reachable"])
check("engine playbook id + signature come through",
      st["engine"]["playbook"]["id"] == "r1__100" and st["engine"]["playbook"]["signature"] == "ab" * 8)
check("engine course summarized", st["engine"]["course"] == {"available": True, "route": "r1", "marks": 2,
                                                              "mark_names": ["Start", "Finish"]})
check("copilot playbook from /health", st["copilot"]["playbook"] == {
    "loaded": True, "race_id": "r1", "variants": 2, "signed": True, "signature_ok": True})
check("only GETs to the three read endpoints", sorted(g.calls) == sorted([
    "http://pi:8200/playbook", "http://pi:8200/course", "http://pi:8091/copilot/health"]))

# an engine from before GET /playbook: fall back to /playbook/track for the race
g = fake_get({
    "http://pi:8200/playbook": (False, {"error": "HTTP 404", "status": 404}),
    "http://pi:8200/playbook/track": (True, {"available": True, "race_id": "r1", "recommended": "left",
                                             "points": [{"lat": 1, "lon": 2}]}),
    "http://pi:8200/course": COURSE,
    "http://pi:8091/copilot/health": HEALTH,
})
st = D.boat_status(get=g)
check("older engine: race from /playbook/track, flagged partial",
      st["engine"]["playbook"] == {"loaded": True, "race_id": "r1", "recommended": "left", "partial": True})

# boat off the line
st = D.boat_status(get=fake_get({}))
check("unreachable boat says so", st["reachable"] is False and not st["engine"]["reachable"]
      and not st["copilot"]["reachable"])
check("unreachable carries the error, no stale playbook", st["engine"]["error"] == "connection refused"
      and st["engine"]["playbook"] is None and st["copilot"]["playbook"] is None)

# engine up, copilot down (the Pi↔Orin crossover unplugged)
g = fake_get({"http://pi:8200/playbook": (True, {"loaded": False}), "http://pi:8200/course": COURSE})
st = D.boat_status(get=g)
check("half-reachable: engine yes, copilot no", st["reachable"] and st["engine"]["reachable"]
      and not st["copilot"]["reachable"] and st["engine"]["playbook"] == {"loaded": False})

print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
