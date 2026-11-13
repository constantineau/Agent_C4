"""#309: a buoy network outside US water (Ireland's Marine Institute, on ERDDAP), and the opt-in
world depth grid.

No network: every ERDDAP read goes through obsarea._get, stubbed here with small CSV fixtures in
ERDDAP's format (a header row, a units row, then data). No grid is downloaded.
  1) an Irish course box gets the active Irish buoys (an archive buoy is left out), named by
     their source, beside the plain "NDBC buoys: not here" note;
  2) anywhere else the network is never asked and says nothing (no note per network);
  3) observed wind: knots kept, m/s converted, a unit we don't know dropped (never guessed),
     missing-flagged and blank rows dropped, an empty window (ERDDAP answers 404) is {};
     model skill reads it through modelskill.obs_series;
  4) venue skill anchors on the nearest Irish buoy; Great Lakes anchors unchanged, no fetch;
  5) the venue study's station finder lists past buoys too (with their last report), keeps a
     window they can't cover out, and ticks the nearest buoy with data;
  6) a station list that won't load is a note, not an error; garbage rows are skipped;
  7) depth: the ETOPO world grid is opt-in (DEPTH_GRIDS=default,etopo), a race box picks only the
     15-degree tiles it touches, and they keep the "not a chart" label. Unset = the built-ins only.
"""
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse

_tmp = tempfile.mkdtemp()
os.environ["GRIB_CACHE"] = os.path.join(_tmp, "gribcache")
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("DEPTH_GRIDS", None)

from app import modelskill, obsarea, obserddap, obsstations, venue  # noqa: E402
from app.geo import depthsrc  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra else ''}")


def _day(days_ago):
    return time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(time.time() - days_ago * 86400))


STATIONS_CSV = "\n".join([
    "station_id,longitude,latitude,time",
    ",degrees_east,degrees_north,UTC",
    f"M2,-5.4302,53.4836,{_day(0)}",
    f"M3,-10.548261,51.215956,{_day(0)}",
    f"M5,-6.704336,51.690425,{_day(1)}",
    "M1,-11.2,53.1266,2007-07-09T12:00:00Z",           # archive buoy: not active
    f"bad id!,-9.0,52.0,{_day(0)}",                    # not a usable id: skipped
    f"M9,abc,52.0,{_day(0)}",                          # no position: skipped
    "",
])
OBS = {   # station -> (speed unit, rows "time,speed,dir,qc")
    "M3": ("knots", ["2025-07-01T00:00:00Z,15.6,343.0,0", "2025-07-01T01:00:00Z,13.7,353.0,1",
                     "2025-07-01T02:00:00Z,NaN,348.0,0", "2025-07-01T03:00:00Z,16.0,346.0,9",
                     "2025-07-01T04:00:00Z,,340.0,0", "2025-07-01T05:00:00Z,12.0,360.0,0"]),
    "M5": ("m/s", ["2025-07-01T00:00:00Z,10.0,200.0,0"]),
    "M2": ("furlongs/fortnight", ["2025-07-01T00:00:00Z,10.0,200.0,0"]),
}
FETCHED = []
DOWN = {"on": False}


def fake_get(url, timeout=20):
    FETCHED.append(url)
    if "aviationweather" in url:
        return "[]"                                    # no airports: only the buoys matter here
    if "erddap.marine.ie" not in url:
        raise AssertionError("unexpected fetch " + url)
    if DOWN["on"]:
        raise OSError("down")
    q = urllib.parse.unquote(url.split("?", 1)[1])
    if "orderByMax" in q:
        return STATIONS_CSV
    sid = q.split('station_id="', 1)[1].split('"', 1)[0]
    if sid not in OBS or "time>=2030" in q:
        raise urllib.error.HTTPError(url, 404, "Your query produced no matching results.", {}, None)
    unit, rows = OBS[sid]
    return "\n".join(["time,WindSpeed,WindDirection,QC_Flag", f"UTC,{unit},degrees true,"] + rows)


_real_get = obsarea._get
obsarea._get = fake_get


def reset():
    obsarea.clear_cache()
    FETCHED.clear()
    DOWN["on"] = False


SW_IRELAND = (51.9, 51.0, -10.8, -9.0)     # (n, s, w, e) off the south-west coast
FLL = (26.6, 25.6, -80.6, -79.6)
CHANNEL = (50.82, 49.6, -1.8, -0.25)
BAYVIEW = (46.35, 42.5, -85.1, -81.3)
SRC = obserddap.MARINE_IE

print("1) an Irish course: the active Irish buoys, named by their source")
reset()
got = SRC.find(obsarea._pad(SW_IRELAND, obsarea.PAD_DEG))
check("active buoys in the box only (M1 is archived)", sorted(s["id"] for s in got) == ["M3"],
      [s["id"] for s in got])
res = obsarea.find(SW_IRELAND, pad_deg=2.5)
ids = [(s["id"], s["kind"], s["source"]) for s in res["stations"]]
check("find(): M3 then M5, kind and source ie_buoy",
      ids == [("M3", "ie_buoy", "ie_buoy"), ("M5", "ie_buoy", "ie_buoy")], ids)
check("plain notes: NDBC not here, nothing about the Irish list",
      "NDBC buoys: not here (US and Caribbean only)." in res["notes"]
      and not any("Irish" in n for n in res["notes"]), res["notes"])
check("its label names the source", SRC.label == "Irish buoys (Marine Institute)")
check("add by id finds an active buoy", (obsarea.lookup("m5") or {}).get("source") == "ie_buoy")
check("its kind can be picked", obsarea.clean_pick(dict(got[0])) is not None)
check("the boat does not read it live", "ie_buoy" not in obsarea.READABLE_KINDS)

print("2) anywhere else: never asked, nothing said")
for name, box in (("Fort Lauderdale", FLL), ("English Channel", CHANNEL), ("Lake Huron", BAYVIEW)):
    reset()
    check(f"{name}: not covered", not SRC.covers_bbox(box))
    check(f"{name}: no 'Irish buoys' note", SRC.not_here_note() is None)
    fr, fd = obsarea.find(box), obsarea.finder(box)
    irish = [u for u in FETCHED if "erddap.marine.ie" in u]
    check(f"{name}: find() and the Lab finder never ask the Irish list", not irish, irish)
    check(f"{name}: and list no Irish buoy",
          not any(s_["kind"] == "ie_buoy" for s_ in fr["stations"] + fd["stations"]))

print("3) observed wind")
reset()
m3 = SRC.observed("M3", "2025-07-01", "2025-07-01")
check("knots kept; NaN, blank and missing-flagged (9) rows dropped; 360 is 0",
      sorted(m3.values()) == [(12.0, 0.0), (13.7, 353.0), (15.6, 343.0)], m3)
check("one query, for that station and window", any(
    'station_id="M3"' in urllib.parse.unquote(u) and "time>=2025-07-01T00:00:00Z" in
    urllib.parse.unquote(u) for u in FETCHED), FETCHED)
m5 = SRC.observed("M5", "2025-07-01", "2025-07-01")
check("m/s converted to knots", [round(v[0], 2) for v in m5.values()] == [19.44], m5)
check("a unit we don't know: dropped", SRC.observed("M2", "2025-07-01", "2025-07-01") == {})
check("an empty window (ERDDAP 404): {}", SRC.observed("M3", "2030-01-01", "2030-01-02") == {})
check("an unknown station: {}", SRC.observed("ZZ", "2025-07-01", "2025-07-01") == {})
st = {"id": "M3", "kind": "ie_buoy", "lat": 51.2, "lon": -10.5}
check("model skill reads it through obs_series",
      modelskill.obs_series(st, "2025-07-01", "2025-07-01") == m3)

print("4) venue skill anchors")
reset()
sk = venue.stations_for(51.4, -10.0)
check("off SW Ireland: the nearest Irish buoy", [(s["id"], s["kind"]) for s in sk] == [("M3", "ie_buoy")],
      sk)
reset()
sk = venue.stations_for(44.5, -82.6)
check("Lake Huron: curated anchors, no fetch", sk and all(s.get("source") is None for s in sk)
      and FETCHED == [], (sk, FETCHED))

print("5) the venue study's finder")
reset()
src = obsstations.AreaSource(SRC)
near = src.find(53.0, -11.0, 60)
check("lists a past buoy with its last report", [(s["id"], s.get("end")) for s in near]
      == [("M1", "2007-07-09")], near)
check("outside its water: not asked", src.find(26.1, -80.0, 60) == [] and len(FETCHED) == 1)
check("registered networks join the finder's sources",
      [s.kind for s in obsstations.area_sources()] == ["ie_buoy"])
reset()
d = obsstations.discover(51.5, -10.0, 60, window=(2025, "2025-07-01", "2025-07-02"),
                         sources=[obsstations.CuratedSource(), src],
                         probe_fn=lambda s_, w_: len(modelskill.obs_series(s_, w_[1], w_[2])))
rows = {s["id"]: s for s in d["stations"]}
check("M3 found and checked against the window", rows.get("M3", {}).get("obs") == 3, d)
check("the nearest buoy with data is ticked", rows.get("M3", {}).get("ticked") is True, d)
d = obsstations.discover(53.0, -11.0, 60, window=(2025, "2025-07-01", "2025-07-02"),
                         sources=[src], probe_fn=lambda s_, w_: 1)
check("a buoy archived before the window is left out", not d["stations"], d)

print("6) a list that won't load")
reset()
DOWN["on"] = True
res = obsarea.find(SW_IRELAND)
check("a plain note, no stations", res["stations"] == [] and
      "Irish buoys (Marine Institute): could not load the station list." in res["notes"], res)
check("observed() while down: {}", SRC.observed("M3", "2025-07-01", "2025-07-01") == {})
reset()
_saved = STATIONS_CSV
STATIONS_CSV = "<html>Service Unavailable</html>"
check("a page that isn't a station list: no stations", SRC.stations() == [])
STATIONS_CSV = _saved

print("7) depth: the world grid is opt-in")
os.environ.pop("DEPTH_GRIDS", None)
check("unset: the built-in grids only", [g["name"] for g in depthsrc.grid_catalog()]
      == [g["name"] for g in depthsrc.GRIDS])
check("unset: an Irish box gets the chart source only, no grid",
      [s.name for s in depthsrc.sources_for(SW_IRELAND)] == ["noaa_enc"])
os.environ["DEPTH_GRIDS"] = "default,etopo"
cat = depthsrc.grid_catalog()
check("default,etopo: built-ins first, then 288 world tiles",
      len(cat) == len(depthsrc.GRIDS) + 288 and cat[0]["name"] == depthsrc.GRIDS[0]["name"])
got = depthsrc.sources_for(SW_IRELAND)
check("an Irish box: the chart source + the one tile it touches",
      [s.name for s in got] == ["noaa_enc", "etopo_n60w015"], [s.name for s in got])
t = got[1]
check("tile N60W015 covers 45-60 N, 15 W-0", t.bbox == (60.0, 45.0, -15.0, 0.0), t.bbox)
check("not a chart, says so", not t.navigational and t.label == "ETOPO world grid (coarse)")
check("public-domain NOAA file, elevation up", t.spec["url"].startswith("https://www.ngdc.noaa.gov/")
      and t.spec["url"].endswith("ETOPO_2022_v1_15s_N60W015_surface.tif") and t.spec["z"] == "elevation")
check("a box over a tile corner touches four tiles", len(depthsrc.sources_for((45.5, 44.5, -0.5, 0.5))) == 5)
check("south and east tiles are named right", [g["name"] for g in cat if g["bbox"] == [-30, -45, 150, 165]]
      == ["etopo_s30e150"])
check("in the depth cache key", "etopo_n60w015" in depthsrc.data_version(SW_IRELAND))
os.environ["DEPTH_GRIDS"] = "none"
check("none: no grids", depthsrc.grid_catalog() == [])
os.environ.pop("DEPTH_GRIDS", None)

obsarea._get = _real_get
print("RESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
