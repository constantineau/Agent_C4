"""#30 — the polar lookup interpolates on the grid instead of adding knots to degrees.

The old lookup picked the "nearest" cell by |dTWS kn| + |dTWA deg|, so TWS 2 kn at 30° read the 10 kn
row's beat speed (6.52 kn) and every heading between 30° and the beat angle got full beat speed (beats
pinched to 30°). Pinned here on the committed SR33 cert, constant wind, no network:
  - the lookup at table points, between angles, between rows, below the beat angle, below the lowest
    TWS row, above the highest;
  - the beat VMG angle is the cert's beat angle, and a beat never sails inside it;
  - the final direct-lay step is priced like every other step (helm factor + current);
  - the approved overlay reaches the per-sail curves and a gear-loss envelope; clamp 0.85–1.15;
  - `realized` shows for helm > 1.0; the per-model fan routes on the injected polar.
Exits non-zero on any failure.
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _seed in (os.path.join(HERE, "..", "db", "seed"), "/srv"):
    if os.path.exists(os.path.join(_seed, "polars_sr33.sql")):
        os.environ["POLARS_FILE"] = os.path.join(_seed, "polars_sr33.sql")
        os.environ["SAIL_POLARS_FILE"] = os.path.join(_seed, "sr33_sail_polars.json")
        os.environ["CROSSOVERS_FILE"] = os.path.join(_seed, "sr33_crossovers.json")
        break

from app import optimizer as OPT          # noqa: E402
from app import polars as POL             # noqa: E402
from app.current import ConstantCurrent   # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def near(a, b, eps=1e-6):
    return a is not None and abs(a - b) <= eps


P = POL.polars_stw()
SP = POL.sail_polars()
sp = lambda tws, twa: OPT._polar_speed(P, tws, twa)
check("cert polar loaded", len(P) > 100 and SP)

print("1) lookup at known table points")
check("10 kn / 37.6° (beat) = 6.52", near(sp(10, 37.6), 6.52))
check("10 kn / 90° = 7.56", near(sp(10, 90), 7.56))
check("4 kn / 45.3° (beat) = 3.88", near(sp(4, 45.3), 3.88))
check("24 kn / 180° = 9.68", near(sp(24, 180), 9.68))

print("2) between angles and between rows")
check("10 kn / 45° is linear between 37.6° (6.52) and 52° (7.22)",
      near(sp(10, 45), 6.52 + (7.22 - 6.52) * (45 - 37.6) / (52 - 37.6)))
check("11 kn / 90° is midway between 10 kn (7.56) and 12 kn (7.94)", near(sp(11, 90), 7.75))
check("21 kn / 110° is a quarter of the way from 20 kn (10.19) to 24 kn (10.78)",
      near(sp(21, 110), 10.19 + (10.78 - 10.19) * 0.25))
check("above the highest row clamps to it (30 kn / 90° = 24 kn's 9.64)", near(sp(30, 90), 9.64))

print("3) below the beat angle → 0")
check("THE BUG: 2 kn / 30° is 0 (was the 10 kn row's 6.52)", sp(2, 30) == 0.0)
check("10 kn / 30° is 0", sp(10, 30) == 0.0)
check("10 kn / 37° (just inside the 37.6° beat angle) is 0", sp(10, 37.0) == 0.0)
edge11 = (37.6 + 36.7) / 2
check(f"11 kn: the beat angle is interpolated too ({edge11:.2f}°) — 0 just inside, > 0 just outside",
      sp(11, edge11 - 0.1) == 0.0 and sp(11, edge11 + 0.1) > 6.5)
check("0 below the beat angle at every TWS, never the 30° hard floor",
      all(sp(t, OPT.SPOL.min_twa(OPT.SPOL.rows_for(P), t) - 0.5) == 0.0 for t in (3, 4, 5, 7, 9, 13, 18, 22)))

print("4) below the lowest TWS row → scaled toward 0, not clamped to 4 kn")
check("2 kn / 90° = half the 4 kn row's 4.66", near(sp(2, 90), 2.33))
check("1 kn / 120° = a quarter of 4.11", near(sp(1, 120), 4.11 / 4))
check("THE BUG: 0 kn / 90° is 0 (was 4.66)", sp(0, 90) == 0.0)
check("2 kn / 44° (inside the 4 kn row's 45.3° beat) is 0", sp(2, 44) == 0.0)

print("5) continuity — no steps in TWS across the row boundaries")
worst = max(abs(sp(i / 10.0, 75) - sp((i - 1) / 10.0, 75)) for i in range(1, 260))
# the steepest legit slope is under 4 kn, where the 4 kn row (4.80 kn at 75°) scales to 0: 0.12 kn per 0.1 kn
check(f"speed at 75° is continuous 0–26 kn (largest 0.1 kn step {worst:.3f} kn)", worst < 0.13)

print("6) beat VMG angle")
check("10 kn beat VMG angle = the cert's 37.6°", OPT._vmg_twa(P, 10, "beat") == 37.6)
check("16 kn beat VMG angle = 36.0°", OPT._vmg_twa(P, 16, "beat") == 36.0)
vmg = lambda twa: sp(10, twa) * math.cos(math.radians(twa))
best = max((a / 2.0 for a in range(60, 180)), key=vmg)
check(f"brute-force best upwind VMG at 10 kn sits at the beat angle ({best}°), not 30°",
      abs(best - 37.6) <= 0.5 and vmg(30) == 0.0)
check("5 kn beat VMG angle is between the 4 and 6 kn rows' (45.3°, 42.6°)",
      42.6 <= OPT._vmg_twa(P, 5, "beat") <= 45.3)
# downwind, straight-line interpolation between 152.5° and 165° bows the VMG curve a little past the
# charted run angle (VMG 5.641 at 152.5° vs 5.644 at 154°), so the run angle lands within 2°
rows = OPT.SPOL.rows_for(P)
for t in (19.0, 19.04, 21.37, 4.5, 2.0):
    b = OPT._vmg_twa(P, t, "beat")
    hd = [h for h in OPT._vmg_headings(P, t, 0.0) if abs(OPT._wrap180(h)) < 90]
    check(f"{t} kn: the beat VMG angle ({b:.3f}°) is not inside the no-go edge "
          f"({OPT.SPOL.min_twa(rows, t):.3f}°) and its injected headings sail (> 0 kn)",
          b >= OPT.SPOL.min_twa(rows, t) and sp(t, b) > 0
          and hd and all(sp(t, abs(OPT._wrap180(h))) > 0 for h in hd))
check(f"10 kn run VMG angle ≈ the cert's 152.5° ({OPT._vmg_twa(P, 10, 'run')}°)",
      abs(OPT._vmg_twa(P, 10, "run") - 152.5) <= 2.0)


class Const:
    def __init__(self, tws, twd=0.0):
        self.tws, self.twd = tws, twd

    def wind_at(self, lat, lon, t):
        return (self.tws, self.twd)

    def detail_at(self, lat, lon, t):
        return {"tws": self.tws, "twd": self.twd, "confidence": 1.0}

    loaded = True

    def status(self):
        return {"models": []}


slat, slon = 44.0, -82.0


def twas(r, twd=0.0):
    pts = r["path"]
    out = []
    for a, b in zip(pts, pts[1:]):
        h = OPT._bearing(a["lat"], a["lon"], b["lat"], b["lon"])
        out.append(abs(OPT._wrap180(h - twd)))
    return out


print("7) a beat never pinches inside the beat angle")
beat = OPT.route_leg(Const(10.0), P, slat, slon, 0.0, slat + 12 / 60.0, slon, sail_polars=SP)
tw = twas(beat)
print(f"     12 nm beat @10 kn: {beat['eta'] / 60:.1f} min, track TWAs {[round(a, 1) for a in tw]}")
check("every step of a 12 nm beat at 10 kn sails at or below 37.6° − 1° (no 30° pinch)",
      tw and min(tw) >= 37.6 - 1.0)
check("…and its time is at least the pure-VMG bound (12 nm / (6.52·cos 37.6°))",
      beat["eta"] / 3600 >= 12 / (6.52 * math.cos(math.radians(37.6))) - 1e-6)
light = OPT.route_leg(Const(2.0), P, slat, slon, 0.0, slat + 3 / 60.0, slon, sail_polars=SP)
check(f"a 3 nm beat in 2 kn of breeze takes > 1.5 h ({light['eta'] / 3600:.2f} h; "
      "the old lookup sailed it at the 10 kn row's speed)", light["eta"] / 3600 > 1.5)

print("8) the final direct-lay step gets the helm factor and the current")
# 0.3 nm beam reach (wind from N, mark due E) — short enough to be one direct-lay step from the start.
mark = (slat, slon + 0.3 / (60.0 * math.cos(math.radians(slat))))
f = lambda **kw: OPT.route_leg(Const(12.0), P, slat, slon, 0.0, *mark, sail_polars=SP, **kw)
h10, h05 = f(helm_factor=1.0), f(helm_factor=0.5)
print(f"     0.3 nm reach: helm 1.0 {h10['eta']:.1f} s, helm 0.5 {h05['eta']:.1f} s")
check("one direct-lay step (path = start + mark)", len(h10["path"]) == 2 and len(h05["path"]) == 2)
check("helm 0.5 doubles the final step's time (was identical)", near(h05["eta"] / h10["eta"], 2.0, 1e-3))
fair = f(cur=ConstantCurrent(90.0, 1.0))
foul = f(cur=ConstantCurrent(270.0, 1.0))
cross = f(cur=ConstantCurrent(180.0, 1.0))   # sets to leeward: the boat crabs up, ~same speed
print(f"     current 1 kn: fair {fair['eta']:.1f} s, foul {foul['eta']:.1f} s, cross {cross['eta']:.1f} s")
check("a fair current shortens the final step, a foul one lengthens it",
      fair["eta"] < h10["eta"] < foul["eta"])
check("a cross current costs a little (the boat crabs) and the step still lands on the mark",
      cross["eta"] > h10["eta"] and abs(cross["path"][-1]["lat"] - mark[0]) < 1e-6)
v = OPT._polar_speed(P, 12.0, 90.0)
check("fair-current time = d / (polar + drift) on the beam reach",
      near(fair["eta"], 0.3 / (v + 1.0) * 3600, 0.5))

print("8b) the crab onto a direct lay is only accepted once its own heading is priced (Codex, PR #62)")
# 4 kn from N, mark at 51.2°, 0.4 kn current toward 141.2°: the crab walks 51.2 → 45.75 → 45.32 →
# 45.285°, a hair inside the 45.3° beat edge where the polar is 0. Three passes used to stop there.
_c = 0.4 * math.sin(math.radians(141.2 - 51.2))
_lay = OPT._crab_onto(51.2, _c, lambda h: (lambda v: (v, None) if v >= 0.3 else None)(sp(4.0, abs(OPT._wrap180(h)))))
check(f"no direct lay onto a heading inside the no-go zone ({_lay})",
      _lay is None or sp(4.0, abs(OPT._wrap180(_lay[0]))) >= 0.3)
_c = 0.4 * math.sin(math.radians(180.0 - 90.0))   # beam reach, 12 kn: settles well clear of the edge
_lay = OPT._crab_onto(90.0, _c, lambda h: (sp(12.0, abs(OPT._wrap180(h))), None))
check(f"a beam reach with a cross current still settles ({_lay and round(_lay[0], 2)}°, crabbed up)",
      _lay is not None and 80.0 < _lay[0] < 90.0)

print("9) the approved overlay reaches the per-sail curves, and a gear-loss envelope; clamp 0.85")
adj = [{"tws": 12, "twa": 150, "mult": 0.85}]
SPa = POL.adjust_sail_polars(SP, adj, P)
a3 = dict(((t, a), v) for t, a, v in SP["A3"])
a3a = dict(((t, a), v) for t, a, v in SPa["A3"])
check("A3 at 12 kn / 150° carries the 0.85 cell", near(a3a[(12.0, 150.0)], a3[(12.0, 150.0)] * 0.85))
check("A3 at 12 kn / 120° (no cell) is untouched", near(a3a[(12.0, 120.0)], a3[(12.0, 120.0)]))
check("a sail point between envelope angles in an adjusted row gets the interpolated multiplier",
      near(a3a[(12.0, 153.0)], a3[(12.0, 153.0)] * (0.85 + (1.0 - 0.85) * 3.0 / 9.2)))   # envelope row: 150 → 159.2
check("held A3 can no longer beat the adjusted envelope at 12/150 (issue: 7.14 vs 6.12)",
      OPT._sail_speed(SPa, OPT._sail_domains(SPa), "A3", 12, 150)
      <= OPT._polar_speed(POL.apply_adjustments(P, adj), 12, 150) + 1e-9)
check("the module caches are not mutated", near(dict(((t, a), v) for t, a, v in POL.sail_polars()["A3"])[(12.0, 150.0)],
                                                a3[(12.0, 150.0)]))
check("clamp lower bound is 0.85 (was 0.80)",
      near(dict(((t, a), v) for t, a, v in POL.apply_adjustments(P, [{"tws": 10, "twa": 90, "mult": 0.5}]))[(10.0, 90.0)],
           7.56 * 0.85))
check("…and 0.85 on the sail curves too",
      near(dict(((t, a), v) for t, a, v in POL.adjust_sail_polars(SP, [{"tws": 10, "twa": 90, "mult": 0.5}], P)["J1"])[(10.0, 90.0)],
           7.52 * 0.85))

# gear loss: route a run with the S1 out of service, with and without an all-run 0.85 overlay. The
# rebuilt envelope (max over the remaining sails) must keep the overlay → slower.
run_def = {"courses": [{"id": "x", "start": {"lat": slat, "lon": slon},
                        "finish": {"points": [{"lat": slat - 8 / 60.0, "lon": slon}]}}]}
slow_all = [{"tws": t, "twa": a, "mult": 0.85} for t, a, _v in P if a >= 90]
r0 = OPT.optimize_course(run_def, "x", 0, Const(12.0), avoid=False, emit_exploration=False,
                         exclude_sails=["S1"], resolution="fast", time_budget_s=60)
r1 = OPT.optimize_course(run_def, "x", 0, Const(12.0), avoid=False, emit_exploration=False,
                         exclude_sails=["S1"], polar_adjustments=slow_all,
                         resolution="fast", time_budget_s=60)
print(f"     8 nm run, S1 out: no overlay {r0.get('total_minutes')} min, 0.85 overlay {r1.get('total_minutes')} min")
check("gear-loss route keeps the overlay (0.85 everywhere downwind → ≥ 15% slower)",
      r0.get("available") and r1.get("available") and r1["total_minutes"] >= r0["total_minutes"] * 1.15)

print("9b) no wind at all: the course must not come back as a finished 0-minute route (needs #31 / PR #61)")
r_calm = OPT.optimize_course(run_def, "x", 0, Const(0.0), avoid=False, emit_exploration=False,
                             resolution="fast", time_budget_s=30)
print(f"     TWS 0: available={r_calm.get('available')} degraded={r_calm.get('degraded')} "
      f"minutes={r_calm.get('total_minutes')} warnings={(r_calm.get('warnings') or [])[:1]}")
check("TWS 0 → not available, or degraded with the stalled leg (Finish) named in a warning",
      r_calm.get("available") is False
      or (r_calm.get("degraded") is True
          and any("Finish" in w for w in (r_calm.get("warnings") or []))))

print("10) `realized` is reported for helm > 1.0; the per-model fan gets the injected polar")
rh = OPT.optimize_course(run_def, "x", 0, Const(12.0), avoid=False, emit_exploration=False, helm_factor=1.05)
check("helm 1.05 → result.realized present with helm_factor 1.05",
      (rh.get("realized") or {}).get("helm_factor") == 1.05)
rn = OPT.optimize_course(run_def, "x", 0, Const(12.0), avoid=False, emit_exploration=False)
check("helm 1.0 + flat water → realized stays None", rn.get("realized") is None)

seen = []
_orig = OPT.optimize_course


def _spy(*a, **kw):
    seen.append(kw)
    return {"available": False}


class _Sub:
    series = {("gfs", 0): [object()], ("hrrr", 0): [object()]}
    meta = [{"model": "gfs"}, {"model": "hrrr"}]
    bbox, t_start, t_end = None, 0, 1


OPT.optimize_course = _spy
try:
    OPT._per_model_paths(run_def, "x", 0, _Sub(), None, P, [(0, "S", slat, slon), (1, "F", slat - 0.1, slon)],
                         None, None, None, 1.0, polar=[(10, 40, 5.0)], polar_adjustments=adj,
                         sail_config={"k": 1}, exclude_sails=["S1"])
finally:
    OPT.optimize_course = _orig
check("each model's route gets the polar, adjustments, sail config and exclusions",
      len(seen) == 2 and all(k.get("polar") == [(10, 40, 5.0)] and k.get("polar_adjustments") == adj
                             and k.get("sail_config") == {"k": 1} and k.get("exclude_sails") == ["S1"]
                             for k in seen))

print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
