"""Deploy is frozen at the gun (RRS 41; vps/lab/app/deploy.py push).

Pins:
  * before the race start (our gun, else the first minute of the start date: the plume's rule),
    the push sends everything;
  * at or after the start, nothing is sent at all, and the answer says so in plain words;
  * no start known: it sends, and the boat decides;
  * a boat that refuses (racing) any step answers loaded: false, and that step is never green
    (fleet, checklist and watch used to pass on any answer).

No network: the posts are faked. Run in-container (make ci): python3 /repo/vps/lab/test_deploy_race_start.py
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi:8200"
os.environ["DEPLOY_COPILOT_URL"] = "http://pi:8091/copilot"

from app import deploy as D, labstate, orinassets, racestart  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


RACE = {"race_id": "r1", "name": "Race", "timezone": "America/Detroit",
        "division_starts": {"PHRF A": "2026-07-18 13:00"}, "own": {"division": "PHRF A"},
        "courses": []}
GUN = racestart.race_start(RACE)["epoch"]
RACES = {"r1": RACE}
D.store.get_race = lambda rid: RACES.get(rid)
D.package = lambda rid, course_id=None: {
    "race_name": "Race", "course_id": "c", "course_load": {"definition": RACES[rid], "course_id": "c"},
    "fleet_load": {"definition": RACES[rid]}, "checklist_load": {"items": [{"id": "x"}]},
    "watch_load": {"plan": {"blocks": [{"start": 1, "end": 2, "on": "A"}]}}}
orinassets.build = lambda *a, **k: ({"schema": "c4.orin-assets/v1"}, [])
D.orinassets = orinassets

posted = []
BOAT = {"racing": False}


def fake_post(url, body, timeout=25):
    posted.append(url.rsplit(":8200", 1)[-1])
    if BOAT["racing"]:
        return True, {"loaded": False, "detail": "Racing now. Not loaded."}
    return True, {"loaded": True}


D._post_json = fake_post

print("before the gun:")
D._clock = lambda: GUN - 60
res = D.push("r1")
check("everything sent, all green", res["ok"] and posted == ["/course/load", "/reroute/assets",
                                                             "/fleet/load", "/checklist/load", "/watch"])

print("at and after the gun:")
for label, t in (("at the gun", GUN), ("an hour after", GUN + 3600)):
    posted.clear()
    D._clock = lambda t=t: t
    res = D.push("r1")
    check(f"{label}: nothing sent", posted == [])
    check(f"{label}: refused, plain", res["ok"] is False and res["refused"] is True
          and res["detail"] == "Race started. Not sent." and res["steps"] == [])

print("no gun, start date only (the plume's rule: the first minute of that day):")
RACES["r2"] = {"race_id": "r2", "start_date": "2026-07-18", "timezone": "America/Detroit",
               "courses": []}
day0 = D.race_start(RACES["r2"])
check("start = midnight of the start date, before any gun that day", day0 is not None and day0 < GUN)
posted.clear()
D._clock = lambda: day0 + 60
check("race day morning: not sent", D.push("r2")["refused"] and posted == [])

print("no start known: sent (the boat decides):")
RACES["r3"] = {"race_id": "r3", "courses": []}
posted.clear()
D._clock = lambda: GUN + 10 * 86400
res = D.push("r3")
check("sent", res["ok"] and len(posted) == 5)

print("the boat refuses (racing):")
BOAT["racing"] = True
D._clock = lambda: GUN - 60
res = D.push("r1")
check("every step red", not res["ok"] and [s["ok"] for s in res["steps"]] == [False] * 5)

print("short text:")
check("≤ 6 words", len(D.RACE_STARTED.split()) <= 6)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
