"""#66: a beat in steady wind sails as few boards as the polar needs, and no slower.

With the mark inside the boat's VMG cone, one tack does it: sail one board to the layline, tack,
fetch. The router used to take 2-3 board changes on many such legs. Two causes, both in the
isochrone prune:
  - the fan's equal-time steps put the tack where a step ends, so it tacked a bit short of the
    layline and then needed two more tacks to fetch;
  - the prune ranked by straight-line distance to the mark, which rewards footing toward it, and
    it couldn't tell a board sailing the layline from one that tacked short (same ground made
    to windward, but one still owes a tack).

Synthetic polar (not any real boat's), steady wind at 6/10/16 kn, 9 beat legs per wind.
Deterministic, no network. Run: python3 test_routing_lightair.py (lab image, cwd /srv).
"""
import math

from app import optimizer as OPT

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  - ' + extra) if extra else ''}")


# A made-up displacement boat: beat angle opens from 46 deg at 4 kn to ~34 deg at 24 kn, and the
# VMG optimum is the first (beat) point of each row.
SHAPE = [(0, 0.80), (52, 0.85), (60, 0.90), (75, 0.96), (90, 1.00), (110, 0.98),
         (135, 0.90), (150, 0.80), (165, 0.70), (180, 0.62)]
P = []
for tws in (4, 6, 8, 10, 12, 16, 20, 24):
    beat = 46.0 - 0.6 * (tws - 4)
    vmax = min(7.4, 1.8 + 0.6 * tws)
    for twa, f in SHAPE:
        P.append((float(tws), beat if twa == 0 else float(twa), round(vmax * f, 3)))


class Steady:
    def __init__(self, tws, twd):
        self.tws, self.twd = tws, twd % 360

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}


S_LAT, S_LON = 44.0, -82.3


def board_changes(leg, twd):
    p, sides = leg["path"], []
    for a, b in zip(p, p[1:]):
        if OPT._hav_nm(a["lat"], a["lon"], b["lat"], b["lon"]) < 1e-4:
            continue
        sides.append(OPT._wrap180(twd - OPT._bearing(a["lat"], a["lon"], b["lat"], b["lon"])) > 0)
    return sum(1 for x, y in zip(sides, sides[1:]) if x != y)


def one_tack_minutes(tws, L, rel):
    """Both boards at the VMG angle, plus the tack's clock cost."""
    half = OPT._vmg_twa_cached(P, tws, "beat")
    v = OPT._polar_speed(P, tws, half)
    return L * math.cos(math.radians(abs(rel))) / math.cos(math.radians(half)) / v * 60 + OPT.TACK_COST_S / 60


for tws in (6.0, 10.0, 16.0):
    changes, worst = [], 0.0
    for L in (4.0, 9.0, 15.0):
        for rel in (0, 10, -15):
            dla = S_LAT + L / 60.0
            leg = OPT.route_leg(Steady(tws, rel), P, S_LAT, S_LON, 0.0, dla, S_LON,
                                hstep=12, dt_cap=1.0)        # the Lab's "auto" resolution
            if not leg["reached"]:
                check(f"{tws:g} kn, {L:g} nm, wind {rel:+d}: reaches the mark", False)
                continue
            changes.append(board_changes(leg, rel))
            worst = max(worst, (leg["eta"] / 60.0) / one_tack_minutes(tws, L, rel) - 1.0)
    print(f"  {tws:g} kn: board changes per leg {changes}, worst ETA vs one clean tack {worst:+.1%}")
    check(f"{tws:g} kn: every steady beat is two boards (one tack)", changes == [1] * 9, str(changes))
    check(f"{tws:g} kn: no leg slower than one clean tack by more than 1%", worst <= 0.01, f"{worst:+.2%}")

print("\nRESULT:", "ALL OK" if ok else "FAILURES")
raise SystemExit(0 if ok else 1)
