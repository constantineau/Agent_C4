"""Routing fidelity 2e: finish/mark over-tack ("scramble") fixes — layline-commit (#1),
cumulative tack cost (#2), position-prune near the mark (#3).

End-to-end the scramble only appears in the real multi-model GRIB field near a complex shoreline
(the St Ignace finish), so the production proof is the live Bayview Mackinac A/B in the PR notes.
Here we lock the INVARIANTS deterministically (no network):
  - the helpers compute the right VMG cone angle;
  - on a high-frequency oscillating wind a dead-upwind leg MICRO-TACKS under the baseline, and the
    cumulative tack cost cuts that substantially while STILL reaching the mark (no under-tacking,
    no overstanding);
  - on a steady dead-upwind leg the fixes still tack the minimum needed and reach (anti-under-tack).
"""
import os, math, importlib
from app import optimizer as OPT

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

def reload_with(flags):
    for k in ("ROUTE_LAYLINE_COMMIT", "ROUTE_TACK_CUMULATIVE", "ROUTE_MARK_POS_PRUNE"):
        os.environ[k] = "1" if flags.get(k) else "0"
    return importlib.reload(OPT)

# SR33-ish polar: beat VMG optimum ~42, run VMG optimum ~150
rows = {30: 2.8, 40: 4.2, 42: 4.4, 50: 4.9, 60: 5.3, 75: 5.7, 90: 5.9,
        110: 5.8, 135: 5.2, 150: 4.6, 165: 4.0, 180: 3.2}
P = [(8.5, a, s) for a, s in rows.items()]

# 1) VMG cone half-angle helper
O = reload_with(dict(ROUTE_LAYLINE_COMMIT=1, ROUTE_TACK_CUMULATIVE=1, ROUTE_MARK_POS_PRUNE=1))
beat_twa = O._vmg_twa(P, 8.5, "beat")
run_twa = O._vmg_twa(P, 8.5, "run")
print(f"     _vmg_twa beat={beat_twa} run={run_twa}")
check("_vmg_twa beat ~42 (close-hauled VMG)", beat_twa is not None and 36 <= beat_twa <= 50)
check("_vmg_twa run ~150 (running VMG)", run_twa is not None and 140 <= run_twa <= 160)
check("_vmg_twa reach → None", O._vmg_twa(P, 8.5, "reach") is None)

# 2) high-frequency oscillating shift → baseline micro-tacks; cumulative cost cuts it, still reaches.
class Osc:
    def _w(self, lat, lon):
        return (8.5, (22.0 * math.sin(lat * 800.0) + 11.0 * math.cos(lon * 560.0)) % 360)
    def wind_at(self, lat, lon, t): return self._w(lat, lon)
    def detail_at(self, lat, lon, t):
        tws, twd = self._w(lat, lon); return {"tws": tws, "twd": twd, "confidence": 0.5}
osc = Osc()
slat, slon = 44.0, -82.0
dlat, dlon = slat + 14.0 / 60.0, slon          # 14 nm dead upwind into a point

def leg_tacks(flags):
    Ox = reload_with(flags)
    leg = Ox.route_leg(osc, P, slat, slon, 0.0, dlat, dlon)
    end = Ox._hav_nm(leg["path"][-1]["lat"], leg["path"][-1]["lon"], dlat, dlon)
    return leg, end

base, base_end = leg_tacks(dict())
cum, cum_end = leg_tacks(dict(ROUTE_TACK_CUMULATIVE=1))
allf, all_end = leg_tacks(dict(ROUTE_LAYLINE_COMMIT=1, ROUTE_TACK_CUMULATIVE=1, ROUTE_MARK_POS_PRUNE=1))
print(f"     osc leg: baseline tacks={base['tacks']} (end {base_end:.2f}); "
      f"+cumulative tacks={cum['tacks']} (end {cum_end:.2f}); all tacks={allf['tacks']} (end {all_end:.2f})")
check("baseline micro-tacks on the oscillating field (>=5)", base["tacks"] >= 5)
check("cumulative tack cost cuts the tack count (>=2 fewer)", cum["tacks"] <= base["tacks"] - 2)
check("all fixes keep the tack count below baseline", allf["tacks"] <= base["tacks"])
check("every variant still reaches the mark", base_end < 0.3 and cum_end < 0.3 and all_end < 0.3)
check("no overstanding (oversail stays sane, <1.6x)", allf["sailed_nm"] / allf["direct_nm"] < 1.6)

# 3) anti-under-tack: a STEADY dead-upwind leg must still tack the minimum + reach (with all fixes on)
class Steady:
    def wind_at(self, lat, lon, t): return (8.5, 0.0)        # 8.5 kt FROM north, constant
    def detail_at(self, lat, lon, t): return {"tws": 8.5, "twd": 0.0, "confidence": 1.0}
Oa = reload_with(dict(ROUTE_LAYLINE_COMMIT=1, ROUTE_TACK_CUMULATIVE=1, ROUTE_MARK_POS_PRUNE=1))
sl = Oa.route_leg(Steady(), P, slat, slon, 0.0, slat + 8.0 / 60.0, slon)
sl_end = Oa._hav_nm(sl["path"][-1]["lat"], sl["path"][-1]["lon"], slat + 8.0 / 60.0, slon)
print(f"     steady upwind leg (all fixes): tacks={sl['tacks']} sailed={sl['sailed_nm']} end={sl_end:.2f}")
check("steady dead-upwind leg STILL tacks (>=1) — fixes don't under-tack", sl["tacks"] >= 1)
check("steady dead-upwind leg reaches the mark", sl_end < 0.3)

# 4) tack-counter fix: the reported count is the genuine port<->starboard maneuver tally along the
# path (classified against LOCAL wind), not a frozen leg-start reference. In constant wind the two
# coincide, so the reported count must EXACTLY equal the crossings recomputed from the path geometry.
pth = sl["path"]
crossings, prev = 0, None
for i in range(len(pth) - 1):
    hd = Oa._bearing(pth[i]["lat"], pth[i]["lon"], pth[i + 1]["lat"], pth[i + 1]["lon"])
    sd = "stbd" if Oa._wrap180(0.0 - hd) > 0 else "port"     # wind FROM 0 (north), constant
    if prev and sd != prev:
        crossings += 1
    prev = sd
print(f"     counter check: reported={sl['tacks']} recomputed-from-path={crossings}")
check("reported tacks == genuine path crossings (local-wind counter)", sl["tacks"] == crossings)

print("RESULT:", "PASS" if ok else "FAIL")
