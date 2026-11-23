"""#360: RRFS replaces the retired NAM as a normal-weight wind model beside HRRR.

No network: every Open-Meteo answer is a fake built here (urlopen is stubbed), and the GRIB
models' fetches return nothing.
  1) the registry: RRFS in, NAM gone, defaults gfs+hrrr+rrfs, normal weight, 84/18 h horizon,
     synoptic cycle for a long race, hourly frames;
  2) its area: exact on the CONUS 3 km Lambert grid (Nassau in, Great Inagua out, a box east of
     the Bahamas inside the lat/lon extent but off the grid is "not here");
  3) the fetch: one gzipped Open-Meteo call per chunk for model ncep_rrfs_conus, only on-grid
     points asked, hourly frames that sample right, NaN (= no wind) off the grid, a batch that
     Open-Meteo refuses for an off-grid point split and retried, any other refusal not retried;
  4) the wind field: RRFS blends at weight 1.0, a saved "nam" is dropped quietly, off the US it is
     "not here" with no call;
  5) model skill / venue study: rrfs -> ncep_rrfs_conus, no calls for years before 2026, and
     its short history leaves its weight at x1;
  6) the plume: RRFS supported and NAM not, Lab and boat lists the same.
"""
import ast
import datetime as dt
import gzip
import io
import json
import math
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request

_tmp = tempfile.mkdtemp()
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ["PLUME_DIR"] = os.path.join(_tmp, "plume")
os.environ["GRIB_ISOLATE_PARSE"] = "0"
os.environ.pop("ANTHROPIC_API_KEY", None)

import numpy as np  # noqa: E402

from app import modelskill, plume, venuestudy  # noqa: E402
from app.wind import grib, models as M, windfield as WF  # noqa: E402

M.CACHE = os.environ["GRIB_CACHE"]

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra else ''}")


R = M.MODELS["rrfs"]
UTC = dt.timezone.utc
SORC = (27.0, 25.0, -80.5, -77.0)              # (n, s, w, e): Fort Lauderdale -> Nassau
NASSAU = (25.06, -77.35)
LAUDERDALE = (26.1, -80.05)
CHANNEL = (50.6, 49.9, -1.6, -0.5)

# ---- 1) the registry ----------------------------------------------------------------------------
print("registry:")
check("rrfs is a model, nam is gone", "rrfs" in M.MODELS and "nam" not in M.MODELS)
check("NAM's class is deleted", not hasattr(M, "NAM"))
check("default blend is gfs + hrrr + rrfs", M.DEFAULT_MODELS == ("gfs", "hrrr", "rrfs"), M.DEFAULT_MODELS)
check("normal weight: priority 1.0, no boost", R.priority == 1.0 and R.kind == "deterministic")
am = M.available_models()
check("/api/models lists RRFS (84 h, US only), not NAM",
      am["rrfs"]["horizon_h"] == 84 and am["rrfs"]["coverage"] == "US only" and "nam" not in am, am.get("rrfs"))
check("synoptic cycles reach 84 h, the others 18 h",
      [R.horizon_for(dt.datetime(2026, 11, 16, h, tzinfo=UTC)) for h in (0, 6, 12, 18, 1, 13, 23)]
      == [84, 84, 84, 84, 18, 18, 18])
now = dt.datetime(2026, 11, 16, 15, 30, tzinfo=UTC)
check("a short race takes the freshest hourly cycle", R.pick_cycle(now).hour == 13, R.pick_cycle(now))
check("a race past 18 h takes the freshest synoptic cycle",
      R.pick_cycle(now, min_horizon_h=40) == dt.datetime(2026, 11, 16, 12, tzinfo=UTC))
check("hourly forecast hours to f84", R.fhrs(200) == list(range(0, 85)) and R.fhrs(10) == list(range(0, 11)))
check("cycle step-back goes one hour", R.prev_cycle(dt.datetime(2026, 11, 16, 0, tzinfo=UTC))
      == dt.datetime(2026, 11, 15, 23, tzinfo=UTC))

# ---- 2) the area ----------------------------------------------------------------------------
print("area:")
i, j = M.conus3km_ij(21.138123, -122.719528)
i2, j2 = M.conus3km_ij(47.842, -60.917)
check("grid maths: first point is (0, 0), the far corner (1798, 1058)",
      abs(i) < 1e-6 and abs(j) < 1e-6 and abs(i2 - 1798) < 0.1 and abs(j2 - 1058) < 0.1, (i2, j2))
check("covers Nassau, Fort Lauderdale, Lake Huron",
      M.covers("rrfs", *NASSAU) and M.covers("rrfs", *LAUDERDALE) and M.covers("rrfs", 44.5, -82.5))
check("not Great Inagua, the Channel, Puerto Rico, or the Atlantic east of the Bahamas",
      not any(M.covers("rrfs", la, lo) for la, lo in ((21.0, -73.6), (50.3, -1.2), (18.4, -66.0), (24.0, -60.5))))
check("the SORC box is covered", M.covers_bbox("rrfs", SORC))
EAST = (24.0, 22.0, -64.0, -62.0)              # inside the lat/lon extent, off the grid
check("a box inside the lat/lon extent but off the grid is not", not M.covers_bbox("rrfs", EAST)
      and M.covers_bbox("hrrr", EAST))
check("a box only partly on the grid is", M.covers_bbox("rrfs", (23.0, 20.0, -77.0, -73.0)))
check("the Channel box is not", not M.covers_bbox("rrfs", CHANNEL))
check("the plain note", M.coverage_note("rrfs") == "RRFS: not here (US only)", M.coverage_note("rrfs"))
check("a big box coarsens the grid to stay under the point cap",
      len(R._grid((35.0, 20.0, -90.0, -60.0))[0]) * len(R._grid((35.0, 20.0, -90.0, -60.0))[1]) <= R.MAX_POINTS
      and R._step(SORC) >= R.GRID_STEP)

# ---- 3) the fetch (Open-Meteo stubbed) ------------------------------------------------------------
print("fetch:")
# A day ahead of the clock, on a whole hour: a fixed date turned the GRIB path off once it
# passed (build_windfield only fetches the forecast hours still ahead of now).
T0 = (int(dt.datetime.now(UTC).timestamp()) // 3600 + 24) * 3600
calls = []
BAD = set()                 # points the fake server refuses as off-grid
REFUSE = {"why": None}      # a reason to refuse every call with


def wind_at(lat, lon, t):
    """The fake forecast: speed grows east, direction 90°, both fixed in time + an hour ramp."""
    return 5.0 + (lon + 80.5) + 0.01 * ((t - T0) / 3600.0), 90.0


class FakeResp(io.BytesIO):
    def __init__(self, body, gz=True):
        super().__init__(gzip.compress(body) if gz else body)
        self.headers = {"Content-Encoding": "gzip"} if gz else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    lats = [float(x) for x in q["latitude"][0].split(",")]
    lons = [float(x) for x in q["longitude"][0].split(",")]
    calls.append({"q": q, "n": len(lats), "gzip": req.get_header("Accept-encoding"),
                  "pts": list(zip(lats, lons))})
    if REFUSE["why"]:
        raise urllib.error.HTTPError(url, 400, "Bad Request", {},
                                     io.BytesIO(json.dumps({"error": True, "reason": REFUSE["why"]}).encode()))
    if any((la, lo) in BAD for la, lo in zip(lats, lons)):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(
            b'{"error":true,"reason":"No data is available for this location"}'))
    t0 = int(dt.datetime.strptime(q["start_hour"][0], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp())
    t1 = int(dt.datetime.strptime(q["end_hour"][0], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp())
    times = list(range(t0, t1 + 1, 3600))
    out = []
    for la, lo in zip(lats, lons):
        sp, dr = zip(*(wind_at(la, lo, t) for t in times))
        out.append({"latitude": la, "longitude": lo,
                    "hourly": {"time": times, "wind_speed_10m": list(sp), "wind_direction_10m": list(dr)}})
    return FakeResp(json.dumps(out if len(out) > 1 else out[0]).encode())


real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_urlopen
try:
    log = []
    series, meta = R.load_series(SORC, T0, T0 + 30 * 3600, on_progress=log.append)
    fr = series.get(("rrfs", "det")) or []
    check("one call per 90 points, model ncep_rrfs_conus in m/s, gzip asked",
          calls and all(c["q"]["models"] == ["ncep_rrfs_conus"] and c["q"]["wind_speed_unit"] == ["ms"]
                        and c["gzip"] == "gzip" and c["n"] <= 90 for c in calls), calls[0]["q"]["models"] if calls else None)
    asked = sum(c["n"] for c in calls)
    check("the SORC box is asked at under the point cap", 0 < asked <= R.MAX_POINTS, asked)
    check("hourly frames across the window (+3 h pads)", len(fr) == 37
          and all(b.valid_time - a.valid_time == 3600 for a, b in zip(fr, fr[1:])), len(fr))
    check("meta: frames, the Open-Meteo run, how far it reaches",
          meta["frames"] == 37 and "ncep_rrfs_conus" in meta["cycle"] and meta["last_valid"] == T0 + 33 * 3600
          and meta["priority"] == 1.0, meta)
    uv = fr[3].sample_uv(*NASSAU)
    tws, twd = grib.uv_to_tws_twd(*uv)
    want = wind_at(*NASSAU, fr[3].valid_time)[0] * 1.9438445
    check("a sample at Nassau reads the fake wind back (bilinear, 90°)",
          abs(tws - want) < 0.3 and abs(twd - 90.0) < 0.5, (round(tws, 2), round(want, 2), round(twd, 1)))
    check("the progress line says what it did", any("rrfs:" in m and "Open-Meteo" in m for m in log), log)

    # a box over the grid's south-east edge: only on-grid points are asked, off-grid stays empty
    calls.clear()
    EDGE = (23.5, 21.0, -76.0, -73.0)
    series, meta = R.load_series(EDGE, T0, T0 + 6 * 3600)
    fr = series.get(("rrfs", "det")) or []
    pts = [p for c in calls for p in c["pts"]]
    check("edge box: every point asked is on the grid", pts and all(M.on_conus3km(*p) for p in pts), len(pts))
    check("edge box: frames built", len(fr) == 13, len(fr))
    check("edge box: no wind off the grid (Great Inagua), wind on it",
          fr and fr[0].sample_uv(21.05, -73.6) is None and fr[0].sample_uv(23.4, -75.9) is not None)

    # Open-Meteo refuses a batch with one off-grid point: split, retried, that point left empty
    calls.clear()
    R.on_grid = lambda la, lo: True             # pretend the grid test let an off-grid point through
    lats, lons = R._grid(SORC)
    bad = (lats[2], lons[3])
    BAD.add(bad)
    try:
        series, meta = R.load_series(SORC, T0, T0 + 6 * 3600)
    finally:
        del R.on_grid
        BAD.clear()
    fr = series.get(("rrfs", "det")) or []
    check("a refused batch is split and the rest still loads", len(fr) == 13 and len(calls) > 5, len(calls))
    check("…and only that point is empty",
          fr and math.isnan(fr[0].u[2, 3]) and np.isfinite(fr[0].u).sum() == fr[0].u.size - 1)

    # any other refusal is a real failure: not retried, the model is skipped, no exception
    calls.clear()
    REFUSE["why"] = "Parameter 'models' is invalid"
    log = []
    try:
        series, meta = R.load_series(SORC, T0 + 7200, T0 + 6 * 3600, on_progress=log.append)
    finally:
        REFUSE["why"] = None
    check("other 400: one call, nothing loaded, said", len(calls) == 1 and series == {} and meta["frames"] == 0
          and any("unavailable" in m for m in log), (len(calls), log))

    # ---- 4) the wind field ------------------------------------------------------------------------
    print("wind field:")
    grib_asked = []

    def stub_fetch(self, cycle, fhr, member, bbox, timeout=60):
        grib_asked.append(self.name)
        return None

    real_fetch = M.ModelSource.fetch
    M.ModelSource.fetch = stub_fetch
    try:
        calls.clear()
        wf = WF.build_windfield(SORC, T0, T0 + 6 * 3600, models=("gfs", "rrfs", "nam"), fallback_tries=0)
        check("a saved 'nam' is dropped quietly; gfs asked, rrfs loaded",
              "gfs" in grib_asked and [m["model"] for m in wf.meta] == ["gfs", "rrfs"]
              and ("rrfs", "det") in wf.series, [m["model"] for m in wf.meta])
        d = wf.detail_at(*NASSAU, T0 + 3600)
        check("the field answers at Nassau from RRFS", d and d["models"] == ["rrfs"], d)
        calls.clear()
        wf = WF.build_windfield(CHANNEL, T0, T0 + 6 * 3600, models=("gfs", "rrfs"), fallback_tries=0)
        check("off the US: no Open-Meteo call, and the plain note", not calls
              and WF.coverage_notes(wf.meta) == ["RRFS: not here (US only)."], WF.coverage_notes(wf.meta))
    finally:
        M.ModelSource.fetch = real_fetch
finally:
    urllib.request.urlopen = real_urlopen

lat = np.array([25.0, 26.0, 27.0])
lon = np.array([-80.0, -79.0, -78.0])
u = np.ones((3, 3))
u[2, 2] = np.nan                               # one point off a regional grid
f = grib.GribFrame("rrfs", "det", float(T0), lat, lon, u, u.copy(), True)
check("a sample touching a NaN point is no wind, not NaN; away from it, wind",
      f.sample_uv(26.9, -78.1) is None and f.sample_uv(25.5, -79.5) == (1.0, 1.0))
blend = WF.WindField({("nam", "det"): [f]}, [], SORC, T0, T0 + 3600)._samples(25.5, -79.5, T0, hold=False)[0]
check("a series of an unregistered model blends at x1, never a KeyError", blend and blend[0][2] == 1.0, blend)

# ---- 5) model skill / venue study -----------------------------------------------------------------
print("model skill:")
check("rrfs -> ncep_rrfs_conus", modelskill.OM_MODELS.get("rrfs") == "ncep_rrfs_conus" and "nam" not in modelskill.OM_MODELS)
check("rrfs scored by default and in a venue study",
      "rrfs" in modelskill.DEFAULT_MODELS and "rrfs" in venuestudy.STUDY_MODELS and "nam" not in venuestudy.STUDY_MODELS)
asked_fc = []
real_ff = modelskill.fetch_forecast
modelskill.fetch_forecast = lambda la, lo, sd, ed, m, timeout=45: asked_fc.append((m, sd)) or {T0: (10.0, 90.0)}
try:
    old = modelskill.forecast_series(*NASSAU, "2024-11-01", "2024-11-20", "rrfs", 2024)
    new = modelskill.forecast_series(*NASSAU, "2026-11-01", "2026-11-14", "rrfs", 2026)
    gfs = modelskill.forecast_series(*NASSAU, "2024-11-01", "2024-11-20", "gfs", 2024)
finally:
    modelskill.fetch_forecast = real_ff
check("no call for an RRFS year before 2026; 2026 and GFS 2024 asked",
      old == {} and new and gfs and asked_fc == [("rrfs", "2026-11-01"), ("gfs", "2024-11-01")], asked_fc)
check("an unknown model ('nam') returns nothing without a call", real_ff(25.0, -77.0, "2026-11-01", "2026-11-02", "nam") == {})
scores = {"gfs": {"n": 400, "vector_rmse_kn": 4.0, "speed_bias_kn": 0.5, "dir_bias_deg": 3.0},
          "hrrr": {"n": 400, "vector_rmse_kn": 3.5, "speed_bias_kn": 0.2, "dir_bias_deg": 1.0},
          "rrfs": {"n": modelskill.MIN_N - 1, "vector_rmse_kn": 2.0, "speed_bias_kn": 0.1, "dir_bias_deg": 1.0}}
dw = modelskill.derive_weights(scores)
check("a short RRFS history is not weighted (x1) while the others are", "rrfs" not in dw and "gfs" in dw, dw)

# ---- 6) the plume ---------------------------------------------------------------------------------
print("plume:")
check("Lab plume: rrfs supported and recorded by default, nam not",
      "rrfs" in plume.SUPPORTED and "nam" not in plume.SUPPORTED and "rrfs" in plume.MODELS)
agent_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent", "app", "plume.py")
boat = None
for node in ast.parse(open(agent_src).read()).body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "SUPPORTED":
        boat = ast.literal_eval(node.value)
check("the boat accepts exactly what the Lab sends", boat == plume.SUPPORTED, boat)

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
