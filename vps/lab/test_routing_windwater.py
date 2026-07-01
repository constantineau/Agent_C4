"""Wind-over-water 2nd-order correction (routing-fidelity 2d lever b). The boat sails relative to the
WATER, so the polar must be indexed by the wind the sails feel over the water (ground wind − current),
not the ground wind. Deterministic, no network.

Run: docker compose exec -w /srv lab python test_routing_windwater.py
"""
import os
for k, v in {"ROUTE_DMG_PRUNE": "1", "ROUTE_LAYLINE_COMMIT": "1", "ROUTE_TACK_CUMULATIVE": "1",
             "ROUTE_MARK_POS_PRUNE": "1", "ROUTE_LAYLINE_GATE": "1"}.items():
    os.environ[k] = v
from app import optimizer as O
from app import current as CUR

ok = True
def check(name, cond, extra=""):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  — ' + extra) if extra else ''}")

# --- the physics of the correction ------------------------------------------------------------
print("wind-over-water vector math (wind 10 kn FROM north = twd 0):")
O.WIND_OVER_WATER = True
t0, d0 = O._wind_over_water(10, 0, 0, 0.0)
check("no current → unchanged", t0 == 10 and d0 == 0)
tf, df = O._wind_over_water(10, 0, 180, 2)     # current sets SOUTH = downwind → drift away from source
check("following current (drift downwind) → LESS apparent wind (~8 kn)", abs(tf - 8.0) < 0.05,
      f"{tf:.2f} kn")
to, do = O._wind_over_water(10, 0, 0, 2)        # current sets NORTH = into the wind → drift toward source
check("opposing current (drift upwind) → MORE apparent wind (~12 kn)", abs(to - 12.0) < 0.05,
      f"{to:.2f} kn")
tx, dx = O._wind_over_water(10, 0, 90, 2)        # current sets EAST → apparent wind veers
check("cross current → wind DIRECTION shifts", abs(O._wrap180(dx - 0)) > 5,
      f"twd {dx:.1f}°")
check("cross current → speed ~unchanged (slightly up)", 10.0 <= tx < 10.5, f"{tx:.2f} kn")
off_t, _ = O._wind_over_water(10, 0, 180, 2)
O.WIND_OVER_WATER = False
disabled_t, disabled_d = O._wind_over_water(10, 0, 180, 2)
check("flag OFF → no correction (ground wind returned)", disabled_t == 10 and disabled_d == 0)
O.WIND_OVER_WATER = True

# --- integration: it changes the route under current, and is a no-op with none -----------------
print("integration (beam reach north, wind from the east):")
rows = {30: 2.8, 40: 4.2, 42: 4.4, 50: 4.9, 60: 5.3, 75: 5.7, 90: 5.9,
        110: 5.8, 135: 5.2, 150: 4.6, 165: 4.0, 180: 3.2}
P = [(8.5, a, s) for a, s in rows.items()]

class Steady:
    def wind_at(self, la, lo, t): return (8.5, 90.0)
    def detail_at(self, la, lo, t): return {"tws": 8.5, "twd": 90.0, "confidence": 0.9}

S, LON = 44.0, -82.0
DLAT, DLON = S + 12.0 / 60.0, LON

def eta(cur, wow):
    O.WIND_OVER_WATER = wow
    return O.route_leg(Steady(), P, S, LON, 0.0, DLAT, DLON, hstep=6, dt_cap=0.5, cur=cur)["eta"] / 3600.0

cur = CUR.ConstantCurrent(0, 2.0)    # 2 kn setting north (into the east wind → apparent wind shifts)
eta_off, eta_on = eta(cur, False), eta(cur, True)
print(f"  under 2 kn current: eta OFF={eta_off:.3f}h  ON={eta_on:.3f}h")
check("wind-over-water changes the routed ETA under a current", abs(eta_on - eta_off) > 1e-3)

nc_off, nc_on = eta(None, False), eta(None, True)
check("no current → byte-identical ETA (safe no-op)", nc_off == nc_on, f"{nc_off:.4f} vs {nc_on:.4f}")
O.WIND_OVER_WATER = True

print("\nRESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
