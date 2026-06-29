"""Crew-facing roundings summary: the optimize result + briefing must state which side to leave each
mark — nav marks AND islands (the route already ENFORCES island sides via 2f; this TELLS the crew)."""
from app import store, optimizer as OPT
from shared import race_def

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

# 1) marks_with_side: ordered, includes islands, excludes 'none'
d = store.get_race("bayview-mackinac-2026")
rs = race_def.marks_with_side(d, "cove_island")
print("     roundings:", [(r["name"], r["type"], r["side"]) for r in rs])
by = {r["name"]: r["side"] for r in rs}
check("Duck Islands -> starboard (island included)", by.get("Duck Islands") == "starboard")
check("Bois Blanc Island -> port (island included)", by.get("Bois Blanc Island") == "port")
check("Cove Island Virtual Gate -> gate", by.get("Cove Island Virtual Gate") == "gate")
check("ordered as in the course (gate, Duck, Bois Blanc)",
      [r["name"] for r in rs] == ["Cove Island Virtual Gate", "Duck Islands", "Bois Blanc Island"])

# 2) a synthetic 'none'-only course yields no roundings
syn = {"courses": [{"id": "x", "start": {"lat": 44, "lon": -82},
       "marks": [{"name": "Free Mark", "type": "buoy", "lat": 44.1, "lon": -82, "rounding": "none"}],
       "finish": {"points": [{"lat": 44.2, "lon": -82}]}}]}
check("a 'none'-only course has no required roundings", race_def.marks_with_side(syn, "x") == [])

# 3) the deterministic briefing names the roundings (force fallback by clearing the API key)
saved = OPT.API_KEY
OPT.API_KEY = None
try:
    result = {
        "available": True, "total_hours": 42.5, "total_sailed_nm": 306.0, "total_tacks": 9,
        "route_confidence": 0.39, "min_confidence": 0.3, "wind_coverage": 1.0, "degraded": False,
        "warnings": [], "windfield": {"models": [{"model": "gfs"}, {"model": "nam"}, {"model": "hrrr"}]},
        "roundings": rs,
        "legs": [{"to": "Finish", "leg_minutes": 2550, "point_of_sail": "reach", "tacks": 5,
                  "wind": {"tws": 8, "twd": 250, "confidence": 0.4}}],
        "skipped_marks": [],
    }
    txt = OPT.briefing(result, "2026 Bayview Mackinac Race")
finally:
    OPT.API_KEY = saved
print("     briefing roundings line:", next((l for l in txt.splitlines() if l.startswith("Roundings")), "(none)"))
check("briefing states leave Duck Islands to starboard", "leave Duck Islands to starboard" in txt)
check("briefing states leave Bois Blanc Island to port", "leave Bois Blanc Island to port" in txt)
check("briefing notes the gate", "Cove Island Virtual Gate (gate" in txt)

print("RESULT:", "PASS" if ok else "FAIL")
