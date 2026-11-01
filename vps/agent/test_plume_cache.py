"""Onboard forecast-plume cache (vps/agent/app/plume.py, served by the Pi engine's /plume).

Pins: a pushed plume lands on disk and comes back with its age; it survives a "restart" (a fresh
read of the file, nothing held in memory); a bad payload is refused and never replaces the good
one; no plume aboard is an honest `available: false`.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_plume_cache.py
"""
import json
import os
import tempfile
import threading
import time

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")

from app import plume  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


NOW = 1_800_000_000
GOOD = {"schema": "c4.plume/v1", "key": "r1__pb", "race_id": "r1", "basis": "route",
        "race_start": NOW + 10 * 86400, "race_end": None, "built_at": NOW - 600, "window": [NOW - 6 * 3600, NOW + 24 * 3600],
        "models": [{"model": "hrrr", "runs": [{"cycle": NOW - 7200, "at": NOW - 600,
                                               "pts": [[NOW, 11.2, 240], [NOW + 3600, 12.0, 245]]}]}]}

print("nothing aboard:")
g = plume.get(NOW)
check("available false with a reason", g["available"] is False and "Lab" in g["note"])

print("push → disk → read:")
r = plume.load(GOOD, now=NOW - 300)
check("load accepted", r["loaded"] and r["models"] == 1 and r["built_at"] == NOW - 600)
path = os.path.join(_tmp, "engine", "plume.json")
check("written next to the engine store", os.path.exists(path))
g = plume.get(NOW)
check("served back with the data age (since the Lab recorded it)", g["available"] and g["age_s"] == 600)
check("...and how long since the boat got it", g["received_age_s"] == 300)
check("the runs come back intact", g["models"][0]["runs"][0]["pts"][1] == [NOW + 3600, 12.0, 245])

print("survives a restart / offline:")
with open(path) as f:
    on_disk = json.load(f)
check("the file alone holds the plume (nothing in memory)", on_disk["plume"]["key"] == "r1__pb")
g = plume.get(NOW + 6 * 3600)
check("six hours offline: still served, age grows", g["available"] and g["age_s"] == 6 * 3600 + 600)

print("bad payloads refused:")
for name, bad in (("wrong schema", dict(GOOD, schema="x")),
                  ("no models", dict(GOOD, models=[])),
                  ("bad points", dict(GOOD, models=[{"model": "gfs", "runs": [{"pts": [[1, 2]]}]}])),
                  ("not a dict", ["nope"]),
                  ("unknown model id", dict(GOOD, models=[{"model": "xyz", "runs": GOOD["models"][0]["runs"]}])),
                  ("label not a string", dict(GOOD, label={"a": 1})),
                  ("infinite timestamp", dict(GOOD, built_at=float("inf"))),
                  ("infinite point", dict(GOOD, models=[{"model": "hrrr", "runs": [
                      dict(GOOD["models"][0]["runs"][0], pts=[[NOW, 1e309, 10]])]}])),
                  ("NaN race start", dict(GOOD, race_start=float("nan"))),
                  ("no race start (can't tell if its race is on)", dict(GOOD, race_start=None)),
                  ("cycle not a number", dict(GOOD, models=[{"model": "hrrr", "runs": [
                      dict(GOOD["models"][0]["runs"][0], cycle="12Z")]}]))):
    r = plume.load(bad, now=NOW)
    check(f"{name} → not loaded", r["loaded"] is False)
check("...and the good plume is still aboard, readable", plume.get(NOW)["key"] == "r1__pb"
      and plume.get(NOW)["age_s"] == 600)
big = dict(GOOD, models=[{"model": "gfs", "runs": [{"pts": [[NOW, 1.0, 1]] * 60000}]}])
check("an absurdly large payload is refused", plume.load(big, now=NOW)["loaded"] is False)

print("frozen at the gun:")
os.remove(path)
START = NOW + 3600
PRE = dict(GOOD, generated_at=NOW - 60, race_start=START, race_end=None)
check("a pre-start plume loads", plume.load(PRE, now=NOW)["loaded"])
check("before the start it is not frozen", plume.get(NOW)["frozen"] is False)
g = plume.get(START + 600)
check("after the gun it is served as frozen (the pre-start copy)", g["available"] and g["frozen"])
check("...and stays frozen days later with no recorded finish", plume.get(START + 9 * 86400)["frozen"])
newer = dict(PRE, generated_at=START + 300, built_at=START + 200, key="r1__new")
check("a plume computed after the start is refused", plume.load(newer, now=START + 400)["loaded"] is False)
late = dict(PRE, key="r1__late", generated_at=START - 30)
check("computed before the gun but arriving after it → refused (boat's own clock)",
      plume.load(late, now=START + 5)["loaded"] is False)
same_race = dict(GOOD, key="r1__other", generated_at=START + 300, race_start=START + 86400, race_end=None)
check("same race, any other window while racing → refused; pre-start copy stays",
      plume.load(same_race, now=START + 400)["loaded"] is False and plume.get(START + 500)["key"] == "r1__pb")
fin = dict(GOOD, key="r1__after", generated_at=START + 30 * 3600, race_start=START, race_end=START + 20 * 3600)
check("a plume carrying the recorded finish (already past) loads",
      plume.load(fin, now=START + 30 * 3600 + 60)["loaded"] and plume.get(START + 31 * 3600)["frozen"] is False)

print("arriving while racing, empty cache:")
os.remove(path)
check("its race is on at receipt → refused even with nothing aboard",
      plume.load(dict(PRE, generated_at=START - 30), now=START + 60)["loaded"] is False
      and plume.get(START + 60)["available"] is False)

print("one rule while r1 is on — any other race is refused too:")
plume.load(PRE, now=NOW)
other = dict(GOOD, race_id="r2", key="r2__pb", generated_at=START + 600, race_start=START + 86400, race_end=None)
check("while r1 is frozen, r2's pre-start plume is refused; r1's stays",
      plume.load(other, now=START + 700)["loaded"] is False and plume.get(START + 800)["race_id"] == "r1")
fin1 = dict(PRE, key="r1__fin", generated_at=START + 9000, race_end=START + 8000)
check("r1 carrying its recorded finish loads", plume.load(fin1, now=START + 9100)["loaded"])
check("...after which r2's pre-start plume loads", plume.load(other, now=START + 9200)["loaded"]
      and plume.get(START + 9300)["race_id"] == "r2")

print("a load queued at the gun is judged when it gets the lock:")
os.remove(path)
clock = {"t": START - 1}
_real_time = plume.time.time
plume.time.time = lambda: clock["t"]
res = {}
plume._LOCK.acquire()
th = threading.Thread(target=lambda: res.update(plume.load(dict(PRE, generated_at=START - 30))))
th.start()
time.sleep(0.2)
clock["t"] = START + 1                     # the gun goes while it waits for the lock
plume._LOCK.release()
th.join()
check("refused (fresh clock inside the lock)", res.get("loaded") is False)
plume.time.time = _real_time

print("engine restart:")
plume.load(PRE, now=NOW)
import importlib  # noqa: E402
plume = importlib.reload(plume)
check("after a restart the cached race is still frozen and other plumes are refused",
      plume.get(START + 60)["frozen"] and plume.load(other, now=START + 700)["loaded"] is False)

print("concurrent loads:")
import threading  # noqa: E402
os.remove(path)
errs = []


def writer(i):
    for j in range(25):
        try:
            r = plume.load(dict(GOOD, key=f"w{i}_{j}"), now=NOW)
            if not r["loaded"]:
                errs.append(r)
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))


ts = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
check("200 concurrent loads, none failed", not errs)
with open(path) as f:
    last = json.load(f)
check("the file is one whole plume afterwards", last["plume"]["key"].startswith("w"))
check("no temp files left behind", not [n for n in os.listdir(os.path.dirname(path)) if n.endswith(".tmp")])

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
