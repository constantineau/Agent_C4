"""Deploy never pushes today's course beside a playbook routed on an older one (#150).

Pins:
  * the course hash covers what the route depends on (marks, rounding sides, islands, finish, the
    race's zones) and ignores prose (notes, SI refs), so a note edit never blocks a push;
  * synthesis stamps the hash of the race it was GIVEN, before routing runs, into the bundle (inside
    the signed content);
  * an SI amendment after the freeze (a mark moved, a rounding side flipped, a zone added) → the push
    is refused before any post, and the Deploy tab says so before Push;
  * the same course → pushed as before;
  * a bundle frozen before this check (no hash) → still pushed, with a plain note to re-freeze.

No network: the posts are faked. Run in-container (make ci): python3 /repo/vps/lab/test_deploy_course_hash.py
"""
import copy
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLAYBOOK_DIR"] = os.path.join(_tmp, "playbooks")
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi:8200"

from shared import race_def  # noqa: E402
from app import deploy as D, labstate, orinassets, pbstore, synthesis  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file
pbstore.PLAYBOOK_DIR = os.environ["PLAYBOOK_DIR"]

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


BASE = {"race_id": "r1", "name": "Race", "fleet": [{"name": "X"}],
        "zones": [{"name": "TSS", "type": "exclusion", "note": "SI 9",
                   "geometry": {"type": "Polygon", "coordinates": [[[-83, 45], [-83, 45.1], [-83.1, 45]]]}}],
        "courses": [
            {"id": "c", "name": "Long", "note": "SI 7.1",
             "start": {"lat": 44.9, "lon": -82.9, "ref": "SI 6"},
             "marks": [{"seq": 1, "name": "A", "type": "mark", "rounding": "port", "lat": 45.0,
                        "lon": -83.0, "coords_source": "si", "note": "leave to port"},
                       {"seq": 2, "name": "Isle", "type": "island", "rounding": "starboard",
                        "lat": 45.2, "lon": -83.2}],
             "finish": {"type": "line", "points": [{"lat": 45.5, "lon": -83.5}, {"lat": 45.51, "lon": -83.5}]}},
            {"id": "s", "marks": [{"name": "Z", "lat": 44.0, "lon": -82.0}]}]}
H = race_def.course_hash(BASE, "c")

print("what the course hash covers:")


def edited(fn):
    d = copy.deepcopy(BASE)
    fn(d)
    return race_def.course_hash(d, "c")


check("stable, short", H == race_def.course_hash(copy.deepcopy(BASE), "c") and len(H) == 16)
check("a note or SI ref edit: same", edited(lambda d: (
    d["courses"][0]["marks"][0].update(note="moved per amendment 2"),
    d["courses"][0].update(note="x"), d["zones"][0].update(note="y"),
    d["courses"][0]["start"].update(ref="SI 6.2"))) == H)
check("a mark moved: differs", edited(lambda d: d["courses"][0]["marks"][0].update(lat=45.01)) != H)
check("a rounding side flipped: differs",
      edited(lambda d: d["courses"][0]["marks"][0].update(rounding="starboard")) != H)
check("an island's side flipped: differs",
      edited(lambda d: d["courses"][0]["marks"][1].update(rounding="port")) != H)
check("a zone added: differs", edited(lambda d: d["zones"].append({"name": "Z2", "geometry": {}})) != H)
check("the finish moved: differs",
      edited(lambda d: d["courses"][0]["finish"]["points"][0].update(lon=-83.6)) != H)
check("another course edited: same", edited(lambda d: d["courses"][1]["marks"][0].update(lat=44.5)) == H)
check("no course id → the first course (same pick as course_to_marks)", race_def.course_hash(BASE, None) == H)

print("synthesis stamps the course it was given, before routing runs:")
pbmod = synthesis.pb


def fake_build(definition, course_id, *a, **k):
    definition["courses"][0]["marks"][0]["lat"] = 0.0          # a router that touches its input
    return {"available": True, "course_id": "c", "start_epoch": 100, "variants": [],
            "consensus": {}, "windfield": {"models": []}}


pbmod.build_playbook = fake_build
synthesis._opus_synthesis = lambda *a, **k: None
synthesis._deterministic_synthesis = lambda *a, **k: {"variants": {}, "recommended": None,
                                                      "decision_tree": [], "headline": ""}
synthesis._venue_stats = lambda *a, **k: None
synthesis._build_plays = lambda *a, **k: ([], None, None)
synthesis._buoy_stations = lambda *a, **k: []
synthesis._fingerprint = lambda *a, **k: None
synthesis._boat_model = lambda *a, **k: {}
b = synthesis.synthesize(copy.deepcopy(BASE), "c", 100, ["gfs"])
check("bundle carries course_hash of the race as given", b.get("course_hash") == H)
signed = synthesis.sign_bundle(dict(b, variants=[{"id": "a"}]))
check("…inside the signed content", synthesis.verify_bundle(signed)
      and not synthesis.verify_bundle(dict(signed, course_hash="0" * 16)))

print("Deploy checks it:")
RACE = {"r1": copy.deepcopy(BASE)}
D.store.get_race = lambda rid: RACE.get(rid)
orinassets.build = lambda *a, **k: ({"schema": "c4.orin-assets/v1"}, [])
D.orinassets = orinassets
D._clock = lambda: 0
posted = []


def fake_post(url, body, timeout=25):
    posted.append(url.rsplit(":8200", 1)[-1])
    if url.endswith("/playbook/load"):
        return True, {"loaded": True, "race_id": "r1", "copilot": {"loaded": True, "race_id": "r1"}}
    return True, {"loaded": True}


D._post_json = fake_post
pid = pbstore.save(signed)
D.lock_in("r1", pid)
res = D.push("r1")
check("same course → pushed, playbook on both boxes", res["ok"] and res["pushed_playbook"]
      and "/playbook/load" in posted)
check("readiness: no problem, no note", D.readiness("r1")["lock_problem"] is None
      and D.readiness("r1")["lock_note"] is None)

RACE["r1"]["courses"][0]["marks"][0]["note"] = "typo fixed"
posted.clear()
check("a note edit → still pushed", D.push("r1")["ok"] and "/playbook/load" in posted)

RACE["r1"]["courses"][0]["marks"][0]["rounding"] = "starboard"     # SI amendment after the freeze
posted.clear()
res = D.push("r1")
check("SI amendment after the freeze → refused before any post", posted == [] and res["refused"]
      and res["ok"] is False and res["steps"] == [])
check("says why, plainly", res["detail"] == "Course changed since this playbook. Freeze a new one. Not sent.")
check("readiness says so before Push", D.readiness("r1")["lock_problem"] == D.PB_COURSE)
RACE["r1"] = copy.deepcopy(BASE)

print("a bundle frozen before this check (no course_hash):")
old = {k: v for k, v in signed.items() if k not in ("course_hash", "signature")}
old = synthesis.sign_bundle(old)
D.lock_in("r1", pbstore.save(old))       # a new revision (#249), activated
RACE["r1"]["courses"][0]["marks"][0]["lat"] = 45.3                 # can't tell: not refused
posted.clear()
res = D.push("r1")
check("still pushed (no way to tell what it was routed on)", res["ok"] and "/playbook/load" in posted)
check("readiness: a plain note to re-freeze, no problem",
      D.readiness("r1")["lock_problem"] is None and D.readiness("r1")["lock_note"] == D.PB_UNCHECKED)

print("an edit + re-freeze between Push's reads never sends one course beside another's playbook:")
A = copy.deepcopy(BASE)
B = copy.deepcopy(BASE)
B["courses"][0]["marks"][0]["lat"] = 45.4                         # the SI amendment
PB_A = synthesis.sign_bundle(dict(signed, course_hash=race_def.course_hash(A, "c"), signature=None))
PB_B = synthesis.sign_bundle(dict(signed, course_hash=race_def.course_hash(B, "c"), signature=None))
reads = {"race": 0, "pb": 0}


def race_then_edit(rid):                 # the first read sees the old race, later ones the new
    reads["race"] += 1
    return A if reads["race"] == 1 else B


def pb_then_refreeze(pid):
    reads["pb"] += 1
    return PB_A if reads["pb"] == 1 else PB_B


real_get_race, real_pb_get = D.store.get_race, D.pbstore.get
D.store.get_race, D.pbstore.get = race_then_edit, pb_then_refreeze
bodies = {}
D._post_json = lambda url, body, timeout=25: (bodies.__setitem__(url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1], body), fake_post(url, body))[1]
res = D.push("r1")
D.store.get_race, D.pbstore.get, D._post_json = real_get_race, real_pb_get, fake_post
sent_course = (bodies.get("course/load") or {}).get("definition")
sent_pb = bodies.get("playbook/load")
check("either refused, or the course sent is the one the sent playbook was routed on",
      res.get("refused") or (sent_pb and race_def.course_hash(sent_course, "c") == sent_pb["course_hash"]))

print("short text:")
for s in (D.PB_COURSE, D.PB_UNCHECKED):
    check(f"{s!r} ≤ 9 words", len(s.split()) <= 9)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
