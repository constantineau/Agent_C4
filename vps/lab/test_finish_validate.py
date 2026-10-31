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

d = copy.deepcopy(base); d["courses"][0]["finish"]["coords_source"] = "needs_review"
_, warns = race_def.validate(d)
check("finish source needs_review -> warning", any("finish coords need review" in w for w in warns))

# malformed finish data is reported, never raises (a raise here 500s the save and the library list)
for label, fin in [("string lat", {"points": [{"lat": "45.8", "lon": -84.6}]}),
                   ("non-dict point", {"points": [42]}),
                   ("points not a list", {"points": "nope"}),
                   ("points an empty object", {"points": {}}),
                   ("points an object", {"points": {"a": 1}}),
                   ("finish not a dict", "line"),
                   ("finish an empty list", []),
                   ("finish false", False)]:
    d = copy.deepcopy(base); d["courses"][0]["finish"] = fin
    try:
        errs, _ = race_def.validate(d)
        check(f"malformed finish ({label}) -> error, no raise", any("finish" in e for e in errs))
    except Exception as e:
        check(f"malformed finish ({label}) raised {type(e).__name__}", False)

# a half-filled finish point (lat but no lon) must not crash the geometry consumers
d = copy.deepcopy(base); d["courses"][0]["finish"]["points"][1]["lon"] = None
cid = d["courses"][0]["id"]
try:
    marks, _, _ = race_def.course_to_marks(d, cid)
    fin = [m for m in marks if m[1] == "Finish"]
    check("course_to_marks: half-filled finish is reported as skipped",
          "Finish (incomplete coords)" in race_def.course_to_marks(d, cid)[1])
    check("course_to_marks: half-filled 2nd point -> finish at the complete point",
          len(fin) == 1 and fin[0][2:] == (45.8372333, -84.6168667))
except Exception as e:
    check(f"course_to_marks raised {type(e).__name__}", False)

d = copy.deepcopy(base)
for p_ in d["courses"][0]["finish"]["points"]:
    p_["lon"] = None
marks, skipped, _ = race_def.course_to_marks(d, cid)
check("both finish lons cleared -> no Finish mark, reported as skipped",
      not any(m[1] == "Finish" for m in marks) and "Finish (incomplete coords)" in skipped)
check("bundled course: nothing skipped for the finish",
      "Finish (incomplete coords)" not in race_def.course_to_marks(base, cid)[1])

print("ALL OK" if ok else "FAILURES")
raise SystemExit(0 if ok else 1)
