"""Playbook build minors (#182-1, -2, -8).

Pins:
  * the scenario fan's point-of-sail profile buckets the router's own beat/reach/run legs (not
    everything as reaching), so a downwind-heavy race puts the pressure scenarios first (#182-1);
  * a long play route is kept whole when the obstacle build failed, never thinned with no land
    check (#182-2);
  * the bundle's boat model is the boat active when the job STARTED, not the one active when
    routing finished (#182-8).

No network, no LLM. Run in-container (make ci): python3 /repo/vps/lab/test_playbook_build.py
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["PLAYBOOK_DIR"] = os.path.join(_tmp, "playbooks")

from app import scenarios as scen, synthesis as SYN  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# ---- #182-1 ------------------------------------------------------------------------------------
print("1) point-of-sail profile from the router's legs")
nominal = {"legs": [{"leg_minutes": 60, "point_of_sail": "beat"},
                    {"leg_minutes": 300, "point_of_sail": "run"},
                    {"leg_minutes": 40, "point_of_sail": "reach"}]}
prof = scen.pos_profile(nominal)
check("beat/reach/run land in upwind/reaching/downwind",
      prof == {"upwind": 0.15, "reaching": 0.1, "downwind": 0.75})
check("the older upwind/downwind names still bucket",
      scen.pos_profile({"legs": [{"leg_minutes": 10, "point_of_sail": "downwind"}]})["downwind"] == 1.0)
check("a downwind-heavy race leads with the pressure scenarios",
      scen.select(prof)[0]["kind"] == "tws_scale")

# ---- #182-2 ------------------------------------------------------------------------------------
print("2) no obstacle field → the play route is not thinned blind")
SYN.API_KEY = None
SYN._play_obstacles = lambda *a, **k: (None, [])          # the build failed
path = [{"lat": 43.0 + i * 0.001, "lon": -82.0, "t": 1000.0 + i} for i in range(500)]
entry = {"id": "low_maneuver", "name": "Low-maneuver", "kind": "low_maneuver",
         "category": "internal", "params": {"prune_mult": 4.0, "maneuvers": 5,
                                            "nominal_maneuvers": 9},
         "narrative_seed": "tired crew", "divergence": {"delta_eta_min": 12},
         "route": {"legs": [], "path": path, "leg_end_idx": [499], "sail_plan": []}}
fake_pb = {"v2": {"scenario_routes": [entry], "robustness": [], "corridor": {}, "pos_profile": {}},
           "consensus": {"legs": []}}
built, _v2, _note = SYN._build_plays(fake_pb, {"name": "Test"}, "x")
r = built[0]["response"]["route"]
check("the full path ships", len(r["path"]) == 500 and r["leg_end_idx"] == [499])


class Open:
    def crosses(self, *a):
        return False


SYN._play_obstacles = lambda *a, **k: (Open(), [Open()])
built, _v2, _note = SYN._build_plays(fake_pb, {"name": "Test"}, "x")
check("with a field it still thins to the budget", len(built[0]["response"]["route"]["path"]) <= 200)

# ---- #182-8 ------------------------------------------------------------------------------------
print("3) the boat model is the boat the job started on")
active = {"boat": "sr33"}
SYN._boat_model = lambda: {"boat_id": active["boat"]}


def fake_build(*a, **k):
    active["boat"] = "other"                                # someone switches boats mid-run
    return {"available": True, "course_id": "c", "start_epoch": 100, "variants": [],
            "consensus": {}, "windfield": {"models": []}}


SYN.pb.build_playbook = fake_build
SYN.race_def.course_hash = lambda *a, **k: "h"
SYN.race_def.course_obstacles = lambda *a, **k: []
SYN._opus_synthesis = lambda *a, **k: None
SYN._deterministic_synthesis = lambda *a, **k: {"variants": {}, "recommended": None,
                                                "decision_tree": [], "headline": ""}
SYN._venue_stats = lambda *a, **k: None
SYN._build_plays = lambda *a, **k: ([], None, None)
SYN._buoy_stations = lambda *a, **k: []
SYN._fingerprint = lambda *a, **k: None
SYN.first_beat_rhumb = lambda *a, **k: None
b = SYN.synthesize({"race_id": "r1", "name": "Race"}, "c", 100, ["gfs"])
check("boat switched mid-run → the bundle keeps the starting boat",
      b["boat_model"]["boat_id"] == "sr33")

print("\nALL OK" if ok else "\nFAILURES")
raise SystemExit(0 if ok else 1)
