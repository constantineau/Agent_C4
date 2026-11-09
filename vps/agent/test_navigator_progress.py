"""Navigator course progression — the mark sequencer.

Regression test for the Bayview Mackinac 2026 failure: mark advance was a stateless proximity
test (`first mark you are not within ROUND_NM of`), so on a distance race the Start remained
`next_mark` for the whole race, `eta_min` was null throughout, and `next_mark.index` stuck at 0
— which silently disabled matcher leg gating, tactics leverage, the onboard re-route destination
and up-course buoy selection.

Covers: the distance-race case that broke, the buoy-race case that must NOT change, the plane
geometry itself, the ratchet, and course-change invalidation.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_navigator_progress.py
"""
from app import navigator as NAV

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def mk(seq, name, lat, lon):
    return {"seq": seq, "name": name, "lat": lat, "lon": lon}


# The real Jul 18 course, straight out of the boat's engine.db.
MACK = [mk(1, "Start", 43.06625, -82.4175),
        mk(2, "Cove Island Virtual Gate", 45.33566665, -81.83858335),
        mk(3, "Finish", 45.8416583, -84.61764170000001)]

# --- the regression: a distance race ----------------------------------------
# Positions replayed from the race archive. Before the fix every one of these returned index 0.
print("distance race (Bayview Mackinac 2026, real positions):")
check("pre-start, south of the line -> Start is next",
      NAV._next_index(MACK, 43.0600, -82.4200) == 0)
check("17:10Z, 1.4 nm north of the line -> Cove Island is next",
      NAV._next_index(MACK, 43.0894, -82.4166) == 1)
check("19:30Z, 16 nm up the course -> still Cove Island",
      NAV._next_index(MACK, 43.33858, -82.37847) == 1)
check("just short of Cove Island -> still Cove Island",
      NAV._next_index(MACK, 45.30, -81.85) == 1)
check("past Cove Island toward the finish -> Finish is next",
      NAV._next_index(MACK, 45.40, -81.95) == 2)
check("at the finish -> stays on Finish (never runs off the end)",
      NAV._next_index(MACK, 45.8416583, -84.6176417) == 2)

# The specific symptom the crew reported: a mark astern can never be the target.
print("the reported symptom:")
idx = NAV._next_index(MACK, 43.33858, -82.37847)
brg = NAV._bearing(43.33858, -82.37847, MACK[idx]["lat"], MACK[idx]["lon"])
check(f"next mark is ahead, not astern (bearing {brg:.0f}deg, was 186deg)", brg < 90 or brg > 270)

# --- must not regress: windward-leeward buoy racing --------------------------
# 0.5 nm beat, marks 0.5 nm apart N-S. Close-aboard rounding still advances.
WL = [mk(1, "Leeward", 43.0000, -82.4000),
      mk(2, "Windward", 43.0083, -82.4000),
      mk(3, "Finish", 43.0000, -82.4000)]
print("windward-leeward (close-aboard rounding preserved):")
check("below the leeward mark -> Leeward is next",
      NAV._next_index(WL, 42.9990, -82.4000) == 0)
check("within ROUND_NM of the leeward mark -> advances to Windward",
      NAV._next_index(WL, 43.00005, -82.4000) == 1)
check("mid-beat, short of the windward mark -> still Windward",
      NAV._next_index(WL, 43.0040, -82.4000) == 1)
check("beyond the windward mark -> advances",
      NAV._next_index(WL, 43.0100, -82.4000) == 2)

# --- plane geometry ----------------------------------------------------------
print("plane geometry:")
# Axis due north from the start; a boat well off to the side is still 'past' if it is north.
check("passed is about along-track distance, not proximity",
      NAV._passed(MACK, 0, 43.20, -82.90) is True)
# This used to say "sitting on the mark counts as a rounding": the close-aboard rule applied to
# the start too, so a boat milling within ~110 m of the start mark before the gun counted as
# started and jumped to the next mark (#246 follow-up). The start is now left only by crossing it.
check("sitting on the START mark is not passed (the start is crossed, not rounded)",
      NAV._passed(MACK, 0, 43.06625, -82.4175) is False)
check("close aboard still counts as a rounding for a mark that is not the start",
      NAV._passed(MACK, 1, 45.33566665, -81.8386) is True)
check("off to the side and short of the plane is not passed",
      NAV._passed(MACK, 0, 43.0500, -82.3000) is False)
check("_beyond_nm is positive past the plane",
      NAV._beyond_nm(43.20, -82.4175, MACK[0], 10.2) > 0)
check("_beyond_nm is negative short of the plane",
      NAV._beyond_nm(43.00, -82.4175, MACK[0], 10.2) < 0)
check("tacking well off the rhumb line does not un-pass a mark",
      NAV._next_index(MACK, 43.33858, -83.20) == 1)

# --- the start is crossed, not approached (#246 follow-up) ---------------------
# Milling inside the old 110 m close-aboard radius, behind the line, before the gun: never past
# the start. Crossing the line (the plane through the start, normal to the leg out) is.
print("start line:")
S = MACK[0]
behind = [(S["lat"] - 0.0003, S["lon"]),            # ~33 m behind the start
          (S["lat"] - 0.0008, S["lon"] + 0.0005),   # ~100 m, behind and to one side
          (S["lat"] - 0.0001, S["lon"] - 0.0010),   # ~80 m off along the line, just behind
          (S["lat"], S["lon"])]                     # right on the start mark
for la, lo in behind:
    d_m = NAV._hav_nm(la, lo, S["lat"], S["lon"]) * 1852
    check(f"milling {d_m:.0f} m from the start, not across: Start is still next",
          NAV._next_index(MACK, la, lo) == 0)
check("across the line (~170 m beyond it) -> Cove Island is next",
      NAV._next_index(MACK, S["lat"] + 0.0015, S["lon"]) == 1)
check("a course with no Start: close aboard its first mark still rounds it",
      NAV._next_index(MACK[1:], 45.33566665, -81.8386) == 1)

# --- the ratchet -------------------------------------------------------------
print("ratchet:")
check("floor holds the index when geometry alone would drop back",
      NAV._next_index(MACK, 43.0600, -82.4200, floor=1) == 1)
check("floor never drags the index backwards past reality",
      NAV._next_index(MACK, 45.40, -81.95, floor=0) == 2)
check("floor is clamped to the course length",
      NAV._next_index(MACK, 43.0600, -82.4200, floor=99) == 2)

# --- course-change invalidation ----------------------------------------------
print("course fingerprint:")
fp = NAV._course_fp(MACK)
check("fingerprint is stable across calls", fp == NAV._course_fp(MACK))
check("fingerprint changes when a mark moves",
      fp != NAV._course_fp([MACK[0], mk(2, "Cove Island Virtual Gate", 45.40, -81.83858335),
                            MACK[2]]))
check("fingerprint changes when the course is a different length",
      fp != NAV._course_fp(MACK[:2]))

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
