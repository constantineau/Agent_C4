"""One debrief per RECORDING, on the recording's own clock.

Three defects found 2026-09-15 while closing the polar → optimizer loop, each of the shape
"designed, wired, silently not in force":

1. `score_track` re-anchored every track on the playbook's gun even when the fixes carried a wall
   clock. A boat-log fix at 17:03:31Z scored as 17:00:00Z — 211 s off on Jul 18, sliding the trust
   sweep's danger windows by that much — and the Jul 15 recording judged against the Jul 18 gun sat
   2.8 DAYS off, so every sail-log entry lay in the past and the last sail of that day got
   credited with every bin.
2. A boat-log track was clipped to the nearest fixes to the start and finish marks like a GPX,
   although its window already IS the racing portion — a recording that never reached the finish
   (a practice sail, a retirement) lost its tail at whichever fix happened to lie closest.
3. The learning archive keyed "latest debrief" on race_id alone, and Bayview 2026 holds two
   recordings under one id — the second debrief would have displaced the first's bins.
4. The judge's oracle wind came from the live sources only, which hold ~10 days; every past race
   answered "no wind data" and archived nothing. Older races route to the GFS/HRRR archive.

Run in-container:
  docker run --rm -v $PWD/vps/lab/app:/srv/app:ro -v $PWD/shared:/srv/shared:ro \
    -v $PWD/vps/lab/test_debrief_recording.py:/srv/test_debrief_recording.py:ro \
    -w /srv sr33-dev-lab python test_debrief_recording.py
"""
import os
import tempfile
import time

from app import track as T
from app import learning as LN
from app import judge as J

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1784155457.0           # Jul 15 2026 22:44:17Z — the boat-log recording's own clock
GUN = 1784394000.0          # Jul 18 2026 17:00Z — the playbook's gun, 2.76 days later
N = 120
POLAR = [(10.0, 90.0, 6.0)]  # one cert cell: 10 kn / 90° → 6.0 kn target


def mktrack(window=True, trust=None, sail_log=None):
    """Two hours at 6 kn on a wall clock, measured wind on every fix (10 kn / 90°)."""
    fixes = [{"t": T0 + i * 60, "lat": 43.0 + i * 0.0015, "lon": -82.4, "sog": 6.0, "cog": 0.0,
              "tws": 10.0, "twa": 90.0, "stw": 6.0} for i in range(N)]
    tr = {"source": "boatlog", "fixes": fixes, "trust": trust, "sail_log": sail_log or []}
    if window:
        tr["window"] = {"start_ts": T0, "end_ts": T0 + (N - 1) * 60, "kind": "derived"}
    return tr


ORACLE = {"path": [{"lat": 43.0, "lon": -82.4}, {"lat": 43.0 + (N - 1) * 0.0015, "lon": -82.4}],
          "total_hours": 2.0, "total_nm": 10.8}
MARKS = [("Start", "start", 43.0, -82.4), ("Finish", "finish", 43.0 + (N - 1) * 0.0015, -82.4)]

print("1. the recording's own clock:")
DANGER = {"available": True, "danger": {"heading": [(T0 + 60 * 60, T0 + 80 * 60)], "attitude": []},
          "summary": {"danger_s": 1200, "line": "heading DANGER for 20 min"}}
# J1 for the recording, then a kite hoisted three hours AFTER it ended (a later sail that day):
# re-anchored on the gun, every fix sat past both entries and the S2 got the credit
SAILS = [{"t": T0, "flying": ["J1"], "reef": None}, {"t": T0 + 3 * 3600, "flying": ["S2"], "reef": None}]
r = T.score_track(mktrack(trust=DANGER, sail_log=SAILS), ORACLE, MARKS, GUN, polars=POLAR)
check("danger window refuses the 21 fixes at 60–80 min although the gun is 2.8 days later",
      r["trust"]["refused_samples"] == 21)
bins = r.get("perf_bins") or []
check("one measured bin comes out of the cell", len(bins) == 1 and bins[0]["wind_source"] == "measured")
check("the bin is attributed to the sail flying DURING the recording (J1), not the later S2",
      bins and bins[0]["config"] == "J1")
check("elapsed hours are unaffected by the anchor", r["elapsed_hours"] == round((N - 1) / 60, 2))
rel = mktrack(trust=None, sail_log=[])
for i, f in enumerate(rel["fixes"]):
    f["t"] = i * 60                    # a YB-style relative clock
rel.pop("window")
r_rel = T.score_track(rel, ORACLE, MARKS, GUN, polars=POLAR)
check("a race-relative clock is still anchored on the gun (YB path unchanged)",
      r_rel["available"] and r_rel["elapsed_hours"] == r["elapsed_hours"])
check("absolute_clock: epoch seconds yes, a 2-hour offset no",
      T.absolute_clock(T0) and not T.absolute_clock(7200) and not T.absolute_clock(None))

print("1b. the wind angle is folded onto the polar's half-circle:")
port = mktrack(window=True)
for f in port["fixes"]:
    f["twa"] = 300.0                       # a port-tack reach, as the record wraps it
    f["tws"], f["stw"] = 10.0, 6.0
POL60 = [(10.0, 60.0, 6.0), (10.0, 180.0, 4.0)]
rp = T.score_track(port, ORACLE, MARKS, GUN, polars=POL60)
pb = rp.get("perf_bins") or []
check("300° lands in the 60° cell, not off the grid and not on 180°",
      len(pb) == 1 and pb[0]["twa"] == 60.0 and pb[0]["pct"] == 100)
check("fold_twa: 300→60, -45→45, 180→180, 200→160, None→None",
      T.fold_twa(300) == 60 and T.fold_twa(-45) == 45 and T.fold_twa(180) == 180
      and T.fold_twa(200) == 160 and T.fold_twa(None) is None)

print("2. a windowed recording is not clipped to the marks:")
FAR = [("Start", "start", 43.066, -82.4175), ("Finish", "finish", 45.77, -84.44)]   # the Mac course
r_win = T.score_track(mktrack(window=True), ORACLE, FAR, GUN, polars=POLAR)
check("with a window every fix is scored", r_win["fixes_scored"] == r_win["fixes_total"] == N)
r_gpx = T.score_track(mktrack(window=False), ORACLE, FAR, GUN, polars=POLAR)
check("without one the nearest-mark clip still applies (fewer fixes scored)",
      r_gpx["fixes_scored"] < N)

print("3. the archive keys on the recording, not the race id:")
tmp = tempfile.mkdtemp()
LN.LEARNING_DB = os.path.join(tmp, "learning.db")


def report(wstart, nbins, pct=95):
    at = {"available": True, "source": "boatlog", "polar_pct": pct,
          "window": ({"start_ts": wstart, "end_ts": wstart + 7200} if wstart else None),
          "perf_bins": [{"tws": 10.0, "twa": 60.0 + 10 * i, "point_of_sail": "reaching", "samples": 8,
                         "best_stw": 6.0, "target_stw": 6.0, "pct": pct, "pct_flat": pct,
                         "config": "J1", "wind_source": "measured"} for i in range(nbins)]}
    return {"available": True, "race_id": "bayview-mackinac-2026", "race_name": "Bayview",
            "regret": {}, "actual_track": at, "critique": {}}


d1 = LN.archive_debrief(report(T0, 3), "sr33")           # Jul 15 recording: 3 bins
d2 = LN.archive_debrief(report(GUN, 4), "sr33")          # Jul 18 recording: 4 bins
p = LN.propose("sr33")
check("both recordings' bins reach the proposal (3 + 4)", p.get("ok") and p["n_bins"] == 7)
check("the proposal counts two recordings of the one race", p["n_debriefs"] == 2
      and len(p["summary"]["recordings"]) == 2)
d3 = LN.archive_debrief(report(T0, 5), "sr33")           # Jul 15 re-run: supersedes ONLY itself
p2 = LN.propose("sr33")
check("a re-run supersedes only its own recording (5 + 4)", p2.get("ok") and p2["n_bins"] == 9)
d4 = LN.archive_debrief(report(None, 2), "sr33")         # a GPX track: no window → keyed on race
d5 = LN.archive_debrief(report(None, 6), "sr33")
p3 = LN.propose("sr33")
check("windowless (GPX) debriefs still key on the race alone (latest of them wins: 5 + 4 + 6)",
      p3.get("ok") and p3["n_bins"] == 15)
tr = LN.trend("sr33")
check("the trend series has one point per recording (Jul 15, Jul 18, the GPX)",
      len(tr.get("series") or []) == 3)
check("recording_key reads the window start and is None without a window",
      LN.recording_key({"window": {"start_ts": T0}}) == T0 and LN.recording_key({}) is None)

print("3b. a cell is judged by its samples, not by its bin count:")
LN.LEARNING_DB = os.path.join(tmp, "learning2.db")
mixed = report(T0, 0)
mixed["actual_track"]["perf_bins"] = [
    {"tws": 10.0, "twa": 120.0, "point_of_sail": "reaching", "samples": 117, "best_stw": 7.0,
     "target_stw": 7.6, "pct": 92, "pct_flat": 92, "config": "A3+J1", "wind_source": "measured"},
    {"tws": 10.0, "twa": 120.0, "point_of_sail": "reaching", "samples": 5, "best_stw": 4.4,
     "target_stw": 7.6, "pct": 59, "pct_flat": 59, "config": "S2", "wind_source": "measured"},
    {"tws": 10.0, "twa": 135.0, "point_of_sail": "downwind", "samples": 200, "best_stw": 7.0,
     "target_stw": 7.2, "pct": 97, "pct_flat": 97, "config": "A3+SS", "wind_source": "measured"},
    {"tws": 6.0, "twa": 142.0, "point_of_sail": "downwind", "samples": 4, "best_stw": 5.4,
     "target_stw": 4.7, "pct": 114, "pct_flat": 114, "config": "A3+J1", "wind_source": "measured"}]
LN.archive_debrief(mixed, "sr33")
pm = LN.propose("sr33")
cell = next((a for a in pm["adjustments"] if a["twa"] == 120.0), None)
check("10 kn/120° reads ~91% (sample-weighted), not the unweighted 76%",
      cell is not None and cell["cell_pct"] == 91)
check("the 4-sample cell is skipped and the skip is reported",
      not any(a["twa"] == 142.0 for a in pm["adjustments"]) and pm["summary"]["thin_cells_skipped"] == 1)

print("3c. a recording that predates the gun is said so:")
sc = J._score_actual_track(mktrack(window=True), ORACLE, MARKS, GUN)
check("the caveat names the gap in days and the bins stand",
      sc.get("predates_gun") is True and any("BEFORE the gun" in c for c in sc.get("caveats") or [])
      and len(sc.get("perf_bins") or []) >= 1)

print("4. the oracle wind of a past race comes from the archive:")
now = time.time()
check("a race two months ago → archive", J.wind_basis_for(now - 60 * 86400, now) == "archive")
check("a race two days ago → live", J.wind_basis_for(now - 2 * 86400, now) == "live")
check("a race next week → live", J.wind_basis_for(now + 7 * 86400, now) == "live")

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
