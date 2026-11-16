"""Build the sea sources' water masks (vps/lab/app/seamasks/*.json) from each model's own grid (#308).

A source covers the cells where ITS model has water, so the masks come from the models, not from a
coastline: LMHOFS's regular grid (no Lake Erie, no Lake St Clair), GLWU's 2.5 km grid (all five
lakes), HYCOM ESPC's global 1/12° ocean (no Great Lakes), GFS-Wave's 1/6° and 1/4° global grids. A cell is
water when any model water point falls in it (then grown by one cell where the raster is finer
than the model grid, so the model's own points don't leave holes).

Needs the network and the Lab image's libraries (netCDF4, cfgrib). Run it by hand when a model's
grid changes, in a throwaway container, and commit the JSON:

    docker run --rm -v "$PWD:/repo" -w /repo -e PYTHONPATH=/repo/vps/lab c4-ci-lab \
        python3 tools/seamasks/build.py [lmhofs glwu hycom-espc gfs-wave-global-0p16 ...]
"""
import datetime
import json
import math
import os
import sys
import tempfile
import urllib.request

import numpy as np

from app.seasource import Coverage

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "vps", "lab", "app", "seamasks")
UA = {"User-Agent": "AgentC4-seamasks/1.0"}


def _get(url, timeout=600):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def rasterize(lat, lon, water, s, w, step, rows, cols, grow=0):
    """Mark every raster cell holding a water point, then grow by `grow` cells."""
    lat, lon, water = np.ravel(lat), np.ravel(lon), np.ravel(water)
    lat, lon = lat[water], lon[water]
    r = np.floor((lat - s) / step).astype(int)
    x = lon - w
    if cols * step >= 359.999:
        x = np.mod(x, 360.0)
    c = np.floor(x / step).astype(int)
    keep = (r >= 0) & (r < rows) & (c >= 0) & (c < cols)
    grid = np.zeros((rows, cols), bool)
    grid[r[keep], c[keep]] = True
    for _ in range(grow):
        g = grid.copy()
        g[1:, :] |= grid[:-1, :]
        g[:-1, :] |= grid[1:, :]
        g[:, 1:] |= grid[:, :-1]
        g[:, :-1] |= grid[:, 1:]
        grid = g
    return grid


def save(name, grid, s, w, step, made_from):
    rows, cols = grid.shape
    cov = Coverage(s, w, step, rows, cols, np.packbits(grid.ravel()).tobytes(), name)
    path = os.path.join(OUT, name + ".json")
    os.makedirs(OUT, exist_ok=True)
    with open(path, "w") as f:
        json.dump(cov.to_json(made_from=made_from,
                              made_at=datetime.date.today().isoformat(),
                              water_cells=int(grid.sum())), f)
        f.write("\n")
    print(f"{name}: {rows}x{cols} @ {step}°, {int(grid.sum())} water cells → {path}")


def lmhofs():
    import netCDF4 as nc
    base = "https://opendap.co-ops.nos.noaa.gov/thredds/dodsC/NOAA/LMHOFS/MODELS"
    d0 = datetime.datetime.utcnow() - datetime.timedelta(hours=8)
    for back in range(8):
        d = d0 - datetime.timedelta(hours=6 * back)
        d = d.replace(hour=(d.hour // 6) * 6)
        url = (f"{base}/{d:%Y/%m/%d}/lmhofs.t{d:%H}z.{d:%Y%m%d}.regulargrid.f001.nc")
        try:
            ds = nc.Dataset(url)
            break
        except OSError:
            continue
    lon = np.asarray(ds.variables["Longitude"][0, :])
    lat = np.asarray(ds.variables["Latitude"][:, 0])
    u = np.ma.filled(ds.variables["u_eastward"][0, 0, :, :].astype(float), np.nan)
    ds.close()
    water = np.isfinite(u) & (u > -9000)
    lon2, lat2 = np.meshgrid(lon, lat)
    s, w, step = 41.5, -88.2, 0.02
    rows, cols = int(math.ceil((46.5 - s) / step)), int(math.ceil((-79.5 - w) / step))
    save("lmhofs", rasterize(lat2, lon2, water, s, w, step, rows, cols, grow=0), s, w, step,
         "NOAA LMHOFS regular grid (u_eastward at the surface)")


def _grib_mask(path):
    import xarray as xr
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    v = ds[list(ds.data_vars)[0]].values
    if v.ndim == 3:
        v = v[0]
    lat, lon = ds["latitude"].values, ds["longitude"].values
    if lat.ndim == 1:
        lon, lat = np.meshgrid(lon, lat)
    lon = np.where(lon > 180, lon - 360, lon)
    return lat, lon, np.isfinite(v)


def glwu():
    for back in range(8):
        c = datetime.datetime.utcnow() - datetime.timedelta(hours=6 + 6 * back)
        hh = max(h for h in (1, 7, 13, 19) if h <= c.hour) if c.hour >= 1 else 19
        if c.hour < 1:
            c -= datetime.timedelta(days=1)
        url = ("https://nomads.ncep.noaa.gov/cgi-bin/filter_glwu.pl?lev_surface=on&var_HTSGW=on"
               f"&file=glwu.grlc_2p5km.t{hh:02d}z.grib2&dir=%2Fglwu.{c:%Y%m%d}")
        try:
            data = _get(url)
        except OSError:
            continue
        if len(data) > 1000:
            break
    with tempfile.NamedTemporaryFile(suffix=".grib2") as f:
        f.write(data)
        f.flush()
        lat, lon, water = _grib_mask(f.name)
    s, w, step = 41.0, -93.0, 0.02
    rows, cols = int(math.ceil((49.5 - s) / step)), int(math.ceil((-75.5 - w) / step))
    save("glwu", rasterize(lat, lon, water, s, w, step, rows, cols, grow=1), s, w, step,
         "NOAA GLWU grlc_2p5km (HTSGW)")


def hycom():
    from app import hycom as H
    lats, lons = H._grid_axes()
    k = len(H._time_axis()) // 2
    # every 2nd point (0.08° lat x 0.16° lon) of one hourly frame, in latitude bands
    lat_l, lon_l, wat_l = [], [], []
    band = 400
    for j0 in range(0, len(lats), band):
        j1 = min(j0 + band, len(lats)) - 1
        got = H.parse_ascii(H._get(H.URL + ".ascii?" + H._q(f"ssu[{k}][{j0}:2:{j1}][0:2:{len(lons) - 1}]")))
        la, lo, u = np.array(got["ssu.lat"]), np.array(got["ssu.lon"]), np.array(got["ssu.ssu"])
        lo2, la2 = np.meshgrid(lo, la)
        lat_l.append(la2.ravel()); lon_l.append(lo2.ravel()); wat_l.append(np.isfinite(u))
        print(f"  hycom rows {j0}..{j1}", flush=True)
    lat, lon, water = np.concatenate(lat_l), np.concatenate(lon_l), np.concatenate(wat_l)
    lon = np.where(lon > 180, lon - 360, lon)
    s, w, step = -90.0, -180.0, 0.25
    save("hycom-espc", rasterize(lat, lon, water, s, w, step, 720, 1440), s, w, step,
         "HYCOM ESPC-D-V02 surface velocity (ssu), every 2nd grid point")


def gfswave(grid):
    c = datetime.datetime.utcnow() - datetime.timedelta(hours=8)
    c = c.replace(hour=(c.hour // 6) * 6)
    url = ("https://nomads.ncep.noaa.gov/cgi-bin/filter_gfswave.pl?var_HTSGW=on&lev_surface=on"
           f"&file=gfswave.t{c:%H}z.{grid}.f000.grib2&dir=%2Fgfs.{c:%Y%m%d}%2F{c:%H}%2Fwave%2Fgridded")
    data = _get(url)
    with tempfile.NamedTemporaryFile(suffix=".grib2") as f:
        f.write(data)
        f.flush()
        lat, lon, water = _grib_mask(f.name)
    s, w, step = -90.0, -180.0, 0.25
    save("gfs-wave-" + grid.replace(".", "-"),
         rasterize(lat, lon, water, s, w, step, 720, 1440, grow=0), s, w, step,
         f"NOAA GFS-Wave {grid} (HTSGW)")


if __name__ == "__main__":
    jobs = {"lmhofs": lmhofs, "glwu": glwu, "hycom-espc": hycom,
            "gfs-wave-global-0p16": lambda: gfswave("global.0p16"),
            "gfs-wave-global-0p25": lambda: gfswave("global.0p25")}
    for name in sys.argv[1:] or list(jobs):
        jobs[name]()
