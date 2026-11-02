"""Deploy is frozen at the gun (RRS 41; vps/lab/app/deploy.py push).

Pins:
  * before the race start (our gun, else the first minute of the start date: the plume's rule),
    the push sends everything;
  * at or after the start, nothing is sent at all, and the answer says so in plain words;
  * the start is checked before EVERY post: a push begun just before the gun stops at the gun,
    and the remaining steps are not sent;
  * the Orin copilot's playbook is never posted by the Lab: the Pi forwards it under its race-log
    lock, and the copilot step reports the Pi's `copilot` answer;
  * no start known: it sends, and the boat decides;
  * the manual commands load the playbook through the Pi (never scp to the Orin: the copilot only
    uses a playbook the Pi approved);
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


TICK = {"step": 0}


def fake_post(url, body, timeout=25):
    posted.append(url.rsplit(":8200", 1)[-1])
    CLOCK["t"] += TICK["step"]                   # each post takes this long
    if BOAT["racing"]:
        return True, {"loaded": False, "detail": "Racing now. Not loaded."}
    if url.endswith("/playbook/load"):
        return True, {"loaded": True, "race_id": "r1",
                      "copilot": BOAT.get("copilot", {"loaded": True, "race_id": "r1"})}
    return True, {"loaded": True}


CLOCK = {"t": 0}


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

print("a push begun just before the gun stops at the gun:")
BOAT["racing"] = False
D._clock = lambda: CLOCK["t"]
CLOCK["t"], TICK["step"] = GUN - 25, 10           # 10 s a post: the gun falls during the 3rd
posted.clear()
res = D.push("r1")
check("only the posts begun before the gun were sent",
      posted == ["/course/load", "/reroute/assets", "/fleet/load"])
check("the rest are red, not sent, plain", [s["step"] for s in res["steps"] if s.get("not_sent")]
      == ["checklist", "watch"] and all(s["detail"]["error"] == "Race started. Not sent."
                                        for s in res["steps"] if s.get("not_sent")))
check("the push is not ok and says why", res["ok"] is False and res["detail"] == "Race started. Not sent.")
TICK["step"] = 0

print("the playbook: the Lab never posts to the Orin; the Pi forwards it:")
D.pbstore.get = lambda pid: {"race_id": "r1", "variants": [{"id": "a"}]} if pid == "pb1" else None
labstate.set("deploy:r1", {"playbook_id": "pb1"})
CLOCK["t"] = GUN - 3600
posted.clear()
res = D.push("r1")
check("one playbook post, to the Pi engine", [u for u in posted if "playbook" in u] == ["/playbook/load"]
      and not any("copilot" in u for u in posted))
cop = res["steps"][-1]
check("copilot step = the Pi's forward answer", cop["target"] == "orin-copilot" and cop["ok"]
      and cop["detail"]["race_id"] == "r1" and res["ok"])
BOAT["copilot"] = {"loaded": False, "detail": "Can't reach the Orin. Playbook not sent there."}
res = D.push("r1")
check("Orin unreachable → copilot step red with the Pi's reason", not res["steps"][-1]["ok"]
      and res["steps"][-1]["detail"]["error"] == "Can't reach the Orin. Playbook not sent there.")
BOAT.pop("copilot")
CLOCK["t"], TICK["step"] = GUN - 45, 10           # the gun falls before the playbook post
posted.clear()
res = D.push("r1")
check("gun before the playbook post → neither the Pi nor the Orin gets it",
      "/playbook/load" not in posted and [s["ok"] for s in res["steps"][-2:]] == [False, False])
TICK["step"] = 0
labstate.set("deploy:r1", {})

print("the manual-load commands the Deploy tab shows:")
JS = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "app.js")).read()
cmds = JS[JS.index("function deployPanel"):JS.index("const res = Dep.pushRes")]
check("the playbook goes to the Pi's /playbook/load", "/playbook/load" in cmds)
check("never copied to the Orin or the copilot restarted by hand (the copilot wouldn't use it)",
      "scp" not in cmds and "orin_playbook_path" not in cmds and "systemctl" not in cmds)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
