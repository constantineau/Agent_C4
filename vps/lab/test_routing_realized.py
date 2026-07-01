"""Routing fidelity 2d-d: realized (achievable) speed — helm-skill factor + sea-state degradation.

The ORC polar is a FLAT-WATER, perfectly-sailed target; the boat never quite makes it. The optimizer
routes on realized_stw = polar × helm_factor × wave_factor(hs, twa), so ETAs are achievable (not
theoretical) and the gap to the polar is a coaching number. Locked deterministically (constant wind +
ConstantWave, no network):
  - the wave factor is 1.0 in flat water, degrades with Hs, and HURTS MORE upwind than downwind;
  - a helm factor < 1 slows every leg (longer ETA) and shows up as the route's realized %;
  - sea state slows a beat more than a run; both reported in result.realized;
  - the default (helm 1.0 + flat water) is a NO-OP — geometry + ETA identical to baseline.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
# Resolve the seed dir wherever the test runs: the repo (vps/lab/../db/seed) OR docker-cp'd to /srv
# (the lab image bakes the seed files there — POLARS_FILE=/srv/polars_sr33.sql). Only override the env
# when the seed is actually found, so a bad guess never zeroes out the polars (which yields empty routes).
for _seed in (os.path.join(HERE, "..", "db", "seed"), "/srv"):
    if os.path.exists(os.path.join(_seed, "polars_sr33.sql")):
        os.environ["POLARS_FILE"] = os.path.join(_seed, "polars_sr33.sql")
        os.environ["SAIL_POLARS_FILE"] = os.path.join(_seed, "sr33_sail_polars.json")
        os.environ["CROSSOVERS_FILE"] = os.path.join(_seed, "sr33_crossovers.json")
        break

from app import optimizer as OPT          # noqa: E402
from app import wave as WAVE              # noqa: E402
from app import polars as POL             # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# 1) wave-factor shape: flat water = 1.0; a low-Hs DEADBAND costs nothing (conservative — no route
# distortion from chop); above it, gentle degradation, upwind worse than downwind, floored.
check("flat water → factor 1.0", OPT._wave_factor(0.0, 40) == 1.0 and OPT._wave_factor(0.0, 160) == 1.0)
check("small chop within the deadband → no penalty (factor 1.0)",
      OPT._wave_factor(OPT.WAVE_HS_DEADBAND, 40) == 1.0 and OPT._wave_factor(OPT.WAVE_HS_DEADBAND * 0.5, 40) == 1.0)
up = OPT._wave_factor(2.0, 40)      # beating in 2 m
down = OPT._wave_factor(2.0, 160)   # running in 2 m
print(f"     Hs=2m: upwind factor={up:.3f} downwind factor={down:.3f} (deadband {OPT.WAVE_HS_DEADBAND} m)")
check("waves slow the boat above the deadband (factor < 1)", up < 1.0 and down < 1.0)
check("conservative: 2 m upwind costs < 10%", up > 0.90)
check("a head sea hurts more than a following sea", up < down)
check("floor respected (huge Hs clamps, never negative)", OPT._wave_factor(20.0, 40) >= OPT.WAVE_FLOOR - 1e-9)
check("realized = helm × wave", abs(OPT._realized_factor(2.0, 40, 0.9) - 0.9 * up) < 1e-9)

# constant field: TWS 12, TWD 0 (from north). heading 0 = beat (dead upwind), 180 = run (dead downwind).
class Const:
    def wind_at(self, lat, lon, t):
        return (12.0, 0.0)

    def detail_at(self, lat, lon, t):
        return {"tws": 12.0, "twd": 0.0, "confidence": 0.7}


W = Const()
P = POL.polars_stw()
SP = POL.sail_polars()
O = OPT
slat, slon = 44.0, -82.0
run_d = (slat - 12.0 / 60.0, slon)        # 12 nm dead downwind
beat_d = (slat + 12.0 / 60.0, slon)       # 12 nm dead upwind


def leg(dlat, dlon, helm=1.0, waves=None):
    return O.route_leg(W, P, slat, slon, 0.0, dlat, dlon, sail_polars=SP, waves=waves, helm_factor=helm)


# 2) helm factor < 1 slows the leg + is reported
base = leg(*run_d)
slow = leg(*run_d, helm=0.85)
print(f"     run leg: helm100 eta={base['eta']:.0f} rf={base['realized_factor']}  helm85 eta={slow['eta']:.0f} rf={slow['realized_factor']}")
check("helm 100% + flat water = no-op (realized_factor 1.0)", base["realized_factor"] == 1.0)
check("helm 85% slows the ETA", slow["eta"] > base["eta"] * 1.10)
check("helm 85% reported as realized_factor ~0.85", abs(slow["realized_factor"] - 0.85) < 0.02)

# 3) sea state slows a beat more than a run (same Hs)
sea = WAVE.ConstantWave(2.0)
beat_sea = leg(*beat_d, waves=sea)
run_sea = leg(*run_d, waves=sea)
print(f"     Hs=2m: beat rf={beat_sea['realized_factor']} run rf={run_sea['realized_factor']}")
check("sea state degrades the beat more than the run", beat_sea["realized_factor"] < run_sea["realized_factor"])
check("beat realized_factor < 1 in a seaway", beat_sea["realized_factor"] < 1.0)

# 4) default (helm 1, no waves) is byte-identical to a no-realized baseline
b1 = O.route_leg(W, P, slat, slon, 0.0, run_d[0], run_d[1], sail_polars=SP)
b2 = O.route_leg(W, P, slat, slon, 0.0, run_d[0], run_d[1], sail_polars=SP, waves=WAVE.ZeroWave(), helm_factor=1.0)
check("default + ZeroWave/helm1.0 == baseline ETA", abs(b1["eta"] - b2["eta"]) < 1e-6 and b2["realized_factor"] == 1.0)


# 5) RESHAPE GATE — a spatially-varying sea may bend the route; a near-uniform one only taxes the ETA.
class Grad(WAVE.WaveField):     # Hs ramps with longitude → real spatial spread along an E-W course
    loaded = True
    source = "grad"
    epochs = [0]
    def wave_at(self, lat, lon, t):
        return max(0.0, (lon + 82.5) * 3.0)   # -82.0 → 1.5 m … -81.0 → 4.5 m
marks_ew = [(0, "start", 44.0, -82.0), (1, "finish", 44.0, -81.0)]      # varies in lon → Grad varies
marks_ns = [(0, "start", 44.0, -82.0), (1, "finish", 44.3, -82.0)]     # constant lon → Grad is uniform
sG = O._wave_field_stats(Grad(), marks_ew)
sU = O._wave_field_stats(WAVE.ConstantWave(1.2), marks_ew)
sN = O._wave_field_stats(Grad(), marks_ns)
check("field stats: gradient course has real Hs spread", sG["spread"] >= O.WAVE_RESHAPE_MIN_SPREAD)
check("field stats: uniform field → zero spread", sU["spread"] == 0.0 and abs(sU["mean"] - 1.2) < 1e-6)
check("field stats: constant-lon course through a gradient → ~uniform", sN["spread"] < O.WAVE_RESHAPE_MIN_SPREAD)
_mode = O.WAVE_RESHAPE_MODE
O.WAVE_RESHAPE_MODE = "auto"
uh, rs, _n = O._wave_reshape_decision(Grad(), sG)
check("auto + variable sea → reshape (local Hs)", rs and uh is None)
uh2, rs2, _n2 = O._wave_reshape_decision(WAVE.ConstantWave(1.2), sU)
check("auto + uniform sea → ETA-only (uniform mean Hs, no reshape)", (not rs2) and abs(uh2 - 1.2) < 1e-6)
O.WAVE_RESHAPE_MODE = "off"
uh3, rs3, _n3 = O._wave_reshape_decision(Grad(), sG)
check("mode off → never reshape (uniform Hs)", (not rs3) and uh3 is not None)
O.WAVE_RESHAPE_MODE = "on"
_uh4, rs4, _n4 = O._wave_reshape_decision(WAVE.ConstantWave(1.2), sU)
check("mode on → always reshape", rs4)
O.WAVE_RESHAPE_MODE = _mode
# ETA-only routing (uniform Hs) matches routing on a ConstantWave of that Hs — the geometry doesn't see
# the gradient, only the mean penalty.
mean = sG["mean"]
uni = O.route_leg(W, P, slat, slon, 0.0, run_d[0], run_d[1], sail_polars=SP, waves=Grad(), wave_uniform_hs=mean)
con = O.route_leg(W, P, slat, slon, 0.0, run_d[0], run_d[1], sail_polars=SP, waves=WAVE.ConstantWave(mean))
check("ETA-only (uniform Hs) == routing on a constant sea of that Hs",
      abs(uni["eta"] - con["eta"]) < 1e-6 and abs(uni["realized_factor"] - con["realized_factor"]) < 1e-9)

print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
raise SystemExit(0 if ok else 1)
