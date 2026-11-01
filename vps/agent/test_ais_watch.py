"""Collision watch, engine side (vps/agent/app/ais.py) — 2026-11-02 system review, items 1, 4, 5.

  1. ONLY a configured own MMSI (316053364, the dock alarm) is dropped; a target that merely looks
     like us (within 50 m, moving with us) is flagged possible_echo and keeps its full CPA — a
     boat 40 m astern overtaking at 1 kn is a real collision (Codex on #191); a freighter shows;
  2. a boat close by that does NOT move with us is not flagged;
  3. a stale own fix is no fix (and the replay switch turns the age check off);
  4. targets are dead-reckoned to now; no SOG/COG = unknown motion, never "stopped";
  5. AIS heard nothing for N minutes while under way → feed_silent (a hint); the transceiver gone
     from the bus → receiver_missing (the real blind state);
  7. the onboard fix age is the age of the reading actually used: value and time from ONE priority
     pick (`latest_sample`) — an expired live entry gives way to the archive (a dropped Signal K
     link with the archiver running is not stale), and a fresher lower-ranked source (Orca 1 s)
     never lends its time to the lead GPS's 40 s-old position (Codex on #191 at f98bda9).
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

    def latest_sample(self, path):
        v = self.vals.get(path)
        return None if v is None else (v, self.epoch)

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
check("own MMSI not set: the echo stays in the list, flagged possible_echo", e and e["possible_echo"] is True)
check("…and keeps its geometry (CPA computed, not blanked)", e and e["cpa_nm"] is not None)
check("the freighter is closing, CPA ~0, TCPA ~5.7 min", f and f["closing"] and f["cpa_nm"] < 0.05
      and 5.0 < f["tcpa_min"] < 6.5)
check("the freighter leads the list", r["targets"][0]["mmsi"] == "366999001")

# Codex's repro: we're northbound at 6 kn; a boat 40 m astern at 7 kn on our course. ~78 s to contact.
over = row("366999009", -40 * M_LAT, 7.0, 0.0, age_s=1, name="OVERTAKER")
t = by(run(Src([over])), "366999009")
check("overtaker 40 m astern at +1 kn: flagged possible_echo but STILL closing",
      t and t["possible_echo"] is True and t["closing"] is True)
check("…with its real CPA (~0) and TCPA (~1.3 min), not blanked",
      t and t["cpa_nm"] is not None and t["cpa_nm"] < 0.01 and 1.0 < t["tcpa_min"] < 1.6)

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
check("40 m off but crossing (8+ kn relative) → not flagged as us", by(r, "366999002")["possible_echo"] is False)
alongside = row("366999003", 80 * M_LAT, 6.0, 0.0, name="ALONGSIDE")    # same motion, 80 m off
check("same motion but 80 m off → not flagged as us",
      by(run(Src([alongside])), "366999003")["possible_echo"] is False)

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


class RxSrc(Src):
    rx_age = None

    def ais_receiver_age_s(self):
        return self.rx_age


rs = RxSrc([], sog_kn=6.0)
rs.rx_age = 20 * 60
r = run(rs)
check("transceiver quiet on the bus 20 min, nothing heard → receiver_missing (not just silent)",
      r["receiver_missing"] is True and r["feed_silent"] is False)
rs.rx_age = 5
r = run(rs)
check("transceiver on the bus, nothing heard → only feed_silent", r["receiver_missing"] is False
      and r["feed_silent"] is True)
check("a source that can't tell (cloud) → receiver_missing None", run(Src([]))["receiver_missing"] is None)

print("\n== 5b. the engine's AIS input link (Codex on #191 at 3530006) ==")


class LinkSrc(Src):
    link = None

    def live_link(self):
        return self.link

    def ais_receiver_age_s(self):
        return None if not self.link.get("connected") else 5.0


# the SK link dropped but the archiver keeps own GPS fresh: own fix true, targets gone
ls_ = LinkSrc([], sog_kn=6.0, fix_age=1)
ls_.link = {"enabled": True, "connected": False, "last_msg_age_s": 120.0}
r = run(ls_)
check("link not connected, own fix fresh → own_fix true AND ais_link_down true",
      r["own_fix"] is True and r["ais_link_down"] is True and r["ais_link_age_s"] == 120)
check("…the louder signal wins: not reported as merely 'silent'", r["feed_silent"] is False)
ls_.link = {"enabled": True, "connected": True, "last_msg_age_s": 60.0}
check("connected but nothing for 60 s → down", run(ls_)["ais_link_down"] is True)
ls_.link = {"enabled": False, "connected": False, "last_msg_age_s": None}
check("live cache switched off → down (no AIS can arrive)", run(ls_)["ais_link_down"] is True)
ls_.link = {"enabled": True, "connected": True, "last_msg_age_s": 0.4}
r = run(ls_)
check("connected and fresh → up; an empty sea is only feed_silent",
      r["ais_link_down"] is False and r["feed_silent"] is True)
check("a source with no live link (cloud) → None", run(Src([]))["ais_link_down"] is None)

print("\n== 6. closing contacts sorted by TCPA ==")
# A: CPA tiny but 40 min out; B: CPA 0.3 nm but 5 min out
a = row("366999010", 10 * NM_LAT, 9.0, 180.0, name="A")                     # 10 nm, closing 15 kn → 40 min
b = row("366999011", 1.25 * NM_LAT, 9.0, 180.0, name="B", dlon_deg=0.3 * NM_LAT / math.cos(math.radians(LAT0)))
r = run(Src([a, b]))
check("B (sooner) before A (smaller CPA, later)", [t["name"] for t in r["targets"]][:2] == ["B", "A"])

print("\n== 7. onboard fix age: the age of the reading actually used ==")
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from app import datasource_onboard as ds  # noqa: E402

LAT, LON = "navigation.position.latitude", "navigation.position.longitude"
GPS24, ORCA = "n2k-socketcan.3", "n2k-socketcan.15"     # the 24xd leads lat/lon; the Orca is #2


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def archive(path, rows):
    c = sqlite3.connect(path)
    c.execute("DROP TABLE IF EXISTS readings")
    c.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, boat_id TEXT, time TEXT, path TEXT, "
              "source TEXT, value REAL)")
    c.executemany("INSERT INTO readings (boat_id, time, path, source, value) VALUES (?,?,?,?,?)",
                  [(ds.BOAT_ID, iso(e), p, src, v) for p, src, e, v in rows])
    c.commit()
    c.close()


def onboard(db, live):
    src = object.__new__(ds.OnboardSource)
    src._archive_local = threading.local()
    src._live_lock = threading.Lock()
    src._live = live
    src._ais = {}
    return src


with tempfile.TemporaryDirectory() as d:
    db = os.path.join(d, "archive.db")
    now = time.time()
    ds.ARCHIVE_DB, ds.ARCHIVE_RECHECK_S = db, 0.0
    ds._AIS_SRC_CACHE[db] = set()

    archive(db, [(LAT, "gps", now - 2, 45.0)])                  # the archiver is still writing
    src = onboard(db, {(LAT, "gps"): (now - 300, 45.0)})         # the SK link dropped 5 min ago
    smp = src.latest_sample(LAT)
    check("live entry 5 min old (expired) + archive 2 s old → the archive's reading and time",
          smp and smp[0] == 45.0 and abs(now - smp[1] - 2) < 1.5)
    src._live = {(LAT, "gps"): (now - 1, 45.5)}
    smp = src.latest_sample(LAT)
    check("a fresh live entry wins, with its own time", smp[0] == 45.5 and abs(now - smp[1] - 1) < 0.5)

    # Codex's repro: archived 24xd fix 40 s old (inside the 45 s failover band, so it still LEADS)
    # and an Orca fix 1 s old. The position is the 24xd's, so the age must be 40 s, not 1 s.
    archive(db, [(LAT, GPS24, now - 40, 45.0), (LON, GPS24, now - 40, -83.0),
                 (LAT, ORCA, now - 1, 45.0003), (LON, ORCA, now - 1, -83.0003)])
    src = onboard(db, {})
    smp = src.latest_sample(LAT)
    check("24xd 40 s + Orca 1 s → the 24xd's value AND its 40 s time, from one pick",
          smp and smp[0] == 45.0 and abs(now - smp[1] - 40) < 1.5)
    check("latest_value agrees with latest_sample (one code path)", src.latest_value(LAT) == smp[0])
    ais.datasource.active = lambda: src
    ais.OWN_FIX_MAX_AGE_S = 30
    r = ais.get_ais_targets()
    check("…so the stale-fix guard trips: own_fix false, 'GPS fix stale', age ~40 s",
          r["own_fix"] is False and r["own_fix_reason"] == "GPS fix stale"
          and 38 <= r["own_fix_age_s"] <= 42)
    src._live = {(LAT, GPS24): (now - 1, 45.0), (LON, GPS24): (now - 1, -83.0)}
    check("the 24xd live and fresh → own_fix true", ais.get_ais_targets()["own_fix"] is True)

    # the real live-link state the loop keeps
    src._link_connected, src._link_last_msg = True, now - 2
    lk = src.live_link()
    check("OnboardSource.live_link: connected, last message ~2 s ago",
          lk["connected"] is True and abs(lk["last_msg_age_s"] - 2) < 1)
    src._link_connected = False
    check("…and disconnected after a drop", src.live_link()["connected"] is False)

    # the transceiver's own-context data in the live cache (#191: only OFF when it's off the bus)
    ds._AIS_SRC_CACHE[db] = {"b951"}
    src._live = {(LAT, "gps"): (now - 1, 45.0), ("sensors.ais.class", "b951"): (now - 30, 1.0)}
    check("transceiver heard 30 s ago → age ~30 s", abs(src.ais_receiver_age_s() - 30) < 1.5)
    src._live = {(LAT, "gps"): (now - 300, 45.0)}
    check("live link itself quiet → can't tell (None)", src.ais_receiver_age_s() is None)
    ds._AIS_SRC_CACHE[db] = set()
    check("no transceiver known → can't tell (None)", src.ais_receiver_age_s() is None)

print("\n" + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
