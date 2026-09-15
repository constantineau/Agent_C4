"""The debrief as a PRODUCT: self-serve, per recording, and never hostage to a plan.

Cole, 2026-09-15: "I want the debrief to be something available in the C4 Lab after every race —
we shouldn't have to do a fresh session to create one." Three things stood in the way, and each
is asserted here:

1. **A debrief required a frozen playbook.** `run_judge` refused outright without one, so a race
   nobody wrote a playbook for could not be debriefed at all — including the boat's own SPEED,
   which needs no plan to be true. The performance half now always runs and the tactics half
   reports itself unavailable by name.
2. **One stored track per race id.** A race definition holds as many recordings as the boat made
   under it; loading one replaced the other.
3. **No way to see what had been debriefed.** `/api/debrief/recordings` joins the boat log's
   sessions with the tracks the Lab holds and the debriefs it has archived — the tab's home
   screen, and the answer to "has last weekend been debriefed?".

Run in-container:
  docker run --rm -v $PWD/vps/lab/app:/srv/app:ro -v $PWD/shared:/srv/shared:ro \
    -v $PWD/vps/lab/test_debrief_product.py:/srv/test_debrief_product.py:ro \
    -w /srv sr33-dev-lab python test_debrief_product.py
"""
import os
import tempfile

from app import judge as J
from app import learning as LN
from app import main
from app import monitor
from app import pbstore
from app import store
from app import track as T

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


JUL15 = 1784155457.9
JUL18 = 1784394211.4
N = 90
RACE = {"race_id": "bayview-mackinac-2026", "name": "2026 Bayview Mackinac Race",
        "courses": [{"id": "cove_island", "start": {"lat": 43.066, "lon": -82.417},
                     "marks": [{"name": "Finish", "lat": 45.77, "lon": -84.44}]}]}

tmp = tempfile.mkdtemp()
T.TRACK_DIR = tmp
LN.LEARNING_DB = os.path.join(tmp, "learning.db")
store.get_race = lambda rid: RACE if rid == RACE["race_id"] else None


def mktrack(start, name):
    """A straight two-hour sail at 6 kn with measured wind on every fix."""
    return {"source": "boatlog", "boat": name,
            "fixes": [{"t": start + i * 80, "lat": 43.0 + i * 0.0015, "lon": -82.4, "sog": 6.0,
                       "cog": 0.0, "tws": 10.0, "twa": 300.0, "stw": 6.0} for i in range(N)],
            "window": {"start_ts": start, "end_ts": start + (N - 1) * 80, "kind": "derived",
                       "hours": round((N - 1) * 80 / 3600.0, 2)},
            "trust": {"available": True, "danger": {}, "summary": {"line": "no danger windows"}},
            "sail_log": [{"t": start, "flying": ["J1"]}]}


print("1. two recordings of one race both persist:")
m15 = T.save_track(RACE["race_id"], mktrack(JUL15, "Race 2026-07-15"))
m18 = T.save_track(RACE["race_id"], mktrack(JUL18, "Race 2026-07-18"))
check("saving the second did not replace the first",
      T.load_track(RACE["race_id"], JUL15)["boat"] == "Race 2026-07-15"
      and T.load_track(RACE["race_id"], JUL18)["boat"] == "Race 2026-07-18")
check("list_tracks returns both, newest recording first",
      [t["boat"] for t in T.list_tracks(RACE["race_id"])] == ["Race 2026-07-18", "Race 2026-07-15"])
gpx = {"source": "gpx", "boat": "GPX", "fixes": mktrack(JUL15, "x")["fixes"]}
T.save_track(RACE["race_id"], gpx)
check("a GPX (no window) keeps the bare race path and sits alongside them",
      T.load_track(RACE["race_id"])["source"] == "gpx" and len(T.list_tracks(RACE["race_id"])) == 3)
check("clearing one recording leaves the others", T.clear_track(RACE["race_id"], JUL15)
      and T.load_track(RACE["race_id"], JUL15) is None
      and T.load_track(RACE["race_id"], JUL18) is not None)
T.save_track(RACE["race_id"], mktrack(JUL15, "Race 2026-07-15"))

legacy = mktrack(JUL18 + 5, "Legacy single-slot")
import json as _json
with open(T._path(RACE["race_id"]), "w") as fh:
    _json.dump(legacy, fh)                       # the pre-2026-09-15 one-track-per-race file
check("a legacy track filed at the bare race path is served for ITS recording",
      (T.load_track(RACE["race_id"], JUL18 + 5) or {}).get("boat") == "Legacy single-slot")
check("...and is not mistaken for another recording",
      (T.load_track(RACE["race_id"], JUL18 + 5000) or {}) .get("boat") != "Legacy single-slot")
with open(T._path(RACE["race_id"]), "w") as fh:
    _json.dump(gpx, fh)                          # put the plain GPX back for the join test below

print("\n2. a debrief runs with NO frozen playbook:")
pbstore.list_bundles = lambda: []
r = J.run_judge(RACE["race_id"], recording=JUL18)
check("the debrief is available", r.get("available") is True)
check("...and says the tactics half is not, by name",
      r.get("tactics_available") is False and "playbook" in (r.get("tactics_note") or ""))
at = r.get("actual_track") or {}
check("the boat's speed was scored anyway", at.get("available") and at.get("polar_pct") is not None)
check("measured cells were archived for the boat model", len(at.get("perf_bins") or []) >= 1)
check("no oracle is invented — no time-behind, no side, no regret",
      at.get("oracle_hours") is None and at.get("time_behind_optimal_min") is None
      and not r.get("regret") and not r.get("oracle"))
check("the critique talks about speed and never about a side that paid",
      "polar" in (r["critique"]["assessment"] or "").lower()
      and "paid" not in (r["critique"]["assessment"] or "").lower())
check("it archived to the learning DB under its own recording",
      r.get("archived_id") and LN.list_debriefs(None, RACE["race_id"])[0]["window_start"] == JUL18)
r15 = J.run_judge(RACE["race_id"], recording=JUL15)
check("the other recording archives separately (2 debriefs, one per recording)",
      len(LN.list_debriefs(None, RACE["race_id"])) == 2 and r15["archived_id"] != r["archived_id"])
check("each archived row says how many cells it contributed",
      all(d["n_bins"] >= 1 for d in LN.list_debriefs(None, RACE["race_id"])))
none = J.run_judge(RACE["race_id"], recording=99.0)
check("with neither a playbook nor a track it refuses, and says what to do",
      none.get("available") is False and "load the boat's log" in none.get("note", ""))

print("\n2b. an archive row written before the flags existed still reads right:")
did = LN.list_debriefs(None, RACE["race_id"])[0]["id"]
import sqlite3 as _sq
_c = _sq.connect(LN.LEARNING_DB)
_c.execute("UPDATE debriefs SET report_json=?, oracle_hours=40.5 WHERE id=?",
           ('{"race_id": "bayview-mackinac-2026", "regret": {"side_paid": "right"}}', did))
_c.commit(); _c.close()
old_row = LN.get_debrief(did)
check("a judged debrief archived before the flag is NOT relabelled a performance debrief",
      old_row["report"]["tactics_available"] is True)
check("...and its recording is recovered from the row's window_start",
      old_row["report"]["recording"]["start_ts"] == old_row["window_start"])

print("\n3. the recordings surface answers 'has this been debriefed?':")
SESSIONS = {"sessions": [
    {"id": 2, "name": "Race 2026-07-18 17:03Z", "race_id": RACE["race_id"], "kind": "race",
     "start_ts": JUL18, "end_ts": JUL18 + 6000,
     "window": {"start_ts": JUL18, "end_ts": JUL18 + 26000, "hours": 7.36, "marker_hours": 1.81}},
    {"id": 1, "name": "Race 2026-07-15 22:44Z", "race_id": RACE["race_id"], "kind": "race",
     "start_ts": JUL15, "end_ts": JUL15 + 4000,
     "window": {"start_ts": JUL15, "end_ts": JUL15 + 5700, "hours": 1.6}},
    {"id": 7, "name": "Never stopped", "race_id": RACE["race_id"], "kind": "race",
     "start_ts": JUL18 + 999999, "end_ts": None, "window": {}},
    {"id": 9, "name": "Another regatta", "race_id": "mills-trophy-race-2026", "kind": "race",
     "start_ts": JUL15 - 99999, "end_ts": JUL15 - 98000, "window": {"hours": 0.5}}]}
monitor.agent_json = lambda path, timeout=None: SESSIONS
main.boats.active_boat = lambda: {"boat_id": None}
rec = main._recordings(RACE["race_id"])
rows = rec["recordings"]
check("both race sessions are listed, newest first",
      [x["session_id"] for x in rows][:2] == [2, 1])
check("a session with no end at all is not offered (nothing to debrief)",
      not any(x["session_id"] == 7 for x in rows))
check("another regatta's session is not in this race's list",
      not any(x["session_id"] == 9 for x in rows))
check("each row carries its debrief, its track and the derived hours",
      rows[0]["debrief"] and rows[0]["track"] and rows[0]["hours"] == 7.36
      and rows[0]["marker_hours"] == 1.81)
check("the GPX track the boat log cannot explain is still listed",
      any(x["session_id"] is None and x["kind"] == "gpx" for x in rows))
monitor.agent_json = lambda path, timeout=None: (_ for _ in ()).throw(RuntimeError("no route"))
off = main._recordings(RACE["race_id"])
check("with the boat unreachable it says so and still lists what the Lab holds",
      off["agent"]["available"] is False and "unreachable" in off["agent"]["note"]
      and len(off["recordings"]) == 3)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
