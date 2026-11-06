"""GPS-OUT — the gameplan is checked against the course aboard before it goes to the GPS (#183-11).

The re-route already carries the course it was made for (_course_ok); the gameplan used to be
matched by mark NAME only, so a course re-laid aboard after the Lab's push still sent the old
track. Now the bundle's Lab course hash must match the definition the engine holds, and that
definition must still be bound to the marks and the course load aboard.

Run:  PYTHONPATH=vps/agent:.:pi/archiver python3 vps/agent/test_gpsout_course.py
"""
import copy
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")

from shared import race_def  # noqa: E402
from app import gpsout, orinroute  # noqa: E402
from app import navigator as NAV  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


DEF = {"race_id": "r1", "name": "Test race", "timezone": "America/Toronto",
       "courses": [{"id": "c1", "start": {"lat": 43.0, "lon": -82.4},
                    "marks": [{"name": "Top", "lat": 43.3, "lon": -82.4, "rounding": "port",
                               "note": "prose, not part of the hash"},
                              {"name": "Rock", "type": "island", "lat": 43.2, "lon": -82.3}],
                    "finish": {"lat": 43.0, "lon": -82.41}},
                   {"id": "c2", "start": {"lat": 44.0, "lon": -82.0}, "marks": []}],
       "zones": [{"name": "keep out", "polygon": [[43.1, -82.5], [43.1, -82.45], [43.15, -82.45]]}]}


def flat_marks(definition, cid):
    marks, _s, _c = race_def.course_to_marks(definition, cid)
    return [{"name": n, "lat": a, "lon": o} for _seq, n, a, o in marks]


ABOARD = {"marks": flat_marks(DEF, "c1")}
NAV.active_route = lambda: "race"
NAV._marks = lambda route: ABOARD["marks"] if route == "race" else []

orinroute.save_definition(DEF, "c1", "race")          # what the engine's /course/load keeps
PATH = [{"lat": 43.0 + i * 0.02, "lon": -82.4} for i in range(16)]
BUNDLE = {"race_id": "r1", "course_id": "c1", "course_hash": race_def.course_hash(DEF, "c1"),
          "recommended": "a", "variants": [{"id": "a", "route": {"path": PATH}}]}

print("gameplan course check:")
check("routed on the course aboard → ok", gpsout._playbook_course_ok(BUNDLE) is None)
check("…also named by route", gpsout._playbook_course_ok(BUNDLE, "race") is None)
check("no hash (frozen before the check) → refused",
      gpsout._playbook_course_ok({**BUNDLE, "course_hash": None}) == gpsout.PLAN_UNCHECKED)

amended = copy.deepcopy(DEF)
amended["courses"][0]["marks"][0]["lat"] = 43.31
check("routed on an older course (a mark moved in the Lab since) → refused",
      gpsout._playbook_course_ok({**BUNDLE, "course_hash": race_def.course_hash(amended, "c1")})
      == gpsout.PLAN_OLD_COURSE)
check("routed on the other course of the race → refused",
      gpsout._playbook_course_ok({**BUNDLE, "course_hash": race_def.course_hash(DEF, "c2")})
      == gpsout.PLAN_OLD_COURSE)

# the case the finding names: the course re-laid ABOARD after the push
moved = copy.deepcopy(ABOARD["marks"])
moved[1]["lat"] += 0.01
ABOARD["marks"] = moved
check("a mark moved aboard after the push → refused",
      gpsout._playbook_course_ok(BUNDLE) == gpsout.PLAN_OLD_COURSE)
ABOARD["marks"] = flat_marks(DEF, "c1")
check("(control) marks back → ok again", gpsout._playbook_course_ok(BUNDLE) is None)

load = orinroute._read(orinroute._load_id_path())
orinroute.set_load_id("race", "another-load")          # e.g. a practice course laid aboard
check("another course load aboard → refused",
      gpsout._playbook_course_ok(BUNDLE) == gpsout.PLAN_OLD_COURSE)
orinroute.set_load_id("race", load["load_id"])

os.remove(orinroute._definition_path())
check("no deployed definition aboard (course laid by hand) → refused",
      gpsout._playbook_course_ok(BUNDLE) == gpsout.PLAN_OLD_COURSE)
orinroute.save_definition(DEF, "c1", "race")

print("through show():")
gpsout.deviation._load_playbook = lambda: BUNDLE
gpsout.own_fix = lambda now=None: (43.001, -82.4, 0)
gpsout._course_context = lambda route=None: ([], (43.001, -82.4), None, None)
posts = []
gpsout._post = lambda path, payload=None, **kw: (posts.append(path) or
                                           {"broadcasting": True, "n_waypoints": len(PATH)})
r = gpsout.show()
check(f"gameplan for the course aboard → sent [{r.get('note')}]",
      r.get("shown") is True and "/route" in posts)
posts.clear()
ABOARD["marks"] = moved
r = gpsout.show()
check("course re-laid aboard → not sent, says why",
      r == {"shown": False, "note": gpsout.PLAN_OLD_COURSE} and posts == [])

print("ALL OK" if ok else "FAILED")
raise SystemExit(0 if ok else 1)
