"""Engine GET /playbook — the few fields the Lab's Deploy tab reads back to show what the boat
holds (#82). Pure: feeds bundles straight to `deviation.playbook_summary`.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_playbook_summary.py
"""
from app import deviation

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


check("no bundle -> not loaded", deviation.playbook_summary({}) == {"loaded": False})
check("None -> not loaded", deviation.playbook_summary(None) == {"loaded": False})
check("bundle without variants -> not loaded",
      deviation.playbook_summary({"race_id": "x", "variants": []}) == {"loaded": False})

B = {"race_id": "Bayview-Mackinac 2026", "start_epoch": 1784386800.0, "generated_at": 1784000000,
     "headline": "Go left", "recommended": "left",
     "variants": [{"id": "left", "route": {"path": [{"lat": 1, "lon": 2}] * 500}}, {"id": "right"}],
     "plays": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}],
     "signature": {"value": "0123456789abcdef" + "f" * 48, "algo": "sha256"}}
s = deviation.playbook_summary(B)
check("loaded with race + counts", s["loaded"] and s["race_id"] == B["race_id"]
      and s["variants"] == 2 and s["plays"] == 3 and s["recommended"] == "left")
check("id matches the Lab's bundle id (<race>__<start>)", s["id"] == "bayview-mackinac2026__1784386800")
check("signature is the 16-char prefix the Lab's lock-in records",
      s["signed"] and s["signature"] == "0123456789abcdef")
check("no route geometry in the answer", "variants" in s and all(not isinstance(v, (list, dict))
                                                                for v in s.values()))
u = deviation.playbook_summary({"race_id": "r", "variants": [{"id": "a"}]})
check("unsigned bundle says so", u["signed"] is False and u["signature"] == "" and u["id"] == "r__0")

print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
