"""Realized-speed phase 2: the GLWU sea-state provider.

GLWUWave samples NOAA GLWU significant wave height (curvilinear 2-D grid → nearest in space, linear in
time; NaN cells = land → flat). Locked deterministically with a synthetic grid (no network):
  - nearest-neighbour sampling returns the right cell value;
  - time interpolation blends bracketing slices;
  - a land (NaN) cell and an out-of-grid position read as flat (0.0);
  - peak_hs ignores NaN;
  - the cycle picker lands on a real GLWU base cycle (01/07/13/19Z) and the GRIB-filter URL is well-formed;
  - a non-Great-Lakes course rejects to ZeroWave with no network call.
"""
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

from app import wave as WAVE              # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# --- synthetic curvilinear field --------------------------------------------
lon1d = np.linspace(-83.0, -82.0, 5)
lat1d = np.linspace(43.0, 44.0, 5)
lon2d, lat2d = np.meshgrid(lon1d, lat1d)        # both (5, 5)
hs0 = np.ones((5, 5), dtype="float32")          # 1 m everywhere ...
hs0[0, 0] = np.nan                              # ... except a land cell at (43.0, -83.0)
hs1 = hs0 * 2.0                                 # next slice: 2 m
E0 = 1_700_000_000
field = WAVE.GLWUWave(lat2d, lon2d, [(E0, hs0), (E0 + 3600, hs1)],
                      {"cycle": "test", "product": WAVE.GLWU_PRODUCT})

# 1) nearest sampling at a grid point
check("center cell at t0 = 1.0 m", abs(field.wave_at(43.5, -82.5, E0) - 1.0) < 1e-6)
# 2) linear-in-time blend (halfway → 1.5 m)
check("center cell at t0+30min = 1.5 m (time blend)", abs(field.wave_at(43.5, -82.5, E0 + 1800) - 1.5) < 1e-6)
check("center cell at t1 = 2.0 m", abs(field.wave_at(43.5, -82.5, E0 + 3600) - 2.0) < 1e-6)
# 3) before/after the window clamps to the end slices (no extrapolation)
check("before window clamps to first slice", abs(field.wave_at(43.5, -82.5, E0 - 9999) - 1.0) < 1e-6)
check("after window clamps to last slice", abs(field.wave_at(43.5, -82.5, E0 + 99999) - 2.0) < 1e-6)
# 4) land (NaN) cell → flat
check("land (NaN) cell reads flat (0.0)", field.wave_at(43.0, -83.0, E0) == 0.0)
# 5) out-of-grid position → flat
check("far-away position reads flat (0.0)", field.wave_at(48.0, -90.0, E0) == 0.0)
# 6) peak ignores NaN
check("peak_hs = 2.0 (ignores NaN)", abs(field.peak_hs() - 2.0) < 1e-6)
# 7) status surfaces the source (flows into result.realized.wave_source → briefing)
check("status source = glwu", field.status().get("source") == "glwu")

# --- cycle picker + URL ------------------------------------------------------
import datetime  # noqa: E402
cyc = WAVE._cycle_for(E0)
check("cycle picker lands on a GLWU base cycle (01/07/13/19Z)", cyc.hour in WAVE.GLWU_CYCLES)
cyc_back = WAVE._cycle_for(E0, back=1)
check("stepping back one cycle is also a base cycle, 6 h earlier",
      cyc_back.hour in WAVE.GLWU_CYCLES and (cyc - cyc_back) == datetime.timedelta(hours=6))
url = WAVE._grib_filter_url(cyc, (43.0, 46.0, -84.5, -82.0))
check("URL has HTSGW + surface + product + dir",
      "var_HTSGW=on" in url and "lev_surface=on" in url and f"glwu.{WAVE.GLWU_PRODUCT}" in url and "%2Fglwu." in url)

# --- domain reject (no network) ---------------------------------------------
# Atlantic bbox (north, south, west, east) — well outside the Great-Lakes domain → ZeroWave, no fetch.
atl = WAVE.build_wavefield((40.0, 38.0, -70.0, -68.0), E0, E0 + 3600, on_progress=lambda *_: None)
check("non-Great-Lakes course → ZeroWave (no network)", isinstance(atl, WAVE.ZeroWave))
# the seam still works: ConstantWave what-if + ZeroWave defaults
check("ConstantWave what-if returns the uniform Hs", WAVE.ConstantWave(1.5).wave_at(0, 0, 0) == 1.5)
check("ZeroWave is flat everywhere", WAVE.ZeroWave().wave_at(43.5, -82.5, E0) == 0.0)

print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
raise SystemExit(0 if ok else 1)
