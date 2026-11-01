"""GRIB parse isolation: a native child crash (the intermittent eccodes finalizer segfault) must be
SURVIVED — the child dies, the parent respawns + retries, the frame is skipped, the process lives on.

The crash path needs no real GRIB (the worker has a test hook that os.abort()s on a path substring).
The success path parses a real cached frame when one is present, else a tiny 10 m-wind GRIB2 frame
written here with eccodes (4x3 grid, known values), so it runs in CI too. The test process REACHING
its assertions is itself the proof the crash was isolated.
"""
import glob
import os
import tempfile

from app.wind import grib

ok = True
def check(name, cond):
    global ok; ok = ok and cond
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")

# 1) a child CRASH on every attempt → parse returns None (skip) and does NOT take down this process.
os.environ["GRIB_PARSER_CRASH_ON"] = "CRASHME"
p = grib.IsolatedGribParser()
try:
    res = p.parse("/tmp/CRASHME_frame.grib2")        # worker os.abort()s each attempt
    check("child crash is survived → parse() returns None (process still alive)", res is None)
    # 2) the parser RECOVERS: a subsequent non-crashing path respawns the child and parses-or-errors
    #    cleanly (None via the genuine-error path), proving the child was respawned and is functional.
    res2 = p.parse("/tmp/not_a_real_grib_file.grib2")  # respawns, open_uv fails → ok:false → None
    check("parser recovers after a crash (clean None on next, non-crashing, path)", res2 is None)
    check("child process is alive again after recovery", p._alive())
finally:
    p.close()
    os.environ.pop("GRIB_PARSER_CRASH_ON", None)



def synthetic_frame(path):
    """A tiny regular lat/lon 10 m u/v GRIB2 frame: u = 3 + 0.25*i, v = -2 + 0.25*i over a 4x3 grid."""
    import eccodes
    import numpy as np
    with open(path, "wb") as fh:
        for short, base in (("10u", 3.0), ("10v", -2.0)):
            h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            for k, v in (("Ni", 4), ("Nj", 3), ("latitudeOfFirstGridPointInDegrees", 44.0),
                         ("longitudeOfFirstGridPointInDegrees", 277.0),
                         ("latitudeOfLastGridPointInDegrees", 43.0),
                         ("longitudeOfLastGridPointInDegrees", 278.5),
                         ("iDirectionIncrementInDegrees", 0.5), ("jDirectionIncrementInDegrees", 0.5),
                         ("typeOfLevel", "heightAboveGround"), ("level", 10), ("shortName", short)):
                eccodes.codes_set(h, k, v)
            eccodes.codes_set_values(h, base + np.arange(12, dtype=float) * 0.25)
            eccodes.codes_write(h, fh)
            eccodes.codes_release(h)
    return path


# 3) SUCCESS path: isolated parse == in-process open_uv, on a real cached frame or the synthetic one.
cached = sorted(glob.glob("/srv/gribcache/gfs/*.grib2"))
if not cached:
    f = synthetic_frame(os.path.join(tempfile.mkdtemp(), "synthetic_gfs_f000.grib2"))
    elat, elon, eu, ev, ereg = grib.open_uv(f)
    print("  (no cached GRIB frame: using a synthetic 4x3 frame)")
    check("synthetic frame reads back as written (in-process)",
          ereg and list(elat) == [44.0, 43.5, 43.0] and list(elon) == [-83.0, -82.5, -82.0, -81.5]
          and eu[0, 0] == 3.0 and eu[2, 3] == 5.75 and ev[0, 0] == -2.0)
    cached = [f]
f = cached[0]
p2 = grib.IsolatedGribParser()
try:
    iso = p2.parse(f)
    check("isolated parse of the frame returns arrays", iso is not None)
    if iso:
        import numpy as np
        lat, lon, u, v, regular = open_iso = iso
        elat, elon, eu, ev, ereg = grib.open_uv(f)
        same = (regular == ereg and np.allclose(lat, elat) and np.allclose(lon, elon)
                and np.allclose(u, eu, equal_nan=True) and np.allclose(v, ev, equal_nan=True))
        check("isolated parse matches in-process open_uv exactly", same)
    # 4) crash on a frame that would parse: survived (None), never a dead process.
    os.environ["GRIB_PARSER_CRASH_ON"] = os.path.basename(f)
    p3 = grib.IsolatedGribParser()
    try:
        crashed = p3.parse(f)
        check("crash on a parseable frame → None (survived)", crashed is None)
    finally:
        p3.close(); os.environ.pop("GRIB_PARSER_CRASH_ON", None)
finally:
    p2.close()

print("RESULT:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
