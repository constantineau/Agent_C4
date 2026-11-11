"""#322 — Practice sails: sessions with no race id, which no race's Recordings list shows.

  * GET /api/debrief/recordings?no_race=1 lists every ended session with no race (date, length,
    the boat sailed, its debrief); a race's list never shows them, and they never show a race's;
  * a practice sail is debriefed with no race: the speed half runs, the tactics half is skipped
    with a plain note, no race definition or playbook is read, and the debrief is filed under the
    session's boat (from the boat log) with no race id;
  * its track is stored apart from every race's;
  * changing its boat withdraws its old debriefs, like any session (#321 rules);
  * learning reads (suggestion, wave fit, re-score) take a debrief with no race id;
  * the Lab page has the Practice sails card, which posts no race and no plan.
"""
import contextlib
import json
import os
import re
import tempfile
import time

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ.pop("ANTHROPIC_API_KEY", None)
HERE = os.path.dirname(os.path.abspath(__file__))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, boats, judge, learning, main, monitor, rescore, sessionboat, track  # noqa: E402
import boat_fixtures  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


boat_fixtures.install()                      # bravo: a second, test-only boat
learning.LEARNING_DB = os.path.join(_tmp, "learning.db")
boats.EDITED_DIR = os.path.join(_tmp, "boats")
track.TRACK_DIR = _tmp
boats.set_active("sr33")
client = TestClient(main.app)
TEAM = {"Authorization": "Bearer " + auth.issue_token()}

RID = "some-race"
S_P1, S_P2, S_RACE, S_OPEN = 1_790_000_000.0, 1_790_100_000.0, 1_790_200_000.0, 1_790_300_000.0
SESSIONS = [
    {"id": 1, "name": "Sail 2026-09-20 14:00Z", "race_id": None, "kind": "practice",
     "start_ts": S_P1, "end_ts": S_P1 + 5400, "kit": "sr33", "boat_id": "bravo",
     "boat_name": "Bravo", "window": {"start_ts": S_P1 - 300, "end_ts": S_P1 + 5400, "hours": 1.58}},
    {"id": 2, "name": "Sail 2026-09-21 10:00Z", "race_id": None, "kind": "practice",
     "start_ts": S_P2, "end_ts": S_P2 + 3600, "kit": "sr33",
     "window": {"start_ts": S_P2, "end_ts": S_P2 + 3600, "hours": 1.0}},
    {"id": 3, "name": "Race day", "race_id": RID, "kind": "race", "start_ts": S_RACE,
     "end_ts": S_RACE + 3600, "kit": "sr33",
     "window": {"start_ts": S_RACE, "end_ts": S_RACE + 3600, "hours": 1.0}},
    {"id": 4, "name": "Still out", "race_id": None, "kind": "practice", "start_ts": S_OPEN,
     "end_ts": None, "kit": "sr33", "window": {}}]
W1 = SESSIONS[0]["window"]["start_ts"]
monitor.agent_json = lambda path, timeout=30: {"sessions": [dict(s) for s in SESSIONS]}


def recs(q):
    return client.get("/api/debrief/recordings?" + q, headers=TEAM).json()["recordings"]


print("1) the Practice sails list")
p = {x["session_id"]: x for x in recs("no_race=1")}
check("lists the ended sessions with no race, not the race's nor the open one",
      sorted(p) == [1, 2])
check("each row: when, how long, the boat sailed (from the boat log), Change needs kit + start",
      p[1]["start_ts"] == S_P1 and p[1]["hours"] == 1.58 and p[1]["boat_id"] == "bravo"
      and p[1]["boat_name"] == "Bravo" and p[1]["kit"] == "sr33" and p[1]["race_id"] is None
      and p[2]["boat_id"] == "sr33" and p[2]["boat_source"] == "kit")
check("not debriefed yet", p[1]["debrief"] is None and p[1]["n_debriefs"] == 0)
check("a race's list never shows a practice sail",
      [x["session_id"] for x in recs(f"race_id={RID}")] == [3])

print("2) its track is stored apart from every race")
t = {"source": "boatlog", "fixes": [{"t": W1 + i, "lat": 45.0, "lon": -82.0} for i in range(20)],
     "window": {"start_ts": W1, "end_ts": S_P1 + 5400}, "sail_log": []}
track.save_track(None, t)
track.save_track("norace", dict(t, window={"start_ts": W1 + 7, "end_ts": S_P1 + 5400}))
check("no race → its own file", os.path.exists(track._path(None, W1))
      and track._path(None, W1) != track._path("norace", W1))
check("listed with the practice sails only",
      [x["recording"] for x in track.list_tracks(None)] == [W1]
      and [x["recording"] for x in track.list_tracks("norace")] == [W1 + 7])
check("the race library skips it ('_' prefix)", os.path.basename(track._path(None, W1))[0] == "_")
check("loads back", (track.load_track(None, W1) or {}).get("window", {}).get("start_ts") == W1)
os.remove(track._path("norace", W1 + 7))

print("3) a practice sail is debriefed with no race")
BINS = [{"tws": 10, "twa": 90, "point_of_sail": "reach", "samples": 40, "best_stw": 6.2,
         "target_stw": 6.5, "pct": 95.0, "wind_source": "measured", "config": "J1",
         "hs_mean": 0.4}]
_get_race, _bundles = judge.store.get_race, judge.pbstore.list_bundles
judge.store.get_race = lambda rid: (_ for _ in ()).throw(AssertionError("race read"))
judge.pbstore.list_bundles = lambda: (_ for _ in ()).throw(AssertionError("playbooks read"))
judge._own_speed_fields = lambda *a, **k: (None, None)
_score = judge._score_actual_track
seen = {}


def fake_score(t, oracle, marks, start, *a, **k):
    seen.update(oracle=oracle, marks=marks, boat=(k.get("boat") or {}).get("boat_id"))
    return {"available": True, "window": t["window"], "polar_pct": 95.0, "elapsed_hours": 1.58,
            "perf_bins": [dict(b) for b in BINS]}


judge._score_actual_track = fake_score
rep = judge.run_judge(None, recording=W1, boat_id="bravo", name="Sail 2026-09-20 14:00Z",
                      archive_guard=lambda: contextlib.nullcontext((True, None)))
check("speed scored for the session's boat, on no course",
      rep["available"] and seen == {"oracle": {}, "marks": [], "boat": "bravo"})
check("tactics skipped with a plain note", rep["tactics_available"] is False
      and rep["tactics_note"] == judge.NO_RACE_NOTE and rep["no_race"] is True)
check("named after the session, no race id", rep["race_id"] is None
      and rep["race_name"] == "Sail 2026-09-20 14:00Z")
check("the critique says practice sail, never 'no frozen playbook'",
      "Practice sail" in rep["critique"]["assessment"]
      and "playbook" not in rep["critique"]["assessment"])
check("filed under Bravo with no race id", rep.get("archived_id")
      and [(d["boat_id"], d["race_id"]) for d in learning.list_debriefs(no_race=True)]
      == [("bravo", None)])
check("the stored report keeps 'no race' (the Lab hides 'Save to Learnings')",
      learning.get_debrief(rep["archived_id"])["report"]["no_race"] is True)
check("no race's debrief list shows it", learning.list_debriefs(None, RID) == [])
p = {x["session_id"]: x for x in recs("no_race=1")}
check("the Practice sails row shows its debrief and its track",
      p[1]["debrief"] and p[1]["debrief"]["id"] == rep["archived_id"]
      and p[1]["track"] and p[1]["track"]["recording"] == W1)
rep2 = judge.run_judge(None, recording=W1, boat_id="bravo", archive_guard=lambda:
                       contextlib.nullcontext((False, sessionboat.BOAT_CHANGED)))
check("boat changed while it ran → filed nothing, said (the #321 guard)",
      "archived_id" not in rep2 and rep2["archive_note"] == sessionboat.BOAT_CHANGED
      and len(learning.list_debriefs(no_race=True)) == 1)
judge._score_actual_track = _score
judge.store.get_race, judge.pbstore.list_bundles = _get_race, _bundles

print("4) the Debrief button: no race, the boat from the boat log")
got = {}
_rj, _llt = judge.run_judge, main.load_log_track
judge.run_judge = lambda rid, pid=None, **k: got.update(rid=rid, pid=pid, boat=k.get("boat_id"),
                                                       name=k.get("name")) or {"available": True}


def fake_llt(b):
    got["llt"] = dict(b)
    return {"window": {"start_ts": W1}, "n": 20}, 200


main.load_log_track = fake_llt
r = client.post("/api/debrief/run", headers=TEAM, json={
    "session_id": 1, "start_ts": S_P1, "recording": W1, "kit": "sr33", "boat_id": "sr33",
    "playbook_id": "some-plan"}).json()
for _ in range(100):
    if "rid" in got:
        break
    time.sleep(0.05)
check("started", r.get("ok") is not False)
check("run with no race and no plan, for Bravo (the boat log's, not the request's sr33)",
      got.get("rid") is None and got.get("pid") is None and got.get("boat") == "bravo"
      and got.get("name") == SESSIONS[0]["name"])
check("the track is fetched as a practice sail", got.get("llt", {}).get("no_race") is True
      and got["llt"].get("race_id") is None)
judge.run_judge, main.load_log_track = _rj, _llt
check("no race and no session → refused", client.post(
    "/api/debrief/run", headers=TEAM, json={}).status_code == 400)
check("from-log with no race and no practice flag → refused as before",
      main.load_log_track({"session_id": 1, "start_ts": S_P1})[1] == 422)
_flt = main.fetch_log_track
main.fetch_log_track = lambda b, sessions=None: (dict(t, window={"start_ts": W1 + 60,
                                                                 "end_ts": S_P1 + 5400}), 200)
res, code = main.load_log_track({"session_id": 1, "start_ts": S_P1, "no_race": True})
main.fetch_log_track = _flt
check("a practice sail's track is stored with no race", code == 200 and res["race_id"] is None
      and track.load_track(None, W1 + 60) is not None)
os.remove(track._path(None, W1 + 60))

print("5) Change: its old debrief leaves the old boat's learning (#321 rules)")
r = client.post("/api/debrief/session-boat", headers=TEAM, json={
    "kit": "sr33", "start_ts": S_P1, "boat_id": "sr33"})
check("edited, the Bravo debrief withdrawn", r.status_code == 200 and r.json()["withdrawn"] == 1)
check("kept, marked whose it was", [d["boat_id"] for d in learning.list_debriefs(no_race=True)]
      == [learning.WITHDRAWN + "bravo"])
p = {x["session_id"]: x for x in recs("no_race=1")}
check("the row shows the new boat, from the Lab, and no debrief for it yet",
      p[1]["boat_id"] == "sr33" and p[1]["boat_source"] == "lab" and p[1]["debrief"] is None)

print("6) learning takes a debrief with no race id")
learning.archive_debrief({"available": True, "race_id": None, "race_name": "Sail",
                          "actual_track": {"available": True, "window": {"start_ts": S_P2},
                                           "perf_bins": [dict(b) for b in BINS]}}, "sr33")
learning.archive_debrief({"available": True, "race_id": RID, "race_name": "Race day",
                          "actual_track": {"available": True, "window": {"start_ts": S_RACE},
                                           "perf_bins": [dict(b, hs_mean=1.6, pct=85.0)
                                                         for b in BINS]}}, "sr33")
try:
    prop = learning.propose("sr33")
    check("a suggestion builds over a race and a practice sail", isinstance(prop, dict)
          and None in [r["race_id"] for r in (prop.get("summary") or {}).get("recordings", [])])
except TypeError as exc:
    check(f"a suggestion builds over a race and a practice sail ({exc})", False)
try:
    w = learning.fit_waves([dict(b, race_id=None, window_start=S_P2) for b in BINS]
                           + [dict(b, race_id=RID, window_start=S_RACE, hs_mean=1.6, pct=85.0)
                              for b in BINS], learning._current_wave_coeffs("sr33"))
    check("the wave fit takes it", isinstance(w, dict))
except TypeError as exc:
    check(f"the wave fit takes it ({exc})", False)
track.save_track(None, dict(t, window={"start_ts": S_P2, "end_ts": S_P2 + 3600}))
got_t, how = rescore._track_for({"race_id": None, "window_start": S_P2}, None)
check("the re-score finds a practice sail's stored track", got_t is not None and how == "stored")

print("7) the Lab page")
JS = open(os.path.join(HERE, "web", "app.js")).read()
check("a Practice sails card, fed by ?no_race=1",
      "function debPracticeCard(" in JS and "<h3>Practice sails" in JS
      and '"/api/debrief/recordings?no_race=1"' in JS)
check("shown with and without a race picked",
      JS.count("${debPracticeCard()}") == 2)
m = re.search(r"async function debRunRecording\(i, src\)[\s\S]*?\n}\n", JS)
check("its Debrief posts no race and no plan", m and 'practice ? { recording: x.recording }' in m.group(0))
check("Debrief and Change act on the practice list",
      "debRunRecording(${arg(i)})" in JS and "debEditBoat(${arg(i)})" in JS
      and "function debList(src)" in JS)
check("a practice debrief has no 'Save to Learnings' (it writes a race's notes)",
      '${r.no_race ? "" : `<div class="card">' in JS)
check("Cole's wording: 'Practice sails', never 'without a race'",
      "without a race" not in JS.lower())

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
