"""Onboard forecast-plume cache (vps/agent/app/plume.py, served by the Pi engine's /plume).

Pins: a pushed plume lands on disk and comes back with its age; it survives a "restart" (a fresh
read of the file, nothing held in memory); a bad payload is refused and never replaces the good
one; no plume aboard is an honest `available: false`.

Run:  PYTHONPATH=vps/agent python3 vps/agent/test_plume_cache.py
"""
import json
import os
import tempfile

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
        "built_at": NOW - 600, "window": [NOW - 6 * 3600, NOW + 24 * 3600],
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
                  ("cycle not a number", dict(GOOD, models=[{"model": "hrrr", "runs": [
                      dict(GOOD["models"][0]["runs"][0], cycle="12Z")]}]))):
    r = plume.load(bad, now=NOW)
    check(f"{name} → not loaded", r["loaded"] is False)
check("...and the good plume is still aboard", plume.get(NOW)["key"] == "r1__pb")
big = dict(GOOD, models=[{"model": "gfs", "runs": [{"pts": [[NOW, 1.0, 1]] * 60000}]}])
check("an absurdly large payload is refused", plume.load(big, now=NOW)["loaded"] is False)

print("frozen at the gun:")
os.remove(path)
START, END = NOW + 3600, NOW + 30 * 3600
PRE = dict(GOOD, generated_at=NOW - 60, race_start=START, race_end=END)
check("a pre-start plume loads", plume.load(PRE, now=NOW)["loaded"])
check("before the start it is not frozen", plume.get(NOW)["frozen"] is False)
g = plume.get(START + 600)
check("after the gun it is served as frozen (the pre-start copy)", g["available"] and g["frozen"])
newer = dict(PRE, generated_at=START + 300, built_at=START + 200, key="r1__new")
check("a plume computed after the start is refused", plume.load(newer, now=START + 400)["loaded"] is False)
sneaky = dict(GOOD, key="r1__other", generated_at=START + 300)
check("while racing, any new plume is refused (pre-start one stays)",
      plume.load(sneaky, now=START + 400)["loaded"] is False and plume.get(START + 500)["key"] == "r1__pb")
check("after the race the next plume loads again",
      plume.load(dict(GOOD, key="r1__after", generated_at=END + 60), now=END + 120)["loaded"]
      and plume.get(END + 200)["frozen"] is False)

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
