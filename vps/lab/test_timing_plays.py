"""#153: the "system early" and "system late" timing plays must never arm on the same evidence.

They used to share one predicate (unsigned TWD drift >= 15 deg for 30 min), so any sustained
drift armed both opposite plays at once. Until a phase signal can tell early from late, timing
plays carry no machine predicates (manual-only). The general guard: no two opposite-direction
scenarios of the same kind may resolve to the same non-empty predicates.
"""
from app import scenarios as SCEN

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


CTX = {"mean_tws": 12.0, "venue_stats": {}}

timing = [s for s in SCEN.EXTERNAL if s["kind"] == "time_shift"]
check("registry has early and late timing scenarios",
      any(s["params"]["hours"] > 0 for s in timing) and any(s["params"]["hours"] < 0 for s in timing))
for s in timing:
    preds = s["detect"](s["params"]["hours"], CTX)
    check(f"{s['id']} is manual-only (no predicates)", preds == [])

# Opposite-direction twins (same kind, param sign flipped) must not arm on identical evidence.
for a in SCEN.EXTERNAL:
    for b in SCEN.EXTERNAL:
        if a["id"] >= b["id"] or a["kind"] != b["kind"]:
            continue
        pa, pb = next(iter(a["params"].values())), next(iter(b["params"].values()))
        if a["kind"] == "tws_scale":
            pa, pb = pa - 1.0, pb - 1.0
        if pa * pb >= 0:
            continue
        da, db = a["detect"](next(iter(a["params"].values())), CTX), \
            b["detect"](next(iter(b["params"].values())), CTX)
        check(f"{a['id']} vs {b['id']}: opposite plays don't share predicates",
              not da or not db or da != db)

print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
