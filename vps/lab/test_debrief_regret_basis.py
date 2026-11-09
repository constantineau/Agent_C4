"""#244 / #182-3 — debrief regret measures the STRATEGY call only.

Cole decided (2026-11-09) that the hindsight-optimal (oracle) route must sail the same current and
wave fields the boat sailed in, so sea-state losses are not blamed on route choice. Before the fix
`judge._tactics` built the current and sea-state fields and then routed the oracle without them, on
the bare cert polar (no helm factor, no approved adjustments), while the frozen plan it is compared
with was routed WITH forecast current, waves and that boat model. Locked here, no network, no paid
calls (the Opus critique is never reached; the deterministic one is checked):

  1) the oracle is routed through the very current and wave fields the boat is scored on;
  2) ...with the same boat model the playbook routes on (helm, adjustments, wave calibration,
     jib bands) and the chart the bundle was frozen with;
  3) the fields really move the oracle (a fair current shortens it in the real optimizer);
  4) with no current/wave data for the race time, regret says so plainly (report, caveat,
     deterministic critique) instead of silently comparing different things.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
for _seed in (os.path.join(HERE, "..", "db", "seed"), "/srv"):
    if os.path.exists(os.path.join(_seed, "polars_sr33.sql")):
        os.environ["POLARS_FILE"] = os.path.join(_seed, "polars_sr33.sql")
        os.environ["SAIL_POLARS_FILE"] = os.path.join(_seed, "sr33_sail_polars.json")
        os.environ["CROSSOVERS_FILE"] = os.path.join(_seed, "sr33_crossovers.json")
        break
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import judge as JD                      # noqa: E402
from app import current as CURMOD, wave as WAVEMOD   # noqa: E402

JD.API_KEY = None

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class WF:
    loaded = True

    def __init__(self, tws=12.0, twd=270.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    def status(self):
        return {"models": []}


T0 = 1.75e9
NS = {"courses": [{"id": "x", "start": {"lat": 44.0, "lon": -82.0},
                   "finish": {"points": [{"lat": 44.1333, "lon": -82.0}]}}]}
PATH = [{"lat": 44.0, "lon": -82.0, "t": T0}, {"lat": 44.1333, "lon": -82.0, "t": T0 + 3600}]
ORACLE = {"available": True, "complete": True, "total_hours": 1.0, "path": PATH, "legs": []}
PB = {"course_id": "x", "start_epoch": T0, "recommended": "middle",
      "variants": [{"id": "middle", "total_hours": 1.25}],
      "chart": {"source": "enc", "safety_depth_m": 3.1}}
BOAT = {"boat_id": "c4", "helm_factor": 0.9,
        "polar_adjustments": [{"tws": 12, "twa": 90, "mult": 1.03}],
        "wave_coeffs": {"hs_deadband": 0.3, "k_up": 0.1, "k_reach": 0.05, "k_down": 0.02,
                        "floor": 0.6},
        "jib_crossovers": [{"sail": "J2", "tws_min": 14}]}

_saved = (JD._oracle_windfield, JD.optimizer.optimize_course, CURMOD.build_currentfield,
          WAVEMOD.build_wavefield)
_real_opt = JD.optimizer.optimize_course
calls = []


def run(cur, wv, boat=BOAT, pb=PB):
    calls.clear()
    CURMOD.build_currentfield = lambda *a, **k: cur
    WAVEMOD.build_wavefield = lambda *a, **k: wv
    return JD._tactics(NS, "r", pb, None, lambda *_: None, boat=boat)


try:
    JD._oracle_windfield = lambda *a, **k: (WF(), "archive")

    def _spy(*a, **k):
        calls.append(k)
        return dict(ORACLE)
    JD.optimizer.optimize_course = _spy

    # ---- 1 + 2) same fields, same boat --------------------------------------------------------
    print("1) the oracle sails the boat's current and waves")
    cur, wv = CURMOD.ConstantCurrent(0.0, 1.5), WAVEMOD.ConstantWave(1.2)
    tac = run(cur, wv)
    k = calls[-1] if calls else {}
    check("tactics available", tac.get("available") is True)
    check("oracle got the very current field the boat is scored on",
          k.get("cur") is cur and tac.get("cur") is cur)
    check("oracle got the very wave field the boat is scored on",
          k.get("waves") is wv and tac.get("wave") is wv)

    print("2) ...on the same boat model and chart the playbook routes on")
    check(f"helm factor 0.9 (got {k.get('helm_factor')})", k.get("helm_factor") == 0.9)
    check("approved polar adjustments", k.get("polar_adjustments") == BOAT["polar_adjustments"])
    check("approved wave calibration", k.get("wave_coeffs") == BOAT["wave_coeffs"])
    check("jib bands", k.get("jib_crossovers") == BOAT["jib_crossovers"])
    check("the chart the bundle was frozen with",
          k.get("source") == "enc" and k.get("safety_depth") == 3.1)
    b = tac["regret"].get("basis") or {}
    check("regret basis says current and waves are in",
          b.get("current") is True and b.get("waves") is True and not b.get("note"))
    check("no 'without' line when both are in", "without" not in tac.get("caveat", ""))
    check("regret is plan minus oracle (15 min)", tac["regret"]["minutes"] == 15)

    # an older bundle with no chart snapshot falls back to the optimizer's defaults
    run(cur, wv, pb={k_: v for k_, v in PB.items() if k_ != "chart"})
    check("no chart snapshot: optimizer defaults, not None forced in",
          "source" not in calls[-1] and "safety_depth" not in calls[-1])

    # ---- 4) no data: say so plainly ------------------------------------------------------------
    print("4) no current/wave data for the race time is said plainly")
    tac = run(CURMOD.ZeroCurrent(), WAVEMOD.ZeroWave())
    b = tac["regret"].get("basis") or {}
    print(f"     {b.get('note')}")
    check("basis marks both missing", b.get("current") is False and b.get("waves") is False)
    check("note: without current and waves", "without current and waves" in (b.get("note") or ""))
    check("the caveat carries it", "without current and waves" in tac.get("caveat", ""))
    tac_w = run(cur, WAVEMOD.ZeroWave())
    check("only waves missing → 'without waves'",
          (tac_w["regret"]["basis"].get("note") or "").startswith("Time lost is without waves"))

    report = {"tactics_available": True, "race_name": "Test", "regret": tac["regret"],
              "actual_track": {}}
    report["regret"]["side_matched"] = False
    report["regret"]["recommended_side"] = "left"
    crit = JD._deterministic_critique(report)
    check("deterministic critique says so in the assessment",
          "without current and waves" in crit["assessment"])
    check("...and in the Learnings bullet",
          "(without current and waves)" in crit["proposed_learnings"])
    full = {"tactics_available": True, "race_name": "Test",
            "regret": {**run(cur, wv)["regret"], "side_matched": False,
                       "recommended_side": "left"}, "actual_track": {}}
    check("no 'without' in the critique when both fields are in",
          "without" not in JD._deterministic_critique(full)["proposed_learnings"])

    # ---- 3) the fields really move the oracle ---------------------------------------------------
    print("3) a fair current shortens the real oracle")
    JD.optimizer.optimize_course = lambda *a, **k: _real_opt(
        *a, **{**k, "avoid": False, "emit_exploration": False,
               **{n: None for n in ("source", "safety_depth")}})
    still = run(CURMOD.ZeroCurrent(), WAVEMOD.ZeroWave(), boat={})
    fair = run(CURMOD.ConstantCurrent(0.0, 2.0), WAVEMOD.ZeroWave(), boat={})
    h0 = (still.get("oracle_summary") or {}).get("total_hours")
    h1 = (fair.get("oracle_summary") or {}).get("total_hours")
    print(f"     still water {h0} h, 2 kn fair current {h1} h")
    check("fair current makes the oracle faster", h0 and h1 and h1 < h0 - 0.05)
finally:
    (JD._oracle_windfield, JD.optimizer.optimize_course, CURMOD.build_currentfield,
     WAVEMOD.build_wavefield) = _saved

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
