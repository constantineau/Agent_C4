"""#151: a mark or finish with no position never gives a "complete" playbook, and isn't pushed.

`course_to_marks` drops a mark with no position, and a finish line with a blank point. The
playbook then routed and froze the SHORTER course, and its completeness check passed against the
last surviving mark. Deploy pushed it anyway. A gate with one end set routed TO that end, and a
course with no finish ended at its last mark, both with nothing reported.

Pinned:
  * build_playbook refuses (available: false, the names in plain words) before any wind is fetched;
  * synthesize passes that refusal on unchanged, so nothing can be frozen;
  * a full course still builds (control);
  * Deploy refuses the whole push while the course, or the locked playbook, is missing positions.

No network: the wind field, router and posts are stubbed.
"""
import copy
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi:8200"
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import deploy as D, labstate, playbook, synthesis  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


FULL = {"race_id": "m", "name": "M", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 43.5, "lon": -82.0},
    "marks": [{"name": "M1", "type": "buoy", "lat": 44.0, "lon": -82.0, "rounding": "port"},
              {"name": "M2", "type": "buoy", "lat": 44.2, "lon": -82.2, "rounding": "port"}],
    "finish": {"points": [{"lat": 44.5, "lon": -82.0}, {"lat": 44.5, "lon": -81.9}]}}]}

HALF_FINISH = copy.deepcopy(FULL)
HALF_FINISH["courses"][0]["finish"]["points"][1] = {"lat": None, "lon": None}
NO_MARK = copy.deepcopy(FULL)
NO_MARK["courses"][0]["marks"][1].update(lat=None, lon=None)
GATE = {"name": "G", "type": "gate", "lat": 44.1, "lon": -82.1, "lat2": 44.1, "lon2": -82.0,
        "rounding": "gate"}
FULL["courses"][0]["marks"].insert(1, dict(GATE))
HALF_GATE = copy.deepcopy(FULL)
HALF_GATE["courses"][0]["marks"][1].update(lat2=None, lon2=None)
NO_FINISH = copy.deepcopy(FULL)
del NO_FINISH["courses"][0]["finish"]

fetched = []


class Empty:
    loaded = False

    def status(self):
        return {}


def fake_wind(*a, **k):
    fetched.append(1)
    return Empty()                     # the control stops right after the wind field


playbook.build_windfield = fake_wind

print("1) build_playbook refuses a course with a missing position")
for name, d, missing in (("finish with a blank point", HALF_FINISH, "Finish"),
                         ("turning mark with no position", NO_MARK, "M2"),
                         ("gate with one end", HALF_GATE, "G (one end)"),
                         ("no finish at all", NO_FINISH, "Finish")):
    fetched.clear()
    pb = playbook.build_playbook(d, "c", 0, ["gfs"], v2_scenarios=False, model_skill=False)
    check(f"{name}: no playbook", pb.get("available") is False)
    check(f"{name}: names it", missing in (pb.get("note") or "")
          and any(missing in s for s in pb.get("skipped_marks") or []))
    check(f"{name}: refused before fetching wind", not fetched)

print("2) synthesize passes the refusal on: nothing to freeze")
b = synthesis.synthesize(HALF_FINISH, "c", 0, ["gfs"])
check("not available, no variants", b.get("available") is False and not b.get("variants"))

print("3) (control) a full course goes on to build")
fetched.clear()
pb = playbook.build_playbook(FULL, "c", 0, ["gfs"], v2_scenarios=False, model_skill=False)
check("wind fetched, no missing-position refusal", fetched and not pb.get("skipped_marks"))

print("4) Deploy refuses while positions are missing")
RACES = {"m": FULL}
D.store.get_race = lambda rid: RACES.get(rid)
D.race_start = lambda d: None
posted = []
D._post_json = lambda url, body, timeout=25: (posted.append(url) or True, {"loaded": True})
D.orinassets.build = lambda *a, **k: ({"schema": "x"}, [])
BUNDLES = {}
D.pbstore.get = lambda pid: BUNDLES.get(pid)


def push(definition, bundle=None):
    RACES["m"] = definition
    BUNDLES.clear()
    labstate.set(D._lock_key("m"), {})
    if bundle is not None:
        # a real, signed bundle (#152) whose route checks passed (#242)
        BUNDLES["pb1"] = synthesis.sign_bundle(dict({"safety": {"ok": True, "problems": [], "overrides": []}, "consensus": {"safety": {"ok": True, "problems": [], "overrides": []}}}, **bundle))
        labstate.set(D._lock_key("m"), {"playbook_id": "pb1", "course_id": "c"})
    posted.clear()
    return D.push("m")


res = push(HALF_FINISH)
check("course missing the finish: refused, nothing sent",
      res["ok"] is False and res.get("refused") and not posted)
check("…and says why in plain words", "Missing positions" in res.get("detail", "")
      and "Finish" in res.get("detail", ""))
res = push(HALF_GATE, bundle={"race_id": "m", "course_id": "c", "skipped_marks": []})
check("a half gate, even under a bundle that lists nothing: refused, nothing sent",
      res.get("refused") and not posted and "G (one end)" in res.get("detail", ""))
res = push(NO_FINISH)
check("no finish at all: refused", res.get("refused") and not posted)
res = push(FULL, bundle={"race_id": "m", "course_id": "c", "skipped_marks": ["M2"]})
check("an older playbook frozen on a short course: refused, nothing sent",
      res["ok"] is False and res.get("refused") and not posted and "M2" in res.get("detail", ""))
res = push(FULL, bundle={"race_id": "m", "course_id": "c", "skipped_marks": []})
check("(control) a full course and playbook: sent", posted and not res.get("refused"))

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
