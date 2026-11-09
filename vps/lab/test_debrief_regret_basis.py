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
     deterministic critique) instead of silently comparing different things;
  5) the boat model is the PLAN's: a new bundle saves its routing inputs, the oracle uses them
     even after the boat's settings change, and an older bundle says it used today's boat.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLAYBOOK_DIR"] = os.path.join(_tmp, "playbooks")
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
ROUTING = {"helm_factor": 0.9,
           "polar_adjustments": [{"tws": 12, "twa": 90, "mult": 1.03}],
           "wave_coeffs": {"hs_deadband": 0.3, "k_up": 0.1, "k_reach": 0.05, "k_down": 0.02,
                           "floor": 0.6},
           "jib_crossovers": [{"sail": "J2", "tws_min": 14}],
           "sail_config": {"code0": {"enabled": True}}}
PB["routing"] = ROUTING
# the boat as it is at debrief time: settings changed since the plan was frozen
BOAT = {"boat_id": "c4", "helm_factor": 0.7, "polar_adjustments": [],
        "jib_crossovers": [{"sail": "J3", "tws_min": 18}]}

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

    print("2) ...on the plan's boat model and chart, not today's boat")
    check(f"the plan's helm factor 0.9, not today's 0.7 (got {k.get('helm_factor')})",
          k.get("helm_factor") == 0.9)
    check("the plan's polar adjustments", k.get("polar_adjustments") == ROUTING["polar_adjustments"])
    check("the plan's wave calibration", k.get("wave_coeffs") == ROUTING["wave_coeffs"])
    check("the plan's jib bands", k.get("jib_crossovers") == ROUTING["jib_crossovers"])
    check("the plan's sail config", k.get("sail_config") == ROUTING["sail_config"])
    check("the chart the bundle was frozen with",
          k.get("source") == "enc" and k.get("safety_depth") == 3.1)
    b = tac["regret"].get("basis") or {}
    check("regret basis says current, waves and the plan's boat are in",
          b.get("current") is True and b.get("waves") is True and b.get("boat") == "plan"
          and not b.get("note"))
    check("no 'without' line when both are in", "without" not in tac.get("caveat", ""))
    check("regret is plan minus oracle (15 min)", tac["regret"]["minutes"] == 15)

    # an older bundle with no chart snapshot falls back to the optimizer's defaults
    run(cur, wv, pb={k_: v for k_, v in PB.items() if k_ != "chart"})
    check("no chart snapshot: optimizer defaults, not None forced in",
          "source" not in calls[-1] and "safety_depth" not in calls[-1])

    print("5) an older bundle with no saved routing uses today's boat, and says so")
    old = run(cur, wv, pb={k_: v for k_, v in PB.items() if k_ != "routing"})
    k = calls[-1]
    ob = old["regret"]["basis"]
    check("today's helm factor 0.7", k.get("helm_factor") == 0.7)
    check("today's jib bands", k.get("jib_crossovers") == BOAT["jib_crossovers"])
    print(f"     {ob.get('note')}")
    check("basis says the boat is today's, with a plain note",
          ob.get("boat") == "now" and "today's" in (ob.get("note") or ""))
    oldrep = {"tactics_available": True, "race_name": "Test",
              "regret": {**old["regret"], "side_matched": False, "recommended_side": "left"},
              "actual_track": {}}
    _pl = JD._deterministic_critique(oldrep)["proposed_learnings"]
    print(f"     {_pl}")
    check("...and the Learnings bullet doesn't print an empty 'without ()'", "without" not in _pl)
    check("...but keeps the today's-boat qualifier with the regret number",
          "(today's boat settings)." in _pl)

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
    bare = {**PB, "routing": {}}
    still = run(CURMOD.ZeroCurrent(), WAVEMOD.ZeroWave(), boat={}, pb=bare)
    fair = run(CURMOD.ConstantCurrent(0.0, 2.0), WAVEMOD.ZeroWave(), boat={}, pb=bare)
    h0 = (still.get("oracle_summary") or {}).get("total_hours")
    h1 = (fair.get("oracle_summary") or {}).get("total_hours")
    print(f"     still water {h0} h, 2 kn fair current {h1} h")
    check("fair current makes the oracle faster", h0 and h1 and h1 < h0 - 0.05)
finally:
    (JD._oracle_windfield, JD.optimizer.optimize_course, CURMOD.build_currentfield,
     WAVEMOD.build_wavefield) = _saved

# ---- 6) a flat-water plan is compared with a flat-water oracle -----------------------------------
print("6) a plan built without waves gets an oracle without waves, and says so")
try:
    JD._oracle_windfield = lambda *a, **k: (WF(), "archive")
    JD.optimizer.optimize_course = _spy
    sea = WAVEMOD.ConstantWave(1.5)
    t6 = run(CURMOD.ConstantCurrent(0.0, 1.0), sea,
             pb={**PB, "routing": {**ROUTING, "use_waves": False}})
    b6 = t6["regret"]["basis"]
    print(f"     {b6.get('note')}")
    check("oracle routed on flat water", not getattr(calls[-1].get("waves"), "loaded", True))
    check("the boat's own speed still gets the real sea state", t6.get("wave") is sea)
    check("basis: waves out, with the reason",
          b6.get("waves") is False and "plan didn't use them" in (b6.get("note") or "")
          and "no data" not in (b6.get("note") or ""))
finally:
    (JD._oracle_windfield, JD.optimizer.optimize_course, CURMOD.build_currentfield,
     WAVEMOD.build_wavefield) = _saved

# ---- 5b) a new bundle saves the inputs it was routed with -------------------------------------
print("5b) synthesis saves the routing inputs in the bundle")
from app import synthesis as SYN   # noqa: E402
SYN.API_KEY = None
seen = {}


def fake_build(*a, **k):
    seen.update(k)
    return {"available": True, "course_id": "c", "start_epoch": 100, "variants": [],
            "consensus": {}, "windfield": {"models": []}}


SYN.pb.build_playbook = fake_build
SYN.race_def.course_hash = lambda *a, **k: "h"
SYN._boat_model = lambda: {"boat_id": "c4"}
SYN._opus_synthesis = lambda *a, **k: None
SYN._deterministic_synthesis = lambda *a, **k: {"variants": {}, "recommended": None,
                                                "decision_tree": [], "headline": ""}
SYN._venue_stats = lambda *a, **k: None
SYN._build_plays = lambda *a, **k: ([], None, None)
SYN._buoy_stations = lambda *a, **k: []
SYN._fingerprint = lambda *a, **k: None
SYN.first_beat_rhumb = lambda *a, **k: None
bnd = SYN.synthesize({"race_id": "r1", "name": "Race"}, "c", 100, ["gfs"], use_waves=False,
                     **ROUTING)
rt = bnd.get("routing") or {}
check("bundle.routing holds exactly what the routes were built with",
      all(rt.get(n) == ROUTING[n] == seen.get(n) for n in ROUTING)
      and rt.get("use_waves") is False and seen.get("use_waves") is False)
rt = {**rt, "use_waves": True}
JD.optimizer.optimize_course = lambda *a, **k: calls.append(k) or dict(ORACLE)
JD._oracle_windfield = lambda *a, **k: (WF(), "archive")
try:
    calls.clear()
    CURMOD.build_currentfield = lambda *a, **k: CURMOD.ConstantCurrent(0.0, 1.0)
    WAVEMOD.build_wavefield = lambda *a, **k: WAVEMOD.ConstantWave(1.0)
    t5 = JD._tactics(NS, "r", {**PB, "routing": rt}, None, lambda *_: None, boat=BOAT)
    check("the debrief's oracle reads it back (helm 0.9 from the bundle)",
          calls and calls[-1].get("helm_factor") == 0.9 and t5["regret"]["basis"]["boat"] == "plan")
finally:
    (JD._oracle_windfield, JD.optimizer.optimize_course, CURMOD.build_currentfield,
     WAVEMOD.build_wavefield) = _saved

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
