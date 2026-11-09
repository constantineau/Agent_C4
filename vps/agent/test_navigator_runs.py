"""Navigator runs, mark advance and the layline/rounding calls (#41, #45.1, #64-adjacent, #183-2/-3/-4,
decided in #246).

Pins, on the REAL engine endpoints over a temp onboard DB (the kv that holds course progress):
  * #41.1 tack names: starboard tack sails TWD - TWA (wind from 000, beat 42 -> starboard 318);
  * #41.2 a mark advance needs two DISTINCT fixes: one fix read five times (one /strategy poll) is
    one fix; bad, good, bad never latches; until confirmed the navigator keeps the old mark, so the
    chartplotter (gpsout reads next_mark.index) never steers to a mark seen on one fix (#246);
  * #41.3 a new run: a course load and the crew's Start tap both put the next mark back to the
    first one (a second race on the same course used to start at the finish);
  * #246 Undo last mark goes back one mark change (an advance, a pick or a Start) and HOLDS there
    until the boat is seen short of the mark; the crew can pick the right repeated mark;
  * #45.1 an old position is no position, old wind is no wind;
  * #183-2 rounding prep knows tacks and gybes; #183-3 the layline call knows the current tack;
  * #183-4 a practice course with no wind direction is refused, not laid due north.

Run in-container: PYTHONPATH=vps/agent:.:pi/archiver python3 vps/agent/test_navigator_runs.py
"""
import os
import sys
import tempfile
import time

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "engine"))

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")

from fastapi.testclient import TestClient  # noqa: E402

import engine_app  # noqa: E402
from app import datasource, gpsout  # noqa: E402
from app import navigator as NAV  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


client = TestClient(engine_app.app)

# The boat, driven by the test: position + wind + the fix's time.
B = {"lat": 43.99, "lon": -82.0, "twd": 0.0, "tws": 12.0, "heading": None, "cog": None, "sog": None,
     "t": time.time()}


def latest():
    return {"lat": B["lat"], "lon": B["lon"], "twd": B["twd"], "tws": B["tws"], "heading": B["heading"],
            "cog": B["cog"], "sog": B["sog"], "fix_t": B["t"], "ages": {}}


NAV._latest = latest
NAV._best_angles = lambda tws: (42.0, 150.0)
NAV._smoothed_wind = lambda minutes=None: (None, None)


def fix(lat, lon=-82.0, reads=1):
    """A NEW fix at (lat, lon), read `reads` times (one poll reads the navigator several times)."""
    B["lat"], B["lon"], B["t"] = lat, lon, B["t"] + 1.0
    nav = None
    for _ in range(reads):
        nav = client.get("/navigator").json()
    return nav


def idx(nav):
    return nav["next_mark"]["index"]


# A windward-leeward course, two laps: Leeward/Windward repeat (marks ~0.5 nm apart, N-S).
L, W = 44.0, 44.0083
DEF = {"race_id": "wl", "name": "WL",
       "courses": [{"id": "c", "start": {"lat": L, "lon": -82.0},
                    "marks": [{"name": "Windward", "type": "buoy", "lat": W, "lon": -82.0},
                              {"name": "Leeward", "type": "buoy", "lat": L, "lon": -82.0},
                              {"name": "Windward", "type": "buoy", "lat": W, "lon": -82.0}],
                    "finish": {"points": [{"lat": L, "lon": -82.0}]}}]}
r = client.post("/course/load", json={"definition": DEF, "course_id": "c"}).json()
check("the course loads", r.get("loaded") and r.get("marks") == 5)
marks = NAV._marks(NAV.active_route())
print("course:", [m["name"] for m in marks])

# ---------------------------------------------------------------- #41.1 tack names
print("#41.1 tack names:")
nav = fix(43.99)
lay = (nav.get("leg") or {}).get("laylines") or {}
check(f"wind 000, beat 42: starboard course 318 (got {lay.get('starboard_course')})",
      abs((lay.get("starboard_course") or 0) - 318.0) < 0.5)
check(f"... and port course 042 (got {lay.get('port_course')})", abs((lay.get("port_course") or 0) - 42.0) < 0.5)

# ---------------------------------------------------------------- #41.2 distinct fixes
print("#41.2 mark advance counts distinct fixes:")
check("pre-start, south of the line: Start is next", idx(fix(43.99)) == 0)
nav = fix(44.002, reads=5)                 # past the start plane, ONE fix read five times
check("one fix read five times does not advance (still the start)", idx(nav) == 0)
check("... and nothing is stored", (datasource.active().get_nav_progress(NAV.active_route()) or {}).get("i", 0) == 0)
gpsout._NEXT_MARK["at"] = None
check("... so the chartplotter is not told either", gpsout.next_mark_index() == 0)
idx(fix(43.99))                            # a good fix back short of the line: clears the count
check("bad, good, bad does not latch", idx(fix(44.002)) == 0)
check("two distinct fixes past the line advance to Windward", idx(fix(44.003)) == 1)
gpsout._NEXT_MARK["at"] = None
check("... and the chartplotter follows", gpsout.next_mark_index() == 1)
check("geometry back short of the line keeps the stored mark", idx(fix(43.99)) == 1)
nav = fix(44.004)
check("progress says Undo is possible", (nav.get("progress") or {}).get("can_undo") is True)

# ---------------------------------------------------------------- #246 Undo + pick
print("#246 undo and pick a mark:")
fix(44.0090); nav = fix(44.0092)           # past Windward on two fixes
check("rounding Windward advances to Leeward (index 2)", idx(nav) == 2)
r = client.post("/navigator/undo").json()
check("Undo last mark -> back to Windward (index 1)", r.get("ok") and r.get("index") == 1
      and r.get("next_mark") == "Windward")
check("held: still beyond Windward, geometry can't re-advance", idx(fix(44.0093)) == 1 and idx(fix(44.0094)) == 1)
check("navigator reports the hold", (fix(44.0095).get("progress") or {}).get("held") is True)
fix(44.006)                                # a fix short of Windward releases the hold
fix(44.0090); nav = fix(44.0092)
check("after the hold is released, a real rounding advances again", idx(nav) == 2)
r = client.post("/navigator/mark", json={"index": 3}).json()
check("pick the SECOND Windward (index 3) for lap 2", r.get("ok") and r.get("index") == 3)
check("navigator follows the pick", idx(fix(44.004)) == 3)
r = client.post("/navigator/mark", json={"index": 9}).json()
check("a mark that isn't on the course is refused", r.get("ok") is False and r.get("note"))
r = client.post("/navigator/undo").json()
check("Undo after a pick goes back to the mark before it", r.get("ok") and r.get("index") == 2)

# ---------------------------------------------------------------- #41.3 new run
print("#41.3 a new run:")
r = client.post("/navigator/start").json()
check("Start -> first mark again", r.get("ok") and r.get("index") == 0)
check("navigator says Start is next (south of the line)", idx(fix(43.99)) == 0)
r = client.post("/navigator/undo").json()
check("a stray Start is one Undo to put right", r.get("ok") and r.get("index") == 2)
# a stray Start mid-race, then the boat sails on beyond the start: one Undo still puts it right
client.post("/navigator/mark", json={"index": 3})
idx(fix(44.004)); idx(fix(44.0041))
client.post("/navigator/start")
fix(44.0042); fix(44.0043); nav = fix(44.0044)
check("after a stray Start the start is held while the boat is beyond the line", idx(nav) == 0)
r = client.post("/navigator/undo").json()
check("... and one Undo goes back to the mark it was sailing to", r.get("ok") and r.get("index") == 3)
# a progress write that fails is reported, not claimed
_ds = datasource.active()
_real_save = _ds.save_nav_progress


def _boom(route, blob):
    raise OSError("disk full")


_ds.save_nav_progress = _boom
r = client.post("/navigator/mark", json={"index": 1}).json()
check(f"a pick that can't be saved says so ({r.get('note')})", r.get("ok") is False and r.get("note"))
r = client.post("/course/load", json={"definition": DEF, "course_id": "c"}).json()
check("a course load whose run can't be reset is not reported loaded", r.get("loaded") is False)
_ds.save_nav_progress = _real_save
check("the stored mark is unchanged", idx(fix(44.004)) == 3)
# race 1 done: sail to the finish
client.post("/navigator/mark", json={"index": 4})
check("at the finish", idx(fix(43.999)) == 4)
r = client.post("/course/load", json={"definition": DEF, "course_id": "c"}).json()
check("reloading the SAME course is a new run: Start is next, not the finish",
      r.get("loaded") and idx(fix(43.99)) == 0)
check("... with nothing to undo (a new course)", client.post("/navigator/undo").json().get("ok") is False)

# ---------------------------------------------------------------- #183-3 layline call
print("#183-3 layline call knows the current tack:")
# boat SW of Windward on the starboard layline: bearing to the mark = 042 (port course)... use the
# port layline: from the boat, Windward bears 042 when it lies on the port-tack course.
client.post("/navigator/mark", json={"index": 1})
import math  # noqa: E402
d = 0.3 / 60.0
blat = W - d * math.cos(math.radians(42.0))
blon = -82.0 - d * math.sin(math.radians(42.0)) / math.cos(math.radians(W))
B["heading"] = 42.0                       # sailing the port layline already
nav = fix(blat, blon)
check(f"on the port layline, heading 042: laying the mark ({nav.get('layline_call')})",
      (nav.get("layline_call") or "").startswith("On the port layline") and "Laying" in nav["layline_call"])
B["heading"] = 318.0                      # on starboard, crossing the port layline
nav = fix(blat, blon)
check(f"on the port layline, on starboard: tack to lay ({nav.get('layline_call')})",
      "Tack to lay" in (nav.get("layline_call") or ""))
B["heading"] = None

# ---------------------------------------------------------------- #183-2 rounding prep
print("#183-2 rounding prep knows tacks and gybes:")


def mk(seq, name, lat, lon):
    return {"seq": seq, "name": name, "lat": lat, "lon": lon}


A0 = mk(1, "Here", 44.0, -82.0)
C0 = mk(3, "After", 44.0 + 0.05, -82.0 + 0.05 / math.cos(math.radians(44.0)))   # 045 from the mark
r_ = NAV._next_rounding([A0, A0 | {"seq": 2, "name": "Mark"}, C0], A0 | {"seq": 2, "name": "Mark"},
                        0.0, 42.0, 150.0, NAV._wrap180(315.0 - 0.0))
check(f"wind N, in on 315, out on 045: tack (got {r_ and r_['maneuver']})", r_ and r_["maneuver"] == "tack")
C1 = mk(3, "After", 44.0 - 0.05, -82.0 + 0.05 / math.cos(math.radians(44.0)))   # 135 from the mark
r_ = NAV._next_rounding([A0, A0 | {"seq": 2, "name": "Mark"}, C1], A0 | {"seq": 2, "name": "Mark"},
                        0.0, 42.0, 150.0, NAV._wrap180(225.0 - 0.0))
check(f"wind N, in on 225, out on 135: gybe (got {r_ and r_['maneuver']})", r_ and r_["maneuver"] == "gybe")
C2 = mk(3, "After", 44.0 - 0.05, -82.0 - 0.02 / math.cos(math.radians(44.0)))   # ~202 from the mark
r_ = NAV._next_rounding([A0, A0 | {"seq": 2, "name": "Mark"}, C2], A0 | {"seq": 2, "name": "Mark"},
                        0.0, 42.0, 150.0, NAV._wrap180(318.0 - 0.0))
check(f"windward mark to port: stbd beat in, stbd run out = bear away (got {r_ and r_['maneuver']})",
      r_ and r_["maneuver"] == "bear away")

# ---------------------------------------------------------------- #45.1 old readings
print("#45.1 old readings are not now:")
_saved_latest = NAV._latest
NAV._latest = lambda: dict(latest(), ages={"lat": 600.0, "lon": 600.0, "twd": 600.0, "tws": 1.0})
nav = client.get("/navigator").json()
check(f"a 10 min old fix is no position ({nav.get('note')})", nav.get("available") is False
      and "10 min" in (nav.get("note") or ""))
NAV._latest = lambda: dict(latest(), ages={"lat": 1.0, "lon": 1.0, "twd": 600.0, "tws": 600.0})
nav = client.get("/navigator").json()
check("old wind is no wind (no laylines, no wind shown)", nav.get("available")
      and nav["wind"]["twd"] is None and nav.get("layline_call") is None)
NAV._latest = _saved_latest

# ---------------------------------------------------------------- #183-4 practice with no wind
print("#183-4 practice course with no wind:")
B["twd"] = None
r = client.post("/course/practice").json()
check(f"refused, not laid due north ({r.get('note')})", r.get("available") is False
      and "wind" in (r.get("note") or "").lower())
check("... and the course aboard is unchanged", NAV.active_route() == "wl")
B["twd"] = 270.0
r = client.post("/course/practice").json()
check("with wind it lays the course", r.get("available") is True and r.get("twd") == 270.0)

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
