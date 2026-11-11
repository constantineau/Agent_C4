"""#308: currents and waves come from the source that covers the course area, and say when none does.

No network: every fetch is stubbed.
  1) a synthetic second venue (open sea, Florida-like) next to a synthetic lake: the area picks the
     source, the finest covering source wins, a coarser one is tried when it has no data, and
     outside every source the field is "none: outside coverage", said plainly;
  2) the real masks: Lake Erie and Lake St Clair (inside LMHOFS's old box, not its water) get no
     current source; Lake Huron gets LMHOFS; off Fort Lauderdale gets HYCOM and GFS-Wave; the
     middle of a continent gets none; tidal water with no current gets the loud warning;
  3) a route through a strong north-setting current band (the Florida Current shape) bends into it
     and gets there sooner;
  4) the HYCOM reader: OPeNDAP ASCII parsing, a box across 0° longitude, and a full stubbed fetch;
  5) GFS-Wave asks only for forecast hours that exist.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ.pop("ANTHROPIC_API_KEY", None)

from app import current as CUR, wave as WAV, seasource as S, hycom as H, optimizer as O  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra else ''}")


def box_cov(s, n, w, e, step=0.1):
    """A synthetic water mask: water in s..n, w..e on a 0.1° raster from (0, 0)."""
    cells = [(r, c) for r in range(int(s / step), int(n / step))
             for c in range(int(w / step), int(e / step))]
    return S.Coverage.from_cells(0.0, 0.0, step, 400, 400, cells)


class Fake(S.SeaSource):
    def __init__(self, kind, sid, res, cov, sea, result):
        self.kind, self.id, self.name, self.resolution_km, self.sea = kind, sid, sid, res, sea
        self._coverage, self.result, self.calls = cov, result, []

    def fetch(self, bbox, t_start, t_end, log):
        self.calls.append(bbox)
        return self.result() if callable(self.result) else self.result


def loaded_current(label):
    f = CUR.ConstantCurrent(0, 2.5)
    f.source, f.label = label.lower(), label
    return f


def loaded_wave(label):
    f = WAV.ConstantWave(1.2)
    f.source, f.label = label.lower(), label
    return f


T0 = 1_800_000_000
REAL_CUR, REAL_WAV = list(CUR.SOURCES), list(WAV.SOURCES)

print("1) a synthetic sea venue and a synthetic lake")
# the lake: 10..12 N, 10..12 E. The sea: 20..30 N, 20..30 E, and its coarse model also reaches
# over the lake's water (so "finest wins" is what picks the lake model there).
lake_c = Fake("current", "lake-c", 1.0, box_cov(10, 12, 10, 12), False, lambda: loaded_current("LAKE"))
sea_c = Fake("current", "sea-c", 8.0, box_cov(9, 31, 9, 31), True, lambda: loaded_current("SEA"))
lake_w = Fake("waves", "lake-w", 2.5, box_cov(10, 12, 10, 12), False, lambda: loaded_wave("LAKEW"))
sea_w = Fake("waves", "sea-w", 18.0, box_cov(9, 31, 9, 31), True, lambda: loaded_wave("SEAW"))
CUR.SOURCES[:] = [lake_c, sea_c]
WAV.SOURCES[:] = [sea_w, lake_w]          # order doesn't matter: resolution does

SEA_BOX = (25.6, 25.0, 25.0, 25.6)        # (north, south, west, east)
LAKE_BOX = (11.6, 11.0, 11.0, 11.6)
LAND_BOX = (35.6, 35.0, 35.0, 35.6)

cur = CUR.build_currentfield(SEA_BOX, T0, T0 + 6 * 3600)
wv = WAV.build_wavefield(SEA_BOX, T0, T0 + 6 * 3600)
check("sea course → the sea current source", cur.label == "SEA" and sea_c.calls and not lake_c.calls)
check("sea course → the sea wave source", wv.label == "SEAW")
d = S.data_block(cur, wv, {}, SEA_BOX)
check("data block names both sources", d["current"] == "SEA" and d["waves"] == "SEAW", d)
check("…and has nothing missing to say", d["notes"] == [] and d["warning"] is None, d)

for f in (lake_c, sea_c):
    f.calls.clear()
cur = CUR.build_currentfield(LAKE_BOX, T0, T0 + 6 * 3600)
check("lake course → the finer lake model wins over the sea model that also covers it",
      cur.label == "LAKE" and lake_c.calls and not sea_c.calls)
check("lake course → lake waves", WAV.build_wavefield(LAKE_BOX, T0, T0 + 3600).label == "LAKEW")

lake_c.result = None                       # the lake model has no data today
cur = CUR.build_currentfield(LAKE_BOX, T0, T0 + 6 * 3600)
check("the finest source has no data → the next covering one is used", cur.label == "SEA")
lake_c.result = lambda: loaded_current("LAKE")

logs = []
cur = CUR.build_currentfield(LAND_BOX, T0, T0 + 6 * 3600, on_progress=logs.append)
wv = WAV.build_wavefield(LAND_BOX, T0, T0 + 6 * 3600)
check("outside every source → no current, no fetch", not cur.loaded and cur.why == S.OUTSIDE)
check("outside every source → flat water", not wv.loaded and wv.why == S.OUTSIDE)
d = S.data_block(cur, wv, {}, LAND_BOX)
check('data block says "none: outside coverage"',
      d["current"] == "none: outside coverage" and d["waves"] == "none: outside coverage", d)
check("…and says it plainly", d["notes"] == ["No current data for this area.",
                                             "No wave data for this area."], d["notes"])
check("…said in the log too", any("no source covers" in m for m in logs), logs)

sea_c.result = None                        # the sea model is down: tidal water, no current
cur = CUR.build_currentfield(SEA_BOX, T0, T0 + 6 * 3600)
d = S.data_block(cur, wv, {}, SEA_BOX)
check("sea model down → none: no data for the race time", d["current"] == "none: " + S.NO_DATA, d)
check("tidal water with no current → the loud warning", d["warning"] == S.TIDAL_WARNING, d)
d = S.data_block(cur, wv, {"tidal": False}, SEA_BOX)
check("a race that says it isn't tidal → no warning", d["warning"] is None)
d = S.data_block(CUR.ZeroCurrent(S.NO_DATA), wv, {}, LAKE_BOX)
check("a lake with no current → not tidal, no warning", d["warning"] is None)
d = S.data_block(CUR.ZeroCurrent(S.OFF), wv, {}, SEA_BOX)
check("current turned off → said, but no tidal alarm", d["warning"] is None
      and d["current"] == "none: turned off")
sea_c.result = lambda: loaded_current("SEA")

CUR.SOURCES[:], WAV.SOURCES[:] = REAL_CUR, REAL_WAV

print("2) the real masks")
seen = []
REAL_HFETCH = H.fetch
CUR._build_lmhofs = lambda *a: seen.append("lmhofs") or loaded_current("NOAA LMHOFS")
H.fetch = lambda *a, **k: seen.append("hycom") or loaded_current("HYCOM ESPC (global)")
WAV._build_glwu = lambda *a: seen.append("glwu") or loaded_wave("NOAA GLWU")
WAV._build_gfswave = lambda bbox, t0, t1, log, grid, label, now=None: \
    seen.append("gfswave " + grid) or loaded_wave(label)


def run(bbox):
    seen.clear()
    c = CUR.build_currentfield(bbox, T0, T0 + 6 * 3600)
    w = WAV.build_wavefield(bbox, T0, T0 + 6 * 3600)
    return c, w, list(seen)


VENUES = {   # (north, south, west, east), padded like optimizer.course_bbox
    "Lake Erie (Put-in-Bay)": (42.1, 41.3, -83.2, -82.3),
    "Lake St Clair": (42.62, 42.38, -82.85, -82.55),
    "Lake Huron (Bayview)": (45.9, 42.95, -84.7, -82.3),
    "off Fort Lauderdale": (26.6, 25.6, -80.6, -79.5),
    "Kansas": (39.0, 38.0, -99.0, -98.0),
}
c, w, s = run(VENUES["Lake Erie (Put-in-Bay)"])
check("Lake Erie → no current source (inside LMHOFS's old box, not its water)",
      not c.loaded and c.why == S.OUTSIDE and "lmhofs" not in s and "hycom" not in s, s)
check("Lake Erie → GLWU waves", w.label == "NOAA GLWU", s)
c, w, s = run(VENUES["Lake St Clair"])
check("Lake St Clair → no current source", not c.loaded and c.why == S.OUTSIDE, s)
c, w, s = run(VENUES["Lake Huron (Bayview)"])
check("Lake Huron → LMHOFS current, GLWU waves", c.label == "NOAA LMHOFS" and w.label == "NOAA GLWU", s)
c, w, s = run(VENUES["off Fort Lauderdale"])
check("off Fort Lauderdale → HYCOM current", c.label == "HYCOM ESPC (global)" and "lmhofs" not in s, s)
check("off Fort Lauderdale → GFS-Wave 1/6°", s[-1:] == ["gfswave global.0p16"], s)
check("Fort Lauderdale is tidal water", S.is_tidal({}, VENUES["off Fort Lauderdale"]))
check("Lake Huron isn't", not S.is_tidal({}, VENUES["Lake Huron (Bayview)"]))
c, w, s = run(VENUES["Kansas"])
check("the middle of a continent → none, nothing fetched", not c.loaded and not w.loaded and not s, s)

print("3) a route through a strong north-setting current band bends into it")
# a Florida-Current shape: 3 kn north from 0.04° east of the rhumb line, still water inshore
lons = [round(-80.30 + 0.02 * i, 2) for i in range(31)]          # -80.30 .. -79.70
lats = [round(25.90 + 0.02 * j, 2) for j in range(31)]           # 25.90 .. 26.50
V = 3.0 / 1.94384


def frame():
    v = [[V if lo >= -80.01 else 0.0 for lo in lons] for _la in lats]
    return [[0.0] * len(lons) for _ in lats], v


u0, v0 = frame()
stream = CUR.GLOFSCurrent(lons, lats, [(0, u0, v0), (10 ** 10, u0, v0)], {}, source="synthetic")
check("the synthetic stream is 3 kn north offshore", abs(stream.current_at(26.2, -79.9, 0)[1] - 3.0) < 0.05
      and abs(stream.current_at(26.2, -79.9, 0)[0]) < 1e-6)
check("…and still inshore", stream.current_at(26.2, -80.2, 0) == (0.0, 0.0))

rows = {30: 2.8, 40: 4.2, 42: 4.4, 50: 4.9, 60: 5.3, 75: 5.7, 90: 5.9,
        110: 5.8, 135: 5.2, 150: 4.6, 165: 4.0, 180: 3.2}
P = [(8.5, a, sp) for a, sp in rows.items()]


class Steady:               # wind FROM the east: due north is a beam reach
    def wind_at(self, la, lo, t): return (8.5, 90.0)
    def detail_at(self, la, lo, t): return {"tws": 8.5, "twd": 90.0, "confidence": 0.9}


A, B = (26.0, -80.05), (26.4, -80.05)        # 24 nm due north, just inshore of the stream
L0 = O.route_leg(Steady(), P, A[0], A[1], 0.0, B[0], B[1], hstep=6, dt_cap=0.5)
L1 = O.route_leg(Steady(), P, A[0], A[1], 0.0, B[0], B[1], hstep=6, dt_cap=0.5, cur=stream)
east0 = max(p["lon"] for p in L0["path"])
east1 = max(p["lon"] for p in L1["path"])
end1 = O._hav_nm(L1["path"][-1]["lat"], L1["path"][-1]["lon"], B[0], B[1])
print(f"     no current: {L0['eta'] / 3600:.2f} h, east-most {east0:.3f}; "
      f"stream: {L1['eta'] / 3600:.2f} h, east-most {east1:.3f}")
check("with the stream the route still reaches the mark", end1 < 0.3, f"{end1:.2f} nm")
check("the route bends east into the stream", east1 > -80.0 and east1 > east0 + 0.04,
      f"{east1:.3f} vs {east0:.3f}")
check("…and gets there sooner", L1["eta"] < L0["eta"] * 0.92,
      f"{L1['eta'] / 3600:.2f} h vs {L0['eta'] / 3600:.2f} h")

print("4) the HYCOM reader")
ASCII = """Dataset {
    Grid {
     ARRAY:
        Float32 ssu[time = 2][lat = 2][lon = 3];
    } ssu;
} x;
---------------------------------------------
ssu.ssu[2][2][3]
[0][0], NaN, 0.5, 1.0
[0][1], 0.0, 0.25, 0.75
[1][0], NaN, 0.5, 1.0
[1][1], 0.0, 0.25, 0.75

ssu.time[2]
10.0, 11.0

ssu.lat[2]
26.0, 26.04

ssu.lon[3]
279.92, 280.0, 280.08

"""
got = H.parse_ascii(ASCII)
import math  # noqa: E402
check("parses a grid array in row order", math.isnan(got["ssu.ssu"][0])
      and got["ssu.ssu"][1:6] == [0.5, 1.0, 0.0, 0.25, 0.75], got["ssu.ssu"][:6])
check("…and its maps", got["ssu.time"] == [10.0, 11.0] and got["ssu.lon"] == [279.92, 280.0, 280.08])

axis = [round(0.08 * i, 2) for i in range(4500)]
r = H.lon_ranges(axis, -80.3, -79.6)
check("a western box is one range, shifted to -180..180",
      len(r) == 1 and abs(axis[r[0][0]] + r[0][2] - (-80.32)) < 0.05 and r[0][2] == -360.0, r)
r = H.lon_ranges(axis, -0.5, 0.4)
check("a box across 0° is two ranges, ascending",
      len(r) == 2 and axis[r[0][0]] + r[0][2] < 0 and axis[r[1][1]] + r[1][2] > 0.3, r)

# a whole fetch against a stubbed server: DAS, time, axes, then the box
lat_axis = [round(-80 + 0.04 * j, 2) for j in range(4251)]
NOW = 1_800_000_000.0


def fake_get(url):
    if url.endswith(".das"):
        return 'Attributes {\n    time {\n        String units "hours since 2027-01-15 00:00:00.000 UTC";\n    }\n}'
    q = H.urllib.parse.unquote(url.split("?", 1)[1])
    if q == "time":
        return "x\n---\ntime[400]\n" + ", ".join(str(float(h)) for h in range(400)) + "\n"
    if q == "lat,lon":
        return ("x\n---\nlat[4251]\n" + ", ".join(map(str, lat_axis)) + "\n\nlon[4500]\n"
                + ", ".join(map(str, axis)) + "\n")
    # ssu[k0:ks:k1][j0:hs:j1][i0:hs:i1],ssv[...]: answer 0.5 m/s east, 1.0 m/s north everywhere
    import re
    k0, ks, k1, j0, js, j1, i0, is_, i1 = map(int, re.findall(r"\d+", q.split(",")[0]))
    ts = list(range(k0, k1 + 1, ks))
    js_ = list(range(j0, j1 + 1, js))
    is2 = list(range(i0, i1 + 1, is_))
    out = "x\n---\n"
    for var, val in (("ssu", 0.5), ("ssv", 1.0)):
        out += f"{var}.{var}[{len(ts)}][{len(js_)}][{len(is2)}]\n"
        for a in range(len(ts)):
            for b in range(len(js_)):
                out += f"[{a}][{b}], " + ", ".join([str(val)] * len(is2)) + "\n"
        out += (f"\n{var}.time[{len(ts)}]\n" + ", ".join(str(float(t)) for t in ts)
                + f"\n\n{var}.lat[{len(js_)}]\n" + ", ".join(str(lat_axis[j]) for j in js_)
                + f"\n\n{var}.lon[{len(is2)}]\n" + ", ".join(str(axis[i]) for i in is2) + "\n\n")
    return out


H.fetch, H._get, H._axes, H._cache = REAL_HFETCH, fake_get, {}, {}
import datetime  # noqa: E402
ref = datetime.datetime(2027, 1, 15, tzinfo=datetime.timezone.utc).timestamp()
t0 = ref + 100 * 3600
field = H.fetch((26.4, 26.0, -80.2, -79.8), t0, t0 + 12 * 3600, lambda *_: None, now=t0)
check("a stubbed HYCOM fetch loads", field is not None and field.loaded, field and field.status())
sd = field.current_at(26.2, -80.0, t0 + 3600)
check("…north-north-east at 2.2 kn (0.5 east, 1.0 north m/s)",
      abs(sd[1] - 1.118 * 1.94384) < 0.02 and abs(sd[0] - 26.57) < 0.5, sd)
check("…time slices every 3 h over the window (with a step each side)",
      [round((e - t0) / 3600) for e in field.epochs] == [-3, 0, 3, 6, 9, 12, 15], field.epochs)
check("…named for the data block", S.describe(field) == "HYCOM ESPC (global)")
far = H.fetch((26.4, 26.0, -80.2, -79.8), NOW + 30 * 86400, NOW + 31 * 86400, lambda *_: None,
              now=NOW)
check("a race past the forecast's reach → None, without asking the server", far is None)

print("5) GFS-Wave hours")
hrs = WAV._gfswave_hours(0, 100 * 3600, 130 * 3600)
check("hourly files to f120, 3-hourly after, one step each side",
      hrs[0] == 97 and all(h <= 120 or h % 3 == 0 for h in hrs) and hrs[-1] >= 130, hrs)

print("ALL OK" if ok else "SOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
