"""The learning loop only learns from evidence it can stand behind (#49, #50, #63, #68).

Each block below failed on the code before the fix:

1. SOG standing in for a dead paddlewheel was labelled "measured" and taught the polar (#49).
2. Binning picked a cell by |ΔTWS kn| + |ΔTWA °| and graded each sample against the cell corner,
   so a boat sailing exactly on the polar read 112% at 4 kn (#63).
3. The latest run per recording came from perf_bins, so a re-run with zero bins brought the old
   bins back (#49). The wave fit took forecast cells (#49). trend() kept the oldest 50 (#49).
   config_polars read every superseded and forecast run (#49).
4. A stitched race (two sessions, one derived window) was counted twice in the Polar tab (#49).
5. A debrief with no playbook had no current/wave fields (#68), and a recording from another day
   was scored on the race day's fields (#49).
6. The Recordings table joined on the marker, not the derived window, and a GPX debrief never
   showed as debriefed (#50).

Run in the CI image:
  docker run --rm --network none -v "$PWD:/repo:ro" -w /srv -e PYTHONPATH=/repo/vps/lab:/repo \
    -e C4_CI=1 sr33-ci-lab python3 /repo/vps/lab/test_lab_learning_evidence.py
"""
import os
import sqlite3
import tempfile

from shared import polar as SPOL
from app import judge as J
from app import learning as LN
from app import monitor
from app import obspolar as OP
from app import polars as POL
from app import track as T

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1784154000.0
tmp = tempfile.mkdtemp()
LN.LEARNING_DB = os.path.join(tmp, "learning.db")

# ---- 1) SOG fallback is inferred, never measured ------------------------------------------------
print("1) a dead paddlewheel: SOG stands in, labelled inferred")
P1 = [(10.0, 120.0, 7.59), (10.0, 135.0, 7.17), (10.0, 150.0, 6.36), (10.0, 60.0, 6.5)]
f = {"t": T0, "lat": 45.0, "lon": -83.0, "sog": 6.8, "cog": 0.0, "tws": 10.0, "twa": 120.0}
w = T._fix_wind(f, T0, None, None)
check(f"no STW on the fix -> SOG with source 'inferred' (got {w and w[3]})",
      w is not None and w[2] == 6.8 and w[3] == "inferred")
seg = [{**f, "t": T0 + 60 * i, "lat": 45.0 + i * 1e-3} for i in range(30)]
eps = [x["t"] for x in seg]
pol = T._polar_pct(seg, eps, None, P1)
check(f"polar % over SOG-only fixes is not called measured (got {pol and pol['wind_source']})",
      pol is not None and pol["wind_source"] == "inferred")
bins = T._performance_bins(seg, eps, None, P1)
check("its bins are tagged inferred", bins and all(b["wind_source"] == "inferred" for b in bins))
mixed = seg[:15] + [{**x, "stw": 6.8} for x in seg[15:]]
bins_m = T._performance_bins(mixed, eps, None, P1)
check("one SOG sample makes the cell 'mixed', not measured",
      bins_m and all(b["wind_source"] == "mixed" for b in bins_m))
LN.archive_debrief({"available": True, "race_id": "sog", "race_name": "SOG", "regret": {},
                    "actual_track": {"available": True, "source": "boatlog",
                                     "window": {"start_ts": T0}, "perf_bins": bins}}, "b1")
pr = LN.propose("b1")
check(f"propose() leaves inferred cells out and says so ({pr.get('note')})",
      pr["ok"] is False and "1 from GPS speed" in pr["note"])

# ---- 2) #63 binning -------------------------------------------------------------------------
print("2) binning: nearest TWS row first, each sample graded at its own wind")
GRID = [(6.0, 60.0, 5.0), (8.0, 45.0, 5.5), (8.0, 75.0, 6.5)]
near = [{"t": T0 + i, "lat": 45.0, "lon": -83.0, "sog": 5.8, "cog": 0.0,
         "tws": 7.6, "twa": 60.0, "stw": 5.8} for i in range(10)]
gb = T._performance_bins(near, [x["t"] for x in near], None, GRID)
check(f"7.6 kn / 60° goes to the 8 kn row, not the 6 kn row a degree-sum picks "
      f"(got {[(b['tws'], b['twa']) for b in gb]})", gb and all(b["tws"] == 8.0 for b in gb))
P = POL.polars_stw()
on_polar = []
for i in range(400):
    tws = 4.0 + 5.0 * ((i * 37) % 400) / 400.0
    twa = 85.0 + 10.0 * ((i * 91) % 400) / 400.0
    stw = SPOL.target(P, tws, twa)
    on_polar.append({"t": T0 + i, "lat": 45.0, "lon": -83.0, "sog": stw, "cog": 0.0,
                     "tws": tws, "twa": twa, "stw": stw})
ob = T._performance_bins(on_polar, [x["t"] for x in on_polar], None, P)
pcts = {(b["tws"], b["twa"]): b["pct"] for b in ob}
print(f"     cells: {pcts}")
check("a boat sailing exactly on the polar reads 100% in every cell (was 112% at 4 kn)",
      ob and all(b["pct"] == 100 for b in ob))
check("best_stw / target_stw agrees with pct",
      all(abs(100 * b["best_stw"] / b["target_stw"] - b["pct"]) <= 1 for b in ob))

# ---- 3) learning archive ------------------------------------------------------------------
print("3) latest run per recording, wave fit provenance, trend, config polars")


def archive(boat, rid, rec, bins, src="boatlog"):
    return LN.archive_debrief({"available": True, "race_id": rid, "race_name": rid, "regret": {},
                               "actual_track": {"available": True, "source": src,
                                                "window": {"start_ts": rec}, "perf_bins": bins}},
                              boat)


def mkbin(tws, twa, pct, ws="measured", hs=None, cfg="J1", pos="upwind"):
    return {"tws": tws, "twa": twa, "point_of_sail": pos, "samples": 50, "best_stw": 6.0,
            "target_stw": 6.0, "pct": pct, "pct_flat": pct, "hs_mean": hs, "config": cfg,
            "wind_source": ws}


archive("b2", "r", T0, [mkbin(10.0, 60.0, 90)])
archive("b2", "r", T0, [])                      # a re-run that yields no bins
src = LN.bin_sources("b2")
check(f"a re-run with zero bins retires the old run's bins (sources: {len(src)})", src == [])
check("...and propose() finds nothing", LN.propose("b2")["ok"] is False)

# wave fit: measured cells flat, forecast cells with a steep slope. Only the measured ones count.
wb = [mkbin(10.0, 60.0, 95, hs=h) for h in (0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.6, 2.0)]
wb += [mkbin(12.0, 60.0, 100 - 20 * h, ws="grib", hs=h) for h in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)]
archive("b3", "w", T0, wb)
cw = LN.calibrate_waves("b3")
up = ((cw.get("summary") or cw).get("by_point_of_sail") or {}).get("upwind") or {}
check(f"the wave fit uses measured cells only ({up.get('n_cells')} cells, "
      f"{(cw.get('summary') or cw).get('excluded_not_measured')} left out)",
      up.get("n_cells") == 8 and (cw.get("summary") or cw).get("excluded_not_measured") == 6)
only_grib = [mkbin(12.0, 60.0, 100 - 20 * h, ws="grib", hs=h) for h in (0.5, 1.0, 2.0, 3.0)]
archive("b4", "w", T0, only_grib)
cw4 = LN.calibrate_waves("b4")
check("forecast-only cells propose no wave coefficients", cw4["ok"] is False)

for i in range(5):
    archive("b5", f"race{i}", T0 + i * 86400, [mkbin(10.0, 60.0, 90)])
tr = LN.trend("b5", limit=3)
check(f"trend keeps the NEWEST races, oldest first ({[s['race_id'] for s in tr['series']]})",
      [s["race_id"] for s in tr["series"]] == ["race2", "race3", "race4"])

archive("b6", "c", T0, [mkbin(10.0, 60.0, 90, cfg="A3+SS")])
archive("b6", "c", T0, [mkbin(10.0, 60.0, 70, cfg="A3+SS"),
                        mkbin(12.0, 60.0, 80, cfg="C0", ws="grib")])
cp = LN.config_polars("b6")
by = {g["config"]: g for g in cp["configs"]}
check("config polars read only the latest run, measured cells only",
      set(by) == {"A3+SS"} and by["A3+SS"]["cells"][0]["pct"] == 70
      and by["A3+SS"]["samples"] == 50)

# ---- 4) obspolar: a stitched race counts once ------------------------------------------------
print("4) Polar tab: two sessions sharing one derived window count once")
WIN = {"start_ts": T0, "end_ts": T0 + 49, "hours": 0.01, "provenance": []}
SESS = {"sessions": [{"id": 1, "name": "leg A", "race_id": "x", "window": dict(WIN)},
                     {"id": 2, "name": "leg B", "race_id": "x", "window": dict(WIN)}]}
FIX = [{"t": T0 + i, "lat": 45.0, "lon": -83.0, "tws": 10.0, "twa": 90.0, "stw": 6.0}
       for i in range(50)]


def fake_agent(path, timeout=None):
    if path.startswith("/racelog/sessions"):
        return SESS
    if path.startswith("/racelog/track"):
        return {"fixes": FIX, "sail_log": []}
    return {"danger": {}, "summary": {"line": "ok", "danger_s": 0}}


_ag = monitor.agent_json
monitor.agent_json = fake_agent
try:
    art = OP.compute(min_seconds=60)
finally:
    monitor.agent_json = _ag
listed = [r for r in art["races"] if not r.get("skipped")]
check(f"the race is listed once ({len(listed)} rows), naming the other session",
      len(listed) == 1 and listed[0].get("also_sessions") == ["leg B"])
check("50 seconds stays 50 seconds: under the 60-second gate, no cell published",
      art["cells"] == [] and art["thin"]["cells"] == 1)

# ---- 5) judge: scoring fields cover the recording's own time ---------------------------------
print("5) debrief: current and sea state over the recording's own time")
calls = []
from app import current as CURMOD, wave as WAVEMOD   # noqa: E402
_saved = (CURMOD.build_currentfield, WAVEMOD.build_wavefield)
CURMOD.build_currentfield = lambda bbox, a, b, on_progress=None: calls.append(("cur", a, b)) or "CUR"
WAVEMOD.build_wavefield = lambda bbox, a, b, on_progress=None: calls.append(("wave", a, b)) or "WAVE"
try:
    rec = {"fixes": [{"t": T0 + 60 * i, "lat": 45.0, "lon": -83.0} for i in range(10)],
           "window": {"start_ts": T0, "end_ts": T0 + 540}}
    cur, wv = J._own_speed_fields(rec, {"available": False}, None, T0, lambda *_: None)
    check("no playbook: fields are built, over the recording's span (#68)",
          (cur, wv) == ("CUR", "WAVE") and calls == [("cur", T0, T0 + 540), ("wave", T0, T0 + 540)])
    calls.clear()
    GUN = T0 + 3 * 86400
    tac = {"cur": "RACE_CUR", "wave": "RACE_WAVE"}
    cur, wv = J._own_speed_fields(rec, tac, (GUN, GUN + 36000), GUN, lambda *_: None)
    check("a recording days before the gun gets its own fields, not race day's (#49)",
          (cur, wv) == ("CUR", "WAVE") and calls and calls[0][1] == T0)
    calls.clear()
    inside = {**rec, "window": {"start_ts": GUN + 60, "end_ts": GUN + 3600}}
    cur, wv = J._own_speed_fields(inside, tac, (GUN, GUN + 36000), GUN, lambda *_: None)
    check("a recording inside the gun window reuses the race fields (no second fetch)",
          (cur, wv) == ("RACE_CUR", "RACE_WAVE") and calls == [])
    gpx = {"fixes": [{"t": T0 + 60 * i, "lat": 45.0, "lon": -83.0} for i in range(10)]}
    calls.clear()
    J._own_speed_fields(gpx, {"available": False}, None, 0, lambda *_: None)
    check("a GPX with a wall clock uses its first..last fix", calls and calls[0][1:] == (T0, T0 + 540))
finally:
    CURMOD.build_currentfield, WAVEMOD.build_wavefield = _saved

# ---- 6) Recordings table joins -----------------------------------------------------------------
print("6) Recordings table: join on the derived window; GPX debriefs show")
from app import main   # noqa: E402
RID = "rj"
MARK_B = T0 + 4000                      # the later session of a stitched pair
SS = {"sessions": [{"id": 1, "name": "A", "race_id": RID, "start_ts": T0, "end_ts": T0 + 3000,
                    "window": {"start_ts": T0, "end_ts": T0 + 9000, "hours": 2.5}},
                   {"id": 2, "name": "B", "race_id": RID, "start_ts": MARK_B, "end_ts": T0 + 9000,
                    "window": {"start_ts": T0, "end_ts": T0 + 9000, "hours": 2.5}}]}
_lt, _ld, _ag = main.track.list_tracks, main.learning.list_debriefs, monitor.agent_json
_ab = main.boats.active_boat
main.track.list_tracks = lambda rid: [{"recording": T0, "source": "boatlog", "window": {}},
                                      {"recording": None, "source": "gpx", "window": {}}]
main.learning.list_debriefs = lambda bid, rid: [
    {"id": 1, "window_start": T0, "created_at": 1.0},
    {"id": 2, "window_start": None, "created_at": 2.0}]
monitor.agent_json = lambda path, timeout=None: SS
main.boats.active_boat = lambda: {"boat_id": None}
try:
    rows = main._recordings(RID)["recordings"]
finally:
    main.track.list_tracks, main.learning.list_debriefs, monitor.agent_json = _lt, _ld, _ag
    main.boats.active_boat = _ab
b = next((r for r in rows if r["session_id"] == 2), {})
check("session B shows its track and debrief (keyed on the derived window)",
      b.get("track") and (b.get("debrief") or {}).get("id") == 1)
check("...keeps its marker for the debrief button", b.get("start_ts") == MARK_B)
check("no orphan row for the boat-log track", not any(
    r["session_id"] is None and r.get("kind") == "boatlog" for r in rows))
g = next((r for r in rows if r.get("kind") == "gpx"), {})
check("the GPX track shows its archived debrief", (g.get("debrief") or {}).get("id") == 2)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
