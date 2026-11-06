"""#167 — window reads must follow ONE wind publisher, not mix two second by second.

`series()` decimated to the last sample per second across ALL sources. With two TWD publishers
(the Orca and `derived`) ~10° apart, the window alternated between them, so tactics read a
steady breeze as a 10° oscillation. When one publisher dropped out mid-window the regression saw
a 10° step and called a lasting shift that never happened.

`series()` now names one publisher for the whole window by the source policy (the same ranking
`latest_value` uses), failing over to the next rank when the lead has gone quiet. Controls: one
publisher with a real shift is still a shift, and so is a real shift seen by both publishers.

The archive is a real SQLite file; the clock is injected with freezegun (no wall-clock reads).
Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_wind_source.py
"""
import math
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.dirname(os.path.dirname(_here))):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("ONBOARD_LIVE_WS", "false")

from freezegun import freeze_time   # noqa: E402

from app import datasource, datasource_onboard as ds, tactics   # noqa: E402
from shared import source_policy   # noqa: E402
from app import navigator as NAV   # noqa: E402

ok = True
_REAL_LATEST, _REAL_NAVIGATOR = NAV._latest, NAV.get_navigator


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


TWD = "environment.wind.directionTrue"
ORCA = "n2k-socketcan.15"          # "Orca Core" in the SR33 device map: TWD rank 1
DERIVED = "derived-data"           # matches `derived`: TWD rank 2
NOW = datetime(2026, 7, 18, 15, 0, 0, tzinfo=timezone.utc)   # injected clock
WINDOW_S = tactics.WINDOW_MIN * 60


def stamp(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def build(path, samples):
    """samples: [(source, seconds_before_NOW, twd_deg)] -> a fresh archive.db at `path`."""
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, "
              "boat_id TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL, value REAL, "
              "str_value TEXT)")
    c.executemany("INSERT INTO readings (time, boat_id, source, path, value) VALUES (?,?,?,?,?)",
                  [(stamp(NOW - timedelta(seconds=ago)), ds.BOAT_ID, src, TWD,
                    math.radians(deg % 360)) for src, ago, deg in samples])
    c.commit()
    c.close()


def interleaved(seconds, orca_deg, derived_deg, orca_until=0):
    """Both publishers, alternating seconds (Orca on even, derived on odd) — so a per-second
    'last writer wins' read swaps device every sample. `orca_until`: the Orca stops publishing
    that many seconds before NOW (a dropout mid-window)."""
    out = []
    for ago in range(seconds, 0, -1):
        age = seconds - ago                      # seconds since the window opened
        if ago % 2 == 0:
            if ago > orca_until:
                out.append((ORCA, ago - 0.2, orca_deg(age)))
        else:
            out.append((DERIVED, ago - 0.2, derived_deg(age)))
    return out


def fresh_source():
    src = object.__new__(ds.OnboardSource)
    import threading
    src._archive_local = threading.local()
    src._series_cache, src._series_lock = {}, threading.Lock()
    return src


def run_tactics(src):
    """tactics.get_tactics on the real onboard series, with position/heading stubbed."""
    series = src.series(TWD, tactics.WINDOW_MIN)
    cur = math.degrees(series[-1][1]) % 360 if series else None
    datasource.active = lambda: src
    tactics.datasource.active = lambda: src
    NAV._latest = lambda: {"twd": cur, "lat": 44.0, "lon": -79.0, "heading": None,
                           "cog": None, "sog": 6.0, "tws": 12.0}
    NAV.get_navigator = lambda route=None: {"available": False}
    return tactics.get_tactics()


def spread(src):
    vals = [math.degrees(v) % 360 for _, v in src.series(TWD, tactics.WINDOW_MIN)]
    return (max(vals) - min(vals)) if vals else None


with tempfile.TemporaryDirectory() as d, freeze_time(NOW):
    db = os.path.join(d, "archive.db")
    ds.ARCHIVE_DB = db
    ds.ARCHIVE_RECHECK_S = 0.0

    print("two TWD publishers 10° apart, steady breeze, interleaved second by second:")
    build(db, interleaved(WINDOW_S, lambda a: 200.0, lambda a: 210.0))
    ds._AIS_SRC_CACHE.clear()
    src = fresh_source()
    check(f"the window holds one publisher's values (spread {spread(src)}°, want < 1°)",
          spread(src) is not None and spread(src) < 1.0)
    check("…and it is the Orca, the policy's lead for TWD",
          abs(math.degrees(src.series(TWD, tactics.WINDOW_MIN)[-1][1]) - 200.0) < 0.01)
    r = run_tactics(src)
    check(f"tactics: oscillation {r['wind']['oscillation_deg']}° (want < 3°, not the 10° gap)",
          r["available"] and r["wind"]["oscillation_deg"] < 3.0)
    check("tactics: no lasting shift", r["available"] and not r["wind"]["persistent"])

    print("the Orca drops out 5 min before now; derived carries on 10° to the right:")
    build(db, interleaved(WINDOW_S, lambda a: 200.0, lambda a: 210.0, orca_until=300))
    src = fresh_source()
    check(f"the window holds one publisher's values (spread {spread(src)}°, want < 1°)",
          spread(src) is not None and spread(src) < 1.0)
    check("…the one still publishing (derived), not the quiet lead",
          abs(math.degrees(src.series(TWD, tactics.WINDOW_MIN)[-1][1]) - 210.0) < 0.01)
    r = run_tactics(src)
    check(f"tactics: no lasting shift from the handover (persistent={r['wind']['persistent']}, "
          f"slope {r['wind']['slope_deg_min']}°/min)",
          r["available"] and not r["wind"]["persistent"])
    check("tactics: trend steady", r["available"] and r["wind"]["trend"] == "steady")

    print("control: ONE publisher, wind really shifts right 200° -> 215° over the window:")
    build(db, [(ORCA, ago - 0.2, 200.0 + 15.0 * (WINDOW_S - ago) / WINDOW_S)
               for ago in range(WINDOW_S, 0, -1)])
    src = fresh_source()
    r = run_tactics(src)
    check(f"tactics: a lasting shift (slope {r['wind']['slope_deg_min']}°/min)",
          r["available"] and r["wind"]["persistent"])
    check("tactics: shifted right", r["available"] and r["wind"]["trend"] == "right")

    print("control: BOTH publishers see the same real shift right (derived reads 10° higher):")
    shift = lambda a: 200.0 + 15.0 * a / WINDOW_S
    build(db, interleaved(WINDOW_S, shift, lambda a: shift(a) + 10.0))
    src = fresh_source()
    r = run_tactics(src)
    check(f"tactics: a lasting shift (slope {r['wind']['slope_deg_min']}°/min)",
          r["available"] and r["wind"]["persistent"])
    check("tactics: shifted right", r["available"] and r["wind"]["trend"] == "right")
    check(f"tactics: oscillation {r['wind']['oscillation_deg']}° is the shift only (< 17°)",
          r["available"] and r["wind"]["oscillation_deg"] < 17.0)

    print("an unranked path with two publishers still reads one of them, deterministically:")
    c = sqlite3.connect(db)
    c.executemany("INSERT INTO readings (time, boat_id, source, path, value) VALUES (?,?,?,?,?)",
                  [(stamp(NOW - timedelta(seconds=ago - 0.2)), ds.BOAT_ID,
                    "src-a" if ago % 2 else "src-b", "some.unranked.path",
                    1.0 if ago % 2 else 2.0) for ago in range(120, 0, -1)])
    c.commit()
    c.close()
    src = fresh_source()
    vals = {v for _, v in src.series("some.unranked.path", 5)}
    check(f"one publisher's values only (got {sorted(vals)})", len(vals) == 1)

print("cloud source: the same pick, live and window, through a fake Postgres pool:")


class _FakeConn:
    """Answers the CloudSource queries from in-memory rows [(source, path, epoch, value)] and a
    `source_priority` table {channel: [match, ...]}. Dispatches on the SQL text."""

    def __init__(self, pool):
        self.p = pool
        self._out = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        p = self.p
        if "FROM source_priority" in sql:
            self._out = [{"channel": ch, "match": m}
                         for ch, ms in sorted(p.prio.items()) for m in ms]
            return self
        path = params[1]
        rows = sorted((r for r in p.rows if r[1] == path), key=lambda r: r[2])
        if "DISTINCT ON (source)" in sql:
            newest = rows[-1][2] if rows else None
            last = {}
            for s, _p, t, v in rows:
                if newest - t <= float(params[-1]):
                    last[s] = {"source": s, "value": v, "t": t}
            self._out = list(last.values())
        elif "LIMIT 1" in sql:
            self._out = ([{"value": rows[-1][3], "t": rows[-1][2], "time": rows[-1][2]}]
                         if rows else [])
        else:
            self._out = [{"source": s, "value": v, "t": t} for s, _p, t, v in rows]
        return self

    def fetchone(self):
        return self._out[0] if self._out else None

    def fetchall(self):
        return self._out


class _FakePool:
    def __init__(self, rows, prio):
        self.rows, self.prio = rows, prio

    def connection(self):
        return _FakeConn(self)


T = NOW.timestamp()


def cloud_rows(orca_deg=200.0, derived_deg=210.0, orca_quiet_s=0):
    """12 min of TWD from both publishers (derived lands 0.5 s after the Orca, so it is always
    the last writer), plus a single-source position/speed/TWS for the navigator."""
    rows = []
    for i in range(720):
        t = T - 720 + i
        if t <= T - orca_quiet_s:
            rows.append((ORCA, TWD, t, math.radians(orca_deg)))
        rows.append((DERIVED, TWD, t + 0.5, math.radians(derived_deg)))
    for path, v in (("navigation.position.latitude", 44.0),
                    ("navigation.position.longitude", -79.0),
                    ("navigation.speedOverGround", 3.0),
                    ("environment.wind.speedTrue", 6.0)):
        rows.append(("gps", path, T, v))
    return rows


def run_cloud(rows, prio):
    datasource.pool = _FakePool(rows, prio)
    datasource._priority_cache = (None, 0.0)   # a new boat DB: no cached ranking
    cloud = datasource.CloudSource()
    datasource.active = lambda: cloud
    NAV._latest = _REAL_LATEST
    NAV.get_navigator = lambda route=None: {"available": False}
    return cloud


SEEDED = {k: list(v) for k, v in source_policy.DEFAULT_PRIORITY.items()}
# The source pick judges freshness on the wall clock (#221, same rule as /conditions), so every
# cloud read below runs on the fixtures' clock, not today's.
_cloud_clock = freeze_time(NOW)
_cloud_clock.start()
cloud = run_cloud(cloud_rows(), SEEDED)
vals = {round(math.degrees(v), 3) for _, v in cloud.series(TWD, 12)}
check(f"cloud series is the Orca only (got {sorted(vals)})", vals == {200.0})
live = round(math.degrees(cloud.latest_value(TWD)), 3)
check(f"cloud live TWD is the Orca too, though derived wrote last (got {live})", live == 200.0)
with freeze_time(NOW):
    r = tactics.get_tactics()
check(f"cloud tactics through the real navigator: no false shift (shift {r['wind']['shift_deg']}°)",
      r["available"] and abs(r["wind"]["shift_deg"]) < 1.0)

cloud = run_cloud(cloud_rows(orca_quiet_s=300), SEEDED)
vals = {round(math.degrees(v), 3) for _, v in cloud.series(TWD, 12)}
check(f"cloud series fails over to derived when the Orca goes quiet (got {sorted(vals)})",
      vals == {210.0})
live = round(math.degrees(cloud.latest_value(TWD)), 3)
check(f"…and so does the live read (got {live})", live == 210.0)

print("cloud source: the operator's DB priority, not the committed default, picks the publisher:")
PROMOTED = dict(SEEDED, twd=["derived", "orca"])
cloud = run_cloud(cloud_rows(), PROMOTED)
vals = {round(math.degrees(v), 3) for _, v in cloud.series(TWD, 12)}
check(f"derived promoted in source_priority: the window follows derived (got {sorted(vals)})",
      vals == {210.0})
# make the Orca the last writer, so a freshest-wins live read would give 200
rows = [(s, p, t + (1.0 if s == ORCA else 0.0), v) for s, p, t, v in cloud_rows()]
cloud = run_cloud(rows, PROMOTED)
live = round(math.degrees(cloud.latest_value(TWD)), 3)
check(f"…and so does the live read, though the Orca wrote last (got {live})", live == 210.0)

print("cloud source: two publishers on ONE rank — window and live read pick the same one:")
rows = cloud_rows()
rows = [r for r in rows if r[1] != TWD]
rows += [("derived-a", TWD, T - 100 + i, math.radians(200.0)) for i in range(100)]   # last T-1
rows += [("derived-b", TWD, T - 10 + i, math.radians(210.0)) for i in range(11)]     # last T
for prio_name, prio in (("same rank", SEEDED), ("unranked", dict(SEEDED, twd=[]))):
    cloud = run_cloud(rows, prio)
    win = {round(math.degrees(v), 3) for _, v in cloud.series(TWD, 12)}
    live = round(math.degrees(cloud.latest_value(TWD)), 3)
    check(f"{prio_name}: window {sorted(win)} and live {live} are the same publisher",
          win == {live})
    with freeze_time(NOW):
        r = tactics.get_tactics()
    check(f"{prio_name}: no false shift (shift {r['wind']['shift_deg']}°)",
          r["available"] and abs(r["wind"]["shift_deg"]) < 1.0)

cloud = run_cloud([], SEEDED)
check("cloud series of nothing is empty", cloud.series(TWD, 12) == [])
check("cloud live read of nothing is None", cloud.latest_value(TWD) is None)
_cloud_clock.stop()

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
