"""Routing fidelity 2d: mark-approach fidelity — the layline/overstand gate (stop sailing PAST a mark
then doubling back) + the rounding-side standoff (leave port/starboard marks on the legal side)."""
import math
from app import optimizer as OPT

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

rows = {30: 4.0, 40: 6.0, 42: 6.2, 50: 6.8, 60: 7.2, 75: 7.6, 90: 7.9,
        110: 7.8, 135: 7.2, 150: 6.5, 165: 6.0, 180: 5.0}
P = [(12.0, a, s) for a, s in rows.items()]

def windward_past(path, mlat, mlon, twd, pos):
    """Max nm any path point sits PAST the mark on the up/down-wind axis (overstand)."""
    wb = twd if pos == "beat" else (twd + 180.0)
    ax = (math.cos(math.radians(wb)), math.sin(math.radians(wb)))
    cl = math.cos(math.radians(mlat))
    return max((p["lat"] - mlat) * 60 * ax[0] + (p["lon"] - mlon) * 60 * cl * ax[1] for p in path)

# --- the gate is on by default; the classic prune still reaches + tacks upwind -----------------
check("layline gate on by default", OPT.LAYLINE_GATE is True)

class WF:
    def wind_at(self, la, lo, t): return (12.0, 0.0)            # TWS12 from N everywhere
    def detail_at(self, la, lo, t): return {"tws": 12.0, "twd": 0.0, "confidence": 1.0}
wf = WF()
slat, slon, dlat, dlon = 44.0, -82.0, 44.20, -82.0             # 12 nm dead upwind
leg = OPT.route_leg(wf, P, slat, slon, 0.0, dlat, dlon)
endd = OPT._hav_nm(leg["path"][-1]["lat"], leg["path"][-1]["lon"], dlat, dlon)
check("route reaches the mark", endd < 0.3)
check("route still tacks upwind", leg["tacks"] >= 1 and leg["sailed_nm"] > leg["direct_nm"])
check("beat: overstand within the gate tolerance",
      windward_past(leg["path"], dlat, dlon, 0.0, "beat") <= OPT.OVERSTAND_NM + 0.2)

# --- #2 the layline/overstand gate is a GUARANTEE: with it on, the route never sails more than the
#        tolerance past a beat/run mark on the wind axis (the "double back" can't happen), and the gate
#        never makes the route worse than with it off. (The made-good prune (#1) is what removes the
#        dogleg in the field; the gate is the hard backstop — see the real-course A/B in the README.)
class WFrun:
    def wind_at(self, la, lo, t): return (12.0, 180.0)         # FROM S → mark to the N is dead downwind
    def detail_at(self, la, lo, t): return {"tws": 12.0, "twd": 180.0, "confidence": 1.0}
wfr = WFrun()
s2 = (44.0, -82.0); m2 = (44.12, -82.0)
OPT.LAYLINE_GATE = False
off = OPT.route_leg(wfr, P, s2[0], s2[1], 0.0, m2[0], m2[1])
OPT.LAYLINE_GATE = True
on = OPT.route_leg(wfr, P, s2[0], s2[1], 0.0, m2[0], m2[1])
po = windward_past(off["path"], m2[0], m2[1], 180.0, "run")
pn = windward_past(on["path"], m2[0], m2[1], 180.0, "run")
print(f"     run overstand: gate OFF={po:.2f} nm, ON={pn:.2f} nm (tol {OPT.OVERSTAND_NM})")
check("run: overstand within the gate tolerance", pn <= OPT.OVERSTAND_NM + 0.2)
check("gate never makes overstand worse", pn <= po + 1e-6)
check("gated run still reaches the mark",
      OPT._hav_nm(on["path"][-1]["lat"], on["path"][-1]["lon"], *m2) < 0.4)

# --- #3 rounding-side standoff ----------------------------------------------
# approaching a mark heading due N (b_in=0): leave-to-PORT → standoff to the E (right of course);
# leave-to-STARBOARD → standoff to the W (left). gate/none → unchanged.
mlat, mlon = 45.0, -82.0
pl = OPT._rounding_offset(44.0, -82.0, mlat, mlon, "port", nm=0.5)
sb = OPT._rounding_offset(44.0, -82.0, mlat, mlon, "starboard", nm=0.5)
check("port rounding offsets to the right of the inbound course (east)", pl[1] > mlon)
check("starboard rounding offsets to the left (west)", sb[1] < mlon)
check("gate/none rounding leaves the mark unchanged",
      OPT._rounding_offset(44.0, -82.0, mlat, mlon, "gate") == (mlat, mlon) and
      OPT._rounding_offset(44.0, -82.0, mlat, mlon, "none") == (mlat, mlon))

print("RESULT:", "PASS" if ok else "FAIL")
