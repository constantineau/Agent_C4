"""Data-path ages and source picks (#44.5, #45.2b, #45.3, #45.4, #48.2, #67.1-2).

- #44.5  an empty or failed AIS-source probe is retried, not memoised for the process
         (cloud `datasource.ais_bearing_sources`, onboard `OnboardSource.ais_sources`)
- #45.2b the cloud engine reads (`CloudSource.latest_sample` / `series_by_source`) and the
         /conditions read leave AIS-bearing sources out
- #45.4  onboard: a ranked archive source within the failover window is not hidden by a
         lower-ranked live one
- #45.3  the strip reports each channel's own age, not only the freshest channel's
- #48.2  the cloud strip withholds fatigue while racing
- #67.1-2 `get_polar_target` interpolates on the polar grid, not the nearest bucket

No DB: the pool is a fake; the onboard archive is a real SQLite file in a temp dir.
Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_data_ages.py
"""
import math
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.dirname(os.path.dirname(_here))):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("ONBOARD_LIVE_WS", "false")

from app import datasource, datasource_onboard as ds, onboard_conditions, race_mode, tools  # noqa: E402
from shared import n2k_sources  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


# --- a fake psycopg pool ----------------------------------------------------------------------
class _Res:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakePool:
    """Every execute is logged; `handler(sql, params)` returns rows or raises."""

    def __init__(self, handler):
        self.handler, self.log = handler, []

    @contextmanager
    def connection(self):
        pool = self

        class _Conn:
            def execute(self, sql, params=()):
                pool.log.append((sql, params))
                return _Res(pool.handler(sql, params))
        yield _Conn()


def reset_cloud_ais():
    datasource._ais_cache, datasource._ais_retry = None, (None, 0.0)


AIS_SRC = "n2k-socketcan.43"     # the em-trak AIS box on the SR33 map
ORCA = "n2k-socketcan.15"        # Orca Core: rank 1 for TWD / heel
DERIVED = "derived-data"         # rank 2 for TWD


# --- #44.5 cloud ------------------------------------------------------------------------------
print("#44.5 cloud: an empty or failed AIS probe is retried")
state = {"fail": True, "probes": 0}


def ais_handler(sql, params):
    if "AIS_MARKER" in sql or "path = ANY" in sql and "DISTINCT source" in sql:
        state["probes"] += 1
        if state["fail"]:
            raise RuntimeError("db not up yet")
        return [{"source": AIS_SRC}]
    return []


datasource.pool = tools.pool = FakePool(ais_handler)
reset_cloud_ais()
datasource.AIS_RECHECK_S = 3600.0
check("a failed probe returns an empty set (degrade to unfiltered)",
      tools.ais_bearing_sources() == set())
check("...and is not re-run on every request inside the recheck window",
      tools.ais_bearing_sources() == set() and state["probes"] == 1)
state["fail"] = False
datasource.AIS_RECHECK_S = 0.0                   # the window has passed
check("after the recheck window the probe runs again and finds the AIS box",
      tools.ais_bearing_sources() == {AIS_SRC} and state["probes"] == 2)
state["fail"] = True
check("a found set is kept (no probe, even with the DB failing)",
      tools.ais_bearing_sources() == {AIS_SRC} and state["probes"] == 2)

# --- #45.2b cloud engine reads leave AIS sources out -----------------------------------------
print("#45.2b cloud reads leave AIS-bearing sources out")
NOW_DT = datetime.now(timezone.utc)
NOW = NOW_DT.timestamp()


def read_handler(sql, params):
    if "ORDER BY time DESC LIMIT 1" in sql:
        return [{"time": NOW_DT}]
    if "DISTINCT ON (source)" in sql:
        rows = [{"source": AIS_SRC, "value": 0.5, "t": NOW},
                {"source": "n2k-socketcan.3", "value": 0.7, "t": NOW - 1}]
        if "NOT (source = ANY(%s))" in sql:
            excl = params[-1]
            rows = [r for r in rows if r["source"] not in excl]
        return rows
    if "FROM telemetry_raw" in sql and "ORDER BY time" in sql:
        return []
    return []


datasource.pool = FakePool(read_handler)
datasource._priority_cache = ({}, time.monotonic() + 1e9)   # no table: freshest wins
cs = datasource.CloudSource()
v = cs.latest_sample("navigation.position.latitude")
check(f"latest_sample takes the own GPS, not the fresher AIS row ({v})", v and v[0] == 0.7)
q = [s for s, _ in datasource.pool.log if "telemetry_raw" in s]
check("both latest_sample queries carry the AIS exclusion",
      len(q) == 2 and all("NOT (source = ANY(%s))" in s for s in q))
datasource.pool.log.clear()
cs.series_by_source("navigation.position.latitude", 5)
sql, params = datasource.pool.log[-1]
check("series_by_source carries it too, with the AIS list as the last parameter",
      "NOT (source = ANY(%s))" in sql and params[-1] == [AIS_SRC])
check("CloudSource exposes ais_sources() like the onboard source", cs.ais_sources() == {AIS_SRC})

tools.pool = FakePool(lambda sql, params: [])
tools._latest_per_source(["navigation.position.latitude"], 5)
sql, params = tools.pool.log[-1]
check("/conditions' per-source read leaves AIS sources out",
      "NOT (source = ANY(%s))" in sql and params[-1] == [AIS_SRC])

# get_route_status uses the engine read and reports the fix age
class _PosSrc:
    def latest_sample(self, path):
        return (44.0 if "latitude" in path else -82.0, time.time() - 90)


datasource.active = lambda: _PosSrc()
tools.pool = FakePool(lambda sql, params: [{"seq": 1, "name": "M1", "lat": 44.1, "lon": -82.0}])
rs = tools.get_route_status("x")
check(f"route status reports how old the fix is ({rs.get('fix_age_s')} s)",
      rs["available"] and 85 <= (rs.get("fix_age_s") or 0) <= 100)


# --- onboard archive helpers -----------------------------------------------------------------
def stamp(epoch):
    t = datetime.fromtimestamp(epoch, timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def build(path, rows):
    """rows: [(source, path, epoch, value)]"""
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, "
              "boat_id TEXT NOT NULL, source TEXT NOT NULL, path TEXT NOT NULL, value REAL, "
              "str_value TEXT)")
    c.executemany("INSERT INTO readings (time, boat_id, source, path, value) VALUES (?,?,?,?,?)",
                  [(stamp(e), ds.BOAT_ID, s, p, v) for s, p, e, v in rows])
    c.commit()
    c.close()


def fresh_source():
    src = object.__new__(ds.OnboardSource)
    src._archive_local = threading.local()
    src._series_cache, src._series_lock = {}, threading.Lock()
    src._live, src._live_lock = {}, threading.Lock()
    return src


MARKER = sorted(n2k_sources.AIS_MARKER_PATHS)[0]

with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "archive.db")
    ds.ARCHIVE_DB = db
    ds.ARCHIVE_RECHECK_S = 0.0

    # --- #44.5 onboard ---------------------------------------------------------------------
    print("#44.5 onboard: an empty AIS probe is retried")
    build(db, [(ORCA, "navigation.position.latitude", NOW - 5, 44.0)])   # no AIS yet
    ds._AIS_SRC_CACHE.clear()
    ds.AIS_RECHECK_S = 3600.0
    src = fresh_source()
    check("no AIS source in the archive yet -> empty", src.ais_sources() == set())
    build(db, [(ORCA, "navigation.position.latitude", NOW - 5, 44.0),
               (AIS_SRC, MARKER, NOW - 2, 1.0),
               (AIS_SRC, "navigation.position.latitude", NOW - 1, 45.5)])
    src = fresh_source()
    check("inside the recheck window the empty answer stands (no probe per request)",
          src.ais_sources() == set())
    ds.AIS_RECHECK_S = 0.0
    check("after it, the AIS box is found (was: filter off until restart)",
          src.ais_sources() == {AIS_SRC})
    ds.AIS_RECHECK_S = 3600.0
    check("...and kept: a found set is not provisional",
          not isinstance(ds._AIS_SRC_CACHE[db], ds._ProvisionalAis))
    lat = src.latest_sample("navigation.position.latitude")
    check(f"own-ship latitude no longer reads the AIS target ({lat})", lat and lat[0] == 44.0)

    # --- #45.4 ------------------------------------------------------------------------------
    print("#45.4 onboard: live cache does not hide a ranked archive source")
    TWD = "environment.wind.directionTrue"
    build(db, [(ORCA, TWD, NOW - 20, math.radians(200))])
    ds._AIS_SRC_CACHE.clear()
    src = fresh_source()
    src._live[(TWD, DERIVED)] = (NOW - 1, math.radians(210))
    v = src.latest_sample(TWD)
    check(f"the Orca (rank 1, 20 s old) beats live derived (rank 2) ({v and round(math.degrees(v[0]))}°)",
          v and abs(math.degrees(v[0]) - 200) < 0.01 and abs(v[1] - (NOW - 20)) < 0.01)
    build(db, [(ORCA, TWD, NOW - 120, math.radians(200))])
    src = fresh_source()
    src._live[(TWD, DERIVED)] = (NOW - 1, math.radians(210))
    v = src.latest_sample(TWD)
    check("an Orca sample past the failover window does not (live derived stands)",
          v and abs(math.degrees(v[0]) - 210) < 0.01)
    src = fresh_source()
    src._live[(TWD, ORCA)] = (NOW - 1, math.radians(201))
    os.remove(db)                                       # archive gone: the lead live pick needs none
    v = src.latest_sample(TWD)
    check("a live lead-source pick never touches the archive", v and abs(math.degrees(v[0]) - 201) < 0.01)
    src = fresh_source()
    src._live[(TWD, DERIVED)] = (NOW - 1, math.radians(210))
    v = src.latest_sample(TWD)
    check("archive unreadable -> the live pick still stands", v and abs(math.degrees(v[0]) - 210) < 0.01)

# --- #45.3 + #48.2 strips --------------------------------------------------------------------
print("#45.3 per-channel ages; #48.2 cloud strip withholds fatigue while racing")
CC = {"available": True, "as_of": "now", "channels": {
    "bank_voltage": {"freshest_age_s": 0.4, "preferred": {"value": 12.6, "age_s": 0.4}},
    "tws": {"freshest_age_s": 240.0, "preferred": {"value": 11.0, "age_s": 240.0}},
}}
tools.get_current_conditions = lambda max_age_minutes=5: CC
called = []
tools.fatigue.compute_fatigue_index = lambda: called.append(1) or {"index": 55, "level": "watch"}
race_mode._cache["mode"] = "race"
s = tools.get_strip()
check(f"racing: no fatigue on the cloud strip ({s.get('fatigue')}, {s.get('fatigue_level')})",
      s["fatigue"] is None and s["fatigue_level"] is None and s.get("fatigue_withheld") is True
      and not called)
check(f"per-channel ages: wind is 240 s old even though the bank is fresh ({s.get('ages')})",
      s.get("ages") == {"bank_voltage": 0.4, "tws": 240.0})
race_mode._cache["mode"] = "practice"
s = tools.get_strip()
check("practice: fatigue is on the strip", s["fatigue"] == 55 and "fatigue_withheld" not in s)

onboard_conditions.get_current_conditions = lambda max_age_minutes=5: CC
onboard_conditions.fatigue.compute_fatigue_index = lambda: {"index": 55, "level": "watch"}
s = onboard_conditions.get_strip()
check("the Pi strip keeps fatigue (its own tile stays) and carries per-channel ages",
      s["fatigue"] == 55 and s.get("ages") == {"bank_voltage": 0.4, "tws": 240.0})

# --- #67.1-2 polar target --------------------------------------------------------------------
print("#67.1-2 get_polar_target interpolates")
SQL = os.path.join(_here, "..", "db", "seed", "polars_sr33.sql")
PTS = [(float(t), float(a), float(s)) for t, a, s in re.findall(
    r"\(\s*'sr33'\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,", open(SQL).read())]


class _PolarSrc:
    def polars_stw(self):
        return PTS


datasource.active = lambda: _PolarSrc()
p = tools.get_polar_target(11, 90)
check(f"11 kn / 90° = 7.75 between the rows (was the 10 or 12 kn bucket) ({p.get('target_stw')})",
      p["available"] and abs(p["target_stw"] - 7.75) < 1e-6)
check("target VMG at 90° is ~0", abs(p["target_vmg"]) < 0.01)
check("wrapped 270° reads as 90°", tools.get_polar_target(11, 270)["target_stw"] == p["target_stw"])
check("inside 30°: no target", tools.get_polar_target(10, 20)["target_stw"] is None)
check("the nearest-bucket helpers are gone from both sources",
      not hasattr(datasource.CloudSource, "polar_nearest")
      and not hasattr(ds.OnboardSource, "polar_nearest"))


class _NoPolar:
    def polars_stw(self):
        return []


datasource.active = lambda: _NoPolar()
check("no polar table -> unavailable", tools.get_polar_target(11, 90)["available"] is False)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
