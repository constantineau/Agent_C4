"""Finish geometry is editable in the Course & Marks tab (#88), so validate() must check it like it
checks marks: out-of-range finish coords block, missing coords / no points / >2 points are flagged."""
import copy
from app import store
from shared import race_def

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

base = store.get_race("bayview-mackinac-2026")
errs, warns = race_def.validate(base)
check("bundled race: no finish errors", not any("finish" in e for e in errs))
check("bundled race: no finish-point warnings", not any("finish point" in w for w in warns))

d = copy.deepcopy(base); d["courses"][0]["finish"]["points"][1]["lat"] = 123.0
errs, _ = race_def.validate(d)
check("finish lat 123 -> error", any("finish point 2 coord out of range" in e for e in errs))

d = copy.deepcopy(base); d["courses"][0]["finish"]["points"][0]["lon"] = None
errs, warns = race_def.validate(d)
check("missing finish lon -> warning, not error",
      any("finish point 1 has no coordinates" in w for w in warns) and not any("finish" in e for e in errs))

d = copy.deepcopy(base); d["courses"][0]["finish"]["points"] = []
_, warns = race_def.validate(d)
check("empty finish -> warning", any("finish has no points" in w for w in warns))

d = copy.deepcopy(base); d["courses"][0]["finish"]["points"].append({"lat": 45.8, "lon": -84.6})
errs, _ = race_def.validate(d)
check("3 finish points -> error", any("finish has 3 points" in e for e in errs))

d = copy.deepcopy(base); d["courses"][0]["finish"]["points"] = d["courses"][0]["finish"]["points"][:1]
errs, warns = race_def.validate(d)
check("single-point finish mark is valid", not any("finish" in e for e in errs)
      and not any("finish" in w for w in warns))

print("ALL OK" if ok else "FAILURES")
raise SystemExit(0 if ok else 1)
