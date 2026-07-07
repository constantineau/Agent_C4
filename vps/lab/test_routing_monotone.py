"""Progress-monotonicity loop gate — the 'circles/zig-zags at marks' fix.

In near-drifting ROTATING air the time-optimal isochrone path can loop (sail away to meet the new
breeze) and near-equal light-air frontier nodes make the winner arbitrary — the crew got a
self-crossing scribble at the finish (user report 2026-07-07). MONOTONE rejects a candidate once
it gives back more than ROUTE_BACKTRACK_NM of its path's best progress toward the mark; a node
whose whole fan is monotone-blocked re-expands with the gate off (fail-open) so obstacle detours
and foul-current drift never strand the boat. Locked here deterministically:
  - a light rotating breeze produces NO self-crossing legs and sane oversail, still reaching;
  - upwind/downwind VMG behavior is unchanged (a beat still tacks, gains ground each board);
  - obstacle detours still route around and reach (the gate never blocks avoidance);
  - fail-open: an overwhelming foul current (all progress lost every step) still returns a path
    instead of stranding at generation 1;
  - a self-crossing path is reported by the _leg_self_crossings sanity tell.
"""
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
for _seed in (os.path.join(HERE, "..", "db", "seed"), "/srv"):
    if os.path.exists(os.path.join(_seed, "polars_sr33.sql")):
        os.environ["POLARS_FILE"] = os.path.join(_seed, "polars_sr33.sql")
        os.environ["SAIL_POLARS_FILE"] = os.path.join(_seed, "sr33_sail_polars.json")
        os.environ["CROSSOVERS_FILE"] = os.path.join(_seed, "sr33_crossovers.json")
        break

from app import optimizer as OPT       # noqa: E402
from app import polars as POL          # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def self_x(pts):
    def ccw(p, q, r):
        return (r[1] - p[1]) * (q[0] - p[0]) > (q[1] - p[1]) * (r[0] - p[0])

    def cross(a, b, c, d):
        return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)

    n, x = len(pts), 0
    for i in range(n - 1):
        for j in range(i + 2, n - 1):
            if cross(pts[i], pts[i + 1], pts[j], pts[j + 1]):
                x += 1
    return x


class RotWF:
    """Light air, TWD rotating linearly twd0→twd1 over span_h — the loop-driving field shape."""
    loaded = True

    def __init__(self, twd0, twd1, span_h=24.0, tws=4.0):
        self.twd0, self.twd1, self.span_h, self.tws = twd0, twd1, span_h, tws

    def _twd(self, t):
        f = max(0.0, min(1.0, (t / 3600.0) / self.span_h))
        return (self.twd0 + (self.twd1 - self.twd0) * f) % 360.0

    def wind_at(self, lat, lon, t):
        return (self.tws, self._twd(max(0.0, t)))

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self._twd(max(0.0, t)), "confidence": 0.3}


class ConstWF:
    loaded = True

    def __init__(self, tws=12.0, twd=0.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}


P = POL.polars_stw()

# ---- 1) rotating light air: no loops, still reaches ---------------------------------------------
print("1) rotating light air (the loop driver) — no self-crossing, sane oversail")
# ~115 nm westward leg at 45.5N (the Straits finish shape), light 4 kn breeze rotating 140 deg
s, f = (45.5, -82.0), (45.5, -84.8)
for twd0, twd1 in ((340, 250), (250, 340), (250, 390), (70, 250)):
    leg = OPT.route_leg(RotWF(twd0, twd1), P, s[0], s[1], 0.0, f[0], f[1])
    pts = [(p["lat"], p["lon"]) for p in leg["path"]]
    direct = OPT._hav_nm(*s, *f)
    endd = OPT._hav_nm(pts[-1][0], pts[-1][1], f[0], f[1])
    x = self_x(pts)
    over = leg["sailed_nm"] / direct
    print(f"     rot {twd0}->{twd1}: selfX={x} oversail={over:.2f} tacks={leg['tacks']} "
          f"end-dist={endd:.1f}")
    check(f"rot {twd0}->{twd1}: no self-crossing", x == 0)
    check(f"rot {twd0}->{twd1}: reaches the mark", endd < 0.5)
    check(f"rot {twd0}->{twd1}: oversail sane (<1.35)", over < 1.35)

# ---- 2) VMG behavior unchanged ------------------------------------------------------------------
print("2) beat/run VMG behavior unchanged under the gate")
leg = OPT.route_leg(ConstWF(), P, 44.0, -82.0, 0.0, 44.17, -82.0)   # 10 nm dead upwind
endd = OPT._hav_nm(leg["path"][-1]["lat"], leg["path"][-1]["lon"], 44.17, -82.0)
check("dead-upwind leg still tacks and reaches", leg["tacks"] >= 1 and endd < 0.3)
leg = OPT.route_leg(ConstWF(), P, 44.17, -82.0, 0.0, 44.0, -82.0)   # 10 nm dead downwind
endd = OPT._hav_nm(leg["path"][-1]["lat"], leg["path"][-1]["lon"], 44.0, -82.0)
check("dead-downwind leg reaches", endd < 0.3)

# ---- 3) obstacle detour untouched ----------------------------------------------------------------
print("3) obstacle detour still works (gate never blocks avoidance)")


class Obs:
    box = (44.02, 44.08, -82.03, -81.97)

    def _in(self, la, lo):
        s_, n, w, e = self.box
        return s_ <= la <= n and w <= lo <= e

    def crosses(self, la1, lo1, la2, lo2):
        for k in range(0, 21):
            fk = k / 20.0
            if self._in(la1 + (la2 - la1) * fk, lo1 + (lo2 - lo1) * fk):
                return True
        return False

    def blocked(self, la, lo):
        return self._in(la, lo)


leg = OPT.route_leg(ConstWF(), P, 44.0, -82.0, 0.0, 44.10, -82.0, obstacles=Obs())
in_box = any(Obs()._in(p["lat"], p["lon"]) for p in leg["path"])
endd = OPT._hav_nm(leg["path"][-1]["lat"], leg["path"][-1]["lon"], 44.10, -82.0)
check("route detours around the box", not in_box)
check("route still reaches past the box", endd < 0.5)

# ---- 4) fail-open: overwhelming foul current never strands the search ---------------------------
print("4) fail-open under an overwhelming foul current (all progress lost every step)")


class FoulCur:
    """A stream sweeping the boat AWAY from the mark faster than it can sail in the light air."""

    def __init__(self, set_deg):
        self.set_deg = set_deg

    def current_at(self, lat, lon, t):
        return (self.set_deg, 6.0)     # 6 kn foul vs ~2-3 kn boatspeed at TWS 3


import time as _time
t_start = _time.time()
leg = OPT.route_leg(ConstWF(tws=3.0, twd=0.0), P, 45.5, -82.0, 0.0, 45.5, -82.3,
                    cur=FoulCur(90.0), deadline=_time.time() + 8)   # mark west, current sets east
check("search survives (returns a path, no strand/exception)", len(leg["path"]) >= 2)
check("bounded runtime (deadline honored)", _time.time() - t_start < 30)

# ---- 5) the self-crossing sanity tell ------------------------------------------------------------
print("5) _leg_self_crossings reports a crossing leg")
sq = [(0.0, 0.0), (0.2, 0.0), (0.2, 0.1), (0.05, 0.1), (0.05, -0.05), (-0.1, -0.05)]
path = [{"lat": la, "lon": lo, "t": i * 3600.0} for i, (la, lo) in enumerate(sq)]
legs = [{"to": "M", "eta_epoch": (len(sq) - 1) * 3600.0}]
res = OPT._leg_self_crossings(path, legs, 0.0)
check("crossing leg detected", res.get("M", 0) >= 1)
straight = [{"lat": 0.0, "lon": 0.01 * i, "t": i * 3600.0} for i in range(6)]
check("clean leg silent", OPT._leg_self_crossings(straight, legs, 0.0) == {})

print("RESULT:", "PASS" if ok else "FAIL")
