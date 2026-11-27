"""Course edits aboard (vps/agent/app/courserev.py, #406), on the REAL engine endpoints over a temp
onboard DB.

Pins:
  * every Deploy load is a course revision from the Lab; every edit aboard is a new, higher one
    from the boat, with a time; a new Deploy load starts the edits over;
  * a changed rounding side: the definition aboard changes, the navigator's marks don't, the
    course counts as a new one (old routes are for an old course);
  * a shortened finish: the navigator's marks end at the new finish and the run stays at the
    mark it was on; a finish behind the boat is refused;
  * a repeated mark: the edit names its place, so lap 2's mark is the one changed;
  * skip to a mark moves the navigator's next mark;
  * undo goes back one edit (a new revision, never an old number);
  * the re-route request names the revision and carries the Lab's course under the edits;
    the Orin's route is stamped with the revision it was asked on;
  * the Pi's GPS export still trusts the Lab's chart for an edit that covers no new water.
  * #415: with no GPS fix the saved run still refuses a finish behind the boat and lets an undo
    of a skip go back; a skip whose next mark can't be saved fails whole (no revision kept); a
    navigator read never sees the marks of an edit in flight (the course lock).

Run in-container: PYTHONPATH=vps/agent:.:pi/archiver python3 vps/agent/test_courserev.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pi", "engine"))

_tmp = tempfile.mkdtemp()
os.environ["ENGINE_DB"] = os.path.join(_tmp, "engine", "engine.db")

from fastapi.testclient import TestClient  # noqa: E402

import engine_app  # noqa: E402
from app import courserev, landchart, orinassets, orinroute  # noqa: E402
from app import navigator as NAV  # noqa: E402
from shared import chartid  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


client = TestClient(engine_app.app)
B = {"lat": 43.99, "lon": -82.0, "t": time.time()}
NAV._latest = lambda: {"lat": B["lat"], "lon": B["lon"], "twd": 0.0, "tws": 12.0, "heading": None,
                       "cog": None, "sog": None, "fix_t": B["t"], "ages": {}}
NAV._best_angles = lambda tws: (42.0, 150.0)
NAV._smoothed_wind = lambda minutes=None: (None, None)
engine_app.gpsout.clear_other_courses = lambda: {"cleared": True}

# two laps: Start → Windward → Leeward → Windward → Rock (island) → Finish
L, W = 44.0, 44.0083
DEF = {"race_id": "wl", "name": "WL", "zones": [],
       "courses": [{"id": "c", "start": {"lat": L, "lon": -82.0},
                    "marks": [{"name": "Windward", "type": "buoy", "lat": W, "lon": -82.0,
                               "rounding": "port"},
                              {"name": "Leeward", "type": "buoy", "lat": L, "lon": -82.0,
                               "rounding": "port"},
                              {"name": "Windward", "type": "buoy", "lat": W, "lon": -82.0,
                               "rounding": "port"},
                              {"name": "Rock", "type": "island", "lat": 44.03, "lon": -82.05,
                               "rounding": "starboard", "radius_nm": 0.2}],
                    "finish": {"points": [{"lat": 44.02, "lon": -82.0}]}}]}


def names():
    return [m["name"] for m in NAV._marks(NAV.active_route())]


def edit(**body):
    return client.post("/course/edit", json=body).json()


def rev():
    return client.get("/course/revision").json()


print("no course aboard:")
check("GET says so", rev() == {"available": False, "note": "No course aboard to change."})
check("an edit says so", edit(op="skip_to", pos=0, name="Windward")["note"]
      == "No course aboard to change.")
check("an unknown change is refused", edit(op="move", pos=0)["note"] == "Unknown course change.")

print("the Lab's deploy is a revision:")
r = client.post("/course/load", json={"definition": DEF, "course_id": "c"}).json()
check("the course loads", r.get("loaded") and names() == ["Start", "Windward", "Leeward",
                                                           "Windward", "Finish"])
v = rev()
check("revision 1, from the Lab, with a time", v["revision"]["number"] == 1
      and v["revision"]["origin"] == "lab" and abs(v["revision"]["at"] - time.time()) < 60)
check("the marks to pick, with their places and navigator indices",
      [(m["pos"], m["name"], m["index"]) for m in v["marks"]]
      == [(0, "Windward", 1), (1, "Leeward", 2), (2, "Windward", 3), (3, "Rock", None)])
check("the definition aboard carries the revision",
      orinroute.record_for("wl", NAV._marks("wl"))["definition"]["course_revision"]["number"] == 1)

print("a changed rounding side on a repeated mark:")
hash0 = orinroute.current_course_hash()
r = edit(op="rounding", pos=2, name="Windward", side="starboard", device="Nav iPad")
check("saved as revision 2", r["ok"] and r["revision"]["number"] == 2
      and r["revision"]["origin"] == "boat" and r["note"] == "Course rev 2 saved.")
rec = orinroute.record_for("wl", NAV._marks("wl"))
sides = [m["rounding"] for m in rec["definition"]["courses"][0]["marks"]]
check("only lap 2's Windward changed side", sides == ["port", "port", "starboard", "starboard"])
check("the Lab's course is kept under it, unchanged",
      rec["base_definition"]["courses"][0]["marks"][2]["rounding"] == "port")
check("who and when are kept", rec["edits"][0]["by"] == "Nav iPad" and rec["edits"][0]["number"] == 2)
check("the navigator's marks didn't change", names() == ["Start", "Windward", "Leeward",
                                                         "Windward", "Finish"])
check("the course counts as a new one (old routes are old-course)",
      orinroute.current_course_hash() not in (None, hash0))
check("a stale edit (another mark at that place) is refused, nothing saved",
      edit(op="rounding", pos=1, name="Windward", side="port")["note"]
      == "Course changed. Pick the mark again." and rev()["revision"]["number"] == 2)

print("the re-route request:")
SENT = []


def orin(method, path, body=None):
    SENT.append(body)
    return 202, {"ok": True, "job_id": "j1"}


code, _b = orinroute.run("iPad", call=orin)
req = SENT[-1]
check("names the revision (number, time, origin, the Lab's base)", code == 202
      and req["revision"]["number"] == 2 and req["revision"]["origin"] == "boat"
      and req["revision"]["base"] == 1)
check("the edited definition goes, with the Lab's course under it",
      req["definition"]["courses"][0]["marks"][2]["rounding"] == "starboard"
      and req["base_definition"]["courses"][0]["marks"][2]["rounding"] == "port")
orinroute._store({"schema": "c4.reroute/v1", "job_id": "j1", "available": True,
                  "finished_at": int(time.time())})
check("an Orin that doesn't name the revision: the route gets the one it was asked on",
      orinroute.stored()["course_revision"]["number"] == 2)

print("the GPS export's chart check:")
DEPTH = chartid.safety_depth_m(None)
CH = {"kind": landchart.CHART_KIND, "fingerprint": chartid.course_fingerprint(DEF, "c", DEPTH)}
orinassets._read = lambda: {"assets": {"race_id": "wl", "course_id": "c", "chart": CH, "boat": None}}
seen = []
landchart.LandMask = lambda chart: seen.append(chart) or type(
    "M", (), {"carve": lambda *a: None, "open_boat": lambda *a: None})()
landchart.for_course("wl", NAV._marks("wl"))
check("an edit that covers no new water: the Lab's chart still fits", seen == [CH])
seen.clear()
CH_OLD = dict(CH, fingerprint="0" * 16)
orinassets._read = lambda: {"assets": {"race_id": "wl", "course_id": "c", "chart": CH_OLD,
                                       "boat": None}}
check("a chart for another course still doesn't",
      landchart.for_course("wl", NAV._marks("wl")) is None and not seen)

print("skip to a mark:")
B["t"] += 1
r = edit(op="skip_to", pos=2, name="Windward")
check("revision 3, the navigator goes to lap 2's Windward", r["ok"] and r["revision"]["number"] == 3
      and r["note"] == "Course rev 3 saved. Next mark: Windward."
      and NAV.get_navigator()["next_mark"]["index"] == 3)

print("a shortened finish:")
check("a finish behind the boat is refused", edit(op="finish_at", pos=1, name="Leeward")["note"]
      == "That mark is behind you.")
r = edit(op="finish_at", pos=2, name="Windward")
check("revision 4", r["ok"] and r["revision"]["number"] == 4 and r["note"] == "Course rev 4 saved.")
check("the navigator's marks end at the new finish", names() == ["Start", "Windward", "Leeward",
                                                                 "Finish"])
check("…at lap 2's Windward", (NAV._marks("wl")[-1]["lat"], NAV._marks("wl")[-1]["lon"]) == (W, -82.0))
check("the run stays where it was (next mark: the new finish)",
      NAV.get_navigator()["next_mark"]["index"] == 3)
v = rev()
check("GET shows the shortened course and every edit", v["shortened"] is True
      and [e["op"] for e in v["edits"]] == ["rounding", "skip_to", "finish_at"])
check("the island after the finish is kept, with no side",
      v["marks"][-1]["name"] == "Rock" and v["marks"][-1]["rounding"] == "none")
check("the definition aboard still pairs with the navigator's marks",
      orinroute._definition_for("wl", NAV._marks("wl"))[0] is not None)

print("undo:")
r = edit(op="undo")
check("undo is a new revision (5), the finish back", r["ok"] and r["revision"]["number"] == 5
      and names() == ["Start", "Windward", "Leeward", "Windward", "Finish"])
check("…the run kept", NAV.get_navigator()["next_mark"]["index"] == 3)
hash_before = orinroute.current_course_hash()
r = edit(op="undo")
check("undo of the skip: the navigator back where it was before it (the start)",
      r["ok"] and r["note"] == "Course rev 6 saved. Next mark: Start."
      and NAV.get_navigator()["next_mark"]["index"] == 0)
check("…and a route made after the skip is old now (it started at the skipped-to mark)",
      orinroute.current_course_hash() != hash_before)
edit(op="undo")
v = rev()
check("every edit undone: the Lab's course again, still a boat revision (7)", v["edits"] == []
      and v["revision"] == {"number": 7, "at": v["revision"]["at"], "origin": "boat"})
check("nothing left to undo", edit(op="undo")["note"] == "No course change to undo.")
check("…the request carries no base when nothing is edited",
      orinroute.run("iPad", call=orin)[0] == 202 and "base_definition" not in SENT[-1])

print("a new Deploy starts over:")
edit(op="rounding", pos=0, name="Windward", side="starboard")
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
v = rev()
check("the next number, from the Lab, no edits", v["revision"]["number"] == 9
      and v["revision"]["origin"] == "lab" and v["edits"] == [])
check("a number the Lab already stated higher is kept", orinroute.next_revision(
    {"number": 40, "origin": "lab"}) == 40 and orinroute.next_revision() == 41)

print("undo of a skip after the boat moved on:")
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
edit(op="skip_to", pos=1, name="Leeward")
check("(skipped to Leeward)", NAV.get_navigator()["next_mark"]["index"] == 2)
NAV.pick_mark(3)                                   # the boat has gone on to lap 2's Windward
r = edit(op="undo")
check("undo says so and leaves the next mark", r["ok"] and r["note"].endswith(
    "Boat has moved on. Next mark not changed.") and NAV.get_navigator()["next_mark"]["index"] == 3)


class PowerCut(BaseException):
    """The Pi loses power: nothing after this line runs, no handler cleans up."""


def cut_at(name):
    real = getattr(orinroute, name)

    def boom(*a, **k):
        setattr(orinroute, name, real)
        raise PowerCut()
    setattr(orinroute, name, boom)


print("a power cut part way through an edit:")
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
n0 = rev()["revision"]["number"]
cut_at("write_record")                             # marks + load id written, record not
try:
    edit(op="finish_at", pos=2, name="Windward")
except PowerCut:
    pass
JOURNAL = os.path.join(_tmp, "engine", "course-edit-journal.json")
check("(the cut left a journal)", os.path.exists(JOURNAL))
v = rev()
check("next read: the edit is finished (the marks were already the new ones)",
      v.get("available") and v["shortened"] and v["revision"]["number"] == n0 + 1
      and names() == ["Start", "Windward", "Leeward", "Finish"] and not os.path.exists(JOURNAL))
check("…and the re-route still gets the zones and sides",
      orinroute._definition_for("wl", NAV._marks("wl"))[0] is not None)
cut_at("set_load_id")                              # a side change cut before its load id
try:
    edit(op="rounding", pos=0, name="Windward", side="starboard")
except PowerCut:
    pass
v = rev()
check("cut before anything changed: back to the course before the edit, still pairing",
      v.get("available") and v["revision"]["number"] == n0 + 1
      and v["marks"][0]["rounding"] == "port" and not os.path.exists(JOURNAL))

edit(op="undo")                                    # the shortened finish off again
cut_at("write_record")                             # a skip cut before the navigator moved
try:
    edit(op="skip_to", pos=2, name="Windward")
except PowerCut:
    pass
check("(the cut left the navigator where it was)", NAV.progress_snapshot("wl")["i"] != 3)
v = rev()
check("next read: the skip's revision and its next mark, together",
      v["edits"][-1]["op"] == "skip_to" and NAV.get_navigator()["next_mark"]["index"] == 3
      and not os.path.exists(JOURNAL))
edit(op="undo")
edit(op="undo")
NAV.pick_mark(2)
try:
    _sp = NAV._save_progress

    def _cut_once(*a, **k):
        NAV._save_progress = _sp
        raise PowerCut()
    NAV._save_progress = _cut_once
    edit(op="finish_at", pos=2, name="Windward")
except PowerCut:
    pass
check("(the cut came between the marks and the run)", names()[-1] == "Finish" and len(names()) == 4)
v = rev()
check("next read: the run is put back on the mark it was on", v["shortened"]
      and NAV.get_navigator()["next_mark"]["index"] == 2 and not os.path.exists(JOURNAL))

print("round 3: the next mark never moves wrongly")
NAV.pick_mark(3)
check("after a shortened course, a finish behind the boat is still refused",
      edit(op="finish_at", pos=1, name="Leeward")["note"] == "That mark is behind you.")
edit(op="undo")                                    # back to the full course
NAV.pick_mark(3)
NAV.pick_mark(2)                                   # Undo history now holds 3
snap0 = NAV.progress_snapshot("wl")
_wr = orinroute.write_record
orinroute.write_record = lambda rec: (_ for _ in ()).throw(OSError("disk full"))
r = edit(op="finish_at", pos=1, name="Leeward")
orinroute.write_record = _wr
check("a failed shortened save: said, the marks back, the run exactly as it was",
      r["ok"] is False and names() == ["Start", "Windward", "Leeward", "Windward", "Finish"]
      and NAV.progress_snapshot("wl") == snap0)
v = rev()                                          # (the journal, if the put-back couldn't finish)
check("…and the next read keeps the old course, run untouched", v.get("available")
      and not v["shortened"] and NAV.progress_snapshot("wl") == snap0 and not os.path.exists(JOURNAL))
cut_at("write_record")
try:
    edit(op="skip_to", pos=2, name="Windward")
except PowerCut:
    pass
check("(a skip cut off: the journal is there, the navigator not moved yet)",
      os.path.exists(JOURNAL) and NAV.progress_snapshot("wl")["i"] == 2)
nav = NAV.get_navigator()                          # the navigator is read first, not the course
check("the first navigator read recovers first: the skip's next mark, journal gone",
      nav["next_mark"]["index"] == 3 and not os.path.exists(JOURNAL))
edit(op="undo")
cut_at("write_record")
try:
    edit(op="skip_to", pos=2, name="Windward")
except PowerCut:
    pass
from fastapi.testclient import TestClient as _TC  # noqa: E402
with _TC(engine_app.app):                          # engine start runs the recovery
    check("engine start recovers too", not os.path.exists(JOURNAL)
          and NAV.progress_snapshot("wl")["i"] == 3)

print("the revision number can't be written:")
_nr = orinroute.next_revision
orinroute.next_revision = lambda stated=None: (_ for _ in ()).throw(OSError("disk full"))
resp = client.post("/course/edit", json={"op": "rounding", "pos": 0, "name": "Windward",
                                         "side": "starboard"})
orinroute.next_revision = _nr
check("a plain failure, not a 500", resp.status_code == 200
      and resp.json() == {"ok": False, "note": "Couldn't save the change. Try again."})
check("…nothing changed", rev()["marks"][0]["rounding"] == "port")

print("a failed save changes nothing:")
_orig = orinroute.write_record
orinroute.write_record = lambda rec: (_ for _ in ()).throw(OSError("disk full"))
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
before = rev()
r = edit(op="finish_at", pos=1, name="Leeward")
orinroute.write_record = _orig
after = rev()
check("said plainly", r == {"ok": False, "note": "Couldn't save the change. Try again."})
check("the old course and marks are still aboard, and still pair", after.get("available")
      and after["revision"] == before["revision"] and names() == ["Start", "Windward", "Leeward",
                                                                  "Windward", "Finish"])

print("#415: no GPS fix, the saved run still decides:")
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
NAV.pick_mark(3)                                   # the run is at lap 2's Windward
_lt = NAV._latest
NAV._latest = lambda: {"lat": None, "lon": None, "twd": 0.0, "tws": 12.0, "heading": None,
                       "cog": None, "sog": None, "fix_t": None, "ages": {}}
check("(no fix: the navigator has no next mark)", not NAV.get_navigator().get("available"))
check("a finish behind the saved run is refused", edit(op="finish_at", pos=1, name="Leeward")["note"]
      == "That mark is behind you." and not rev()["shortened"]
      and NAV.progress_snapshot("wl")["i"] == 3)
NAV.pick_mark(0)
r = edit(op="skip_to", pos=2, name="Windward")
check("(a skip with no fix moves the run)", r["ok"] and NAV.progress_snapshot("wl")["i"] == 3)
r = edit(op="undo")
check("undo of that skip, still no fix: back to the mark before it",
      r["ok"] and r["note"].endswith("Next mark: Start.") and NAV.progress_snapshot("wl")["i"] == 0)
NAV._latest = _lt

print("#415: a skip whose navigator save fails:")
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
NAV.pick_mark(1)
NAV.get_navigator()                                # (a read may release the pick's hold first)
before, snap0 = rev(), NAV.progress_snapshot("wl")
_pm = NAV.pick_mark
NAV.pick_mark = lambda index, route=None: {"ok": False, "save_failed": True, "route": route,
                                           "note": "Couldn't save. Try again."}
r = edit(op="skip_to", pos=2, name="Windward")
NAV.pick_mark = _pm
after = rev()
check("the edit fails, said plainly", r == {"ok": False, "note": "Couldn't save the change. Try again."})
check("…no new revision, no skip kept", after["revision"] == before["revision"]
      and after["edits"] == [] and not os.path.exists(JOURNAL))
check("…the run as it was", NAV.progress_snapshot("wl") == snap0)

print("#415: a failed skip whose run can't be put back either:")
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
NAV.pick_mark(1)
NAV.get_navigator()
before = rev()
_pm, _rp = NAV.pick_mark, NAV.restore_progress
NAV.pick_mark = lambda index, route=None: {"ok": False, "save_failed": True, "route": route,
                                           "note": "Couldn't save. Try again."}
NAV.restore_progress = lambda *a, **k: False
r = edit(op="skip_to", pos=2, name="Windward")
NAV.pick_mark, NAV.restore_progress = _pm, _rp
check("the edit fails, and the journal stays for the next read",
      r == {"ok": False, "note": "Couldn't save the change. Try again."} and os.path.exists(JOURNAL))
NAV.get_navigator()
check("…the next read puts it right: the old course, no skip",
      not os.path.exists(JOURNAL) and rev()["revision"] == before["revision"] and rev()["edits"] == [])

print("#415: a skip that moved the run, then failed, and the run won't go back at once:")
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
NAV.pick_mark(1)
NAV.get_navigator()
before, snap0 = rev(), NAV.progress_snapshot("wl")
_ee, _rp = orinroute.end_edit, NAV.restore_progress


def _ee_fail_once():
    orinroute.end_edit = _ee
    raise OSError("disk full")


def _rp_fail_forced(route, snap, force=False):
    NAV.restore_progress = _rp
    return False


orinroute.end_edit, NAV.restore_progress = _ee_fail_once, _rp_fail_forced
r = edit(op="skip_to", pos=2, name="Windward")      # the pick landed, then the edit failed
check("the edit fails, the journal stays", not r["ok"] and os.path.exists(JOURNAL))
NAV.get_navigator()
check("…the next read: the old course and the run exactly as it was",
      not os.path.exists(JOURNAL) and rev()["revision"] == before["revision"]
      and NAV.progress_snapshot("wl") == snap0)

print("#415: recovery after a cut, when the skip's next mark won't save:")
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
NAV.pick_mark(1)
NAV.get_navigator()
_pm = NAV.pick_mark
NAV.pick_mark = lambda index, route=None: (_ for _ in ()).throw(PowerCut())
try:
    edit(op="skip_to", pos=2, name="Windward")
except PowerCut:
    pass
want = json.load(open(JOURNAL))["nav_index"]           # the cut came as the pick was saving
NAV.pick_mark = lambda index, route=None: {"ok": False, "save_failed": True, "route": route,
                                           "note": "Couldn't save. Try again."}
NAV.get_navigator()
check("the store still failing: the journal stays", os.path.exists(JOURNAL))
NAV.pick_mark = _pm
nav = NAV.get_navigator()
check("…the store back: the skip's next mark lands and the journal goes",
      nav["next_mark"]["index"] == want and not os.path.exists(JOURNAL))

print("#415: a navigator read while an edit is in flight:")
import threading  # noqa: E402
client.post("/course/load", json={"definition": DEF, "course_id": "c"})
B["t"] += 1
checked, paused = threading.Event(), threading.Event()
seen_total, nav_thread = [], []
_rce = NAV._recover_course_edit


def _hooked_recover():
    _rce()
    if threading.current_thread() in nav_thread:
        checked.set()                              # passed the journal check: no edit yet
        paused.wait(1.0)                           # let an edit start now, if it can


def _nav_read():
    seen_total.append(NAV.get_navigator().get("marks_total"))


def _slow_failing_write(rec):
    orinroute.write_record = _wr                   # once: the put-back writes the old record
    paused.set()                                   # marks already replaced, record not written
    time.sleep(0.5)
    raise OSError("disk full")


def _edit_read():
    checked.wait(5)
    orinroute.write_record = _slow_failing_write
    try:
        seen_edit.append(courserev.edit({"op": "finish_at", "pos": 2, "name": "Windward"}))
    finally:
        orinroute.write_record = _wr


seen_edit = []
NAV._recover_course_edit = _hooked_recover
tn = threading.Thread(target=_nav_read)
te = threading.Thread(target=_edit_read)
nav_thread.append(tn)
tn.start()
te.start()
tn.join(10)
te.join(10)
NAV._recover_course_edit = _rce
check("(the edit failed and put the old marks back)", seen_edit and seen_edit[0]["ok"] is False
      and len(names()) == 5)
check("the navigator never read the marks of an edit in flight", seen_total == [5])
check("…and nothing left to recover", not os.path.exists(JOURNAL))

print("words:")
for w in (courserev.NO_COURSE, courserev.NOTHING, courserev.BEHIND):
    check(f"short: {w!r}", len(w.split()) <= 8)
print("course id survives:", json.dumps(rev().get("course_id")))

print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
