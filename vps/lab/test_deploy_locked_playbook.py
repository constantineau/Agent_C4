"""Deploy never pushes without the playbook it locked in (#152; vps/lab/app/deploy.py push).

Pins:
  * a lock whose bundle is missing (deleted, or unreadable after a crash) → the push is refused
    BEFORE any post, in plain words: the boat keeps its old homework rather than getting new course
    and fleet beside the previous race's playbook while every step shows green;
  * a lock whose bundle fails its signature (edited, half-written) → refused the same way;
  * the Deploy tab is told before anyone presses Push (readiness `lock_problem`);
  * a whole, signed bundle → pushed as before, all steps;
  * nothing locked → pushed without a playbook, as before (the tab says "no playbook in this push").

No network: the posts are faked. Run in-container (make ci): python3 /repo/vps/lab/test_deploy_locked_playbook.py
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


RACE = {"race_id": "r1", "name": "Race", "courses": [{"id": "c", "marks": [
    {"name": "A", "lat": 45.0, "lon": -83.0}, {"name": "B", "lat": 45.5, "lon": -83.5}],
    "finish": {"points": [{"lat": 45.6, "lon": -83.6}, {"lat": 45.6, "lon": -83.5}]}}],
    "fleet": [{"name": "X"}]}
D.store.get_race = lambda rid: RACE if rid == "r1" else None
orinassets.build = lambda *a, **k: ({"schema": "c4.orin-assets/v1"}, [])
D.orinassets = orinassets
D._clock = lambda: 0                                   # no start known in RACE anyway

posted = []


def fake_post(url, body, timeout=25):
    posted.append(url.rsplit(":8200", 1)[-1])
    if url.endswith("/playbook/load"):
        return True, {"loaded": True, "race_id": "r1", "copilot": {"loaded": True, "race_id": "r1"}}
    return True, {"loaded": True}


D._post_json = fake_post

bundle = synthesis.sign_bundle({"race_id": "r1", "course_id": "c", "start_epoch": 100,
                                "variants": [{"id": "a"}], "plays": [],
                                "safety": {"ok": True, "problems": [], "overrides": []}})   # route checks passed (#242)
pid = pbstore.save(bundle)
D.lock_in("r1", pid)

print("a whole, signed bundle:")
res = D.push("r1")
check("pushed, playbook on both boxes", res["ok"] and res["pushed_playbook"]
      and posted[-1] == "/playbook/load" and res["steps"][-1]["target"] == "orin-copilot")
check("readiness: no problem", D.readiness("r1")["lock_problem"] is None)

print("the locked bundle is gone (deleted after the page loaded):")
os.remove(pbstore._path(pid))
posted.clear()
res = D.push("r1")
check("refused before any post", posted == [] and res["ok"] is False and res["refused"] is True
      and res["steps"] == [])
check("says why, plainly", res["detail"] == "Active playbook is missing. Activate one again. Not sent.")
check("readiness says so before Push", D.readiness("r1")["lock_problem"] == D.PB_MISSING)
check("nothing recorded as a push (nothing reached the boat)", (D.last_push()["last"] or {}).get("ok"))

print("the locked bundle is half-written (a crash mid-freeze):")
with open(pbstore._path(pid), "w") as f:
    f.write('{"race_id": "r1", "varia')
posted.clear()
res = D.push("r1")
check("refused before any post", posted == [] and res["refused"]
      and res["detail"] == D.PB_MISSING + D.NOT_SENT)

print("the locked bundle fails its signature (edited after the freeze):")
with open(pbstore._path(pid), "w") as f:   # saves never overwrite (#249): edit the file itself
    json.dump(dict(bundle, variants=[{"id": "a"}, {"id": "b"}]), f)
posted.clear()
res = D.push("r1")
check("refused before any post", posted == [] and res["refused"]
      and res["detail"] == D.PB_BROKEN + D.NOT_SENT)
check("readiness says so", D.readiness("r1")["lock_problem"] == D.PB_BROKEN)

print("a damaged signature field (not a dict):")
with open(pbstore._path(pid), "w") as f:
    json.dump(dict(bundle, signature="broken"), f)
posted.clear()
try:
    res = D.push("r1")
except Exception as exc:                 # noqa: BLE001  (was AttributeError → HTTP 500)
    res = {"error": repr(exc)}
check("refused before any post, not a crash", posted == [] and res.get("refused")
      and res.get("detail") == D.PB_BROKEN + D.NOT_SENT)
check("verify_bundle says no", synthesis.verify_bundle(dict(bundle, signature="broken")) is False)
check("the Deploy tab still loads, the bundle listed as unsigned",
      D.readiness("r1")["lock_problem"] == D.PB_BROKEN
      and [b["signed"] for b in pbstore.list_bundles()] == [False])

print("nothing locked:")
labstate.set("deploy:r1", {})
posted.clear()
res = D.push("r1")
check("pushed without a playbook, as before", res["ok"] and not res["pushed_playbook"]
      and "/playbook/load" not in posted and posted[0] == "/course/load")
check("readiness: no problem", D.readiness("r1")["lock_problem"] is None)

print("short text:")
for s in (D.PB_MISSING, D.PB_BROKEN):
    check(f"{s!r} ≤ 8 words", len(s.split()) <= 8)

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
