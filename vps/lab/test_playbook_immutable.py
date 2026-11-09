"""A locked playbook is immutable bytes (#249, #182-7; vps/lab/app/pbstore.py, deploy.py).

Pins:
  * the id carries race, course and a content digest (the signature's own hash);
  * re-freezing another course, or the same course again, makes a NEW file; the first keeps its bytes;
  * the same content frozen twice keeps the first file's bytes (its signature stays the one recorded);
  * a re-freeze changes nothing on the boat until it is ACTIVATED: Push sends the active revision;
  * an old `<race>__<start>` file still resolves, byte for byte, still verifies (Lab and copilot),
    can be activated and pushed, and a new freeze never writes over it;
  * an old lock whose file was re-frozen after it was activated is refused, never pushed;
  * revisions are numbered per race + course.

No network: the posts are faked. Run in-container: python3 /repo/vps/lab/test_playbook_immutable.py
"""
import json
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLAYBOOK_DIR"] = os.path.join(_tmp, "playbooks")
os.environ["DEPLOY_PI_ENGINE_URL"] = "http://pi:8200"

from app import deploy as D, labstate, orinassets, pbstore, synthesis  # noqa: E402

labstate._PATH = os.path.join(_tmp, "_labstate.json")   # never the image's real state file
pbstore.PLAYBOOK_DIR = os.environ["PLAYBOOK_DIR"]

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def course(cid, lat):
    return {"id": cid, "marks": [{"name": "A", "lat": lat, "lon": -83.0},
                                 {"name": "B", "lat": lat + 0.5, "lon": -83.5}],
            "finish": {"points": [{"lat": lat + 0.6, "lon": -83.6}, {"lat": lat + 0.6, "lon": -83.5}]}}


RACE = {"race_id": "r1", "name": "Race", "courses": [course("north", 45.0), course("south", 44.0)],
        "fleet": [{"name": "X"}]}
D.store.get_race = lambda rid: RACE if rid == "r1" else None
orinassets.build = lambda *a, **k: ({"schema": "c4.orin-assets/v1"}, [])
D.orinassets = orinassets
D._clock = lambda: 0
sent = []


def fake_post(url, body, timeout=25):
    if url.endswith("/playbook/load"):
        sent.append(body)
        return True, {"loaded": True, "race_id": "r1", "copilot": {"loaded": True, "race_id": "r1"}}
    return True, {"loaded": True}


D._post_json = fake_post


def freeze(**kw):
    b = dict({"race_id": "r1", "course_id": "north", "start_epoch": 100, "generated_at": 1,
              "variants": [{"id": "a"}], "plays": []}, **kw)
    b = synthesis.sign_bundle(b)
    return pbstore.save(b), b


def raw(pid):
    return pbstore.get_raw(pid)


print("ids carry race, course and the content digest:")
p1, b1 = freeze()
check(f"id {p1}", p1 == "r1__north__" + b1["signature"]["value"][:12])

print("re-freezing another course keeps the first:")
p2, b2 = freeze(course_id="south", generated_at=2)
check("a new id", p2 != p1 and p2.startswith("r1__south__"))
check("the first is still there, same signature",
      (pbstore.get(p1) or {}).get("signature") == b1["signature"])

print("re-freezing the same course makes a new revision, the old bytes stay:")
before = raw(p1)
p3, b3 = freeze(variants=[{"id": "a"}, {"id": "b"}], generated_at=3)
check("a new id", p3 not in (p1, p2))
check("the old id resolves to its own bytes, not the latest", raw(p1) == before)

print("the same content frozen again keeps the first file:")
pa, _ = freeze()
check("same id, bytes untouched (first signed_at kept)", pa == p1 and raw(p1) == before)

print("revisions numbered per race + course:")
revs = {b["id"]: b["rev"] for b in pbstore.list_bundles()}
check(f"north 1, 2; south 1 ({revs})", revs == {p1: 1, p3: 2, p2: 1})
tie = []
for v in ("y", "x"):                                   # frozen in the same second, y first
    t = synthesis.sign_bundle({"race_id": "r1", "course_id": "tie", "variants": [{"id": v}]})
    t["signature"]["signed_at"] = 5
    tie.append(pbstore.save(t))
os.utime(pbstore._path(tie[0]), ns=(10**9, 10**9))
os.utime(pbstore._path(tie[1]), ns=(2 * 10**9, 2 * 10**9))
revs = {b["id"]: b["rev"] for b in pbstore.list_bundles()}
check("ties in the same second go by write order", revs[tie[0]] == 1 and revs[tie[1]] == 2)

print("a re-freeze changes nothing on the boat until it is activated:")
D.lock_in("r1", p1)
p4, b4 = freeze(variants=[{"id": "c"}], generated_at=4)
res = D.push("r1")
check("Push sends the active revision", res["ok"] and sent[-1]["signature"] == b1["signature"])
check("Deploy still shows it active", D.readiness("r1")["lock_in"]["playbook_id"] == p1)
D.lock_in("r1", p4)
res = D.push("r1")
check("after Activate, Push sends the new one", res["ok"] and sent[-1]["signature"] == b4["signature"])

print("an old <race>__<start> file still resolves, byte for byte:")
legacy = synthesis.sign_bundle({"race_id": "r1", "course_id": "north", "start_epoch": 200,
                                "generated_at": 0, "variants": [{"id": "old"}], "plays": []})
legacy_bytes = json.dumps(legacy, indent=2).encode()
lid = pbstore.legacy_id(legacy)
with open(os.path.join(pbstore.PLAYBOOK_DIR, lid + ".json"), "wb") as f:
    f.write(legacy_bytes)
check(f"{lid} resolves to the same bytes", raw(lid) == legacy_bytes)
check("still verifies", synthesis.verify_bundle(pbstore.get(lid)))
row = next((b for b in pbstore.list_bundles() if b["id"] == lid), {})
check("listed as a revision, marked old", row.get("legacy") is True and isinstance(row.get("rev"), int))
D.lock_in("r1", lid)
res = D.push("r1")
check("can be activated and pushed", res["ok"] and sent[-1] == legacy)
try:
    sys.path.insert(0, "/repo")
    from pi.orin.copilot import playbook as cop      # the boat's own check
    check("the copilot verifies what was pushed", cop.verify_signature(json.loads(json.dumps(sent[-1]))))
except ImportError as exc:
    check(f"copilot check importable ({exc})", False)
freeze(start_epoch=200, variants=[{"id": "new"}], generated_at=5)
check("a new freeze for the same race + start leaves it alone", raw(lid) == legacy_bytes)

print("an old lock whose file was re-frozen after it was activated is refused:")
changed = synthesis.sign_bundle(dict(legacy, variants=[{"id": "other"}], signature=None))
with open(os.path.join(pbstore.PLAYBOOK_DIR, lid + ".json"), "w") as f:   # what old code did
    json.dump(changed, f)
n = len(sent)
res = D.push("r1")
check("refused, nothing sent", res.get("refused") and len(sent) == n
      and res["detail"] == D.PB_CHANGED + D.NOT_SENT)
check("Deploy says so before Push", D.readiness("r1")["lock_problem"] == D.PB_CHANGED)
D.lock_in("r1", lid)
check("activating it again clears it", D.readiness("r1")["lock_problem"] is None)

print("a damaged revision is repaired by freezing the same content again:")
with open(pbstore._path(p1), "w") as f:
    f.write('{"race_id": "r1", "varia')
pb_, _ = freeze()
check("same id, whole and verified again", pb_ == p1 and synthesis.verify_bundle(pbstore.get(p1) or {}))
whole = pbstore.get(p1)
with open(pbstore._path(p1), "w") as f:
    json.dump(dict(whole, signature={"alg": "sha256", "value": "broken"}), f)
freeze()
check("a broken signature alone is repaired too", synthesis.verify_bundle(pbstore.get(p1) or {}))
whole = pbstore.get(p1)
with open(pbstore._path(p1), "w") as f:
    json.dump(dict(whole, signature=dict(whole["signature"], alg="broken")), f)
freeze()
check("so is a broken signature algorithm", synthesis.verify_bundle(pbstore.get(p1) or {}))
ra = []
for v, at in (("a", 10), ("b", 20)):
    t = synthesis.sign_bundle({"race_id": "r1", "course_id": "rep", "variants": [{"id": v}]})
    t["signature"]["signed_at"] = at
    ra.append((pbstore.save(t), t))
with open(pbstore._path(ra[0][0]), "w") as f:
    json.dump(dict(ra[0][1], signature=dict(ra[0][1]["signature"], value="broken")), f)
pbstore.save(synthesis.sign_bundle(dict(ra[0][1], signature=None)))    # re-frozen now
revs = {b["id"]: b["rev"] for b in pbstore.list_bundles()}
check("a repaired revision keeps its number", revs[ra[0][0]] == 1 and revs[ra[1][0]] == 2
      and synthesis.verify_bundle(pbstore.get(ra[0][0])))

print("ok" if ok else "FAILED")
sys.exit(0 if ok else 1)
