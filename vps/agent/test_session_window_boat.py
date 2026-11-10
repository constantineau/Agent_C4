"""#320 (Codex on #321): one derived race window is one boat's.

Two sessions on one kit, sr33 then bravo, 20 min apart, same race id, the boat under way the whole
time. Before: the two markers stitched into one window and each debrief scored both boats' sailing
against its own polar. Now: only the same boat's markers stitch, and a window stops at the other
boat's session. Two markers of the SAME boat still stitch (a mis-tap and its correction).
"""
from app import main

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T = 1_790_000_000.0
A = {"id": 1, "race_id": "r", "kit": "sr33", "boat_id": "sr33", "start_ts": T, "end_ts": T + 3600}
B = {"id": 2, "race_id": "r", "kit": "sr33", "boat_id": "bravo", "start_ts": T + 4800,
     "end_ts": T + 8400}
motion = [(T - 600 + 30 * i, 6.0, 20.0) for i in range(int((9600) / 30))]   # under way throughout

wa = main._window_for(A, [A, B], motion)
wb = main._window_for(B, [A, B], motion)
check("sr33's window stops where bravo's session starts", wa["end_ts"] <= B["start_ts"])
check("bravo's window starts where sr33's session ends", wb["start_ts"] >= A["end_ts"])
check("…said in the provenance", any("another boat" in p for p in wa["provenance"]))
check("the windows don't overlap", wa["end_ts"] <= wb["start_ts"])
A2 = dict(A, id=3, start_ts=T + 3900, end_ts=T + 4500)          # same boat, 5 min later
w2 = main._window_for(A, [A, A2], motion)
check("the same boat's two markers still stitch", w2["end_ts"] >= A2["end_ts"])
OLD = {"id": 4, "race_id": "r", "kit": "sr33", "start_ts": T, "end_ts": T + 3600}
check("a marker from before #320 is the kit's boat", main._sailed(OLD) == main._sailed(A))

# the Lab split them: both tagged sr33 on the iPad, the second edited to bravo in the Lab
B2 = dict(B, boat_id="sr33", sailed_override="bravo")
check("a Lab edit splits two same-tag markers into two boats' windows",
      main._window_for(A, [A, B2], motion)["end_ts"] <= B2["start_ts"]
      and main._window_for(B2, [A, B2], motion)["start_ts"] >= A["end_ts"])

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
