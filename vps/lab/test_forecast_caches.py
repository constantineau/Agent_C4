"""#44.2 / #44.2b: the Lab's current and wave caches.

  1) a built field is reused only for the same dataset cycle, area, full time span and processing
     version: a longer window, a newer cycle or a processing change builds again (the old key was
     area + start hour, so a stale or too-short field came back);
  2) a field built from an older cycle (the newest wasn't posted yet) is retried after an hour,
     and any field expires after the secondary time limit;
  3) the wave parser keeps no frames when none fall in the window, so the build steps back a cycle
     instead of using the earliest frames as if they were the race's.

No network: the fetch and parse are stubbed; the parser child runs against a fake xarray.
"""
import datetime
import os
import subprocess
import sys
import tempfile

import numpy as np

from app import current as CUR
from app import wave as WAVE

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def ep(dt):
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


CLOCK = [ep(datetime.datetime(2026, 7, 15, 14, 30))]
START = CLOCK[0] + 3600
BBOX = (44.5, 43.5, -86.5, -85.5)

# --- currents -----------------------------------------------------------------------------------
print("1) currents: the cache key")
CUR.ENABLED = True
CUR.MAX_SLICES = 4
import time as _time                                # noqa: E402
_real_time = _time.time
_time.time = lambda: CLOCK[0]
_real_ccycle = CUR._cycle_for
CUR._cycle_for = lambda epoch: _real_ccycle(epoch, now=CLOCK[0])
FETCHES = []


def _fake_fetch(url, bbox, want_axes):
    FETCHES.append(url)
    u = [[0.0, 0.0], [0.0, 0.0]]
    if want_axes:
        return [-86.6, -85.4], [43.4, 44.6], u, u
    return u, u


CUR._fetch = _fake_fetch


def builds_cur(t_start, t_end, bbox=BBOX):
    n0 = len(FETCHES)
    CUR.build_currentfield(bbox, t_start, t_end)
    return len(FETCHES) > n0


CUR._cache.clear()
check("first build fetches", builds_cur(START, START + 6 * 3600))
check("same cycle, area, span: cached", not builds_cur(START, START + 6 * 3600))
check("a longer window builds again", builds_cur(START, START + 18 * 3600))
check("another area builds again", builds_cur(START, START + 6 * 3600, (45.0, 44.0, -86.5, -85.5)))
CUR.CACHE_VERSION += 1
check("a processing change builds again", builds_cur(START, START + 6 * 3600))
CLOCK[0] += 6 * 3600                                # the next cycle is posted
check("a newer cycle builds again", builds_cur(START, START + 6 * 3600))
check("…then is cached", not builds_cur(START, START + 6 * 3600))
CLOCK[0] += 0.5 * 3600
check("still cached inside the cycle", not builds_cur(START, START + 6 * 3600))

print("2) currents: fallback cycle retried, time limit")
CUR._cache.clear()
_real_fetch = _fake_fetch


def _newest_missing(url, bbox, want_axes):
    want = CUR._cycle_for(START)
    if f"t{want:%H}z.{want:%Y%m%d}" in url:
        FETCHES.append(url)
        raise OSError("not posted yet")
    return _real_fetch(url, bbox, want_axes)


CUR._fetch = _newest_missing
f = CUR.build_currentfield(BBOX, START, START + 6 * 3600)
check(f"built from the cycle before ({f.status().get('cycle')})", f.loaded)
CUR._fetch = _fake_fetch
check("a fallback field is reused for a while", not builds_cur(START, START + 6 * 3600))
CLOCK[0] += CUR.FALLBACK_RETRY_S + 1
check("…then the newest cycle is tried again", builds_cur(START, START + 6 * 3600))
CUR._cycle_for = lambda epoch: _real_ccycle(START - 86400, now=CLOCK[0])   # pin the cycle
CUR._cache.clear()
builds_cur(START, START + 6 * 3600)
CLOCK[0] += CUR.CACHE_MAX_H * 3600 + 1
check(f"any field expires after {CUR.CACHE_MAX_H:g} h", builds_cur(START, START + 6 * 3600))
check("expired entries are dropped, not kept forever", len(CUR._cache) == 1)
CUR._cycle_for = _real_ccycle

# --- waves --------------------------------------------------------------------------------------
print("3) waves: the cache key")
from app.wind import grib  # noqa: E402

DOWNLOADS = []
grib.http_download = lambda url, path, timeout=60: DOWNLOADS.append(url)
PARSE_SPAN = []


def _fake_parse(path, t_lo, t_hi, log):
    PARSE_SPAN.append((t_lo, t_hi))
    lat2d, lon2d = np.meshgrid(np.array([43.4, 44.6]), np.array([-86.6, -85.4]))
    return lat2d, lon2d, np.array([int(t_lo), int(t_hi)]), np.ones((2, 2, 2), dtype="float32")


WAVE._parse_isolated = _fake_parse
_real_wcycle = WAVE._cycle_for
WAVE.datetime = type("D", (), {"datetime": type("DT", (datetime.datetime,), {
    "utcnow": staticmethod(lambda: datetime.datetime.fromtimestamp(
        CLOCK[0], datetime.timezone.utc).replace(tzinfo=None))}),
    "timedelta": datetime.timedelta, "timezone": datetime.timezone})


def builds_wave(t_start, t_end, bbox=BBOX):
    n0 = len(DOWNLOADS)
    WAVE._build_glwu(bbox, t_start, t_end, lambda *_: None)
    return len(DOWNLOADS) > n0


WAVE._cache.clear()
START = CLOCK[0] + 3600
check("first build downloads", builds_wave(START, START + 6 * 3600))
check("same cycle, area, span: cached", not builds_wave(START, START + 6 * 3600))
check("a longer window builds again", builds_wave(START, START + 18 * 3600))
check("…and parses the whole window", PARSE_SPAN[-1] == (START, START + 18 * 3600))
WAVE.CACHE_VERSION += 1
check("a processing change builds again", builds_wave(START, START + 6 * 3600))
CLOCK[0] += 6 * 3600
check("a newer cycle builds again", builds_wave(START, START + 6 * 3600))
WAVE._cycle_for = lambda epoch, back=0: _real_wcycle(START - 86400, back)  # pin the cycle
WAVE._cache.clear()
builds_wave(START, START + 6 * 3600)
CLOCK[0] += WAVE.CACHE_MAX_H * 3600 + 1
check(f"any field expires after {WAVE.CACHE_MAX_H:g} h", builds_wave(START, START + 6 * 3600))
WAVE._cycle_for = _real_wcycle

# --- wave parser: no frames in the window ------------------------------------------------------
print("4) waves: the parser keeps no frames outside the window (#44.2b)")
fake = tempfile.mkdtemp()
with open(os.path.join(fake, "xarray.py"), "w") as fh:
    fh.write('''
import numpy as np
class _V:
    def __init__(self, v): self.values = v
class _DS:
    data_vars = {"swh": None}
    def __getitem__(self, k):
        vt = (np.array([1000, 4600, 8200]) * 1_000_000_000).astype("datetime64[ns]")
        return {"swh": _V(np.ones((3, 2, 2))), "latitude": _V(np.zeros((2, 2))),
                "longitude": _V(np.zeros((2, 2))), "valid_time": _V(vt)}[k]
    def close(self): pass
def open_dataset(*a, **k): return _DS()
''')


def child(t_lo, t_hi):
    out = os.path.join(fake, "out.npz")
    r = subprocess.run([sys.executable, "-c", WAVE._CHILD, "x.grib2", out, str(t_lo), str(t_hi),
                        "1", "10"], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=fake))
    if r.returncode:
        print(r.stderr)
        return None
    with np.load(out) as d:
        return list(d["ep"])


got = child(0, 9000)
check(f"frames in the window are kept ({got})", got == [1000, 4600, 8200])
got = child(1_000_000, 2_000_000)
check(f"no frame in the window → none kept ({got})", got == [])

_time.time = _real_time
print("\n", "ALL PASS" if ok else "FAILURES ABOVE")
raise SystemExit(0 if ok else 1)
