"""#292 depth sources: a depth grid fills the water no chart covers. No network (a file:// grid).

Pins:
  1) a grid source reads a GeoTIFF onto the mask grid: shallowest depth per cell, south row first,
     None where the grid has no data;
  2) the join: a chart (ENC) depth area wins over the grid; the grid gives the depth only where
     no chart does; outside both it stays "no depth data";
  3) grid water shallower than the boat's safety depth is blocked, re-thresholded per boat (the
     second boat, bravo, is shallower than sr33);
  4) the route verdict names the grid on the legs whose depths came from it, and keeps "No depth
     data" where nothing covers;
  5) a grid that won't load (or a broken catalog) leaves its water "no depth data", is reported,
     and the field is never cached.
"""
import json
import os
import subprocess
import tarfile
import tempfile

TMP = tempfile.mkdtemp(prefix="c4-grid-")
os.environ["ENC_CACHE"] = os.path.join(TMP, "cache")

from app import optimizer as O                       # noqa: E402
from app.geo import coastline                        # noqa: E402
from app.geo import depthsrc as D                    # noqa: E402
from app.geo import enc                              # noqa: E402
from app.geo import obstacles as OB                  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# --- a synthetic grid: 0.6° x 0.5° at 0.01°, elevation (up is +), nodata north of 10.4 N ---------
W, S, E, N = 20.0, 10.0, 20.6, 10.5
STEP = 0.01


def elev(lat, lon):
    if lat >= 10.4:
        return -9999
    if lon < 20.1:
        return -1.0          # 1 m: a shoal the chart (ENC) says is not there; the chart wins
    if 10.12 <= lat < 10.13 and 20.17 <= lon < 20.18:
        return -0.5          # one 0.5 m pixel among 10 m ones: the cell takes the shallowest
    if lon < 20.3:
        return -10.0         # deep
    if lon < 20.45:
        return -1.0          # 1 m: a shoal for every boat
    return -2.3              # 2.3 m: a shoal for sr33 (2.63 m safety), open for bravo (2.08 m)


nx, ny = round((E - W) / STEP), round((N - S) / STEP)
asc = os.path.join(TMP, "g.asc")
with open(asc, "w") as f:
    f.write(f"ncols {nx}\nnrows {ny}\nxllcorner {W}\nyllcorner {S}\ncellsize {STEP}\n"
            "NODATA_value -9999\n")
    for r in range(ny):                                 # north row first
        lat = N - (r + 0.5) * STEP
        f.write(" ".join(str(elev(lat, W + (c + 0.5) * STEP)) for c in range(nx)) + "\n")
os.makedirs(os.path.join(TMP, "pkg", "g"))
subprocess.run(["gdal_translate", "-q", "-of", "GTiff", "-a_srs", "EPSG:4326", "-a_nodata", "-9999",
                asc, os.path.join(TMP, "pkg", "g", "test.tif")], check=True)
with tarfile.open(os.path.join(TMP, "g.tar.gz"), "w:gz") as t:
    t.add(os.path.join(TMP, "pkg", "g"), arcname="g")

SPEC = {"name": "test_grid", "label": "Test grid", "navigational": False,
        "bbox": [N, S, W, E], "url": "file://" + os.path.join(TMP, "g.tar.gz"),
        "member": "g/test.tif", "z": "elevation", "version": "1"}
CAT = os.path.join(TMP, "cat.json")
with open(CAT, "w") as f:
    json.dump([SPEC], f)
os.environ["DEPTH_GRIDS"] = CAT

BBOX = (N, S, W, E)
RES = 0.05
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "boats", "bravo.json")) as f:
    BRAVO = json.load(f)["draft_m"] + OB.DRAFT_MARGIN_M          # 1.582 + 0.5
SR33 = 2.1336 + OB.DRAFT_MARGIN_M

print("1) the grid on the mask grid")
srcs = D.sources_for(BBOX)
check("sources for the box: the chart first, then the grid",
      [s.name for s in srcs] == ["noaa_enc", "test_grid"], [s.name for s in srcs])
check("a box the grid doesn't touch: the chart only",
      [s.name for s in D.sources_for((50.5, 50.0, 0.0, 0.5))] == ["noaa_enc"])
g = srcs[1]
gx, gy = round((E - W) / RES), round((N - S) / RES)
cells = g.cells(BBOX, RES, gx, gy, None)


def cell(lat, lon):
    return cells[int((lat - S) / RES) * gx + int((lon - W) / RES)]


check("deep water: 10 m", cell(10.2, 20.2) == 10.0, cell(10.2, 20.2))
check("a shoal: 1 m", cell(10.2, 20.4) == 1.0, cell(10.2, 20.4))
check("one shallow pixel in a deep cell: the cell is that shallow", cell(10.12, 20.17) == 0.5,
      cell(10.12, 20.17))
check("south row first (the north strip has no data)", cell(10.02, 20.2) == 10.0
      and cell(10.45, 20.2) is None, (cell(10.02, 20.2), cell(10.45, 20.2)))
check("downloaded and unpacked once", os.path.exists(g.raster(None)))

print("1b) any nodata value; a new grid version is a new file (Codex on #300)")
# depth grid (down is +) whose nodata is 0: a cell with one empty pixel is not covered
asc0 = os.path.join(TMP, "d.asc")
with open(asc0, "w") as f:
    f.write(f"ncols 10\nnrows 10\nxllcorner {W}\nyllcorner {S}\ncellsize {STEP}\nNODATA_value 0\n")
    for r in range(10):
        f.write(" ".join("0" if (r, c) == (9, 0) else "10" for c in range(10)) + "\n")
subprocess.run(["gdal_translate", "-q", "-of", "GTiff", "-a_srs", "EPSG:4326", "-a_nodata", "0",
                asc0, os.path.join(TMP, "d.tif")], check=True)
g0 = D.GridSource({"name": "zero_nd", "bbox": [S + 0.1, S, W, W + 0.1], "z": "depth",
                   "url": "file://" + os.path.join(TMP, "d.tif"), "version": "1"})
c0 = g0.cells((S + 0.1, S, W, W + 0.1), 0.05, 2, 2, None)
check("nodata 0: the cell with an empty pixel is not covered, the others are",
      c0[0] is None and c0[1:] == [10.0, 10.0, 10.0], c0)
ce = g0.cells((S + 0.1, S + 0.05, W + 0.025, W + 0.125), 0.05, 2, 1, None)
check("a cell the raster's edge cuts through is not covered", ce == [10.0, None], ce)
# the same grid in UTM: its lon/lat footprint is tilted, so it is refused, never half-checked
subprocess.run(["gdal_translate", "-q", "-of", "GTiff", "-a_srs", "EPSG:32634", "-a_nodata", "0",
                "-a_ullr", "500000", "1120000", "510000", "1110000", asc0,
                os.path.join(TMP, "u.tif")], check=True)
gu = D.GridSource({"name": "utm", "bbox": [10.2, 9.9, 20.9, 21.2], "z": "depth",
                   "url": "file://" + os.path.join(TMP, "u.tif"), "version": "1"})
try:
    gu.cells((10.2, 9.9, 20.9, 21.2), 0.05, 6, 6, None)
    check("a projected (tilted) grid is refused", False)
except D.GridError as exc:
    check("a projected (tilted) grid is refused", "north-up" in str(exc), str(exc))
g1 = D.GridSource(dict(SPEC, version="2"))
check("a new version: a new file and a new data version",
      g1.raster(None) != g.raster(None) and g1.data_version != g.data_version)

check("a changed sign or label: a new data version",
      D.GridSource(dict(SPEC, z="depth")).data_version != g.data_version
      and D.GridSource(dict(SPEC, label="Other")).data_version != g.data_version)

print("2) the join with the chart")
_orig = (coastline.ensure_global, coastline.layers_in_bbox, coastline.active_source,
         enc.ensure_bbox, enc.layers_in_bbox)
coastline.ensure_global = lambda cache_dir=None: None
coastline.layers_in_bbox = lambda bbox, cache_dir=None: {"land": [], "lakes": [], "islands": []}
coastline.active_source = lambda: "stub"
ENC_BOX = [[(20.0, S), (20.15, S), (20.15, N), (20.0, N)]]        # the chart covers the west strip
ENC_EDGE = [[(20.30, S), (20.33, S), (20.33, N), (20.30, N)]]      # ...and the west 3/5 of a column
enc.ensure_bbox = lambda bbox, cache_dir=None: None
enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {
    "land": [], "shoal": [], "obstruction": [], "depth_cover": [ENC_BOX, ENC_EDGE],
    "missing": []}
COURSE = {"race_id": "t292", "zones": [], "courses": [{
    "id": "c", "start": {"lat": 10.2, "lon": 20.05},
    "marks": [{"name": "M", "type": "mark", "rounding": "none", "lat": 10.2, "lon": 20.2}],
    "finish": {"points": [{"lat": 10.45, "lon": 20.2}]}}]}
try:
    f = OB.build_for_course(COURSE, "c", BBOX, res_deg=RES, use_cache=False, source="enc",
                            safety_depth=SR33)
    check("depths checked", f.depth == "ok", f.depth)
    check("chart water: depth known, not from the grid",
          not f.no_depth(10.2, 20.07, 10.2, 20.08) and not f.nonnav_labels(10.2, 20.07, 10.2, 20.08))
    check("the chart wins: its strip isn't blocked by the grid's shoal", not f.blocked(10.2, 20.07))
    check("the chart's edge: the grid's shoal in the cell's uncharted half still blocks",
          f.blocked(10.2, 20.34))
    check("grid water: depth known, from the grid",
          not f.no_depth(10.2, 20.2, 10.2, 20.21)
          and f.nonnav_labels(10.2, 20.2, 10.2, 20.21) == ["Test grid"])
    check("deep grid water is open", not f.blocked(10.2, 20.2))
    check("outside both: no depth data", f.no_depth(10.45, 20.2, 10.46, 20.2))
    check("the summary names the grid", f.summary().get("depth_grids") == ["Test grid"])

    enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {
        "land": [], "shoal": [], "obstruction": [], "depth_cover": [], "missing": []}
    fo = OB.build_for_course(COURSE, "c", BBOX, res_deg=RES, use_cache=False, source="enc",
                             safety_depth=SR33)
    check("grid only, no chart: still the depth-source build (Deploy rebuilds it the same way)",
          fo.source == "enc" and fo.summary()["safety_depth_m"] == round(SR33, 2)
          and fo.blocked(10.2, 20.4), (fo.source, fo.summary()["safety_depth_m"]))
    enc.layers_in_bbox = lambda bbox, cache_dir=None, depth=None: {
        "land": [], "shoal": [], "obstruction": [], "depth_cover": [ENC_BOX, ENC_EDGE],
    "missing": []}

    print("3) shoals per boat")
    check("1 m blocked for sr33", f.blocked(10.2, 20.4))
    check("2.3 m blocked for sr33", f.blocked(10.2, 20.5))
    fb = OB.build_for_course(COURSE, "c", BBOX, res_deg=RES, use_cache=False, source="enc",
                             safety_depth=BRAVO)
    check("1 m blocked for bravo", fb.blocked(10.2, 20.4))
    check("2.3 m open for bravo", not fb.blocked(10.2, 20.5))
    check("different boats, different cache keys",
          OB.cache_key(COURSE, "c", BBOX, RES, source="enc", safety_depth=SR33)
          != OB.cache_key(COURSE, "c", BBOX, RES, source="enc", safety_depth=BRAVO))

    print("4) the route verdict")
    marks = [(0, "Start", 10.2, 20.05), (1, "M", 10.2, 20.2), (2, "Finish", 10.45, 20.2)]
    path = [{"lat": 10.2, "lon": 20.05}, {"lat": 10.2, "lon": 20.12}, {"lat": 10.2, "lon": 20.2},
            {"lat": 10.3, "lon": 20.2}, {"lat": 10.45, "lon": 20.2}]
    v = O.route_safety(COURSE, "c", marks, path, [2, 4], None, f, [f, f], False)
    notes = {n["code"]: n for n in v["notes"]}
    check("passes (notes never refuse)", v["ok"] is True, v["problems"])
    check("the grid is named on both legs",
          notes.get("grid_depth:Test grid", {}).get("legs") == ["Start → M", "M → Finish"], notes)
    check("...in plain words",
          notes.get("grid_depth:Test grid", {}).get("text")
          == "Depths from Test grid, not a chart: check the paper chart.")
    check("no depth data on the leg into the empty strip only",
          notes.get("no_depth", {}).get("legs") == ["M → Finish"], notes)

    print("5) a grid that won't load")
    bad = dict(SPEC, name="gone", url="file://" + os.path.join(TMP, "nope.tar.gz"))
    with open(CAT, "w") as fh:
        json.dump([bad], fh)
    OB._FIELD_CACHE.clear()
    fg = OB.build_for_course(COURSE, "c", BBOX, res_deg=RES, source="enc", safety_depth=SR33)
    check("reported", fg.depth_gaps and "gone" in fg.depth_gaps[0], fg.depth_gaps)
    check("its water: no depth data, depths still 'ok'",
          fg.depth == "ok" and fg.no_depth(10.2, 20.2, 10.2, 20.21))
    check("never cached (tried again next build)", not OB._FIELD_CACHE)
    with open(CAT, "w") as fh:
        fh.write("{not json")
    fc = OB.build_for_course(COURSE, "c", BBOX, res_deg=RES, use_cache=False, source="enc",
                             safety_depth=SR33)
    check("a broken catalog: reported, no depth data, no crash",
          fc.depth_gaps and "catalog" in fc.depth_gaps[0] and fc.no_depth(10.2, 20.2, 10.2, 20.21),
          fc.depth_gaps)
    os.environ["DEPTH_GRIDS"] = "none"
    fn = OB.build_for_course(COURSE, "c", BBOX, res_deg=RES, use_cache=False, source="enc",
                             safety_depth=SR33)
    check("grids off: chart only, the rest no depth data",
          not fn.depth_gaps and fn.no_depth(10.2, 20.2, 10.2, 20.21)
          and not fn.no_depth(10.2, 20.07, 10.2, 20.08))
finally:
    (coastline.ensure_global, coastline.layers_in_bbox, coastline.active_source,
     enc.ensure_bbox, enc.layers_in_bbox) = _orig

print("\nALL OK" if ok else "\nSOME CHECKS FAILED")
raise SystemExit(0 if ok else 1)
