"""The Lab is shared: one user's run never shows up on another's page (#182 findings 5, 9, 10, 11).

Pins:
  * #182-9  a route / playbook poll WITH its job_id gets its own run: while it runs, after it
    finishes, and after the next user's run takes the slot. An unknown id is a plain "lost" error,
    never someone else's progress or result. Polls without an id behave as before (scripts).
  * #182-10 two Deploy pushes at once: the second is refused before any post; the first runs whole.
  * #182-11 the Lab page checks the answer when it saves a checklist tick or a lock-in (a 404/500
    does not throw from fetch), and polls the route/playbook status with its job_id.
  * #182-5  the import prompt asks for the START signal, not the warning signal.

No network. Run in-container (make ci): python3 /repo/vps/lab/test_lab_shared_runs.py
"""
import asyncio
import os
import re
import tempfile
import threading
import time

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLAYBOOK_DIR"] = os.path.join(_tmp, "playbooks")
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi:8200"

from app import deploy as D, extract, jobs, labstate, main, orinassets, pbstore  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file
pbstore.PLAYBOOK_DIR = os.environ["PLAYBOOK_DIR"]
HERE = os.path.dirname(os.path.abspath(__file__))

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def wait_state(name, job_id, want, timeout=5):
    t = time.time() + timeout
    while time.time() < t:
        if jobs.status(name, job_id=job_id).get("state") == want:
            return True
        time.sleep(0.01)
    return False


def gated(result, gate):
    def work(progress):
        progress("working on " + result)
        gate.wait(5)
        return result
    return work


print("#182-9 a route poll gets its own run, never another user's:")
ga, gb = threading.Event(), threading.Event()
a = jobs.start("optimize", gated("route-A", ga), meta={"race_id": "r1"})
check("start returns a job_id", a.get("ok") and isinstance(a.get("job_id"), str) and a["job_id"])
refused = jobs.start("optimize", gated("route-X", gb))
check("a second start while running is refused", refused.get("ok") is False)
st = asyncio.run(main.optimize_status(job_id=a["job_id"]))
check("poll with A's id: A running", st["state"] == "running" and st["job_id"] == a["job_id"])
ga.set()
check("A finishes", wait_state("optimize", a["job_id"], "done"))
b = jobs.start("optimize", gated("route-B", gb), meta={"race_id": "r2"})
check("user B starts the next route", b.get("ok") and b["job_id"] != a["job_id"])
st = asyncio.run(main.optimize_status(job_id=a["job_id"]))
check("A's page still gets A's route, not B's run",
      st["state"] == "done" and st["result"] == "route-A" and st["race_id"] == "r1")
st = asyncio.run(main.optimize_status(job_id=b["job_id"]))
check("B's page sees B running", st["state"] == "running" and st["race_id"] == "r2")
st = asyncio.run(main.optimize_status(job_id="nope"))
check("an unknown id is a plain 'lost' error, not B's progress",
      st["state"] == "error" and st["error"] == jobs.LOST and "progress" not in st)
check("a poll without an id still reads the slot (scripts)",
      jobs.status("optimize").get("job_id") == b["job_id"])
gb.set()
check("B finishes", wait_state("optimize", b["job_id"], "done"))

print("#182-9 the playbook build poll is per run too:")
gc = threading.Event()
c = jobs.start("synthesis", gated("bundle-C", gc), result_key="bundle")
st = asyncio.run(main.playbook_synthesize_status(job_id="someone-else"))
check("another page's id never sees C", st["state"] == "error" and "bundle" not in st)
gc.set()
check("C finishes", wait_state("synthesis", c["job_id"], "done"))
st = asyncio.run(main.playbook_synthesize_status(job_id=c["job_id"]))
check("C's page gets C's bundle", st["state"] == "done" and st["bundle"] == "bundle-C")

print("#182-9 finished runs are kept a short while, then dropped:")
ids = []
for i in range(jobs.KEEP_N + 2):
    r = jobs.start("debrief", lambda p, i=i: i)
    wait_state("debrief", r["job_id"], "done")
    ids.append(r["job_id"])
check("the oldest beyond the cap is gone", jobs.status("debrief", job_id=ids[0])["state"] == "error")
check("a recent one is kept", jobs.status("debrief", job_id=ids[-2]).get("result") == len(ids) - 2)

print("#182-10 two Deploy pushes at once:")
RACE = {"race_id": "r1", "name": "Race", "courses": [{"id": "c", "marks": [
    {"name": "A", "lat": 45.0, "lon": -83.0}, {"name": "B", "lat": 45.5, "lon": -83.5}],
    "finish": {"points": [{"lat": 45.6, "lon": -83.6}, {"lat": 45.6, "lon": -83.5}]}}],
    "fleet": [{"name": "X"}]}
D.store.get_race = lambda rid: RACE if rid == "r1" else None
orinassets.build = lambda *a, **k: ({"schema": "c4.orin-assets/v1"}, [])
D.orinassets = orinassets
D._clock = lambda: 0
posted, in_post, release = [], threading.Event(), threading.Event()


def slow_post(url, body, timeout=25):
    posted.append(url.rsplit(":8200", 1)[-1])
    in_post.set()
    release.wait(5)
    return True, {"loaded": True}


D._post_json = slow_post
first = {}
t = threading.Thread(target=lambda: first.update(D.push("r1")))
t.start()
in_post.wait(5)
n_before = len(posted)
second = D.push("r1")
check("the second push is refused while the first runs",
      second["ok"] is False and second.get("refused") and second["detail"] == D.PUSH_BUSY)
check("the second push sent nothing", len(posted) == n_before and second["steps"] == [])
release.set()
t.join(5)
check("the first push ran whole", first.get("ok") is True and len(first["steps"]) >= 3)
check("after it, a push goes through again", D.push("r1").get("ok") is True)

print("#182-11 the Lab page checks what it saved, and polls its own run:")
js = open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()


def fn_body(name):
    m = re.search(r"async function " + name + r"\([^)]*\) \{\n(.*?)\n\}\n", js, re.S)
    return m.group(1) if m else ""


tick, lock = fn_body("checkToggle"), fn_body("depActivate")   # was depLockIn (#249)
check("checklist tick checks res.ok", "res.ok" in tick and "catch (e) {}" not in tick)
check("a failed tick is undone and said", "alert(" in tick and "delete checked[id]" in tick)
check("the undo hits the race it saved, not one switched to meanwhile",
      "const checked = Lab.checked" in tick and "Lab.checked[id] = true" not in tick.split("catch")[1])
check("one tick saves at a time (a failed one undoes exactly)",
      "if (Lab.checkSaving)" in tick and "finally { Lab.checkSaving = false; }" in tick)
check("activate checks res.ok", "res.ok" in lock and "Couldn't activate" in lock)
check("route poll sends its job_id", '"/api/optimize/status?job_id="' in js)
check("playbook poll sends its job_id", '"/api/playbook/synthesize/status?job_id="' in js)

print("#182-5 the import prompt asks for the start signal:")
brief = extract.SCHEMA_BRIEF
check("no 'WARNING/START gun'", "WARNING/START" not in brief)
check("asks for the START signal, never the warning", "START signal" in brief
      and "never the warning" in brief)

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
