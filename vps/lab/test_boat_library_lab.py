"""#320 — the Lab half of "practice on any boat": the boat library, and sessions filed by boat.

  * GET /api/boats/library: every boat with its own polar block (sr33: its files; bravo: its
    cert), a profile with errors left out by name;
  * the Pi's ingest token opens that one read-only path and nothing else;
  * recordings name the boat each session was SAILED on (the marker's pick; an older marker → the
    kit's boat; an "Other boat" → its name, no profile), and show each session's debrief from
    that boat's archive, never the kit's or the active boat's;
  * an "Other boat" session can't be debriefed until it has a profile; the Lab edits its boat;
  * editing a session's boat takes its old-boat debriefs out of that boat's learning (kept,
    marked withdrawn), so a re-score of the old boat no longer counts them;
  * the debrief is run for the session's boat, not the active one.
"""
import json
import os
import shutil
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["INGEST_TOKEN"] = "pi-ingest-token-for-this-test"
os.environ.pop("BOAT_LIBRARY_TOKEN", None)
HERE = os.path.dirname(os.path.abspath(__file__))

from fastapi.testclient import TestClient  # noqa: E402

from shared import boat_polar as BP, boat_profile  # noqa: E402
from app import auth, boats, judge, labstate, learning, main, monitor, sessionboat  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


learning.LEARNING_DB = os.path.join(_tmp, "learning.db")
boats.EDITED_DIR = os.path.join(_tmp, "boats")
boats.BUNDLED_DIR = os.path.join(_tmp, "bundled")
os.makedirs(boats.EDITED_DIR)
os.makedirs(boats.BUNDLED_DIR)
for bid in ("sr33", "bravo"):
    shutil.copy(os.path.join(HERE, "boats", f"{bid}.json"), boats.BUNDLED_DIR)
with open(os.path.join(boats.BUNDLED_DIR, "broken.json"), "w") as f:
    json.dump({"boat_id": "broken", "name": "Broken", "draft_m": -3}, f)

client = TestClient(main.app)
TEAM = {"Authorization": "Bearer " + auth.issue_token()}
PI = {"Authorization": "Bearer pi-ingest-token-for-this-test"}

print("1) the library: every boat with its own polar")
r = client.get("/api/boats/library", headers=TEAM)
lib = r.json()
by = {b["boat_id"]: b for b in lib.get("boats", [])}
check("served (team login)", r.status_code == 200 and lib["schema"] == "c4.boat-library/v1")
check("sr33: its own files' polar, same block the Deploy push carries",
      by["sr33"]["polar"]["source"] == "files"
      and by["sr33"]["polar"]["version"] == boats.polar_for(boats.get_boat("sr33"))["version"])
check("bravo: its cert polar", by["bravo"]["polar"]["source"] == "cert"
      and by["bravo"]["polar"]["boat_id"] == "bravo")
check("each entry checks out the way the Pi checks it (profile version, polar for this boat)",
      all(b["boat_version"] == boat_profile.version(b["profile"])
          and not BP.check(b["polar"], b["boat_id"]) for b in by.values()))
check("a profile with errors is left out, by name", "broken" not in by
      and [s["boat_id"] for s in lib["skipped"]] == ["broken"])

print("2) the Pi's ingest token opens the library and nothing else")
check("library with the Pi's token: 200", client.get("/api/boats/library", headers=PI).status_code == 200)
check("any other path with it: 401", client.get("/api/boats", headers=PI).status_code == 401
      and client.get("/api/boats/sr33", headers=PI).status_code == 401)
check("a wrong token: 401", client.get("/api/boats/library",
                                       headers={"Authorization": "Bearer nope"}).status_code == 401)
check("no token: 401", client.get("/api/boats/library").status_code == 401)
_saved = os.environ.pop("INGEST_TOKEN")
check("no token configured → only the team login opens it",
      client.get("/api/boats/library", headers=PI).status_code == 401
      and client.get("/api/boats/library", headers=TEAM).status_code == 200)
os.environ["INGEST_TOKEN"] = _saved

print("3) recordings name the boat SAILED, and show that boat's debriefs")
RID = "practice-race"
S_BRAVO, S_OLD, S_OTHER = 1_790_000_000.0, 1_790_100_000.0, 1_790_200_000.0
SESSIONS = [
    {"id": 1, "name": "Bravo sail", "race_id": RID, "kind": "practice", "start_ts": S_BRAVO,
     "end_ts": S_BRAVO + 3600, "kit": "sr33", "boat_id": "bravo", "boat_name": "Bravo",
     "window": {"start_ts": S_BRAVO, "end_ts": S_BRAVO + 3600, "hours": 1.0}},
    {"id": 2, "name": "Old marker", "race_id": RID, "kind": "race", "start_ts": S_OLD,
     "end_ts": S_OLD + 3600, "kit": "sr33",
     "window": {"start_ts": S_OLD, "end_ts": S_OLD + 3600, "hours": 1.0}},
    {"id": 3, "name": "Borrowed boat", "race_id": RID, "kind": "practice", "start_ts": S_OTHER,
     "end_ts": S_OTHER + 3600, "kit": "sr33", "boat_id": None, "boat_name": "Mystery",
     "window": {"start_ts": S_OTHER, "end_ts": S_OTHER + 3600, "hours": 1.0}}]
monitor.agent_json = lambda path, timeout=30: {"sessions": [dict(s) for s in SESSIONS]}


def report(ws):
    return {"available": True, "race_id": RID, "race_name": "Practice",
            "actual_track": {"available": True, "window": {"start_ts": ws}, "polar_pct": 90.0,
                             "perf_bins": [{"tws": 10, "twa": 90, "point_of_sail": "reach",
                                            "samples": 30, "best_stw": 6, "target_stw": 6.5,
                                            "pct": 92.0}]}}


learning.archive_debrief(report(S_BRAVO), "bravo")
learning.archive_debrief(report(S_OLD), "sr33")
boats.set_active("sr33")
recs = {x["session_id"]: x for x in client.get(f"/api/debrief/recordings?race_id={RID}",
                                                headers=TEAM).json()["recordings"]}
check("the Bravo sail is filed under Bravo, with Bravo's debrief (active boat is sr33)",
      recs[1]["boat_id"] == "bravo" and recs[1]["boat_name"] == "Bravo"
      and recs[1]["debrief"] is not None and recs[1]["debrief"]["boat_id"] == "bravo")
check("an older marker (no boat) → the kit's boat, its sr33 debrief",
      recs[2]["boat_id"] == "sr33" and recs[2]["boat_source"] == "kit"
      and recs[2]["debrief"]["boat_id"] == "sr33")
check("an Other boat → its name, no profile, no debrief",
      recs[3]["boat_id"] is None and recs[3]["boat_name"] == "Mystery"
      and recs[3]["boat_has_profile"] is False and recs[3]["debrief"] is None)

print("4) an Other boat can't be debriefed until it has a profile; the Lab edits its boat")
r = client.post("/api/debrief/run", headers=TEAM, json={
    "race_id": RID, "session_id": 3, "start_ts": S_OTHER, "recording": S_OTHER, "kit": "sr33",
    "boat_id": None, "boat_name": "Mystery"}).json()
check("debrief refused, plainly", r == {"ok": False, "note": sessionboat.OTHER_NO_PROFILE})
r = client.post("/api/debrief/session-boat", headers=TEAM, json={
    "kit": "sr33", "start_ts": S_OTHER, "boat_id": "nope", "race_id": RID, "recording": S_OTHER})
check("an unknown profile → refused", r.status_code == 400)
r = client.post("/api/debrief/session-boat", headers=TEAM, json={
    "kit": "sr33", "start_ts": S_OTHER, "boat_id": "bravo", "race_id": RID, "recording": S_OTHER})
check("assigned to Bravo (e.g. after importing its cert)", r.status_code == 200
      and r.json()["boat"]["boat_id"] == "bravo")
check("…and it is now debriefable for Bravo", sessionboat.for_debrief(
    {"kit": "sr33", "start_ts": S_OTHER, "boat_id": None, "boat_name": "Mystery"}) == ("bravo", None))
recs = {x["session_id"]: x for x in client.get(f"/api/debrief/recordings?race_id={RID}",
                                                headers=TEAM).json()["recordings"]}
check("the recordings list shows the Lab's edit", recs[3]["boat_id"] == "bravo"
      and recs[3]["boat_source"] == "lab")

print("5) a wrong pick fixed: the old boat's debrief leaves that boat's learning")
before = [d["window_start"] for d in learning.archived_recordings("sr33")]
r = client.post("/api/debrief/session-boat", headers=TEAM, json={
    "kit": "sr33", "start_ts": S_OLD, "boat_id": "bravo", "race_id": RID, "recording": S_OLD})
check("edited, one debrief withdrawn", r.status_code == 200 and r.json()["withdrawn"] == 1)
after = [d["window_start"] for d in learning.archived_recordings("sr33")]
check("sr33's re-score no longer counts it", S_OLD in before and S_OLD not in after)
check("nothing deleted: the record says whose it was",
      any(d["boat_id"] == learning.WITHDRAWN + "sr33" for d in learning.list_debriefs(None, RID)))
check("Bravo's learning doesn't count it until it is debriefed again",
      S_OLD not in [d["window_start"] for d in learning.archived_recordings("bravo")])

print("6) the debrief runs for the session's boat, not the active one")
seen = {}
_get = boats.get_boat


def spy_get(bid):
    seen.setdefault("asked", []).append(bid)
    return _get(bid)


judge.boats.get_boat = spy_get
judge.boats.active_boat = lambda: (_ for _ in ()).throw(AssertionError("active boat read"))
try:
    out = judge.run_judge("no-such-race", boat_id="bravo")
finally:
    judge.boats.get_boat = _get
check("the judge asked for Bravo and never read the active boat",
      "bravo" in seen.get("asked", []) and out["note"] == "unknown race")
check("a session boat with no profile → said plainly", judge.run_judge(
    "no-such-race", boat_id="nope")["note"] == "No profile for this session's boat.")

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
