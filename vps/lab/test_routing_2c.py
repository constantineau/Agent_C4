"""Routing fidelity 2c: VMG gate, adaptive step, cone pruning (+ obstacle-avoidance regression)."""
import math
from app import optimizer as OPT

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

# synthetic SR33-ish polar at TWS=12: beat VMG optimum ~42, run VMG optimum ~150
rows = {30: 4.0, 40: 6.0, 42: 6.2, 50: 6.8, 60: 7.2, 75: 7.6, 90: 7.9,
        110: 7.8, 135: 7.2, 150: 6.5, 165: 6.0, 180: 5.0}
P = [(12.0, a, s) for a, s in rows.items()]

# 1) VMG headings (TWD=0 → wind from north)
vmg = sorted(OPT._vmg_headings(P, 12.0, 0.0))
check("VMG beat headings 42/318 present", 42 in vmg and 318 in vmg)
check("VMG run headings 165/195 present (argmax of -s*cos)", 165 in vmg and 195 in vmg)


# 3) constant-wind WindField stub (TWS 12 FROM north everywhere)
class WF:
    def wind_at(self, lat, lon, t): return (12.0, 0.0)
    def detail_at(self, lat, lon, t): return {"tws": 12.0, "twd": 0.0, "confidence": 1.0}

wf = WF()
# upwind leg: mark ~6 nm due north of start (dead upwind → must tack at VMG angle)
slat, slon, dlat, dlon = 44.0, -82.0, 44.10, -82.0
leg = OPT.route_leg(wf, P, slat, slon, 0.0, dlat, dlon)
fh = leg["first_heading"]
# first heading should be a beat angle (~42 stbd or ~318 port), not straight at the mark (0°/360°)
beatish = min(abs(((fh - 42) + 180) % 360 - 180), abs(((fh - 318) + 180) % 360 - 180))
print(f"     upwind first_heading={fh}, sailed={leg['sailed_nm']} direct={leg['direct_nm']} tacks={leg['tacks']}")
check("upwind first heading is a VMG beat angle (~42/318)", beatish <= 8)
check("upwind route tacks (sailed > direct)", leg["sailed_nm"] > leg["direct_nm"] and leg["tacks"] >= 1)
check("reaches the mark", _d := OPT._hav_nm(leg["path"][-1]["lat"], leg["path"][-1]["lon"], dlat, dlon) < 0.3)

# 4) obstacle-avoidance regression: block a box straddling the rhumb; route must detour AROUND it
class Obs:
    box = (44.02, 44.05, -82.01, -81.99)   # (s, n, w, e) a small blocking box on the way north
    def _in(self, la, lo):
        s, n, w, e = self.box; return s <= la <= n and w <= lo <= e
    def crosses(self, la1, lo1, la2, lo2):
        # sample the segment; blocked if any sample falls in the box
        for k in range(0, 6):
            f = k / 5.0
            if self._in(la1 + (la2 - la1) * f, lo1 + (lo2 - lo1) * f): return True
        return False
obs = Obs()
leg2 = OPT.route_leg(wf, P, slat, slon, 0.0, dlat, dlon, obstacles=obs)
in_box = any(obs._in(p["lat"], p["lon"]) for p in leg2["path"])
endd = OPT._hav_nm(leg2["path"][-1]["lat"], leg2["path"][-1]["lon"], dlat, dlon)
print(f"     w/ obstacle: path pts={len(leg2['path'])}, any in box={in_box}, end dist={endd:.2f} nm, blocked_steps={leg2['blocked_steps']}")
check("route avoids the obstacle box", not in_box)
check("route still reaches the mark past the obstacle", endd < 0.5)

print("RESULT:", "PASS" if ok else "FAIL")
