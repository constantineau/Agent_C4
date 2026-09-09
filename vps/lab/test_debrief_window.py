"""The debrief loads the race the RECORD supports, not the one the ⏺ LOG button recorded.

`shared/race_window.derive()` shipped 2026-09-08 and the agent serves a `window` per session —
but the Lab's boat-log ingestion still asked for `start_ts`/`end_ts`, so the derived hours reached
nothing. This is the eighth time a thing was designed, seeded, wired and silently not in force in
this project, so the test that matters is not "does the resolver prefer the window" — it is
**what interval does the route actually ask the agent for**. Both are here; the second one is the
one that would have caught the bug.

The fixture is Jul 18 2026 as the live database reports it: marker 17:03:31 -> 18:52:20Z (1.81 h,
the tap caught during a kite hoist) and derived 17:03:31 -> 00:25:00Z (7.36 h, bounded at the
turnaround).

Run in-container (fastapi is not in the system python):
  docker cp vps/lab/test_debrief_window.py sr33-dev-lab-1:/srv/ && \
  docker exec -w /srv sr33-dev-lab-1 python test_debrief_window.py
"""
import calendar
import time

from app import main, track


def ts(s):
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))


MARKER_A, MARKER_B = ts("2026-07-18T17:03:31Z"), ts("2026-07-18T18:52:20Z")
DERIVED_B = ts("2026-07-19T00:25:00Z")

SESSION = {"id": 2, "race_id": "bayviewmack2026", "name": "Bayview Mac", "kind": "race",
           "start_ts": MARKER_A, "end_ts": MARKER_B,
           "window": {"start_ts": MARKER_A, "end_ts": DERIVED_B,
                      "marker_start_ts": MARKER_A, "marker_end_ts": MARKER_B,
                      "hours": 7.36, "marker_hours": 1.81, "session_ids": [2],
                      "motion_source": "orca-core.3", "motion_device": "Orca Core",
                      "provenance": ["extended the end by 5.5 h — the boat was still under way",
                                     "ended at the turnaround — course made good reversed 171°"]}}

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---- the resolver -----------------------------------------------------------------------------
print("resolve_log_window:")
w = main.resolve_log_window({"session_id": 2}, [SESSION])
check("the derived window wins over the marker (7.36 h, not 1.81 h)",
      w["kind"] == "derived" and w["end_ts"] == DERIVED_B and w["hours"] == 7.36)
check("...and the marker is kept alongside it, never overwritten",
      w["marker_end_ts"] == MARKER_B and w["marker_hours"] == 1.81)
check("the provenance travels with the window — a bound nobody can see is worth nothing",
      len(w["provenance"]) == 2 and w["motion_device"] == "Orca Core")

check("a session matches on start_ts too (the UI sends both; older callers send only the anchor)",
      main.resolve_log_window({"start_ts": MARKER_A}, [SESSION])["end_ts"] == DERIVED_B)
check("a start_ts a minute off matches nothing — a near-miss must not silently load another race",
      main.resolve_log_window({"start_ts": MARKER_A + 60}, [SESSION]) is None)
check("no such session is None, not a fabricated window",
      main.resolve_log_window({"session_id": 99}, [SESSION]) is None)

m = main.resolve_log_window({"session_id": 2, "use_marker": True}, [SESSION])
check("use_marker returns exactly the button's window — a derived bound that cannot be refused "
      "is as bad as one that cannot be seen",
      m["kind"] == "marker" and m["end_ts"] == MARKER_B and m["hours"] == 1.81)

nowin = {**SESSION}
nowin.pop("window")
f = main.resolve_log_window({"session_id": 2}, [nowin])
check("a session the agent could not derive falls back to the marker AND says so",
      f["kind"] == "marker" and f["end_ts"] == MARKER_B and "no derived window" in f["provenance"][0])

unclosed = {**SESSION, "end_ts": None}
check("a session the crew never stopped is still loadable — its window has an end even though "
      "the button never wrote one",
      main.resolve_log_window({"session_id": 2}, [unclosed])["end_ts"] == DERIVED_B)


# ---- the read path: what does the route ASK FOR? ------------------------------------------------
print("\nthe route (POST /api/debrief/track/from-log):")
asked = []


def fake_agent_json(path):
    asked.append(path)
    if path == "/racelog/sessions":
        return {"sessions": [SESSION]}
    return {"fixes": [{"t": MARKER_A + i, "lat": 45 + i / 1e4, "lon": -83.0, "sog": 6.0}
                      for i in range(50)],
            "sail_log": [{"ts": MARKER_B + 5, "flying": ["A3", "SS"]}]}


saved = {}
from app import monitor                                    # noqa: E402  (patched, not called)
monitor.agent_json = fake_agent_json
track.save_track = lambda rid, t: saved.update(t) or {"race_id": rid, "n": t["n"],
                                                      "source": t["source"],
                                                      "window": t.get("window"),
                                                      "sail_changes": len(t.get("sail_log") or [])}

r = main.debrief_track_from_log({"race_id": "bayviewmack2026", "session_id": 2,
                                 "start_ts": MARKER_A, "end_ts": MARKER_B, "name": "Bayview Mac"})
check("it asked the agent for the sessions before asking for a track",
      asked and asked[0] == "/racelog/sessions")
check(f"it fetched the DERIVED seven hours, not the button's two — {asked[-1]}",
      f"start={MARKER_A}" in asked[-1] and f"end={DERIVED_B}" in asked[-1])
check("...and asked for points in proportion to it — a 4x longer window must not arrive 4x "
      "coarser (2000 default would be 13 s between fixes over seven hours)",
      "max_points=8000" in asked[-1])
check("the window it used is stored with the track (the judge scores a window, not a race)",
      saved.get("window", {}).get("kind") == "derived")
check("and reported back to the caller", r.get("ok") and r["window"]["hours"] == 7.36)

asked.clear()
main.debrief_track_from_log({"race_id": "bayviewmack2026", "session_id": 2,
                            "start_ts": MARKER_A, "end_ts": MARKER_B, "use_marker": True})
check("use_marker really does fetch the shorter interval",
      f"end={MARKER_B}" in asked[-1])

bad = main.debrief_track_from_log({"race_id": "bayviewmack2026"})
check("no session identifier is a 422, not a full-database scan", bad.status_code == 422)
gone = main.debrief_track_from_log({"race_id": "bayviewmack2026", "session_id": 404})
check("an unknown session is a 404, not a track over the marker's bounds", gone.status_code == 404)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
