"""Collision watch, engine side (vps/agent/app/ais.py) — 2026-11-02 system review, items 1, 4, 5.

  1. our own AIS echo (MMSI 316053364 at 0.0 nm, the dock alarm) never alarms: dropped when the MMSI
     is configured, flagged "echo?" (not dropped) when it isn't; a real freighter still shows;
  2. a boat close by that does NOT move with us is not mistaken for us;
  3. a stale own fix is no fix (and the replay switch turns the age check off);
  4. targets are dead-reckoned to now; no SOG/COG = unknown motion, never "stopped";
  5. AIS heard nothing for N minutes while under way → feed_silent;
  6. closing contacts sorted by TCPA; own MMSI from env, then the race homework.

Pure: a stub data source, no DB, no live AIS. Exits non-zero on any failure.
    PYTHONPATH=vps/agent:. python3 vps/agent/test_ais_watch.py
"""
import math
import os
import sys
import time

from app import ais

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


LAT0, LON0 = 45.0, -83.0
NM_LAT = 1 / 60.0                                   # 1 nm of latitude in degrees
M_LAT = 1 / (60.0 * 1852.0)                         # 1 m of latitude in degrees


class Src:
    """Own ship at (LAT0, LON0) with `own` motion; `fix_age` seconds old; raw AIS `rows`."""

    def __init__(self, rows, sog_kn=6.0, cog=0.0, fix_age=1.0, fleet=None, lat=LAT0):
        now = time.time()
        self.rows, self.fleet = rows, fleet or {}
        self.vals = {"navigation.position.latitude": lat, "navigation.position.longitude": LON0,
                     "navigation.speedOverGround": None if sog_kn is None else sog_kn / ais._MS_TO_KN,
                     "navigation.courseOverGroundTrue": math.radians(cog)}
        self.epoch = None if fix_age is None else now - fix_age

    def latest_value(self, path):
        return self.vals.get(path)

    def latest_epoch(self, path):
        return self.epoch

    def ais_targets(self, max_age_min):
        cut = time.time() - max_age_min * 60
        return [r for r in self.rows if r["time"] >= cut]

    def get_fleet(self):
        return self.fleet


def row(mmsi, dlat_deg, sog, cog, age_s=5.0, name=None, dlon_deg=0.0, pos_age_s=None):
    now = time.time()
    return {"mmsi": mmsi, "name": name, "lat": LAT0 + dlat_deg, "lon": LON0 + dlon_deg,
            "sog": sog, "cog": cog, "time": now - age_s,
            "pos_time": now - (age_s if pos_age_s is None else pos_age_s)}


def run(src):
    ais.datasource.active = lambda: src
    return ais.get_ais_targets()


def by(res, mmsi):
    return next((t for t in res["targets"] if t["mmsi"] == mmsi), None)


ECHO = "316053364"
os.environ.pop("OWN_MMSI", None)

print("\n== 1. own echo + a real freighter ==")
# under way 6 kn north; our echo 10 m off, same motion; a freighter 2 nm ahead steaming south at 15 kn
freighter = row("366999001", 2 * NM_LAT, 15.0, 180.0, name="FREIGHTER")
echo = row(ECHO, 10 * M_LAT, 6.1, 2.0)
r = run(Src([echo, freighter]))
e, f = by(r, ECHO), by(r, "366999001")
check("own MMSI not set: the echo stays in the list, flagged 'echo?'", e and e["own_ship"] == "echo?")
check("…and is never closing, with no CPA claimed", e and e["closing"] is False and e["cpa_nm"] is None)
check("the freighter is closing, CPA ~0, TCPA ~5.7 min", f and f["closing"] and f["cpa_nm"] < 0.05
      and 5.0 < f["tcpa_min"] < 6.5)
check("the freighter leads the list; the echo sorts last",
      r["targets"][0]["mmsi"] == "366999001" and r["targets"][-1]["mmsi"] == ECHO)

# at the dock: stopped, GPS jitter on SOG/COG, the echo at 0.0 nm with SOG 0 (the live case)
r = run(Src([row(ECHO, 0.0, 0.0, 0.0)], sog_kn=0.1, cog=213.0))
check("dock: the 0.0 nm echo is 'echo?', not closing (was 'Collision risk … CPA 0.0 nm')",
      by(r, ECHO)["own_ship"] == "echo?" and not by(r, ECHO)["closing"])

os.environ["OWN_MMSI"] = ECHO
r = run(Src([echo, freighter]))
check("OWN_MMSI set: our MMSI is out of the list", by(r, ECHO) is None and r["own_mmsi"] == ECHO)
check("…but it was heard (own_seen) and the freighter is still shown",
      r["own_seen"] == {"mmsi": ECHO} and by(r, "366999001")["closing"])
os.environ.pop("OWN_MMSI")
r = run(Src([echo, freighter], fleet={"own": {"boat": "C4", "mmsi": ECHO}}))
check("own MMSI from the race homework (own.mmsi) when env is blank", by(r, ECHO) is None)
os.environ["OWN_MMSI"] = "not-a-number"
check("a malformed OWN_MMSI is ignored", run(Src([echo]))["own_mmsi"] is None)
os.environ.pop("OWN_MMSI")

print("\n== 2. close but not moving with us → a real contact ==")
crosser = row("366999002", 40 * M_LAT, 6.0, 270.0, name="CROSSER")     # 40 m off, crossing at 6 kn
r = run(Src([crosser]))
check("40 m off but crossing (8+ kn relative) → not flagged as us", by(r, "366999002")["own_ship"] is None)
alongside = row("366999003", 80 * M_LAT, 6.0, 0.0, name="ALONGSIDE")    # same motion, 80 m off
check("same motion but 80 m off → not flagged as us",
      by(run(Src([alongside])), "366999003")["own_ship"] is None)

print("\n== 3. a stale own fix is no fix ==")
ais.OWN_FIX_MAX_AGE_S = 30                          # the onboard default (DATA_SOURCE=onboard)
r = run(Src([freighter], fix_age=45))
check("fix 45 s old → own_fix false, reason 'GPS fix stale', age reported",
      r["own_fix"] is False and r["own_fix_reason"] == "GPS fix stale" and r["own_fix_age_s"] == 45)
check("…and no geometry is claimed", all(t["cpa_nm"] is None and t["closing"] is None for t in r["targets"]))
check("fix 2 s old → own_fix true", run(Src([freighter], fix_age=2))["own_fix"] is True)
r = run(Src([freighter], fix_age=1, lat=None))
check("no position at all → own_fix false, 'no GPS fix'", r["own_fix"] is False and r["own_fix_reason"] == "no GPS fix")
saved = ais.OWN_FIX_MAX_AGE_S
ais.OWN_FIX_MAX_AGE_S = 0
check("AIS_OWN_FIX_MAX_AGE_S=0 (replay) → the age check is off",
      run(Src([freighter], fix_age=3600))["own_fix"] is True)
ais.OWN_FIX_MAX_AGE_S = saved

print("\n== 4. dead reckoning + unknown motion ==")
# we're stopped; a 24 kn ship last heard 4 min ago 6 nm north, steaming south straight at us
ship = row("366999004", 6 * NM_LAT, 24.0, 180.0, age_s=240, name="LAKER")
t = by(run(Src([ship], sog_kn=0.0)), "366999004")
check("moved forward 4 min at 24 kn: range ~4.4 nm (not 6)", t and abs(t["range_nm"] - 4.4) < 0.05)
check("TCPA ~11 min (not 15)", t and abs(t["tcpa_min"] - 11.0) < 0.3)
check("the report's age is returned", t and 235 <= t["age_s"] <= 245)
# a name update refreshed `time` but the position is 4 min old: dead-reckon by the POSITION's age
named = row("366999007", 6 * NM_LAT, 24.0, 180.0, age_s=5, pos_age_s=240, name="LAKER")
t = by(run(Src([named], sog_kn=0.0)), "366999007")
check("a fresh name update doesn't reset the position's age", t and abs(t["range_nm"] - 4.4) < 0.05)
unk = row("366999005", 0.5 * NM_LAT, None, None, name="NOSPEED")
t = by(run(Src([unk])), "366999005")
check("no SOG/COG → motion 'unknown', no CPA, closing None (not 'stopped')",
      t and t["motion"] == "unknown" and t["cpa_nm"] is None and t["closing"] is None)
check("…and it still has range and bearing", t and abs(t["range_nm"] - 0.5) < 0.01 and t["bearing"] in (0, 360))
old = row("366999006", 1 * NM_LAT, 10.0, 180.0, age_s=60, pos_age_s=400)
check("a position older than the 5-min window is not placed", by(run(Src([old])), "366999006") is None)

print("\n== 5. a silent feed is not an empty sea ==")
r = run(Src([], sog_kn=6.0))
check("under way, nothing heard → feed_silent", r["feed_silent"] is True and r["last_heard_s"] is None)
r = run(Src([row("366999008", 30 * NM_LAT, 10.0, 0.0, age_s=600)], sog_kn=6.0))
check("under way, last heard 10 min ago (< 15) → not silent",
      r["feed_silent"] is False and 595 <= r["last_heard_s"] <= 605)
check("stopped at the dock, nothing heard → not silent (no alarm at the dock)",
      run(Src([], sog_kn=0.2))["feed_silent"] is False)
os.environ["OWN_MMSI"] = ECHO
r = run(Src([echo], sog_kn=6.0))
check("hearing only our own echo still proves the feed is alive", r["feed_silent"] is False)
os.environ.pop("OWN_MMSI")

print("\n== 6. closing contacts sorted by TCPA ==")
# A: CPA tiny but 40 min out; B: CPA 0.3 nm but 5 min out
a = row("366999010", 10 * NM_LAT, 9.0, 180.0, name="A")                     # 10 nm, closing 15 kn → 40 min
b = row("366999011", 1.25 * NM_LAT, 9.0, 180.0, name="B", dlon_deg=0.3 * NM_LAT / math.cos(math.radians(LAT0)))
r = run(Src([a, b]))
check("B (sooner) before A (smaller CPA, later)", [t["name"] for t in r["targets"]][:2] == ["B", "A"])

print("\n" + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
