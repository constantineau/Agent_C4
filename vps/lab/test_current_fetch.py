"""#51: the Lab's water-current fetch (LMHOFS over THREDDS).

  1) the cycle is picked from min(race start, now), like the wave fetch: planning a race 2+ days
     out used to ask for cycles that don't exist yet, and both fallbacks were in the future too,
     so the route silently had no current;
  2) the per-slice timeout really ends the wait: leaving the `with ThreadPoolExecutor` block
     waited for the hung worker, so a hung THREDDS hung the whole job.

No network: the slice reader is stubbed.
"""
import datetime
import os
import threading
import time

from app import current as CUR

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def ep(dt):
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


print("1) the cycle is never in the future")
NOW = ep(datetime.datetime(2026, 7, 15, 14, 30))
START = NOW + 3 * 86400                                     # race in 3 days
cyc = CUR._cycle_for(START, now=NOW)
check(f"a race 3 days out → the latest posted cycle ({cyc:%Y-%m-%d %HZ})",
      cyc == datetime.datetime(2026, 7, 15, 6))
for back in range(CUR.CYCLE_FALLBACKS + 1):
    c = cyc - datetime.timedelta(hours=6 * back)
    check(f"fallback {back} is posted (before now - lag)", ep(c) <= NOW - CUR.CYCLE_LAG_H * 3600)
past = ep(datetime.datetime(2026, 7, 10, 20, 0))
check("a past race still uses its own cycle",
      CUR._cycle_for(past, now=NOW) == datetime.datetime(2026, 7, 10, 12))
check("no `now` given → the real clock (never a future cycle)",
      ep(CUR._cycle_for(time.time() + 5 * 86400)) <= time.time())

# and the build asks THREDDS for that cycle's files
urls = []


def _record(url, bbox, want_axes):
    urls.append(url)
    raise OSError("no network in the test")


CUR._open_slice = _record
CUR.ENABLED = True
CUR.MAX_SLICES = 2
_real_cycle = CUR._cycle_for
CUR._cycle_for = lambda epoch: _real_cycle(epoch, now=NOW)
CUR.build_currentfield((44.5, 43.5, -86.5, -85.5), START, START + 6 * 3600)
CUR._cycle_for = _real_cycle
check("the build fetches posted cycles only",
      urls and all(("lmhofs.t06z.20260715" in u or "lmhofs.t00z.20260715" in u
                    or "lmhofs.t18z.20260714" in u) for u in urls))
check("…at forecast hours that reach the race (f072+)",
      urls and all(int(u.rsplit(".f", 1)[1][:3]) >= 72 for u in urls))

print("2) a hung THREDDS read times out")
CUR.FETCH_TIMEOUT = 0.3


def _hang(url, bbox, want_axes):
    time.sleep(4)
    return ([], [], [], [])


CUR._open_slice = _hang
t0 = time.time()
try:
    CUR._fetch("http://thredds/x.nc", (0, 1, 0, 1), True)
    timed_out = False
except CUR.FTimeout:
    timed_out = True
dt = time.time() - t0
check(f"the fetch gives up at the timeout ({dt:.1f} s, the read hangs 4 s)", timed_out and dt < 1.5)
check("…on a daemon thread (a stuck read never blocks the Lab's shutdown)",
      all(t.daemon for t in threading.enumerate() if t.name == "lmhofs-slice"))
for _t in threading.enumerate():          # let that sleeper finish: the builds below start clean
    if _t.name == "lmhofs-slice":
        _t.join(5)


# a whole build against a hung THREDDS (Codex on #200): one stuck read, not one per slice, and
# none of them may hold up the Lab's shutdown
import threading  # noqa: E402

calls = []
release = threading.Event()


def _hang_until_released(url, bbox, want_axes):
    calls.append(url)
    release.wait(30)
    raise OSError("released")


CUR._open_slice = _hang_until_released
CUR.MAX_SLICES = 8
CUR._cache.clear()
before = {t.ident for t in threading.enumerate()}
CUR._cycle_for = lambda epoch: _real_cycle(epoch, now=NOW)
t0 = time.time()
f = CUR.build_currentfield((44.5, 43.5, -86.5, -85.5), START, START + 24 * 3600)
CUR._cycle_for = _real_cycle
dt = time.time() - t0
stuck = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
check(f"a hung THREDDS: one read tried, then no more ({len(calls)} tried, {dt:.1f} s)",
      len(calls) == 1 and dt < 2.0)
check("…the route gets no current (not a hang)", isinstance(f, CUR.ZeroCurrent))
check("…and the stuck read can't block shutdown (daemon thread)", stuck and all(t.daemon for t in stuck))

# #203: the stuck read from the build above is still hung. More Optimize/Playbook runs must not
# each add another stuck thread: no new read starts while an earlier one is stuck.
calls.clear()
for _ in range(3):
    CUR._cache.clear()
    CUR._cycle_for = lambda epoch: _real_cycle(epoch, now=NOW)
    f2 = CUR.build_currentfield((44.5, 43.5, -86.5, -85.5), START, START + 24 * 3600)
    CUR._cycle_for = _real_cycle
alive = [t for t in threading.enumerate() if t.ident not in before and t.is_alive()]
check(f"3 more builds while it's stuck: no new reads ({len(calls)} tried, {len(alive)} stuck threads)",
      len(calls) == 0 and len(alive) == 1)
check("…they route without current", isinstance(f2, CUR.ZeroCurrent))
release.set()
for t in alive:
    t.join(5)


def _ok_slice(url, bbox, want_axes):
    calls.append(url)
    raise OSError("answered (no data)")


CUR._open_slice = _ok_slice
CUR._cache.clear()
CUR._cycle_for = lambda epoch: _real_cycle(epoch, now=NOW)
CUR.build_currentfield((44.5, 43.5, -86.5, -85.5), START, START + 24 * 3600)
CUR._cycle_for = _real_cycle
check(f"once the stuck read ends, the next build reads again ({len(calls)} tried)", len(calls) >= 1)

# Codex on #220: Optimize and Playbook can fetch at the same moment. Both used to pass the
# "is a read stuck?" check before either had timed out, so both started reads, and only the last
# one stayed tracked. Caller A's read starts and holds until caller B is about to call in; every
# read then hangs until released. With reads serialized the result doesn't depend on timing:
# whoever reads second finds a stuck read and never starts one. (The generous timeout only
# makes the old race show reliably.)
print("3) two builds fetching at once start one read, not two")
CUR.FETCH_TIMEOUT = 2.0
reads = []
hold = threading.Event()
a_reading = threading.Event()
b_calling = threading.Event()


def _hang_on_hold(url, bbox, want_axes):
    reads.append(url)
    if len(reads) == 1:
        a_reading.set()
        b_calling.wait(10)
    hold.wait(30)
    raise OSError("released")


CUR._open_slice = _hang_on_hold
outcome = []


def _caller(before=None):
    if before:
        before.set()
    try:
        CUR._fetch("http://thredds/y.nc", (0, 1, 0, 1), True)
        outcome.append("read")
    except CUR.FTimeout:
        outcome.append("timeout")


ca = threading.Thread(target=_caller)
ca.start()
a_reading.wait(10)
cb = threading.Thread(target=_caller, args=(b_calling,))
cb.start()
for c in (ca, cb):
    c.join(15)
check(f"one read started ({len(reads)}), both callers gave up ({outcome})",
      len(reads) == 1 and outcome == ["timeout", "timeout"])
hold.set()
for _t in threading.enumerate():
    if _t.name == "lmhofs-slice":
        _t.join(5)

print("PASS" if ok else "FAIL")
# a plain exit: a non-daemon stuck reader would hang the test here, which is the point
raise SystemExit(0 if ok else 1)
