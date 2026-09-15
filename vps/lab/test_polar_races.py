"""The observed polar, per RACE INSTANCE — and choosing which races teach the optimizer.

Cole, 2026-09-15: "I'd like the polars page to allow us to show polars from each race instance,
as a filter... radio buttons to show race instances on the polar plot... and be able to
selectively pick which races are to be used to update the polars for the optimizer."

What must hold, and what would silently be wrong if it didn't:

1. **Every view is a p80 over ITS OWN samples, re-pooled from the raw record.** A percentile does
   not average: if the pooled polar were built by combining per-race p80s it would report a speed
   the boat never sailed. The test constructs a case where the pooled p80 (6.0) is nothing like
   the mean of the per-race p80s (7.5) and pins the pooled value.
2. **A race instance is the same key everywhere** — the window start the debrief, the stored
   track and the learning archive already use. A polar filtered to "the Jul 18 race" and a
   proposal restricted to it must mean the same race.
3. **The selection is honest about what it left out.** `propose(recordings=[...])` counts the
   excluded bins and refuses loudly when the chosen races have nothing archived.

Run in-container:
  docker run --rm -v $PWD/vps/lab/app:/srv/app:ro -v $PWD/shared:/srv/shared:ro \
    -v $PWD/vps/lab/test_polar_races.py:/srv/test_polar_races.py:ro \
    -w /srv sr33-dev-lab python test_polar_races.py
"""
import os
import tempfile

from app import learning as LN
from app import monitor
from app import obspolar as OP

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


A, B = 1784155457.9, 1784394211.4          # the two race instances (window starts)


def speeds(race):
    """Race A: 40 samples all 6.0 kn. Race B: 32 at 5.0 then 8 at 9.0 — so B's own p80 is 9.0
    while the two races POOLED sit at 6.0. Averaging the p80s would say 7.5."""
    return [6.0] * 40 if race == "A" else [5.0] * 32 + [9.0] * 8


SESSIONS = {"sessions": [
    {"id": 1, "name": "Race A", "race_id": "r", "window": {"start_ts": A, "end_ts": A + 3600, "hours": 1.0}},
    {"id": 2, "name": "Race B", "race_id": "r", "window": {"start_ts": B, "end_ts": B + 3600, "hours": 1.0}},
    {"id": 3, "name": "Bench", "race_id": "r",
     "window": {"start_ts": 5.0, "end_ts": 9.0, "hours": 0.5,
                "provenance": ["window derived from an unverified source"]}}]}


def fake_agent(path, timeout=None):
    if path.startswith("/racelog/sessions"):
        return SESSIONS
    start = float(path.split("start=")[1].split("&")[0])
    race = "A" if abs(start - A) < 1 else "B"
    return ({"fixes": [{"t": start + i, "tws": 10.0, "twa": 270.0, "stw": v}      # 270 folds to 90
                       for i, v in enumerate(speeds(race))],
             "sail_log": [{"t": start - 10, "flying": ["J1"]}]}
            if path.startswith("/racelog/track")
            else {"danger": {}, "summary": {"line": "no danger windows"}})


monitor.agent_json = fake_agent

print("1. one pass, four views, every one a true p80:")
art = OP.compute(min_samples=30)
pooled = [c for c in art["cells"] if c["tws"] == 10.0 and c["twa"] == 90.0]
check("the pooled cell is the p80 of all 80 samples (6.0), not the mean of the two races' p80s (7.5)",
      len(pooled) == 1 and pooled[0]["stw"] == 6.0 and pooled[0]["samples"] == 80)
per = {r["recording"]: r for r in art["race_cells"] if r["twa"] == 90.0}
check("each race instance keeps its own p80 (A 6.0, B 9.0)",
      per[A]["stw"] == 6.0 and per[B]["stw"] == 9.0 and per[A]["samples"] == 40)
curves = {r["recording"]: r for r in art["race_curves"] if r["twa"] == 90.0}
check("the sail-agnostic race curve exists for each instance and carries no config",
      set(curves) == {A, B} and "config" not in curves[A])
check("...and the pooled sail-agnostic curve agrees with the pooled cell here (one config)",
      [c for c in art["curve"] if c["twa"] == 90.0][0]["stw"] == 6.0)
check("cells are attributed to the sail that was flying", pooled[0]["config"] == "J1")
check("a wrapped TWA (270°) is folded onto the polar's half-circle", pooled[0]["twa"] == 90.0)

head_to_wind = OP.compute.__globals__["MIN_TWA_DEG"]
SESSIONS["sessions"].append({"id": 4, "name": "Race C", "race_id": "r",
                             "window": {"start_ts": 900.0, "end_ts": 4500.0, "hours": 1.0}})
_prev = fake_agent


def with_motoring(path, timeout=None):
    if path.startswith("/racelog/track") and "start=900" in path:
        return {"fixes": [{"t": 900 + i, "tws": 10.0, "twa": 8.0, "stw": 5.5} for i in range(40)],
                "sail_log": []}
    return _prev(path, timeout)


monitor.agent_json = with_motoring
art2 = OP.compute(min_samples=30)
monitor.agent_json = _prev
c = next(m for m in art2["races"] if m["name"] == "Race C")
check(f"an hour head to wind ({head_to_wind}° floor) makes no polar cells and is COUNTED, not dropped",
      c["cells"] == 0 and c["refused_below_min_twa"] == 40 and c["measured_samples"] == 0
      and not any(r["recording"] == 900.0 for r in art2["race_cells"]))

print("\n2. a race instance is the same key everywhere:")
meta = {m.get("name"): m for m in art["races"]}
check("every race carries its recording = the window start the debrief uses",
      meta["Race A"]["recording"] == A and meta["Race B"]["recording"] == B)
check("...and how many cells it contributed", meta["Race A"]["cells"] == 1)
check("a session the record can't attribute to this boat is skipped and said, not silently dropped",
      meta["Bench"].get("skipped") and "cells" not in meta["Bench"])
check("the skipped session contributes nothing to any view",
      not any(r["recording"] == 5.0 for r in art["race_cells"]))

print("\n3. picking which races teach the optimizer:")
LN.LEARNING_DB = os.path.join(tempfile.mkdtemp(), "learning.db")


def archive(rec, n, pct):
    return LN.archive_debrief({"available": True, "race_id": "r", "race_name": "Race", "regret": {},
        "actual_track": {"available": True, "polar_pct": pct, "window": {"start_ts": rec},
        "perf_bins": [{"tws": 10.0, "twa": 60.0 + 10 * i, "point_of_sail": "reaching", "samples": 40,
                       "best_stw": 6.0, "target_stw": 6.0, "pct": pct, "pct_flat": pct,
                       "config": "J1", "wind_source": "measured"} for i in range(n)]}}, "sr33")


archive(A, 3, 88)
archive(B, 5, 97)
src = LN.bin_sources("sr33")
check("both race instances appear as sources, newest first, with what each brings",
      [x["recording"] for x in src] == [B, A] and src[0]["measured_bins"] == 5
      and src[0]["samples"] == 200 and src[1]["measured_bins"] == 3)
everything = LN.propose("sr33")
check("with no selection every recording still teaches (unchanged default)",
      everything["n_bins"] == 8 and everything["summary"]["excluded_by_choice"] == 0
      and everything["summary"]["recordings_selected"] is None)
just_b = LN.propose("sr33", [B])
check("selecting one race uses only its bins", just_b["n_bins"] == 5 and just_b["n_debriefs"] == 1)
check("...and the proposal COUNTS what the choice left out",
      just_b["summary"]["excluded_by_choice"] == 3 and just_b["summary"]["bins_offered"] == 8
      and just_b["summary"]["recordings_selected"] == [B])
check("the level follows the races chosen (B alone 97%, both together 93.6%)",
      just_b["overall_pct"] == 97.0 and everything["overall_pct"] == 93.6)
none = LN.propose("sr33", [1.0])
check("a selection with nothing archived refuses loudly and says what IS available",
      none["ok"] is False and "none of the selected races" in none["note"]
      and "2 recording(s)" in none["note"])
check("selects() matches on a tolerance and treats null as the uploaded-track slot",
      LN.selects(B, [B + 0.4]) and not LN.selects(B, [B + 3]) and LN.selects(None, [None])
      and not LN.selects(None, [B]) and not LN.selects(B, [None]))

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
