"""Water currents (set & drift) in the optimizer — route over ground = boat-through-water + current.

Deterministic, no network. A beam reach due north (wind from the east) into a mark 12 nm north, with a
uniform current, exercises the physics:
  - a cross-current makes the boat CRAB (point upstream of the course) yet still lay the mark;
  - a fair current (toward the mark) cuts the ETA; a foul current (away) raises it;
  - no current = unchanged.

Run: docker compose exec -w /srv lab python test_routing_currents.py
"""
import os, importlib
for k, v in {"ROUTE_DMG_PRUNE": "1", "ROUTE_LAYLINE_COMMIT": "1", "ROUTE_TACK_CUMULATIVE": "1",
             "ROUTE_MARK_POS_PRUNE": "1", "ROUTE_LAYLINE_GATE": "1"}.items():
    os.environ[k] = v
from app import optimizer as O
from app import current as CUR
importlib.reload(O)

ok = True
def check(name, cond, extra=""):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  — ' + extra) if extra else ''}")

rows = {30: 2.8, 40: 4.2, 42: 4.4, 50: 4.9, 60: 5.3, 75: 5.7, 90: 5.9,
        110: 5.8, 135: 5.2, 150: 4.6, 165: 4.0, 180: 3.2}
P = [(8.5, a, s) for a, s in rows.items()]

class Steady:               # steady wind FROM the east (twd=90) → due-north = beam reach (no tacking)
    def wind_at(self, la, lo, t): return (8.5, 90.0)
    def detail_at(self, la, lo, t): return {"tws": 8.5, "twd": 90.0, "confidence": 0.9}

S, LON = 44.0, -82.0
DLAT, DLON = S + 12.0 / 60.0, LON          # mark 12 nm due north

def leg(set_deg, drift):
    cur = CUR.ConstantCurrent(set_deg, drift) if drift > 0 else None
    L = O.route_leg(Steady(), P, S, LON, 0.0, DLAT, DLON, hstep=6, dt_cap=0.5, cur=cur)
    end = O._hav_nm(L["path"][-1]["lat"], L["path"][-1]["lon"], DLAT, DLON)
    return L, end, L["eta"] / 3600.0

nocur, e0, eta0 = leg(0, 0)
print(f"  no current : reaches end={e0:.3f}nm  eta={eta0:.2f}h  first_hdg={nocur['first_heading']}")
check("no current reaches the mark", e0 < 0.2)
check("no current steers ~north", abs(O._wrap180(nocur["first_heading"])) < 12,
      f"hdg={nocur['first_heading']}")

cross, ec, etac = leg(90, 1.5)             # cross-current setting EAST → boat must crab WEST of north
print(f"  cross E 1.5: reaches end={ec:.3f}nm  eta={etac:.2f}h  first_hdg={cross['first_heading']}")
check("cross-current still lays the mark (crabs)", ec < 0.3)
check("cross-current crabs upstream (water heading west of north)",
      O._wrap180(cross["first_heading"]) < O._wrap180(nocur["first_heading"]) - 8,
      f"cross hdg={cross['first_heading']} vs nocur {nocur['first_heading']}")

fair, ef, etaf = leg(0, 1.5)               # current setting NORTH (toward the mark) → faster
print(f"  fair  N 1.5: reaches end={ef:.3f}nm  eta={etaf:.2f}h")
check("fair current reaches the mark", ef < 0.2)
check("fair current cuts the ETA", etaf < eta0 - 0.05, f"{etaf:.2f} vs {eta0:.2f}")

foul, eu, etau = leg(180, 1.5)             # current setting SOUTH (away from the mark) → slower
print(f"  foul  S 1.5: reaches end={eu:.3f}nm  eta={etau:.2f}h")
check("foul current reaches the mark", eu < 0.2)
check("foul current raises the ETA", etau > eta0 + 0.05, f"{etau:.2f} vs {eta0:.2f}")

print("\nRESULT:", "ALL OK" if ok else "FAILURES")
raise SystemExit(0 if ok else 1)
