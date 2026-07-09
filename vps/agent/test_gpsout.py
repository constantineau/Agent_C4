"""GPS-OUT — unit test: variant selection from the bundle, downsampling (endpoints kept),
re-route source, and honest failure notes. Stubs the bundle + reoptimize + the n2kout POST.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_gpsout.py
"""
from app import gpsout

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


PATH = [{"lat": 43.0 + i * 0.02, "lon": -82.4, "t": 1783000000 + i * 600} for i in range(43)]
BUNDLE = {"race_id": "u", "recommended": "left",
          "variants": [{"id": "left", "route": {"path": PATH}},
                       {"id": "middle", "route": {"path": PATH[:2]}},
                       {"id": "bare", "route": {"path": []}}]}
sent = {}


def fake_post(path, payload=None):
    sent["path"], sent["payload"] = path, payload
    return {"broadcasting": True, "route": (payload or {}).get("name"),
            "n_waypoints": len((payload or {}).get("waypoints") or [])}


gpsout._post = fake_post
gpsout.deviation._load_playbook = lambda: BUNDLE

print("downsample:")
ds = gpsout._downsample(PATH, 24)
check("43 → ≤24 points", len(ds) <= 24)
check("endpoints kept", ds[0] is PATH[0] and ds[-1] is PATH[-1])
check("short paths untouched", gpsout._downsample(PATH[:5], 24) == PATH[:5])

print("show (playbook):")
r = gpsout.show()
check("recommended variant broadcast", r["shown"] and "left" in (r["route"] or ""))
check("downsampled_from reports the original size", r["downsampled_from"] == 43)
check("waypoints named C4-01…", sent["payload"]["waypoints"][0]["name"] == "C4-01"
      and sent["payload"]["waypoints"][0]["t"] == 1783000000)
r = gpsout.show(variant="middle")
check("named variant honored", r["shown"] and "middle" in (r["route"] or ""))
r = gpsout.show(variant="bare")
check("pathless variant → honest note", not r["shown"] and "no route track" in r["note"])
r = gpsout.show(variant="nope")
check("unknown variant → honest note", not r["shown"] and "not in the bundle" in r["note"])
gpsout.deviation._load_playbook = lambda: None
r = gpsout.show()
check("no playbook → honest note", not r["shown"] and "no playbook" in r["note"])

print("show (reoptimize):")
gpsout.reoptimize.get_reoptimize = lambda route=None: {"available": True, "path": PATH[:10]}
r = gpsout.show(source="reoptimize")
check("re-route broadcast, labeled off-book", r["shown"] and "off-book" in (r["label"] or ""))
gpsout.reoptimize.get_reoptimize = lambda route=None: {"available": False, "note": "cold"}
r = gpsout.show(source="reoptimize")
check("unavailable re-route → its note", not r["shown"] and r["note"] == "cold")

print("broadcaster down:")
gpsout._post = lambda p, payload=None: {"available": False, "error": "n2kout unreachable: x"}
gpsout.deviation._load_playbook = lambda: BUNDLE
r = gpsout.show()
check("unreachable broadcaster → honest note", not r["shown"] and "unreachable" in r["note"])

print("PASS" if ok else "FAIL")
import sys
sys.exit(0 if ok else 1)
