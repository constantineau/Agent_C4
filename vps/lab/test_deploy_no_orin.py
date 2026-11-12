"""Deploy on a boat with no Orin (#313; vps/lab/app/deploy.py push + boat_status).

Pins:
  * only the Pi's word counts: when its `copilot` answer says `absent` (the profile it holds says
    no Orin) the push has no copilot step, says "No Orin on this boat." and is green when
    everything else loaded; the Lab's own profile saying no Orin never drops a failed copilot
    step;
  * a boat WITH an Orin (said, or not said) still fails the push when the Orin doesn't load it:
    a real failure is never hidden;
  * the live check doesn't call the missing Orin "unreachable";
  * an edited profile saved before #313 takes its bundled seed's hardware block.

No network: the posts are faked. Run in-container (make ci): python3 /repo/vps/lab/test_deploy_no_orin.py
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLAYBOOK_DIR"] = os.path.join(_tmp, "playbooks")
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi:8200"
os.environ["DEPLOY_COPILOT_URL"] = "http://pi:8091/copilot"

from shared import boat_hardware as BH, boat_profile  # noqa: E402

from app import boats, deploy as D, labstate, orinassets, pbstore, synthesis  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file
pbstore.PLAYBOOK_DIR = os.environ["PLAYBOOK_DIR"]
boats.EDITED_DIR = os.path.join(_tmp, "boats")

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


RACE = {"race_id": "r1", "name": "Race", "courses": [{"id": "c", "marks": [
    {"name": "A", "lat": 50.0, "lon": -1.0}, {"name": "B", "lat": 50.5, "lon": -1.5}],
    "finish": {"points": [{"lat": 50.6, "lon": -1.6}, {"lat": 50.6, "lon": -1.5}]}}],
    "fleet": [{"name": "X"}]}
D.store.get_race = lambda rid: RACE if rid == "r1" else None
D._clock = lambda: 0

PIONLY = {"boat_id": "pionly", "name": "Pi Only", "draft_m": 1.8,
          "hardware": {"orin": False, "battery": {"nominal_v": 24, "chemistry": "lithium"}}}
WITH = {"boat_id": "withorin", "name": "With Orin", "draft_m": 1.8}
BOAT = {"profile": PIONLY}
orinassets.build = lambda *a, **k: ({"schema": "c4.orin-assets/v1", "boat": BOAT["profile"],
                                     "boat_id": BOAT["profile"]["boat_id"]}, [])
D.orinassets = orinassets

COP = {"answer": {"loaded": False, "detail": "Can't reach the Orin. Playbook not sent there."}}
posted = []


def fake_post(url, body, timeout=25):
    posted.append(url.rsplit(":8200", 1)[-1])
    if url.endswith("/playbook/load"):
        return True, {"loaded": True, "race_id": "r1", "copilot": COP["answer"]}
    return True, {"loaded": True}


D._post_json = fake_post
bundle = synthesis.sign_bundle({"race_id": "r1", "course_id": "c", "start_epoch": 100,
                                "variants": [{"id": "a"}], "plays": [],
                                "safety": {"ok": True, "problems": [], "overrides": []},
                                "consensus": {"safety": {"ok": True, "problems": [], "overrides": []}}})
D.lock_in("r1", pbstore.save(bundle))

print("the profile:")
check("a hardware block is a valid profile field", boat_profile.validate(PIONLY)[0] == []
      and boat_profile.shape_errors(PIONLY) == [])
bad = dict(PIONLY, hardware={"orin": "no", "battery": {"nominal_v": 240}, "plotter": {"max_route_waypoints": 9999}})
errs = boat_profile.shape_errors(bad)
check(f"a bad one is refused, field by field ({errs})",
      {"bad hardware.orin", "hardware.battery.nominal_v must be between 6 and 60"} <= set(errs)
      and any("max_route_waypoints" in e for e in errs))

print("the Lab's profile says no Orin, but the Pi tried and failed (older Pi, or its profile says an Orin):")
res = D.push("r1")
st = res["steps"][-1]
check("the copilot step is kept and red: the Lab's own profile never hides an Orin result",
      not res["ok"] and st["target"] == "orin-copilot" and not st["ok"] and "notes" not in res)

print("the Pi says no Orin (copilot absent, by the profile it holds):")
COP["answer"] = {"loaded": False, "absent": True, "detail": BH.NO_ORIN}
res = D.push("r1")
check("green: every step that exists loaded", res["ok"] and res["pushed_playbook"])
check("no copilot step", [s["target"] for s in res["steps"]].count("orin-copilot") == 0)
check(f"said plainly ({res.get('notes')})", res.get("notes") == [BH.NO_ORIN])
check("recorded for the Deploy tab", D.last_push()["last"]["notes"] == [BH.NO_ORIN]
      and D.last_push()["last"]["ok"])
BOAT["profile"] = WITH
res = D.push("r1")
check("the same whatever the Lab's profile says", res["ok"] and res.get("notes") == [BH.NO_ORIN]
      and "orin-copilot" not in [s["target"] for s in res["steps"]])

print("a boat with an Orin that didn't load it:")
COP["answer"] = {"loaded": False, "detail": "Can't reach the Orin. Playbook not sent there."}
res = D.push("r1")
st = res["steps"][-1]
check("the copilot step is there and red: a real failure still shows",
      not res["ok"] and st["target"] == "orin-copilot" and not st["ok"] and "notes" not in res)
COP["answer"] = {"loaded": True, "race_id": "r1"}
res = D.push("r1")
check("...and green when it loads", res["ok"] and res["steps"][-1]["target"] == "orin-copilot")

print("the live check (the Pi's word, never an unpushed Lab edit):")
boats.save_boat(PIONLY)                  # the Lab says no Orin for pionly
ANS = {"http://pi:8200/playbook": {"loaded": True, "race_id": "r1"},
       "http://pi:8200/course": {"available": True, "marks": []},
       "http://pi:8200/reroute/assets": {"available": True, "boat_id": "pionly", "orin": False}}


def fake_get(url, timeout=4):
    if url in ANS:
        return True, ANS[url]
    return False, {"error": "timed out"}


st = D.boat_status(get=fake_get)
check(f"the Pi says no Orin: said, not 'unreachable' ({st['copilot']})",
      st["copilot"].get("absent") is True and st["copilot"]["note"] == BH.NO_ORIN
      and st["copilot"]["error"] is None and st["engine"]["orin_assets"]["orin"] is False)
ANS["http://pi:8200/reroute/assets"] = {"available": True, "boat_id": "pionly", "orin": True}
st = D.boat_status(get=fake_get)
check("the Lab's unpushed 'no Orin' never hides the Pi's Orin failing to answer",
      not st["copilot"].get("absent") and st["copilot"]["reachable"] is False
      and st["copilot"]["error"])
HEALTH = {"playbook": {"loaded": True, "race_id": "r1", "variants": 1, "signed": True}}
ANS["http://pi:8091/copilot/health"] = HEALTH
st = D.boat_status(get=fake_get)
check("...nor its loaded playbook", st["copilot"]["reachable"]
      and st["copilot"]["playbook"]["loaded"] is True)
del ANS["http://pi:8091/copilot/health"]
ANS["http://pi:8200/reroute/assets"] = {"available": True, "boat_id": "pionly"}
st = D.boat_status(get=fake_get)
check("an older engine that doesn't say → an Orin (unreachable here)",
      not st["copilot"].get("absent") and st["copilot"]["reachable"] is False)

print("an edited profile from before #313:")
seed = boats._bundled("sr33")
old = {k: v for k, v in seed.items() if k != "hardware"}
boats.save_boat(dict(old, helm_factor=0.9))
got = boats.get_boat("sr33")
check("takes its seed's hardware block", got["helm_factor"] == 0.9
      and got["hardware"] == seed["hardware"])
boats.save_boat(dict(old, hardware={"orin": False}))
check("...but an owner's own block stands", boats.get_boat("sr33")["hardware"] == {"orin": False})
check("the seed's own kit is the one it always had: an Orin, 12 V lead-acid, a 250-point plotter",
      BH.has_orin(seed) and BH.battery(seed) == {"nominal_v": 12.0, "chemistry": "lead_acid",
                                                 "assumed": False}
      and BH.plotter(seed)["max_route_waypoints"] == 250)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
