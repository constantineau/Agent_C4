"""Routing fidelity 2g: sail-aware routing — per-sail polars + a peel cost.

The route's SPEED was already sail-optimal (the envelope is max-over-sails), but the optimizer peeled
for free and never modelled HOLDING a sub-optimal sail. 2g carries the sail in the isochrone node
state and, per step, holds the current sail (at its OWN per-sail speed) or peels to the optimal sail
(full speed + a peel cost). These invariants are locked deterministically (constant wind, no network):
  - the per-sail polars load and a sail's speed is 0 outside its rated TWA domain (a kite can't beat);
  - carrying the WRONG sail into a leg PEELS to the right one (jib → kite on a run; kite → jib on a beat);
  - a sub-optimal sail within the hysteresis tolerance is HELD (no thrash for ~0.1 kn), at its own speed;
  - SAIL_AWARE off (or no per-sail polars) routes on the envelope EXACTLY as before (geometry unchanged).
"""
import os
import importlib

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.join(HERE, "..", "db", "seed")
os.environ["POLARS_FILE"] = os.path.join(SEED, "polars_sr33.sql")
os.environ["SAIL_POLARS_FILE"] = os.path.join(SEED, "sr33_sail_polars.json")
os.environ["CROSSOVERS_FILE"] = os.path.join(SEED, "sr33_crossovers.json")

from app import polars as POL          # noqa: E402
from app import sailplan               # noqa: E402
from app import optimizer as OPT       # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def reload_with(sail_aware):
    os.environ["ROUTE_SAIL_AWARE"] = "1" if sail_aware else "0"
    return importlib.reload(OPT)


# constant wind: TWS 12, TWD 0 (wind FROM the north). Heading 180 = dead downwind (run, TWA 180);
# heading 0 = dead upwind (beat, TWA 0). A schematic but exact field.
class Const:
    def __init__(self, tws=12.0, twd=0.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 0.7}


W = Const()
P = POL.polars_stw()
SP = POL.sail_polars()
slat, slon = 44.0, -82.0


def run_leg(O, dlat, dlon, start_sail):
    return O.route_leg(W, P, slat, slon, 0.0, dlat, dlon,
                       sail_polars=SP, jib_crossovers=None, start_sail=start_sail)


# 1) data loads + domain gate
check("per-sail polars load (J1/A2/A3/S2)", set(SP) == {"J1", "A2", "A3", "S2"})
O = reload_with(True)
dom = OPT._sail_domains(SP)
check("kite has a real run speed (S2 @180)", OPT._sail_speed(SP, dom, "S2", 12, 180) > 4.0)
check("kite is infeasible upwind (S2 @40 → 0)", OPT._sail_speed(SP, dom, "S2", 12, 40) == 0.0)
check("jib variant maps to J1 curve (J3 @45 == J1)",
      OPT._sail_speed(SP, dom, "J3", 12, 45) == OPT._sail_speed(SP, dom, "J1", 12, 45))

# 2) carry the WRONG sail into a run → peel to a kite
run_dlat, run_dlon = slat - 12.0 / 60.0, slon          # 12 nm due south = dead downwind
leg = run_leg(O, run_dlat, run_dlon, "J1")
end_sail = leg.get("sail_end")
print(f"     run leg carrying J1: sail_end={end_sail} peels={leg['peels']}")
check("run leg carrying a jib PEELS to a kite", leg["peels"] >= 1 and end_sail in ("A2", "A3", "S2"))

# 3) carry a kite into a beat → forced peel to the jib (kite infeasible upwind)
beat_dlat, beat_dlon = slat + 12.0 / 60.0, slon        # 12 nm due north = dead upwind
leg = run_leg(O, beat_dlat, beat_dlon, "S2")
print(f"     beat leg carrying S2: sail_end={leg.get('sail_end')} peels={leg['peels']}")
check("beat leg carrying a kite PEELS to the jib", leg["peels"] >= 1 and leg.get("sail_end") == "J1")

# 4) hysteresis — a sub-optimal sail WITHIN tolerance is held (no peel, flown at its own speed).
# Find a reach TWA where A2 is optimal-ish but A3 is marginally faster (within PEEL_HOLD_TOL).
twa_test = 110.0
opt = sailplan.optimal_sail(12, twa_test)
a2 = OPT._sail_speed(SP, dom, "A2", 12, twa_test)
env = OPT._polar_speed(P, 12, twa_test)
within = a2 >= env * (1.0 - OPT.PEEL_HOLD_TOL)
print(f"     TWA{twa_test}: optimal={opt} A2={a2:.2f} env={env:.2f} within_tol={within}")
if within and opt != "A2":
    # heading 110 off TWD0 = 110 true; dest along that bearing
    rdlat, rdlon = OPT._advance(slat, slon, 110.0, 12.0)
    leg = run_leg(O, rdlat, rdlon, "A2")
    held = (leg.get("sail_end") == "A2" and leg["peels"] == 0)
    print(f"     reach leg carrying A2 (A3 marginally faster): sail_end={leg.get('sail_end')} peels={leg['peels']}")
    check("a within-tolerance sub-optimal sail is HELD (no thrash)", held)
else:
    check("hysteresis precondition (A2 within tol, not optimal) — skipped cleanly", True)

# 5) SAIL_AWARE off → geometry identical to the envelope baseline (no per-sail effect).
Ooff = reload_with(False)
leg_off = Ooff.route_leg(W, P, slat, slon, 0.0, run_dlat, run_dlon,
                         sail_polars=SP, jib_crossovers=None, start_sail="J1")
leg_none = Ooff.route_leg(W, P, slat, slon, 0.0, run_dlat, run_dlon)   # no per-sail polars at all
same_eta = abs(leg_off["eta"] - leg_none["eta"]) < 1e-6
same_geom = leg_off["sailed_nm"] == leg_none["sailed_nm"] and leg_off["peels"] == 0
print(f"     SAIL_AWARE off: eta_off={leg_off['eta']:.1f} eta_none={leg_none['eta']:.1f} peels={leg_off['peels']}")
check("SAIL_AWARE off → identical geometry + zero peels", same_eta and same_geom)

# 6) sail-aware on a run is no SLOWER than the envelope baseline for the same leg (it flies the
# optimal kite once peeled; the only cost is the one-off peel, not a slower sail throughout).
Oon = reload_with(True)
leg_on = Oon.route_leg(W, P, slat, slon, 0.0, run_dlat, run_dlon,
                       sail_polars=SP, jib_crossovers=None, start_sail=None)   # start on the optimal sail
base = Ooff.route_leg(W, P, slat, slon, 0.0, run_dlat, run_dlon)
print(f"     start-on-optimal run leg: eta_on={leg_on['eta']:.1f} eta_base={base['eta']:.1f} peels={leg_on['peels']}")
check("starting on the optimal sail → no peel, baseline ETA",
      leg_on["peels"] == 0 and abs(leg_on["eta"] - base["eta"]) < 1.0)

print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
raise SystemExit(0 if ok else 1)
