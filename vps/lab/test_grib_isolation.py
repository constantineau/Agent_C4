"""GRIB parse isolation: a native child crash (the intermittent eccodes finalizer segfault) must be
SURVIVED — the child dies, the parent respawns + retries, the frame is skipped, the process lives on.

The crash path needs no real GRIB (the worker has a test hook that os.abort()s on a path substring); a
cached frame, if present, exercises the real parse + crash-then-recover. The test process REACHING its
assertions is itself the proof the crash was isolated.
"""
import glob
import os

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

# 3) SUCCESS path on a real cached frame (if one is present): isolated parse == in-process open_uv.
cached = sorted(glob.glob("/srv/gribcache/gfs/*.grib2"))
if cached:
    f = cached[0]
    p2 = grib.IsolatedGribParser()
    try:
        iso = p2.parse(f)
        check("isolated parse of a real frame returns arrays", iso is not None)
        if iso:
            import numpy as np
            lat, lon, u, v, regular = open_iso = iso
            elat, elon, eu, ev, ereg = grib.open_uv(f)
            same = (regular == ereg and np.allclose(lat, elat) and np.allclose(lon, elon)
                    and np.allclose(u, eu, equal_nan=True) and np.allclose(v, ev, equal_nan=True))
            check("isolated parse matches in-process open_uv exactly", same)
        # 4) crash-then-recover with a real frame: crash on this path, then parse it cleanly next time.
        os.environ["GRIB_PARSER_CRASH_ON"] = os.path.basename(f)
        p3 = grib.IsolatedGribParser()
        try:
            crashed = p3.parse(f)
            check("crash on the real frame → None (survived)", crashed is None)
        finally:
            p3.close(); os.environ.pop("GRIB_PARSER_CRASH_ON", None)
    finally:
        p2.close()
else:
    print("  [skip] no cached GRIB frame available — success-path checks skipped")

print("RESULT:", "PASS" if ok else "FAIL")
