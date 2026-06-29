"""Mark-approach scramble fix — DMG / cross-track-lane isochrone prune + decoupled tack regularizer.

The legacy isochrone pruned by distance-FROM-START bucketed by bearing-from-start, which rewards
sailing sideways (oversail) and lets BOTH tacks survive every generation (the upwind "staircase" /
mark-approach scramble). The fix ranks candidates by distance MADE GOOD toward the mark, buckets by
CROSS-TRACK LANE, and uses a large prune-only tack penalty (decoupled from the realistic ETA cost) so
a steady beat converges to ONE tack to the layline — while genuine shifts still pay enough to tack.

Deterministic, no network. Run: docker compose exec -w /srv lab python test_routing_scramble.py
"""
import os, math, importlib
from app import optimizer as OPT

ok = True
def check(name, cond, extra=""):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  — ' + extra) if extra else ''}")

def cfg(dmg):
    os.environ.update({"ROUTE_LAYLINE_COMMIT": "1", "ROUTE_TACK_CUMULATIVE": "1",
                       "ROUTE_MARK_POS_PRUNE": "1", "ROUTE_LAYLINE_GATE": "1",
                       "ROUTE_DMG_PRUNE": "1" if dmg else "0"})
    os.environ.pop("ROUTE_TACK_PRUNE_S", None)   # use the shipped default
    return importlib.reload(OPT)

rows = {30: 2.8, 40: 4.2, 42: 4.4, 50: 4.9, 60: 5.3, 75: 5.7, 90: 5.9,
        110: 5.8, 135: 5.2, 150: 4.6, 165: 4.0, 180: 3.2}
P = [(8.5, a, s) for a, s in rows.items()]

class Steady:
    def wind_at(self, la, lo, t): return (8.5, 0.0)
    def detail_at(self, la, lo, t): return {"tws": 8.5, "twd": 0.0, "confidence": 0.9}
class Osc:    # smooth ±15° oscillation along the beat — genuine shifts to tack on
    def _w(self, la): return (8.5, 15.0 * math.sin((la - 44.0) * 60 * 0.9))
    def wind_at(self, la, lo, t): return self._w(la)
    def detail_at(self, la, lo, t): tws, twd = self._w(la); return {"tws": tws, "twd": twd % 360, "confidence": 0.6}

S, LON = 44.0, -82.0
def board_flips(O, leg, W):
    path = leg["path"]
    hd = [O._bearing(path[i]["lat"], path[i]["lon"], path[i+1]["lat"], path[i+1]["lon"]) for i in range(len(path)-1)]
    b = ['S' if O._wrap180(W.wind_at(path[i]["lat"], path[i]["lon"], 0)[1] - hd[i]) < 0 else 'P' for i in range(len(hd))]
    return sum(1 for i in range(1, len(b)) if b[i] != b[i-1])
def end_err(O, leg, dla, dlo): return O._hav_nm(leg["path"][-1]["lat"], leg["path"][-1]["lon"], dla, dlo)
N = lambda nm: (S + nm / 60.0, LON)

# 1) Steady dead-upwind beat: the fix must converge to a single clean tack and oversail LESS than legacy.
dla, dlo = N(12)
Oleg = cfg(False); leg_legacy = Oleg.route_leg(Steady(), P, S, LON, 0.0, dla, dlo, hstep=8, dt_cap=0.6)
Ofix = cfg(True);  leg_fix = Ofix.route_leg(Steady(), P, S, LON, 0.0, dla, dlo, hstep=8, dt_cap=0.6)
f_legacy, f_fix = board_flips(Oleg, leg_legacy, Steady()), board_flips(Ofix, leg_fix, Steady())
print(f"  steady beat: legacy flips={f_legacy}/{leg_legacy['sailed_nm']}nm  fixed flips={f_fix}/{leg_fix['sailed_nm']}nm")
check("steady beat converges to <=1 tack (no scramble)", f_fix <= 1, f"flips={f_fix}")
check("fixed oversails less than legacy", leg_fix["sailed_nm"] <= leg_legacy["sailed_nm"] + 1e-6,
      f"{leg_fix['sailed_nm']} vs {leg_legacy['sailed_nm']}")
check("steady beat lays the mark", end_err(Ofix, leg_fix, dla, dlo) < 0.1)
check("steady beat still tacks (not head-to-wind under-tack)", f_fix >= 1)

# 2) Oscillating beat: must STILL tack on the shifts (no under-tacking) and lay the mark.
leg_osc = Ofix.route_leg(Osc(), P, S, LON, 0.0, dla, dlo, hstep=8, dt_cap=0.6)
f_osc = board_flips(Ofix, leg_osc, Osc())
print(f"  oscillating beat: flips={f_osc}/{leg_osc['sailed_nm']}nm")
check("oscillating beat still tacks on the shifts", f_osc >= 2, f"flips={f_osc}")
check("oscillating beat lays the mark", end_err(Ofix, leg_osc, dla, dlo) < 0.1)

# 3) Reach: a beam reach should be ~straight (no tacks) and ~direct distance.
rla, rlo = S, LON + 12.0 / (60 * math.cos(math.radians(S)))
leg_reach = Ofix.route_leg(Steady(), P, S, LON, 0.0, rla, rlo, hstep=8, dt_cap=0.6)
print(f"  reach: flips={board_flips(Ofix, leg_reach, Steady())}/{leg_reach['sailed_nm']}nm (direct {leg_reach['direct_nm']})")
check("reach is straight (0 tacks)", board_flips(Ofix, leg_reach, Steady()) == 0)
check("reach ~ direct distance", leg_reach["sailed_nm"] <= leg_reach["direct_nm"] * 1.1)

# 4) Run: dead downwind must gybe (>=1) and lay the mark.
dla2, dlo2 = S - 12.0 / 60.0, LON
leg_run = Ofix.route_leg(Steady(), P, S, LON, 0.0, dla2, dlo2, hstep=8, dt_cap=0.6)
print(f"  run: flips={board_flips(Ofix, leg_run, Steady())}/{leg_run['sailed_nm']}nm")
check("run gybes downwind", board_flips(Ofix, leg_run, Steady()) >= 1)
check("run lays the mark", end_err(Ofix, leg_run, dla2, dlo2) < 0.1)

print("\nRESULT:", "ALL OK" if ok else "FAILURES")
raise SystemExit(0 if ok else 1)
