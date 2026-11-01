"""Higher-res coastline backstop (GSHHG full-res): source-pluggability + island-coverage win.

Run in-container:  docker compose -f compose.dev.yml exec -w /srv lab python test_coastline_gshhg.py
The mask A/B needs the GSHHG data on the lab_coastline volume (fetched lazily on first optimize, or
`python -m app.geo.coastline 46.2 43.0 -85.0 -81.5`). Without it (CI: no volume, no network) the A/B
runs on the committed fixture vps/lab/testdata/coastline_cove_island/: the same layers clipped to
this box with `ogr2ogr -clipsrc` (GSHHG 2.3.7 full-res, LGPL; Natural Earth 10m, public domain, box
plus 0.5 deg). The GSHHG clips are named by coastline's own cache key, so the fixture tests the
mask rasterisation and the A/B, not the ogr2ogr clip itself.
"""
import os
import shutil
import tempfile

from app.geo import coastline, obstacles

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

# 1) source pluggability + role mapping (no data / network needed)
check("default global source is gshhg", coastline.active_source() == "gshhg")
check("DATA_VERSION reflects gshhg+res", coastline.DATA_VERSION.startswith("gshhg_") and coastline.GSHHG_RES in coastline.DATA_VERSION)
check("GSHHG hierarchy maps to land/lakes/islands roles",
      coastline.GSHHG_ROLES == {"land": "L1", "lakes": "L2", "islands": "L3"})

# 2) mask A/B over the Bayview Mackinac cove_island bbox (cross-border; covers the islands NE omits)
BBOX = (46.2, 43.0, -85.0, -81.5)   # (n, s, w, e)
empty = {"zones": [], "courses": []}

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "coastline_cove_island")


def use_fixture():
    """Point coastline at a temp copy of the fixture. Empty .shp stand-ins satisfy the "GSHHG is
    installed" check; they are never read, because the clip for this box is already cached."""
    tmp = os.path.join(tempfile.mkdtemp(), "coastline")
    shutil.copytree(FIXTURE, tmp)
    for shp in coastline._gshhg_needed(tmp):
        os.makedirs(os.path.dirname(shp), exist_ok=True)
        open(shp, "w").close()
    coastline.CACHE = tmp
    coastline._LAYERS.clear()


if coastline._gshhg_layers_in_bbox(BBOX, coastline.CACHE) is None:
    print("  (GSHHG data not on the volume: using the committed clipped fixture)")
    use_fixture()

coastline.GLOBAL_SOURCE = "gshhg"; obstacles._FIELD_CACHE.clear()
fg = obstacles.build_for_course(empty, "cove_island", BBOX, source="natural_earth", use_cache=False)

if not fg.active or coastline._gshhg_layers_in_bbox(BBOX, coastline.CACHE) is None:
    check("GSHHG layers load for the mask A/B (fixture or volume)", False)
else:
    check("GSHHG field provenance is gshhg", fg.source == "gshhg")
    coastline.GLOBAL_SOURCE = "natural_earth"; obstacles._FIELD_CACHE.clear()
    fn = obstacles.build_for_course(empty, "cove_island", BBOX, source="natural_earth", use_cache=False)
    coastline.GLOBAL_SOURCE = "gshhg"

    # canaries that must agree: open water open, big Canadian island blocked, US mainland blocked
    check("open Lake Huron stays water (both)", not fg.blocked(44.5, -82.5) and not fn.blocked(44.5, -82.5))
    check("Manitoulin (Canada) blocked (both)", fg.blocked(45.75, -82.25) and fn.blocked(45.75, -82.25))

    # the win: GSHHG blocks mid-lake islands NE leaves open (sample the island-dense North Channel)
    added = 0
    lat = 45.4
    while lat < 46.2:
        lon = -83.0
        while lon < -81.6:
            if fg.blocked(lat, lon) and not fn.blocked(lat, lon):
                added += 1
            lon += 0.02
        lat += 0.02
    print(f"      GSHHG-blocks-but-NE-open cells in the North Channel sample: {added}")
    check("GSHHG adds islands NE misses (>50 cells)", added > 50)

print("PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
